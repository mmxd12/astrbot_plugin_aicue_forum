import asyncio
import html
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

BASE_DEFAULT = "https://flarum.aicue.top"


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


@register("astrbot_plugin_aicue_forum", "mmxd", "言灵工坊中转站帖子监控与查询", "1.2.3")
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

    def cfg(self, key, default=None):
        return self.config.get(key, default)

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
                    self.forum_user_id = str((await resp.json(content_type=None)).get("userId", ""))
                except (ValueError, TypeError):
                    self.forum_user_id = ""
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
                raise RuntimeError(f"论坛 API {resp.status}: {body[:300]}")
            return await resp.json(content_type=None)

    async def discussions(self, *, tag=None, sort="-createdAt", limit=20, offset=0):
        params = [f"sort={sort}", f"page[limit]={limit}", f"page[offset]={offset}", "include=user,tags"]
        if tag:
            params.append(f"filter[tag]={tag}")
        return (await self.api("discussions?" + "&".join(params)))["data"]

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

    def chain(self, post, prefix="论坛新帖", with_images=True):
        local_time = iso_time(post["created"]).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        excerpt = post["content"][:int(self.cfg("excerpt_length", 500))]
        if len(post["content"]) > len(excerpt):
            excerpt += "……"
        text = f"【{prefix}】\n{post['title']}\n作者：{post['author']}  发布时间：{local_time}\n"
        if excerpt:
            text += f"\n{excerpt}\n"
        text += f"\n原帖链接：{post['url']}"
        parts = [Comp.Plain(text)]
        if with_images:
            for image in post["images"][:max(0, int(self.cfg("max_images", 3)))]:
                parts.append(Comp.Image.fromURL(image))
        return MessageChain(parts)

    def markdown_chain(self, rows):
        lines = []
        for row in rows:
            attrs = row["attributes"]
            name = attrs["title"].replace("[", "\\[").replace("]", "\\]")
            lines.append(f"[{name}]({self.base}/d/{attrs['slug']})")
        chain = MessageChain([Comp.Plain(chr(10).join(lines))])
        chain.use_markdown_ = True
        return chain

    def is_qq_official_target(self, target):
        platform_id = str(target).split(":", 1)[0]
        for platform in self.context.platform_manager.platform_insts:
            meta = platform.meta()
            if meta.id == platform_id:
                return meta.name in {"qq_official", "qq_official_webhook"}
        return False

    async def consume_qq_quota(self, discussion_id):
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        quota = await self.get_kv_data("qq_daily_quota", {})
        if not isinstance(quota, dict) or quota.get("date") != today:
            quota = {"date": today, "count": 0, "discussion_ids": []}
        used_ids = {str(x) for x in quota.get("discussion_ids", [])}
        discussion_id = str(discussion_id)
        if discussion_id in used_ids:
            return True
        limit = max(1, int(self.cfg("qq_daily_push_limit", 1000)))
        if int(quota.get("count", 0)) >= limit:
            return False
        quota["count"] = int(quota.get("count", 0)) + 1
        quota["discussion_ids"] = [*used_ids, discussion_id]
        await self.put_kv_data("qq_daily_quota", quota)
        return True

    async def add_pending(self, row):
        pending = await self.get_kv_data("pending_latest_posts", [])
        if not isinstance(pending, list):
            pending = []
        if not any(str(item.get("id")) == str(row["id"]) for item in pending):
            pending.append({"id": str(row["id"]), "attributes": row["attributes"]})
            await self.put_kv_data("pending_latest_posts", pending[-1000:])

    async def remove_pending(self, discussion_id):
        pending = await self.get_kv_data("pending_latest_posts", [])
        if not isinstance(pending, list):
            return
        remaining = [x for x in pending if str(x.get("id")) != str(discussion_id)]
        if len(remaining) != len(pending):
            await self.put_kv_data("pending_latest_posts", remaining)

    async def targets(self):
        value = await self.get_kv_data("push_targets", [])
        return value if isinstance(value, list) else []

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
            await asyncio.sleep(max(30, int(self.cfg("check_interval_seconds", 120))))

    async def check_once(self):
        async with self.check_lock:
            await self.retry_pending_replies()
            seen_ids = [str(x) for x in await self.get_kv_data("seen_discussions", [])]
            seen = set(seen_ids)
            retry_targets = await self.get_kv_data("pending_push_targets", {})
            if not isinstance(retry_targets, dict):
                retry_targets = {}
            completed = set(retry_targets).intersection(seen)
            if completed:
                retry_targets = {key: value for key, value in retry_targets.items() if key not in completed}
                await self.put_kv_data("pending_push_targets", retry_targets)

            rows = []
            found_ids = set()
            offset = 0
            while True:
                page = await self.discussions(tag=str(self.cfg("tag_slug", "transit")), limit=100, offset=offset)
                rows.extend(page)
                found_ids.update(str(x["id"]) for x in page)
                if len(page) < 100 or (seen.intersection(found_ids) and set(retry_targets).issubset(found_ids)):
                    break
                offset += 100
            if len(page) < 100:
                stale = set(retry_targets) - found_ids
                if stale:
                    retry_targets = {key: value for key, value in retry_targets.items() if key not in stale}
                    await self.put_kv_data("pending_push_targets", retry_targets)
                    for discussion_id in stale:
                        await self.remove_pending(discussion_id)

            initialized = bool(await self.get_kv_data("monitor_initialized", False))
            if not initialized:
                if seen_ids:
                    await self.put_kv_data("monitor_initialized", True)
                else:
                    await self.put_kv_data("seen_discussions", list(found_ids)[-1000:])
                    await self.put_kv_data("monitor_initialized", True)
                    logger.info("言灵工坊监控已建立基线，共 %s 帖，不补推旧帖", len(found_ids))
                    return

            new_rows = [x for x in reversed(rows) if str(x["id"]) not in seen]
            targets = await self.targets()
            reply_started = await self.get_kv_data("forum_reply_started", [])
            if not isinstance(reply_started, list):
                reply_started = []
            reply_started = list(dict.fromkeys(str(x) for x in reply_started))[-1000:]
            reply_started_set = set(reply_started)

            for row in new_rows:
                row_id = str(row["id"])
                pending = retry_targets.get(row_id)
                delivery_targets = [x for x in (pending if isinstance(pending, list) else targets) if x in targets]
                retry_targets[row_id] = delivery_targets
                await self.put_kv_data("pending_push_targets", retry_targets)
                post = await self.detail(row_id)
                success = 0
                quota_skipped = False

                for target in delivery_targets.copy():
                    try:
                        if self.is_qq_official_target(target) and not await self.consume_qq_quota(row_id):
                            quota_skipped = True
                            continue
                        if not await self.context.send_message(target, self.chain(post)):
                            continue
                        success += 1
                        retry_targets[row_id].remove(target)
                        await self.put_kv_data("pending_push_targets", retry_targets)
                    except Exception as exc:
                        logger.warning("帖子 %s 推送至 %s 失败: %s", row_id, target, exc)

                if quota_skipped:
                    await self.add_pending(row)
                else:
                    await self.remove_pending(row_id)

                if success and row_id not in reply_started_set:
                    try:
                        await self.reply(row_id)
                    except Exception as exc:
                        await self.add_pending_reply(row_id)
                        logger.warning("帖子 %s 已推送但自动回帖失败: %s", row_id, exc)
                    reply_started.append(row_id)
                    reply_started = reply_started[-1000:]
                    reply_started_set = set(reply_started)
                    await self.put_kv_data("forum_reply_started", reply_started)

                if retry_targets[row_id]:
                    continue
                seen_ids.append(row_id)
                seen_ids = list(dict.fromkeys(seen_ids))[-1000:]
                await self.put_kv_data("seen_discussions", seen_ids)
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
        value = getattr(event, "unified_msg_origin", "")
        return str(value() if callable(value) else value).strip()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("push_here", alias={"推送本群"})
    async def push_here(self, event: AstrMessageEvent):
        origin = self.origin(event)
        if not origin or not event.get_group_id():
            yield event.plain_result("请在需要接收推送的群聊中执行此指令。")
            return
        targets = await self.targets()
        if origin in targets:
            targets.remove(origin); state = "已关闭本群的论坛推送"
        else:
            targets.append(origin); state = "已开启本群的论坛推送"
        await self.put_kv_data("push_targets", targets)
        yield event.plain_result(state)

    async def show(self, event, rows, title):
        if not rows:
            yield event.plain_result(f"{title}\n暂无符合条件的帖子。")
            return
        for row in rows[:int(self.cfg("search_result_count", 5))]:
            try:
                yield event.chain_result(self.chain(await self.detail(row["id"]), title, True).chain)
            except Exception as exc:
                logger.warning("读取帖子 %s 详情失败: %s", row["id"], exc)

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
            rows = await self.discussions(tag=str(self.cfg("tag_slug", "transit")), sort="-commentCount", limit=20)
            if not rows:
                yield event.plain_result("暂无中转站热门帖子。")
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
                    parts.extend(self.chain(await self.detail(row["id"]), "官 Q 限额待发帖子", True).chain)
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
