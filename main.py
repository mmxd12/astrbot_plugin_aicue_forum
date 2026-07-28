import asyncio
import html
import re
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
import astrbot.api.message_components as Comp

BASE_DEFAULT = "https://flarum.aicue.top"
TAG_NAMES = {
    "announcements": "公告",
    "transit": "中转站",
    "register-bot": "注册器",
    "jailbreak-prompts": "破甲词",
    "latest-news": "最新资讯",
    "tech-discussion": "技术讨论",
    "resource-share	": "资源分析",
    "off-topic": "灌水区",
}
TAG_ALIASES = {
    **{name: slug for slug, name in TAG_NAMES.items()},
    "注册机": "register-bot",
    "资源分享": "resource-share	",
    "resource-share": "resource-share	",
}


class ForumAPIError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"论坛 API {status}: {body[:300]}")
        self.status = status


class ContentParser(HTMLParser):
    def __init__(self, base: str):
        super().__init__()
        self.base = base
        self.parts = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "img" and attrs.get("src"):
            url = urljoin(self.base + "/", attrs["src"])
            if url.startswith(("http://", "https://")) and url not in self.images:
                self.images.append(url)
        elif tag in {"br", "p", "div", "li", "blockquote", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def result(self):
        text = html.unescape("".join(self.parts)).replace("\xa0", " ")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        return text, self.images


def parse_content(content_html: str, base: str):
    parser = ContentParser(base)
    parser.feed(content_html or "")
    return parser.result()


def iso_time(value: str):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@register("astrbot_plugin_aicue_forum", "mmxd", "言灵工坊中转站帖子监控与查询", "1.5.1")
class AicueForumPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.base = str(self.config.get("forum_url", BASE_DEFAULT)).rstrip("/")
        self.task = None
        self.http = None
        self.csrf_token = ""
        self.authenticated = False
        self.forum_user_id = ""
        self.auth_generation = 0
        self.auth_lock = asyncio.Lock()
        self.check_lock = asyncio.Lock()
        self.pending_ad_uploads = {}
        self.data_directory = StarTools.get_data_dir("astrbot_plugin_aicue_forum")
        self.migrate_legacy_data()
        self.advertisement_directory().mkdir(parents=True, exist_ok=True)
        migration = self.advertisement_directory() / ".initialized"
        bundled_ad = Path(__file__).parent / "assets" / "forum_header.png"
        if not migration.exists():
            if not self.advertisement_files() and bundled_ad.exists():
                (self.advertisement_directory() / "forum_header.png").write_bytes(bundled_ad.read_bytes())
            migration.touch()

    def migrate_legacy_data(self):
        legacy = Path(__file__).parent / "data"
        for name in ("advertisements", "screenshots"):
            source = legacy / name
            target = self.data_directory / name
            if not source.is_dir():
                continue
            target.mkdir(parents=True, exist_ok=True)
            for path in source.iterdir():
                destination = target / path.name
                if path.is_file() and not destination.exists():
                    shutil.copy2(path, destination)

    def cfg(self, key, default=None):
        return self.config.get(key, default)

    def cfg_int(self, key, default, minimum=0, maximum=None):
        try:
            value = int(self.cfg(key, default))
        except (TypeError, ValueError):
            value = default
        value = max(minimum, value)
        return min(value, maximum) if maximum is not None else value

    def tag_slugs(self):
        raw = str(self.cfg("tag_slug", "transit"))
        tags = (TAG_ALIASES.get(x.strip(), x.strip()) for x in raw.split("|") if x.strip())
        return list(dict.fromkeys(tags)) or ["transit"]

    async def tagged_discussions(self, *, sort="-createdAt", limit=20):
        rows = []
        for tag in self.tag_slugs():
            rows.extend(await self.discussions(tag=tag, sort=sort, limit=limit))
        key = "commentCount" if sort == "-commentCount" else "createdAt"
        return sorted({str(row["id"]): row for row in rows}.values(), key=lambda row: row["attributes"].get(key, 0), reverse=True)

    async def client(self):
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
            self.csrf_token = ""
            self.authenticated = False
            self.forum_user_id = ""
        return self.http

    async def refresh_csrf(self):
        session = await self.client()
        async with session.get(self.base + "/") as resp:
            body = await resp.text()
            match = re.search(r'"csrfToken":"([^"]+)"', body)
            if resp.status >= 400 or not match:
                raise RuntimeError(f"无法获取论坛 CSRF Token（HTTP {resp.status}）")
            self.csrf_token = match.group(1)

    async def login(self, force=False, generation=None):
        async with self.auth_lock:
            if self.authenticated:
                if not force:
                    return
                if generation is not None and generation != self.auth_generation:
                    return
            self.authenticated = False
            self.forum_user_id = ""
            username = str(self.cfg("forum_username", "")).strip()
            password = str(self.cfg("forum_password", ""))
            if not username or not password:
                raise RuntimeError("未配置论坛用户名或密码，无法用本人账号回帖")
            session = await self.client()
            session.cookie_jar.clear()
            self.csrf_token = ""
            await self.refresh_csrf()
            headers = {"Accept": "application/vnd.api+json", "X-CSRF-Token": self.csrf_token}
            payload = {"identification": username, "password": password, "remember": True}
            async with session.post(self.base + "/login", json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"论坛登录失败（HTTP {resp.status}）: {body[:200]}")
                try:
                    user_id = (await resp.json(content_type=None)).get("userId")
                    self.forum_user_id = str(user_id).strip() if user_id is not None else ""
                except (AttributeError, ValueError, TypeError):
                    self.forum_user_id = ""
            if not self.forum_user_id:
                raise RuntimeError("论坛登录响应缺少 userId，无法安全确认自动回帖身份")
            await self.refresh_csrf()
            self.authenticated = True
            self.auth_generation += 1

    async def api(self, path, method="GET", payload=None, auth=False, retry=True):
        headers = {"Accept": "application/vnd.api+json"}
        if auth:
            await self.login()
            generation = self.auth_generation
            headers["X-CSRF-Token"] = self.csrf_token
        session = await self.client()
        async with session.request(method, self.base + "/api/" + path.lstrip("/"), json=payload, headers=headers) as resp:
            body = await resp.text()
            csrf_expired = resp.status in {400, 403, 419} and (
                "csrf" in body.lower() or "text/html" in resp.headers.get("Content-Type", "")
            )
            if auth and retry and (resp.status == 401 or csrf_expired):
                if resp.status == 401:
                    await self.login(force=True, generation=generation)
                else:
                    await self.refresh_csrf()
                return await self.api(path, method, payload, auth=True, retry=False)
            if resp.status >= 400:
                raise ForumAPIError(resp.status, body)
            return await resp.json(content_type=None)

    async def discussions(self, *, tag=None, sort="-createdAt", limit=20, offset=0):
        params = {"sort": sort, "page[limit]": limit, "page[offset]": offset, "include": "user,tags"}
        if tag:
            params["filter[tag]"] = tag
        doc = await self.api("discussions?" + urlencode(params))
        users = {x["id"]: x.get("attributes", {}).get("displayName", "未知用户") for x in doc.get("included", []) if x.get("type") == "users"}
        for row in doc["data"]:
            user = row.get("relationships", {}).get("user", {}).get("data") or {}
            row["attributes"]["author"] = users.get(user.get("id"), "未知用户")
        return doc["data"]

    async def detail(self, discussion_id):
        doc = await self.api(f"discussions/{discussion_id}?include=posts,user,tags")
        discussion = doc["data"]
        included = doc.get("included", [])
        first = min((x for x in included if x["type"] == "posts"), key=lambda x: x["attributes"].get("number", 999999), default=None)
        user_id = discussion.get("relationships", {}).get("user", {}).get("data", {}).get("id")
        user = next((x for x in included if x["type"] == "users" and x["id"] == user_id), None)
        attrs = discussion["attributes"]
        content, images = parse_content(first["attributes"].get("contentHtml", "") if first else "", self.base)
        return {
            "id": discussion["id"], "title": attrs["title"], "created": attrs["createdAt"],
            "comments": attrs.get("commentCount", 0), "author": (user or {}).get("attributes", {}).get("displayName", "未知用户"),
            "content": content, "images": images, "url": f"{self.base}/d/{attrs['slug']}"
        }

    def text_chain(self, post, prefix="论坛新帖", with_images=True):
        local_time = iso_time(post["created"]).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        excerpt = post["content"][:self.cfg_int("excerpt_length", 500, maximum=5000)]
        if len(post["content"]) > len(excerpt):
            excerpt += "……"
        text = f"【{prefix}】\n{post['title']}\n作者：{post['author']}  发布时间：{local_time}\n"
        if excerpt:
            text += f"\n{excerpt}\n"
        text += f"\n{post['author']} - {post['url']}"
        parts = [Comp.Plain(text)]
        if with_images:
            for image in post["images"][:self.cfg_int("max_images", 3, maximum=10)]:
                parts.append(Comp.Image.fromURL(image))
        return MessageChain(parts)

    async def post_chain(self, post, cache_path=None):
        local_time = iso_time(post["created"]).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        excerpt = post["content"][:self.cfg_int("excerpt_length", 500, maximum=5000)]
        if len(post["content"]) > len(excerpt):
            excerpt += "……"
        try:
            image_url = await self.html_render(
                """<div style="width:960px;padding:30px;background:#f7f9fb;font-family:'Microsoft YaHei',sans-serif;color:#24384a">
                <div style="background:#2875b5;color:white;padding:18px 28px;font-size:27px;font-weight:600">言灵工坊 · 中转站</div>
                <div style="background:#20a4b7;color:white;padding:38px 34px;font-size:31px;text-align:center">{{ title | e }}</div>
                <div style="background:white;padding:34px;font-size:22px;line-height:1.75">
                  <div style="color:#526c80;margin-bottom:20px">{{ author | e }}　{{ created | e }}</div>
                  <div style="white-space:pre-wrap">{{ content | e }}</div>
                </div></div>""",
                {
                    "title": str(post["title"]),
                    "author": str(post["author"]),
                    "created": local_time,
                    "content": excerpt or "（暂无正文）",
                },
                return_url=True,
                options={"type": "jpeg", "quality": 80, "full_page": True},
            )
            if not str(image_url).startswith(("http://", "https://")):
                raise RuntimeError("截图服务未返回图片 URL")
            session = await self.client()
            async with session.get(image_url) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"下载截图失败（HTTP {resp.status}）")
                content_type = resp.headers.get("Content-Type", "").lower()
                if not content_type.startswith("image/"):
                    raise RuntimeError(f"截图响应类型异常：{content_type or '未知'}")
                max_bytes = 10 * 1024 * 1024
                if resp.content_length and resp.content_length > max_bytes:
                    raise RuntimeError("截图超过 10 MiB")
                image_bytes = await resp.content.read(max_bytes + 1)
                if not image_bytes or len(image_bytes) > max_bytes:
                    raise RuntimeError("截图为空或超过 10 MiB")
            if cache_path:
                cache_path = Path(cache_path)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(".tmp")
                await asyncio.to_thread(temporary.write_bytes, image_bytes)
                await asyncio.to_thread(temporary.replace, cache_path)
                image = Comp.Image.fromFileSystem(cache_path)
            else:
                image = Comp.Image.fromBytes(image_bytes)
            return MessageChain([image, Comp.Plain(f"\n{post['author']} - {post['url']}")])
        except Exception as exc:
            logger.warning("生成帖子截图失败，改用普通文本消息: %s", exc)
            return self.text_chain(post, with_images=False)

    def markdown_lines(self, rows, limit=10):
        lines = []
        for row in rows[:limit]:
            attrs = row["attributes"]
            name = attrs["title"].replace("[", "\\[").replace("]", "\\]")
            author = attrs.get("author", "未知用户")
            lines.append(f"[{name}]({self.base}/d/{attrs['slug']})---{author}")
        return lines

    def advertisement_directory(self):
        return self.data_directory / "advertisements"

    def advertisement_files(self):
        directory = self.advertisement_directory()
        if not directory.exists():
            return []
        extensions = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        return sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in extensions)

    def advertisement_image(self):
        files = self.advertisement_files()
        if not files:
            return None
        return Comp.Image.fromFileSystem(secrets.choice(files))

    def markdown_chain(self, rows):
        limit = self.cfg_int("search_result_count", 5, minimum=1, maximum=20)
        image = self.advertisement_image()
        parts = ([image] if image else []) + [Comp.Plain("\n" + "\n".join(self.markdown_lines(rows, limit)))]
        chain = MessageChain(parts)
        chain.use_markdown_ = True
        return chain

    def category_chain(self, name, hot, recent):
        parts = [Comp.Plain(f"{name}最火帖子：\n")]
        image = self.advertisement_image()
        if image:
            parts.append(image)
        parts.append(Comp.Plain("\n" + ("\n".join(self.markdown_lines(hot)) or "暂无帖子") + f"\n\n{name}最近帖子：\n"))
        image = self.advertisement_image()
        if image:
            parts.append(image)
        parts.append(Comp.Plain("\n" + ("\n".join(self.markdown_lines(recent)) or "暂无帖子")))
        chain = MessageChain(parts)
        chain.use_markdown_ = True
        return chain

    def announcement_chain(self, rows):
        parts = [Comp.Plain("公告：\n")]
        image = self.advertisement_image()
        if image:
            parts.append(image)
        parts.append(Comp.Plain("\n" + ("\n".join(self.markdown_lines(rows, len(rows))) or "暂无公告")))
        chain = MessageChain(parts)
        chain.use_markdown_ = True
        return chain

    def screenshot_path(self, discussion_id):
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(discussion_id))
        return self.data_directory / "screenshots" / f"{safe_id}.jpg"

    def cleanup_screenshots(self):
        directory = self.data_directory / "screenshots"
        if not directory.exists():
            return
        cutoff = datetime.now().timestamp() - self.cfg_int("screenshot_cache_hours", 24, minimum=1) * 60 * 60
        for path in directory.glob("*.jpg"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError as exc:
                logger.warning("清理帖子截图缓存 %s 失败: %s", path, exc)

    async def monitor_chain(self, grouped, screenshots):
        parts = []
        for tag in self.tag_slugs():
            rows = grouped.get(tag, [])
            if not rows:
                continue
            if parts:
                parts.append(Comp.Plain("\n\n"))
            parts.append(Comp.Plain(f"{TAG_NAMES.get(tag, tag)}最新帖子：\n"))
            for row in rows:
                row_id = str(row["id"])
                if row_id not in screenshots:
                    try:
                        cache_path = self.screenshot_path(row_id)
                        if cache_path.exists():
                            screenshots[row_id] = Comp.Image.fromFileSystem(cache_path)
                        else:
                            rendered = await self.post_chain(await self.detail(row_id), cache_path)
                            screenshots[row_id] = next(part for part in rendered.chain if isinstance(part, Comp.Image))
                    except Exception as exc:
                        logger.warning("帖子 %s 页面截图失败，改用广告图库图片: %s", row_id, exc)
                        screenshots[row_id] = self.advertisement_image() or Comp.Image.fromFileSystem(Path(__file__).parent / "assets" / "forum_header.png")
                parts.extend([screenshots[row_id], Comp.Plain("\n" + self.markdown_lines([row], 1)[0] + "\n")])
        chain = MessageChain(parts)
        chain.use_markdown_ = True
        return chain

    def target_platform(self, target):
        platform_id = str(target).split(":", 1)[0]
        return next((x for x in self.context.platform_manager.platform_insts if x.meta().id == platform_id), None)

    def is_qq_official_target(self, target):
        platform = self.target_platform(target)
        return bool(platform and platform.meta().name in {"qq_official", "qq_official_webhook"})

    async def prepare_qq_target(self, target, scene=None, message_id=None):
        platform = self.target_platform(target)
        if not platform or platform.meta().name not in {"qq_official", "qq_official_webhook"}:
            return True
        parts = str(target).split(":", 2)
        if len(parts) != 3 or not parts[2]:
            logger.warning("官 Q 推送目标格式无效: %s", target)
            return False
        session_id = parts[2]
        scenes = await self.get_kv_data("qq_target_scenes", {})
        if not isinstance(scenes, dict):
            scenes = {}
        if scene:
            scenes[str(target)] = scene
            await self.put_kv_data("qq_target_scenes", scenes)
        scene = scene or scenes.get(str(target)) or getattr(platform, "_session_scene", {}).get(session_id)
        if scene:
            if scenes.get(str(target)) != scene:
                scenes[str(target)] = scene
                await self.put_kv_data("qq_target_scenes", scenes)
            platform.remember_session_scene(session_id, scene)
        if message_id:
            platform.remember_session_message_id(session_id, message_id)
        cached_id = getattr(platform, "_session_last_message_id", {}).get(session_id)
        return scene == "group" or bool(cached_id)

    async def send_target(self, target, chain):
        if not await self.prepare_qq_target(target):
            logger.warning("官 Q 目标 %s 缺少会话场景/消息缓存，保留待重试", target)
            return False
        platform = self.target_platform(target)
        if not platform:
            return False
        parts = str(target).split(":", 2)
        if len(parts) != 3 or not parts[2]:
            return False
        session_id = parts[2]
        qq_official = platform.meta().name in {"qq_official", "qq_official_webhook"}
        previous_id = getattr(platform, "_session_last_message_id", {}).get(session_id) if qq_official else None
        if not await self.context.send_message(target, chain):
            return False
        if not qq_official:
            return True
        sent_id = getattr(platform, "_session_last_message_id", {}).get(session_id)
        if sent_id and sent_id != previous_id:
            return True
        logger.warning("官 Q 目标 %s 未返回新消息 ID，按发送失败保留待重试", target)
        return False

    async def consume_qq_quota(self, discussion_id, commit=True):
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        quota = await self.get_kv_data("qq_daily_quota", {})
        if not isinstance(quota, dict) or quota.get("date") != today:
            quota = {"date": today, "count": 0, "discussion_ids": []}
        raw_ids = quota.get("discussion_ids", [])
        used_ids = {str(x) for x in raw_ids} if isinstance(raw_ids, list) else set()
        try:
            count = max(0, int(quota.get("count", 0)))
        except (TypeError, ValueError):
            count = len(used_ids)
        discussion_id = str(discussion_id)
        if discussion_id in used_ids:
            return True
        limit = self.cfg_int("qq_daily_push_limit", 1000, minimum=1)
        if count >= limit:
            return False
        if commit:
            quota["count"] = count + 1
            quota["discussion_ids"] = [*used_ids, discussion_id]
            await self.put_kv_data("qq_daily_quota", quota)
        return True

    async def qq_quota_eligible(self, rows):
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        quota = await self.get_kv_data("qq_daily_quota", {})
        if not isinstance(quota, dict) or quota.get("date") != today:
            quota = {"count": 0, "discussion_ids": []}
        raw_ids = quota.get("discussion_ids", [])
        used_ids = {str(x) for x in raw_ids} if isinstance(raw_ids, list) else set()
        try:
            remaining = self.cfg_int("qq_daily_push_limit", 1000, minimum=1) - max(0, int(quota.get("count", 0)))
        except (TypeError, ValueError):
            remaining = self.cfg_int("qq_daily_push_limit", 1000, minimum=1) - len(used_ids)
        eligible = []
        for row in rows:
            row_id = str(row["id"])
            if row_id in used_ids:
                eligible.append(row)
            elif remaining > 0:
                eligible.append(row)
                used_ids.add(row_id)
                remaining -= 1
        return eligible

    async def add_pending(self, row):
        pending = await self.get_kv_data("pending_latest_posts", [])
        if not isinstance(pending, list):
            pending = []
        if not any(isinstance(item, dict) and str(item.get("id")) == str(row["id"]) for item in pending):
            pending.append({"id": str(row["id"]), "attributes": row.get("attributes", {})})
            await self.put_kv_data("pending_latest_posts", pending[-1000:])

    async def remove_pending(self, discussion_id):
        pending = await self.get_kv_data("pending_latest_posts", [])
        if not isinstance(pending, list):
            return
        remaining = [x for x in pending if not isinstance(x, dict) or str(x.get("id")) != str(discussion_id)]
        if len(remaining) != len(pending):
            await self.put_kv_data("pending_latest_posts", remaining)

    async def targets(self):
        value = await self.get_kv_data("push_targets", [])
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(x for x in value if isinstance(x, str) and x.strip()))

    async def retry_pending_replies(self):
        pending = await self.get_kv_data("pending_forum_replies", [])
        if not isinstance(pending, list) or not pending:
            return
        remaining = []
        for discussion_id in dict.fromkeys(str(x) for x in pending):
            try:
                await self.reply(discussion_id)
            except Exception as exc:
                remaining.append(discussion_id)
                logger.warning("帖子 %s 自动回帖重试失败: %s", discussion_id, exc)
        await self.put_kv_data("pending_forum_replies", remaining)

    async def add_pending_reply(self, discussion_id):
        pending = await self.get_kv_data("pending_forum_replies", [])
        if not isinstance(pending, list):
            pending = []
        discussion_id = str(discussion_id)
        if discussion_id not in {str(x) for x in pending}:
            pending.append(discussion_id)
            await self.put_kv_data("pending_forum_replies", pending)

    @filter.on_astrbot_loaded()
    async def loaded(self):
        try:
            await self.login()
            logger.info("言灵工坊论坛账号登录成功")
        except Exception as exc:
            logger.warning("言灵工坊论坛账号登录失败，自动回帖暂不可用: %s", exc)
        if self.task is None:
            self.task = asyncio.create_task(self.monitor())

    async def monitor(self):
        await asyncio.sleep(5)
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("言灵工坊监控失败: %s", exc)
            await asyncio.sleep(self.cfg_int("check_interval_seconds", 120, minimum=30))

    async def check_once(self):
        async with self.check_lock:
            self.cleanup_screenshots()
            await self.retry_pending_replies()
            raw_seen = await self.get_kv_data("seen_discussions", [])
            seen_ids = [str(x) for x in raw_seen] if isinstance(raw_seen, list) else []
            seen = set(seen_ids)
            retry_targets = await self.get_kv_data("pending_push_targets", {})
            if not isinstance(retry_targets, dict):
                retry_targets = {}
            completed = set(retry_targets).intersection(seen)
            if completed:
                retry_targets = {key: value for key, value in retry_targets.items() if key not in completed}
                await self.put_kv_data("pending_push_targets", retry_targets)

            initialized = bool(await self.get_kv_data("monitor_initialized", False)) and bool(seen_ids)
            tags = self.tag_slugs()
            raw_tag_seen = await self.get_kv_data("monitor_tag_seen", {})
            tag_seen = {
                str(tag): [str(x) for x in ids]
                for tag, ids in raw_tag_seen.items()
                if isinstance(tag, str) and isinstance(ids, list)
            } if isinstance(raw_tag_seen, dict) else {}
            if initialized and not tag_seen:
                tag_seen[tags[0]] = seen_ids.copy()
            tag_seen = {tag: tag_seen[tag] for tag in tags if tag in tag_seen}

            discovered = []
            discovered_tags = {}
            for tag in tags:
                tag_known = tag in tag_seen
                tag_baseline = set(tag_seen.get(tag, []))
                tag_rows = []
                offset = 0
                while True:
                    page = await self.discussions(tag=tag, limit=100, offset=offset)
                    if initialized and tag_baseline:
                        boundary = next((i for i, row in enumerate(page) if str(row["id"]) in tag_baseline), None)
                        tag_rows.extend(page if boundary is None else page[:boundary])
                        if boundary is not None:
                            break
                    else:
                        tag_rows.extend(page)
                    if len(page) < 100 or (not tag_baseline and len(tag_rows) >= 1000):
                        break
                    offset += 100
                if initialized and not tag_known:
                    tag_seen[tag] = list(dict.fromkeys(str(row["id"]) for row in tag_rows))[:1000]
                    logger.info("新增监控标签 %s 已建立基线，共 %s 帖，不补推旧帖", tag, len(tag_seen[tag]))
                    continue
                for row in tag_rows:
                    row_id = str(row["id"])
                    discovered.append(row)
                    discovered_tags.setdefault(row_id, set()).add(tag)

            if not initialized:
                baseline_ids = list(dict.fromkeys(str(row["id"]) for row in discovered))[:1000]
                for tag in tags:
                    tag_seen[tag] = list(dict.fromkeys(
                        str(row["id"]) for row in discovered if tag in discovered_tags.get(str(row["id"]), set())
                    ))[:1000]
                await self.put_kv_data("seen_discussions", baseline_ids)
                await self.put_kv_data("monitor_tag_seen", tag_seen)
                await self.put_kv_data("monitor_initialized", True)
                logger.info("言灵工坊监控已建立 %s 个标签基线，共 %s 帖，不补推旧帖", len(tags), len(baseline_ids))
                return
            await self.put_kv_data("monitor_tag_seen", tag_seen)

            new_rows = sorted(
                {str(row["id"]): row for row in discovered if str(row["id"]) not in seen}.values(),
                key=lambda row: row.get("attributes", {}).get("createdAt", ""),
            )
            for row in new_rows:
                row["_monitor_tags"] = list(discovered_tags.get(str(row["id"]), ()))
            discovered_ids = {str(row["id"]) for row in new_rows}
            for discussion_id in retry_targets:
                if discussion_id not in discovered_ids:
                    new_rows.insert(0, {"id": discussion_id})
            targets = await self.targets()
            reply_started = await self.get_kv_data("forum_reply_started", [])
            if not isinstance(reply_started, list):
                reply_started = []
            reply_started = list(dict.fromkeys(str(x) for x in reply_started))[-1000:]
            reply_started_set = set(reply_started)

            prepared = {}
            for row in new_rows:
                row_id = str(row["id"])
                pending = retry_targets.get(row_id)
                delivery_targets = [x for x in (pending if isinstance(pending, list) else targets) if x in targets]
                retry_targets[row_id] = delivery_targets
                if "attributes" not in row:
                    try:
                        post = await self.detail(row_id)
                        row["attributes"] = {"title": post["title"], "slug": post["url"].rsplit("/", 1)[-1], "author": post["author"], "createdAt": post["created"]}
                    except ForumAPIError as exc:
                        if exc.status not in {404, 410}:
                            logger.warning("帖子 %s 详情读取失败，保留待重试: %s", row_id, exc)
                            continue
                        retry_targets.pop(row_id, None)
                        await self.put_kv_data("pending_push_targets", retry_targets)
                        await self.remove_pending(row_id)
                        seen_ids.append(row_id)
                        seen_ids = list(dict.fromkeys(seen_ids))[-1000:]
                        await self.put_kv_data("seen_discussions", seen_ids)
                        for tag in row.get("_monitor_tags", []):
                            tag_seen[tag] = list(dict.fromkeys(tag_seen.get(tag, []) + [row_id]))[-1000:]
                        await self.put_kv_data("monitor_tag_seen", tag_seen)
                        seen.add(row_id)
                        continue
                    except Exception as exc:
                        logger.warning("帖子 %s 详情读取失败，保留待重试: %s", row_id, exc)
                        continue
                row_tags = set(row.get("_monitor_tags", ()))
                row["_monitor_tag"] = next((tag for tag in tags if tag in row_tags), tags[0])
                prepared[row_id] = row
            await self.put_kv_data("pending_push_targets", retry_targets)

            sent_by_row = {row_id: 0 for row_id in prepared}
            quota_skipped = set()
            screenshots = {}
            for target in targets:
                target_rows = [row for row_id, row in prepared.items() if target in retry_targets.get(row_id, [])]
                eligible = await self.qq_quota_eligible(target_rows) if self.is_qq_official_target(target) else target_rows
                quota_skipped.update(str(row["id"]) for row in target_rows if row not in eligible)
                if not eligible:
                    continue
                grouped = {}
                for row in eligible:
                    grouped.setdefault(row["_monitor_tag"], []).append(row)
                try:
                    if not await self.send_target(target, await self.monitor_chain(grouped, screenshots)):
                        continue
                    for row in eligible:
                        row_id = str(row["id"])
                        if self.is_qq_official_target(target):
                            await self.consume_qq_quota(row_id)
                        sent_by_row[row_id] += 1
                        retry_targets[row_id].remove(target)
                    await self.put_kv_data("pending_push_targets", retry_targets)
                except Exception as exc:
                    logger.warning("新帖汇总推送至 %s 失败: %s", target, exc)

            for row_id, row in prepared.items():
                if row_id in quota_skipped:
                    await self.add_pending(row)
                else:
                    await self.remove_pending(row_id)
                if sent_by_row[row_id] and row_id not in reply_started_set:
                    try:
                        await self.reply(row_id)
                    except Exception as exc:
                        await self.add_pending_reply(row_id)
                        logger.warning("帖子 %s 已推送但自动回帖失败: %s", row_id, exc)
                    reply_started.append(row_id)
                    reply_started = reply_started[-1000:]
                    reply_started_set.add(row_id)
                    await self.put_kv_data("forum_reply_started", reply_started)
                if retry_targets.get(row_id):
                    continue
                seen_ids.append(row_id)
                seen_ids = list(dict.fromkeys(seen_ids))[-1000:]
                await self.put_kv_data("seen_discussions", seen_ids)
                for tag in row.get("_monitor_tags", []):
                    tag_seen[tag] = list(dict.fromkeys(tag_seen.get(tag, []) + [row_id]))[-1000:]
                await self.put_kv_data("monitor_tag_seen", tag_seen)
                seen.add(row_id)
                retry_targets.pop(row_id, None)
                await self.put_kv_data("pending_push_targets", retry_targets)

    async def reply(self, discussion_id):
        content = str(self.cfg("reply_text", "已推送至群中"))
        await self.login()
        if self.forum_user_id:
            doc = await self.api(f"discussions/{discussion_id}?include=posts&page[limit]=50", auth=True)
            for post in doc.get("included", []):
                user = post.get("relationships", {}).get("user", {}).get("data") or {}
                text, _ = parse_content(post.get("attributes", {}).get("contentHtml", ""), self.base)
                if post.get("type") == "posts" and str(user.get("id")) == self.forum_user_id and text == content:
                    return
        payload = {"data": {"type": "posts", "attributes": {"content": content}, "relationships": {"discussion": {"data": {"type": "discussions", "id": str(discussion_id)}}}}}
        await self.api("posts", "POST", payload, auth=True)

    def origin(self, event):
        session = getattr(event, "session", None)
        group_id = str(event.get_group_id() or "").strip()
        if session and group_id:
            return f"{session.platform_id}:{session.message_type.value}:{group_id}"
        value = getattr(event, "unified_msg_origin", "")
        return str(value() if callable(value) else value).strip()

    def advertisement_upload_key(self, event):
        return f"{self.origin(event)}:{event.get_sender_id()}"

    def message_images(self, event):
        message = getattr(getattr(event, "message_obj", None), "message", [])
        return [part for part in message if isinstance(part, Comp.Image)]

    def cleanup_pending_ad_uploads(self):
        cutoff = datetime.now().timestamp() - 60
        self.pending_ad_uploads = {
            key: created for key, created in self.pending_ad_uploads.items() if created >= cutoff
        }

    def image_extension(self, content):
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if content.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return ".webp"
        return None

    async def save_advertisements(self, images):
        saved = []
        directory = self.advertisement_directory()
        directory.mkdir(parents=True, exist_ok=True)
        for image in images:
            try:
                source = Path(await image.convert_to_file_path())
                if source.stat().st_size > 10 * 1024 * 1024:
                    raise ValueError("图片超过 10 MiB")
                content = await asyncio.to_thread(source.read_bytes)
                extension = self.image_extension(content)
                if not extension:
                    raise ValueError("仅支持 PNG、JPEG、GIF、WebP 图片")
                name = f"ad_{datetime.now():%Y%m%d_%H%M%S_%f}{extension}"
                target = directory / name
                temporary = target.with_suffix(".tmp")
                await asyncio.to_thread(temporary.write_bytes, content)
                await asyncio.to_thread(temporary.replace, target)
                saved.append(name)
            except Exception as exc:
                logger.warning("保存广告图失败: %s", exc)
        return saved

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("upload_ad_image", alias={"上传图片"})
    async def upload_ad_image(self, event: AstrMessageEvent):
        self.cleanup_pending_ad_uploads()
        key = self.advertisement_upload_key(event)
        images = self.message_images(event)
        if images:
            saved = await self.save_advertisements(images)
            self.pending_ad_uploads.pop(key, None)
            if saved:
                yield event.plain_result(f"成功上传 {len(saved)} 张广告图：\n" + "\n".join(saved))
            else:
                yield event.plain_result("上传失败，请确认图片格式正确且单张不超过 10 MiB。")
            return
        self.pending_ad_uploads[key] = datetime.now().timestamp()
        yield event.plain_result("请在 60 秒内发送广告图片，可一次发送多张。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def receive_ad_image(self, event: AstrMessageEvent):
        self.cleanup_pending_ad_uploads()
        key = self.advertisement_upload_key(event)
        if key not in self.pending_ad_uploads or not event.is_admin():
            return
        text = (getattr(event, "message_str", "") or "").strip().lstrip("/")
        if text.startswith(("上传图片", "upload_ad_image")):
            return
        images = self.message_images(event)
        if not images:
            return
        saved = await self.save_advertisements(images)
        self.pending_ad_uploads.pop(key, None)
        event.stop_event()
        if saved:
            yield event.plain_result(f"成功上传 {len(saved)} 张广告图：\n" + "\n".join(saved))
        else:
            yield event.plain_result("上传失败，请重新执行 /上传图片。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("delete_ad_image", alias={"删除图片"})
    async def delete_ad_image(self, event: AstrMessageEvent, name: str = ""):
        files = self.advertisement_files()
        target = None
        if name.isdigit() and 1 <= int(name) <= len(files):
            target = files[int(name) - 1]
        elif name:
            target = next((path for path in files if path.name == name), None)
        if not target:
            yield event.plain_result("未找到指定广告图，请使用 /图片统计 查看序号和文件名。")
            return
        try:
            target.unlink()
        except FileNotFoundError:
            yield event.plain_result("该广告图刚刚已被删除，请使用 /图片统计 刷新列表。")
            return
        yield event.plain_result(f"已删除广告图：{target.name}\n当前剩余 {len(self.advertisement_files())} 张。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ad_image_stats", alias={"图片统计"})
    async def ad_image_stats(self, event: AstrMessageEvent):
        files = self.advertisement_files()
        if not files:
            yield event.plain_result("广告图库当前为空。")
            return
        lines = [f"{index}. {path.name}" for index, path in enumerate(files, 1)]
        yield event.plain_result(f"广告图库共有 {len(files)} 张：\n" + "\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("push_here", alias={"推送本群"})
    async def push_here(self, event: AstrMessageEvent):
        origin = self.origin(event)
        if not origin or not event.get_group_id():
            yield event.plain_result("请在需要接收推送的群聊中执行此指令。")
            return
        targets = await self.targets()
        legacy_origin = str(event.unified_msg_origin).strip()
        if legacy_origin != origin and legacy_origin in targets:
            targets.remove(legacy_origin)
        if origin in targets:
            targets.remove(origin); state = "已关闭本群的论坛推送"
        else:
            platform = self.target_platform(origin)
            scene = getattr(platform, "_session_scene", {}).get(str(event.get_group_id())) if platform else None
            if not await self.prepare_qq_target(origin, scene):
                yield event.plain_result("开启失败：官 Q 会话场景尚未就绪，请在群内重新发送本指令。")
                return
            targets.append(origin); state = "已开启本群的论坛推送"
        await self.put_kv_data("push_targets", targets)
        yield event.plain_result(state)

    async def show(self, event, rows, title):
        if not rows:
            yield event.plain_result(f"{title}\n暂无符合条件的帖子。")
            return
        for row in rows[:self.cfg_int("search_result_count", 5, minimum=1, maximum=20)]:
            try:
                yield event.chain_result((await self.post_chain(await self.detail(row["id"]))).chain)
            except Exception as exc:
                logger.warning("读取帖子 %s 详情失败: %s", row["id"], exc)

    async def category_result(self, event, slug, name):
        try:
            hot, recent = await asyncio.gather(
                self.discussions(tag=slug, sort="-commentCount", limit=10),
                self.discussions(tag=slug, sort="-createdAt", limit=10),
            )
            result = event.chain_result(self.category_chain(name, hot, recent).chain)
            result.use_markdown_ = True
            return result
        except Exception as exc:
            return event.plain_result(f"查询论坛失败：{exc}")

    async def all_announcements(self):
        rows = []
        offset = 0
        while True:
            page = await self.discussions(tag="announcements", limit=100, offset=offset)
            rows.extend(page)
            if len(page) < 100:
                return rows
            offset += 100

    @filter.command("announcements", alias={"公告"})
    async def announcements(self, event: AstrMessageEvent):
        try:
            result = event.chain_result(self.announcement_chain(await self.all_announcements()).chain)
            result.use_markdown_ = True
            yield result
        except Exception as exc:
            yield event.plain_result(f"查询论坛失败：{exc}")

    @filter.command("transit", alias={"中转站"})
    async def transit(self, event: AstrMessageEvent):
        yield await self.category_result(event, "transit", "中转站")

    @filter.command("register_bot", alias={"注册器", "注册机"})
    async def register_bot(self, event: AstrMessageEvent):
        yield await self.category_result(event, "register-bot", "注册器")

    @filter.command("jailbreak_prompts", alias={"破甲词"})
    async def jailbreak_prompts(self, event: AstrMessageEvent):
        yield await self.category_result(event, "jailbreak-prompts", "破甲词")

    @filter.command("latest_news", alias={"最新资讯"})
    async def latest_news(self, event: AstrMessageEvent):
        yield await self.category_result(event, "latest-news", "最新资讯")

    @filter.command("tech_discussion", alias={"技术讨论"})
    async def tech_discussion(self, event: AstrMessageEvent):
        yield await self.category_result(event, "tech-discussion", "技术讨论")

    @filter.command("resource_analysis", alias={"资源分析", "资源分享"})
    async def resource_analysis(self, event: AstrMessageEvent):
        yield await self.category_result(event, "resource-share\t", "资源分析")

    @filter.command("off_topic", alias={"灌水区"})
    async def off_topic(self, event: AstrMessageEvent):
        yield await self.category_result(event, "off-topic", "灌水区")

    @filter.command("recent_posts", alias={"最近帖子"})
    async def recent_posts(self, event: AstrMessageEvent):
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=3)
            rows = [x for x in await self.discussions(limit=50) if iso_time(x["attributes"]["createdAt"]) >= cutoff]
            if not rows:
                yield event.plain_result("三天内暂无新发布的帖子。")
                return
            result = event.chain_result(self.markdown_chain(rows).chain)
            result.use_markdown_ = True
            yield result
        except Exception as exc:
            yield event.plain_result(f"查询论坛失败：{exc}")

    @filter.command("hot_posts", alias={"最火帖子"})
    async def hot_posts(self, event: AstrMessageEvent):
        try:
            rows = await self.tagged_discussions(sort="-commentCount", limit=20)
            if not rows:
                yield event.plain_result("暂无监控标签热门帖子。")
                return
            result = event.chain_result(self.markdown_chain(rows).chain)
            result.use_markdown_ = True
            yield result
        except Exception as exc:
            yield event.plain_result(f"查询论坛失败：{exc}")

    @filter.command("latest_posts", alias={"最新帖子"})
    async def latest_posts(self, event: AstrMessageEvent):
        pending = await self.get_kv_data("pending_latest_posts", [])
        if not isinstance(pending, list) or not pending:
            yield event.plain_result("暂无因官 Q 每日推送限额而待发的帖子。")
            return
        for start in range(0, len(pending), 20):
            parts = []
            for row in pending[start:start + 20]:
                try:
                    if parts:
                        parts.append(Comp.Plain(chr(10) * 2))
                    parts.extend(self.text_chain(await self.detail(row["id"]), "官 Q 限额待发帖子", True).chain)
                except Exception as exc:
                    logger.warning("读取待发帖子 %s 详情失败: %s", row.get("id"), exc)
            if parts:
                yield event.chain_result(parts)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forum_check", alias={"立即检查帖子"})
    async def forum_check(self, event: AstrMessageEvent):
        try:
            await self.check_once()
            yield event.plain_result("检查完成。")
        except Exception as exc:
            yield event.plain_result(f"检查失败：{exc}")

    async def terminate(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.http and not self.http.closed:
            await self.http.close()


if __name__ == "__main__":
    text, images = parse_content('<p>Hello<br>世界<img src="/a.png"></p>', BASE_DEFAULT)
    assert text == "Hello\n世界" and images == [BASE_DEFAULT + "/a.png"]
