import asyncio
import hashlib
import json
import html
import re
import random
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp
from botpy.types.message import MarkdownPayload

BASE_DEFAULT = "https://www.aicue.top"
HELP_BASE = "https://help.aicue.top"
OAUTH_CLIENT_ID = "VOmlyWLHqZV2KCyR1nmryHPSGle7uZk"
IMAGE_HOST_BASE = "http://zzu2.wch1.top:44788"
IMAGE_HOST_DIR = "/AstrBot/data/imgs"
TAG_NAMES = {
    "announcements": "公告",
    "transit": "中转站",
    "register-bot": "注册器",
    "jailbreak-prompts": "破甲词",
    "latest-news": "最新资讯",
    "tech-discussion": "技术讨论",
    "resource-share": "资源分享",
    "off-topic": "灌水区",
}
TAG_ALIASES = {
    **{name: slug for slug, name in TAG_NAMES.items()},
    "注册机": "register-bot",
    "资源分享": "resource-share",
    "resource-share": "resource-share",
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


def md_size(shot, width=900):
    """QQ Markdown 图片必须声明尺寸，按真实截图比例折算，否则图片会被压扁。"""
    _, real_width, real_height = shot
    height = max(1, round(real_height * width / max(1, real_width)))
    return f"#{width}px #{height}px"


def err(exc):
    """aiohttp 超时等异常 str() 为空，日志会变成「失败: 」，故补上类型名。"""
    text = str(exc).strip()
    name = type(exc).__name__
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f"请求超时（{name}），论坛地址不通或网络拥塞"
    return f"{name}: {text}" if text else name


def iso_time(value: str):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class MarkdownPlain(Comp.Plain):
    """官 Q 走原生 Markdown。OneBot 协议没有 markdown 段类型，
    落回 text 段，否则 NapCat 之类会直接拒收整条消息。"""

    def toDict(self):
        return {"type": "text", "data": {"text": self.text}}


@register("astrbot_plugin_aicue_forum", "mmxd", "言灵工坊中转站帖子监控与查询", "1.5.25")
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
        self.ensure_monitor()

    def ensure_monitor(self):
        """插件热重载时 on_astrbot_loaded 不会再触发，这里兜底拉起监控循环。"""
        if self.task is not None and not self.task.done():
            return
        try:
            self.task = asyncio.get_running_loop().create_task(self.monitor())
        except RuntimeError:
            self.task = None

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
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.cfg_int("http_timeout_seconds", 20, minimum=5, maximum=120)))
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
            # 论坛后台的资源分享 slug 末尾误带制表符，兼容到后台修正为止。
            params["filter[tag]"] = "resource-share	" if tag == "resource-share" else tag
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
        user_ref = discussion.get("relationships", {}).get("user", {}).get("data") or {}
        user_id = user_ref.get("id")
        user = next((x for x in included if x["type"] == "users" and x["id"] == user_id), None)
        attrs = discussion["attributes"]
        content, images = parse_content(first["attributes"].get("contentHtml", "") if first else "", self.base)
        tag_refs = discussion.get("relationships", {}).get("tags", {}).get("data", [])
        tag_name = ""
        if tag_refs:
            first_tag = tag_refs[0]
            tag = next((x for x in included if x["type"] == "tags" and x["id"] == first_tag.get("id")), None)
            if tag:
                tag_name = tag.get("attributes", {}).get("name", "")
        return {
            "id": discussion["id"], "title": attrs["title"], "created": attrs["createdAt"],
            "comments": attrs.get("commentCount", 0), "author": (user or {}).get("attributes", {}).get("displayName", "未知用户"),
            "content": content, "images": images, "url": f"{self.base}/d/{attrs['slug']}",
            "tag": tag_name
        }

    def image_base(self):
        """只洗掉空白和引号。开头带 # 视为「本机图床已停用」，直接报错让本轮降级为纯文字。"""
        raw = str(self.cfg("image_host_base", IMAGE_HOST_BASE)).strip().strip("'\"").strip()
        base = raw.rstrip("/")
        if not base.startswith(("http://", "https://")):
            hint = "（开头的 # 号是注释掉的意思吗？删掉它才会启用本机图床）" if base.startswith("#") else ""
            raise RuntimeError(
                f"image_host_base 配置无效，应以 http:// 或 https:// 开头，当前为 {raw!r}{hint}"
            )
        return base

    async def publish_image(self, image_bytes: bytes) -> str:
        """写入本地文件服务目录，返回公网 URL"""
        base = self.image_base()
        directory = Path(str(self.cfg("image_host_dir", IMAGE_HOST_DIR)))
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        name = hashlib.sha1(image_bytes).hexdigest()[:16] + ".jpg"
        path = directory / name
        if not path.exists():
            temporary = path.with_suffix(".tmp")
            await asyncio.to_thread(temporary.write_bytes, image_bytes)
            await asyncio.to_thread(temporary.replace, path)
        return f"{base}/{name}"

    async def screenshot_post(self, url: str):
        """截取帖子网页，返回 (JPEG 字节, 宽, 高)。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("未安装 Playwright，请执行 pip install playwright") from exc

        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox"]
                )
            except Exception as exc:
                raise RuntimeError(
                    "启动 Chromium 失败，pip install playwright 只装了库、没装浏览器内核。"
                    "请在容器内执行 python -m playwright install --with-deps chromium，"
                    f"或改用远端截图服务 shot_service_url：{err(exc)}"
                ) from exc
            try:
                page = await browser.new_page(
                    viewport={"width": 1280, "height": 900},
                    device_scale_factor=1,
                )
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # 等帖子正文真正渲染出来，而不是傻等固定秒数；等不到再退回固定等待
                try:
                    await page.wait_for_selector(".Post-body", timeout=15000)
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception as exc:
                    logger.debug("帖子正文未等到，退回固定等待: %s", err(exc))
                    await page.wait_for_timeout(3000)
                limit = self.cfg_int("screenshot_max_height", 1400, minimum=400)
                try:
                    height = int(await page.evaluate("document.body.scrollHeight") or limit)
                except Exception:
                    height = limit
                height = max(400, min(height, limit))
                await page.set_viewport_size({"width": 1280, "height": height})
                await page.wait_for_timeout(300)
                image_bytes = await page.screenshot(
                    type="jpeg",
                    quality=self.cfg_int("screenshot_quality", 70, minimum=30, maximum=95),
                )
                return image_bytes, 1280, height
            finally:
                await browser.close()

    async def post_image_url(self, url):
        """返回 (公网 URL, 宽, 高)。配了远端截图服务就走远端，否则本机截图 + 本机落盘。"""
        service = str(self.cfg("shot_service_url", "")).strip()
        if service:
            return await self.remote_shot(url, service)
        image_bytes, width, height = await self.screenshot_post(url)
        return await self.publish_image(image_bytes), width, height

    async def remote_shot(self, url, service):
        """截图服务与图床同机，图片不经过本机带宽，只回传一段 JSON。"""
        session = await self.client()
        # 截图比接口慢得多，不能套用 http_timeout_seconds
        timeout = aiohttp.ClientTimeout(total=self.cfg_int("shot_service_timeout", 90, minimum=20, maximum=300))
        headers = {"X-Token": str(self.cfg("shot_service_token", ""))}
        async with session.post(
            service.rstrip("/") + "/shot", json={"url": url}, headers=headers, timeout=timeout
        ) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"截图服务返回 HTTP {resp.status}: {(await resp.text())[:200]}")
            data = await resp.json(content_type=None)
        image_url = str((data or {}).get("url", "")).strip()
        if not image_url.startswith(("http://", "https://")):
            raise RuntimeError(f"截图服务未返回有效图片地址: {str(data)[:200]}")
        return image_url, int(data.get("width") or 1280), int(data.get("height") or 900)

    def markdown_lines(self, rows, limit=10, with_author=True):
        lines = []
        for row in rows[:limit]:
            attrs = row["attributes"]
            name = attrs["title"].replace("[", "\\[").replace("]", "\\]")
            author = attrs.get("author") or "未知用户"
            suffix = f" --{author}" if with_author else ""
            lines.append(f"[{name} ↗]({self.base}/d/{attrs['slug']}){suffix}")
        return lines

    def ad_image(self):
        """从配置直接获取广告图"""
        url = self.cfg("ad_image_url", "")
        if url:
            return Comp.Image.fromURL(url)
        return None

    def ad_image_url(self):
        """直接返回配置的广告图 URL"""
        url = self.cfg("ad_image_url", "")
        if url:
            return url, 2580, 1342
        return None, 2580, 1342

    def fallback_query_chain(self, texts):
        parts = []
        for text in texts:
            image = self.ad_image()
            if image:
                parts.append(image)
            parts.append(MarkdownPlain(text))
        chain = MessageChain(parts)
        chain.use_markdown_ = True
        return chain

    async def public_query_chain(self, texts):
        try:
            image_url, width, height = self.ad_image_url()
            if str(image_url).startswith(("http://", "https://")):
                content = "\n\n".join(
                    f"![论坛图片 #{width}px #{height}px]({image_url})\n\n{text}"
                    for text in texts
                )
                chain = MessageChain([MarkdownPlain(content)])
                chain.use_markdown_ = True
                return chain
        except Exception as exc:
            logger.warning("生成查询公网图片失败，降级为先发图片再发 Markdown: %s", err(exc))
        return self.fallback_query_chain(texts)

    async def markdown_chain(self, rows):
        limit = self.cfg_int("search_result_count", 5, minimum=1, maximum=20)
        text = "\n".join(self.markdown_lines(rows, limit, True))
        return await self.public_query_chain([text])

    async def announcement_chain(self, rows):
        text = "公告：\n" + ("\n".join(self.markdown_lines(rows, len(rows), True)) or "暂无公告")
        return await self.public_query_chain([text])

    async def cleanup_published_images(self):
        """图多了 stat 上千次会卡住事件循环，丢进线程跑。"""
        directory = Path(str(self.cfg("image_host_dir", IMAGE_HOST_DIR)))
        if not directory.is_dir():
            return
        cutoff = datetime.now().timestamp() - self.cfg_int("image_keep_days", 3, minimum=1) * 86400
        await asyncio.to_thread(self._sweep_images, directory, cutoff)

    @staticmethod
    def _sweep_images(directory, cutoff):
        # .tmp 是写盘中途崩溃留下的，不清会永久占盘
        for path in (*directory.glob("*.jpg"), *directory.glob("*.tmp")):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError as exc:
                logger.warning("清理公网图片 %s 失败: %s", path, err(exc))

    async def monitor_delivery_chains(self, grouped, screenshots=None):
        screenshots = screenshots if screenshots is not None else {}
        markdown_lines = []
        fallback_parts = []
        for tag in self.tag_slugs():
            rows = grouped.get(tag, [])
            if not rows:
                continue
            heading = f"{TAG_NAMES.get(tag, tag)}最新帖子："
            markdown_lines.append(heading)
            if fallback_parts:
                fallback_parts.append(Comp.Plain(chr(10) * 2))
            fallback_parts.append(Comp.Plain(heading + chr(10)))
            for row in rows:
                attrs = row["attributes"]
                row_id = str(row["id"])
                post_url = f"{self.base}/d/{attrs['slug']}"
                # 同一帖对多个群只截一次图，多群推送时避免重复启浏览器、重复占带宽
                if row_id not in screenshots:
                    try:
                        screenshots[row_id] = await self.post_image_url(post_url)
                    except Exception as exc:
                        # 截图挂了也要把帖子推出去，否则会一直卡在待重试队列里反复重截
                        logger.warning("帖子 %s 截图失败，降级为纯文字推送: %s", row_id, err(exc))
                        screenshots[row_id] = None
                shot = screenshots.get(row_id)
                title = str(attrs["title"]).replace("[", "\\[").replace("]", "\\]")
                author = attrs.get("author", "未知用户")
                markdown_lines.append(chr(10).join(
                    ([f"![帖子截图 {md_size(shot)}]({shot[0]})"] if shot else [])
                    + [f"[{title} ↗]({post_url})", f"-- {author}"]
                ))
                if shot:
                    fallback_parts.append(Comp.Image.fromURL(shot[0]))
                fallback_parts.append(Comp.Plain(chr(10).join([
                    "",
                    f"标题：{attrs['title']}",
                    f"链接：{post_url}",
                    f"作者：{author}",
                    "",
                ])))
        markdown_chain = MessageChain([MarkdownPlain((chr(10) * 2).join(markdown_lines))])
        markdown_chain.use_markdown_ = True
        fallback_chain = MessageChain(fallback_parts)
        fallback_chain.use_markdown_ = False
        return markdown_chain, fallback_chain

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

    async def send_qq_markdown_target(self, target, markdown_chain, fallback_chain):
        if not await self.prepare_qq_target(target):
            logger.warning("官 Q 目标 %s 缺少会话场景/消息缓存，保留待重试", target)
            return False
        platform = self.target_platform(target)
        parts = str(target).split(":", 2)
        if not platform or len(parts) != 3 or not parts[2]:
            logger.warning("官 Q 目标 %s 无法解析平台实例或会话 ID，跳过推送", target)
            return False
        session_id = parts[2]
        scene = getattr(platform, "_session_scene", {}).get(session_id)
        if scene != "group":
            return await self.send_target(target, fallback_chain)
        content = "".join(
            part.text for part in markdown_chain.chain if isinstance(part, Comp.Plain)
        )
        msg_id = getattr(platform, "_session_last_message_id", {}).get(session_id)
        # 被动消息的 msg_id 仅 5 分钟有效，冷清群必然过期，因此先走主动消息再回退。
        ret = None
        last_error = None
        for attempt_id in dict.fromkeys([None, msg_id]):
            try:
                ret = await platform.client.api.post_group_message(
                    group_openid=session_id,
                    markdown=MarkdownPayload(content=content),
                    msg_type=2,
                    msg_id=attempt_id,
                    msg_seq=random.randint(1, 10000),
                )
                break
            except Exception as exc:
                last_error = exc
                if "不允许发送原生 markdown" in str(exc):
                    logger.warning("官 Q 不允许发送原生 Markdown，改用普通富媒体：%s", target)
                    return await self.send_target(target, fallback_chain)
                logger.warning(
                    "官 Q 推送失败（%s）：%s → %s",
                    "主动消息" if attempt_id is None else "被动消息",
                    target, err(exc),
                )
        if ret is None:
            hint = ""
            if "权限" in str(last_error) or "permission" in str(last_error).lower():
                hint = (
                    "｜主动消息被腾讯侧拒绝：①开放平台该机器人要先拿到「主动消息」权限并已发布上线（沙箱期只有沙箱群能收）；"
                    "②群里 群资料→机器人→允许主动发送消息 要打开；③主动消息有每群条数上限，用尽也报无权限。"
                    "临时办法：让群友在群里 @ 一次机器人，5 分钟内插件会改走被动消息把积压帖推出去。"
                )
            logger.warning("官 Q Markdown 推送全部方式失败，保留待重试：%s（%s）%s", target, err(last_error), hint)
            await self.note_target_error(target, err(last_error))
            return False
        sent_id = platform._extract_message_id(ret)
        if not sent_id:
            logger.warning("官 Q Markdown 推送未返回消息 ID，保留待重试：%s", target)
            return False
        platform.remember_session_message_id(session_id, sent_id)
        await self.note_target_error(target)
        return True

    async def note_target_error(self, target, reason=""):
        """记住某群最近一次推送被拒的原因，/推送状态 直接显示，不用翻日志。传空表示恢复正常。"""
        errors = await self.get_kv_data("push_last_error", {})
        errors = errors if isinstance(errors, dict) else {}
        if errors.get(str(target), "") == reason:
            return
        if reason:
            errors[str(target)] = reason
        else:
            errors.pop(str(target), None)
        await self.put_kv_data("push_last_error", errors)

    @staticmethod
    def permission_text(reason, official=True):
        if not reason:
            return "已开启全量" if official else "正常"
        if "权限" in reason or "permission" in reason.lower():
            return "× 无权限（未开启全量，群资料→机器人→允许主动发送消息）"
        return f"× 上次失败：{reason}"

    async def send_target(self, target, chain):
        if not await self.prepare_qq_target(target):
            logger.warning("官 Q 目标 %s 缺少会话场景/消息缓存，保留待重试", target)
            return False
        platform = self.target_platform(target)
        if not platform:
            logger.warning("推送目标 %s 找不到平台实例（平台 ID 变更或适配器未启用），跳过", target)
            return False
        parts = str(target).split(":", 2)
        if len(parts) != 3 or not parts[2]:
            logger.warning("推送目标 %s 格式无效，跳过", target)
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

    def note_push_failure(self, counts, rows, target, retry_targets):
        """同一帖对同一群连续失败超过上限就放弃，避免死帖每轮重新截图、无限占带宽。"""
        limit = self.cfg_int("push_retry_max", 20, minimum=1)
        for row in rows:
            row_id = str(row["id"])
            key = self.quota_key(row_id, target)
            counts[key] = int(counts.get(key, 0)) + 1
            if counts[key] < limit:
                continue
            counts.pop(key, None)
            if target in retry_targets.get(row_id, []):
                retry_targets[row_id].remove(target)
            logger.warning("帖子 %s 推送至 %s 连续失败 %s 次，放弃该群", row_id, target, limit)

    @staticmethod
    def quota_key(discussion_id, target):
        # QQ 按发出的消息计费，同一帖推给 N 个群就是 N 条，故按 帖子+目标 计数
        return f"{discussion_id}@{target}"

    async def consume_qq_quota(self, discussion_id, target, commit=True):
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
        key = self.quota_key(discussion_id, target)
        if key in used_ids:
            return True
        limit = self.cfg_int("qq_daily_push_limit", 1000, minimum=1)
        if count >= limit:
            logger.warning("官 Q 今日主动消息额度 %s 已用尽，%s 保留待明日重试", limit, key)
            return False
        if commit:
            quota["count"] = count + 1
            quota["discussion_ids"] = [*used_ids, key]
            await self.put_kv_data("qq_daily_quota", quota)
        return True

    async def qq_quota_eligible(self, rows, target):
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
            key = self.quota_key(str(row["id"]), target)
            if key in used_ids:
                eligible.append(row)
            elif remaining > 0:
                eligible.append(row)
                used_ids.add(key)
                remaining -= 1
        return eligible

    async def targets(self):
        value = await self.get_kv_data("push_targets", [])
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(x for x in value if isinstance(x, str) and x.strip()))

    async def retry_pending_replies(self):
        pending = await self.get_kv_data("pending_forum_replies", [])
        if not isinstance(pending, list) or not pending:
            return
        # 每次只重试少量，否则一个 20 秒超时 × N 帖会把整轮监控拖垮
        batch = self.cfg_int("reply_retry_batch", 3, minimum=1, maximum=20)
        fails = await self.get_kv_data("reply_retry_fails", {})
        fails = fails if isinstance(fails, dict) else {}
        limit = self.cfg_int("reply_retry_max", 5, minimum=1, maximum=50)
        queue = list(dict.fromkeys(str(x) for x in pending))
        remaining = queue[batch:]
        for discussion_id in queue[:batch]:
            try:
                await self.reply(discussion_id)
                fails.pop(discussion_id, None)
            except Exception as exc:
                status = getattr(exc, "status", None) if isinstance(exc, ForumAPIError) else None
                if status in {403, 404, 410}:
                    logger.info("帖子 %s 已删除或不可回复（HTTP %s），放弃自动回帖", discussion_id, status)
                    fails.pop(discussion_id, None)
                    continue
                count = int(fails.get(discussion_id, 0)) + 1
                if count >= limit:
                    logger.warning("帖子 %s 自动回帖连续失败 %s 次，放弃：%s", discussion_id, count, err(exc))
                    fails.pop(discussion_id, None)
                    continue
                fails[discussion_id] = count
                remaining.append(discussion_id)
                logger.warning("帖子 %s 自动回帖重试失败（第 %s 次）: %s", discussion_id, count, err(exc))
        await self.put_kv_data("reply_retry_fails", fails)
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
            logger.warning("言灵工坊论坛账号登录失败，自动回帖暂不可用: %s", err(exc))
        # 恢复之前保存的推送目标会话场景，避免重启后推送失效
        targets = await self.targets()
        for target in targets:
            await self.prepare_qq_target(target)

        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.monitor())
    async def monitor(self):
        await asyncio.sleep(5)
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("言灵工坊监控失败: %s", err(exc))
            await asyncio.sleep(self.cfg_int("check_interval_seconds", 120, minimum=30))

    async def check_once(self):
        async with self.check_lock:
            await self.cleanup_published_images()
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
            push_fails = await self.get_kv_data("push_fail_counts", {})
            if not isinstance(push_fails, dict):
                push_fails = {}
            # 群被关掉推送后，它的失败计数会永远留在 KV 里，顺手清掉
            push_fails = {
                key: value for key, value in push_fails.items()
                if str(key).split("@", 1)[-1] in targets
            }

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
                            logger.warning("帖子 %s 详情读取失败，保留待重试: %s", row_id, err(exc))
                            continue
                        retry_targets.pop(row_id, None)
                        await self.put_kv_data("pending_push_targets", retry_targets)
                        seen_ids.append(row_id)
                        seen_ids = list(dict.fromkeys(seen_ids))[-1000:]
                        await self.put_kv_data("seen_discussions", seen_ids)
                        for tag in row.get("_monitor_tags", []):
                            tag_seen[tag] = list(dict.fromkeys(tag_seen.get(tag, []) + [row_id]))[-1000:]
                        await self.put_kv_data("monitor_tag_seen", tag_seen)
                        seen.add(row_id)
                        continue
                    except Exception as exc:
                        logger.warning("帖子 %s 详情读取失败，保留待重试: %s", row_id, err(exc))
                        continue
                row_tags = set(row.get("_monitor_tags", ()))
                row["_monitor_tag"] = next((tag for tag in tags if tag in row_tags), tags[0])
                prepared[row_id] = row
            await self.put_kv_data("pending_push_targets", retry_targets)

            sent_by_row = {row_id: 0 for row_id in prepared}
            screenshots = {}
            for target in targets:
                target_rows = [row for row_id, row in prepared.items() if target in retry_targets.get(row_id, [])]
                eligible = await self.qq_quota_eligible(target_rows, target) if self.is_qq_official_target(target) else target_rows
                if not eligible:
                    continue
                # 单轮单群限量，避免积压一次性糊出去把带宽和客户端拖垮
                eligible = eligible[:self.cfg_int("max_posts_per_push", 5, minimum=1)]
                grouped = {}
                for row in eligible:
                    grouped.setdefault(row["_monitor_tag"], []).append(row)
                try:
                    markdown_chain, fallback_chain = await self.monitor_delivery_chains(grouped, screenshots)
                    if self.is_qq_official_target(target):
                        sent = await self.send_qq_markdown_target(
                            target, markdown_chain, fallback_chain
                        )
                    else:
                        # OneBot 没有 markdown 段类型，非官 Q 一律走普通图文
                        sent = await self.send_target(target, fallback_chain)
                    if not sent:
                        self.note_push_failure(push_fails, eligible, target, retry_targets)
                        continue
                    for row in eligible:
                        row_id = str(row["id"])
                        if self.is_qq_official_target(target):
                            await self.consume_qq_quota(row_id, target)
                        sent_by_row[row_id] += 1
                        retry_targets[row_id].remove(target)
                        push_fails.pop(self.quota_key(row_id, target), None)
                    await self.put_kv_data("pending_push_targets", retry_targets)
                except Exception as exc:
                    self.note_push_failure(push_fails, eligible, target, retry_targets)
                    logger.warning("新帖汇总推送至 %s 失败: %s", target, err(exc))
            await self.put_kv_data("push_fail_counts", push_fails)
            await self.put_kv_data("pending_push_targets", retry_targets)

            for row_id, row in prepared.items():
                if sent_by_row[row_id] and row_id not in reply_started_set:
                    try:
                        await self.reply(row_id)
                    except Exception as exc:
                        await self.add_pending_reply(row_id)
                        logger.warning("帖子 %s 已推送但自动回帖失败: %s", row_id, err(exc))
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

    async def target_names(self):
        names = await self.get_kv_data("target_names", {})
        return names if isinstance(names, dict) else {}

    async def fetch_group_name(self, target, group_id):
        """官方 QQ 机器人拿不到群名（openid 是脱敏的），仅 aiocqhttp 可查。"""
        platform = self.target_platform(target)
        if not platform or platform.meta().name != "aiocqhttp":
            return ""
        try:
            info = await platform.get_client().api.call_action("get_group_info", group_id=int(group_id))
            return str(info.get("group_name") or "").strip()
        except Exception as exc:
            logger.debug("获取群名称失败 %s：%s", target, err(exc))
            return ""

    def target_label(self, target, names):
        note = names.get(target)
        session_id = str(target).split(":", 2)[-1]
        short = session_id if len(session_id) <= 12 else f"{session_id[:6]}…{session_id[-4:]}"
        return f"{note}（{short}）" if note else short

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("push_here", alias={"推送本群"})
    async def push_here(self, event: AstrMessageEvent, note: str = ""):
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
        names = await self.target_names()
        if origin in targets:
            label = note.strip() or names.get(origin) or await self.fetch_group_name(origin, event.get_group_id())
            if label:
                names[origin] = label
                state += f"，备注：{label}"
            else:
                state += "，建议用「/推送本群 备注名」给本群起个名字，方便在 /推送状态 里分辨"
        else:
            names.pop(origin, None)
        await self.put_kv_data("target_names", names)
        await self.put_kv_data("push_targets", targets)
        yield event.plain_result(state)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("push_note", alias={"推送备注"})
    async def push_note(self, event: AstrMessageEvent, index: int = 0, note: str = ""):
        targets = await self.targets()
        if not 1 <= index <= len(targets) or not note.strip():
            listing = " / ".join(f"{i}.{str(x).split(':', 2)[-1][:6]}…" for i, x in enumerate(targets, 1))
            yield event.plain_result(f"用法：/推送备注 <序号> <名字>\n当前目标：{listing or '无'}")
            return
        names = await self.target_names()
        names[targets[index - 1]] = note.strip()
        await self.put_kv_data("target_names", names)
        yield event.plain_result(f"已把第 {index} 个目标备注为：{note.strip()}")

    async def query_results(self, event, chain):
        if event.get_platform_name() not in {"qq_official", "qq_official_webhook"}:
            result = event.chain_result(chain.chain)
            result.use_markdown_ = True
            return [result]
        results = []
        for part in chain.chain:
            result = event.chain_result([part])
            result.use_markdown_ = isinstance(part, MarkdownPlain)
            results.append(result)
        return results

    async def category_delivery_chains(self, name, hot, recent):
        newline = chr(10)
        sections = [
            f"{name}最火帖子：" + newline
            + (newline.join(self.markdown_lines(hot, with_author=True)) or "暂无帖子"),
            f"{name}最近帖子：" + newline
            + (newline.join(self.markdown_lines(recent, with_author=True)) or "暂无帖子"),
        ]
        fallback_parts = []
        header = Path(__file__).parent / "assets" / "forum_header.png"
        image = self.ad_image() or (Comp.Image.fromFileSystem(header) if header.is_file() else None)
        if image:
            fallback_parts.append(image)
        lines = []
        for title, rows in ((f"{name}最火帖子：", hot), (f"{name}最近帖子：", recent)):
            lines.append(title)
            if not rows:
                lines.append("暂无帖子")
                continue
            for row in rows[:10]:
                attrs = row["attributes"]
                lines.append(newline.join([
                    f"标题：{attrs['title']}",
                    f"链接：{self.base}/d/{attrs['slug']}",
                    f"作者：{attrs.get('author') or '未知用户'}",
                ]))
        fallback_parts.append(Comp.Plain(newline + (newline * 2).join(lines)))
        fallback = MessageChain(fallback_parts)
        fallback.use_markdown_ = False
        try:
            image_url, width, height = self.ad_image_url()
            if str(image_url).startswith(("http://", "https://")):
                markdown = MessageChain([MarkdownPlain(
                    f"![论坛图片 #{width}px #{height}px]({image_url})"
                    + newline * 2 + (newline * 2).join(sections)
                )])
                markdown.use_markdown_ = True
                return markdown, fallback
        except Exception as exc:
            logger.warning("生成分类查询公网广告图失败，降级为普通图文: %s", err(exc))
        return None, fallback

    async def send_qq_category(self, event, markdown, fallback):
        if markdown is None:
            await event.send(fallback)
            return True
        source = event.message_obj.raw_message
        content = "".join(part.text for part in markdown.chain if isinstance(part, Comp.Plain))
        payload = {
            "markdown": MarkdownPayload(content=content),
            "msg_type": 2,
            "msg_id": event.message_obj.message_id,
            "msg_seq": random.randint(1, 10000),
        }
        try:
            if getattr(source, "group_openid", None):
                await event.bot.api.post_group_message(
                    group_openid=source.group_openid, **payload
                )
            elif getattr(getattr(source, "author", None), "user_openid", None):
                await event.post_c2c_message(
                    openid=source.author.user_openid, **payload
                )
            else:
                return False
        except Exception as exc:
            if "不允许发送原生 markdown" not in str(exc):
                raise
            logger.warning("官 Q 不允许发送分类 Markdown，改用普通图文。")
            await event.send(fallback)
        return True

    async def category_result(self, event, slug, name):
        try:
            hot, recent = await asyncio.gather(
                self.discussions(tag=slug, sort="-commentCount", limit=10),
                self.discussions(tag=slug, sort="-createdAt", limit=10),
            )
            markdown, fallback = await self.category_delivery_chains(name, hot, recent)
            if event.get_platform_name() in {"qq_official", "qq_official_webhook"}:
                if await self.send_qq_category(event, markdown, fallback):
                    return []
            return await self.query_results(event, markdown or fallback)
        except Exception as exc:
            return [event.plain_result(f"查询论坛失败：{err(exc)}")]

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
            for result in await self.query_results(event, await self.announcement_chain(await self.all_announcements())):
                yield result
        except Exception as exc:
            yield event.plain_result(f"查询论坛失败：{err(exc)}")

    @filter.command("transit", alias={"中转站"})
    async def transit(self, event: AstrMessageEvent):
        for result in await self.category_result(event, "transit", "中转站"):
            yield result

    @filter.command("register_bot", alias={"注册器", "注册机"})
    async def register_bot(self, event: AstrMessageEvent):
        for result in await self.category_result(event, "register-bot", "注册器"):
            yield result

    @filter.command("jailbreak_prompts", alias={"破甲词"})
    async def jailbreak_prompts(self, event: AstrMessageEvent):
        for result in await self.category_result(event, "jailbreak-prompts", "破甲词"):
            yield result

    @filter.command("latest_news", alias={"最新资讯"})
    async def latest_news(self, event: AstrMessageEvent):
        for result in await self.category_result(event, "latest-news", "最新资讯"):
            yield result

    @filter.command("tech_discussion", alias={"技术讨论"})
    async def tech_discussion(self, event: AstrMessageEvent):
        for result in await self.category_result(event, "tech-discussion", "技术讨论"):
            yield result

    @filter.command("resource_analysis", alias={"资源分析", "资源分享"})
    async def resource_analysis(self, event: AstrMessageEvent):
        for result in await self.category_result(event, "resource-share", "资源分享"):
            yield result

    @filter.command("off_topic", alias={"灌水区"})
    async def off_topic(self, event: AstrMessageEvent):
        for result in await self.category_result(event, "off-topic", "灌水区"):
            yield result

    @filter.command("recent_posts", alias={"最近帖子"})
    async def recent_posts(self, event: AstrMessageEvent):
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=3)
            rows = [x for x in await self.discussions(limit=50) if iso_time(x["attributes"]["createdAt"]) >= cutoff]
            if not rows:
                yield event.plain_result("三天内暂无新发布的帖子。")
                return
            for result in await self.query_results(event, await self.markdown_chain(rows)):
                yield result
        except Exception as exc:
            yield event.plain_result(f"查询论坛失败：{err(exc)}")

    @filter.command("hot_posts", alias={"最火帖子"})
    async def hot_posts(self, event: AstrMessageEvent):
        try:
            rows = await self.tagged_discussions(sort="-commentCount", limit=20)
            if not rows:
                yield event.plain_result("暂无监控标签热门帖子。")
                return
            for result in await self.query_results(event, await self.markdown_chain(rows)):
                yield result
        except Exception as exc:
            yield event.plain_result(f"查询论坛失败：{err(exc)}")

    @filter.command("push_help", alias={"推送帮助", "论坛帮助"})
    async def push_help(self, event: AstrMessageEvent):
        lines = [
            "言灵工坊论坛插件 · 指令一览",
            "",
            "【看帖】人人可用",
            "  /最近帖子     三天内新发布的帖子",
            "  /最火帖子     监控标签内按回复数排序",
            "  /公告         全部公告",
            "",
            "【分区】人人可用",
            "  /中转站  /注册器  /破甲词",
            "  /最新资讯  /技术讨论  /资源分享  /灌水区",
            "",
            "【邀请码】人人可用",
            "  /邀请码       申请一个新的邀请码",
            "  /我的邀请码   查看已申请的邀请码及状态",
            "",
            "【推送管理】仅管理员",
            "  /推送本群 [备注]   开启或关闭本群推送，可顺手起个名",
            "  /推送备注 <序号> <名字>   给已开启的群补备注",
            "  /推送状态          额度用量、各群状态、积压帖数、监控是否在跑",
            "  /立即检查帖子      不等定时，马上抓一轮新帖",
            "",
            f"新帖检查间隔：{self.cfg_int('check_interval_seconds', 120, minimum=30)} 秒",
            f"单群单轮最多推：{self.cfg_int('max_posts_per_push', 5, minimum=1)} 帖",
            f"截图保留：{self.cfg_int('image_keep_days', 3, minimum=1)} 天",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("push_status", alias={"推送状态"})
    async def push_status(self, event: AstrMessageEvent):
        targets = await self.targets()
        scenes = await self.get_kv_data("qq_target_scenes", {})
        scenes = scenes if isinstance(scenes, dict) else {}
        retry = await self.get_kv_data("pending_push_targets", {})
        retry = retry if isinstance(retry, dict) else {}
        loaded = [x.meta().id for x in self.context.platform_manager.platform_insts]
        quota = await self.get_kv_data("qq_daily_quota", {})
        quota = quota if isinstance(quota, dict) else {}
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        used = quota.get("count", 0) if quota.get("date") == today else 0
        lines = [
            f"今日官 Q 主动消息：{used} / {self.cfg_int('qq_daily_push_limit', 1000, minimum=1)} 条",
            f"监控循环：{'运行中' if self.task and not self.task.done() else '未运行'}",
            f"已加载平台：{'、'.join(loaded) or '无'}",
            f"推送目标：{len(targets)} 个",
        ]
        names = await self.target_names()
        errors = await self.get_kv_data("push_last_error", {})
        errors = errors if isinstance(errors, dict) else {}
        for idx, target in enumerate(targets, 1):
            platform = self.target_platform(target)
            session_id = str(target).split(":", 2)[-1]
            scene = scenes.get(target) or (getattr(platform, "_session_scene", None) or {}).get(session_id)
            msg_id = (getattr(platform, "_session_last_message_id", None) or {}).get(session_id)
            waiting = sum(1 for x in retry.values() if isinstance(x, list) and target in x)
            qq_official = bool(platform) and platform.meta().name in {"qq_official", "qq_official_webhook"}
            lines.extend([
                "",
                f"{idx}. {self.target_label(target, names)}",
                f"  平台：{platform.meta().name if platform else '× 未找到（推送会被跳过）'}",
                f"  会话场景：{scene or '未知（推送会被跳过）'}",
                f"  msg_id 缓存：{'有' if msg_id else '无（走主动消息）'}",
                f"  权限：{self.permission_text(str(errors.get(target, '')), qq_official)}",
                f"  积压待推：{waiting} 帖",
            ])
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forum_check", alias={"立即检查帖子"})
    async def forum_check(self, event: AstrMessageEvent):
        try:
            await self.check_once()
            yield event.plain_result("检查完成。")
        except Exception as exc:
            yield event.plain_result(f"检查失败：{err(exc)}")

    @filter.command("invite_code", alias={"邀请码", "邀请"})
    async def invite_code(self, event: AstrMessageEvent):
        """自动申请一个言灵工坊邀请码"""
        import urllib.parse, asyncio as _asyncio, re as _re, json
        from astrbot.core.message.message_event_result import MessageChain
        
        try:
            # 1. 获取授权链接 + PHPSESSID
            async with aiohttp.ClientSession() as s:
                async with s.get(HELP_BASE + "/oauth/authorize", allow_redirects=False) as resp:
                    if resp.status not in (302, 303):
                        yield event.plain_result("❌ 无法获取授权链接")
                        return
                    location = resp.headers.get("Location", "")
                    if not location:
                        yield event.plain_result("❌ 授权链接为空")
                        return
                phpsessid = None
                for c in s.cookie_jar:
                    if c.key == "PHPSESSID":
                        phpsessid = c.value
                        break
            
            if not phpsessid:
                yield event.plain_result("❌ 无法获取帮助站 session")
                return
            
            # 2. 尝试私聊发送授权链接
            openid = event.get_sender_id()
            priv_session = f"{event.session.platform_name}:FriendMessage:{openid}"
            link_msg = f"🔗 请点击链接完成授权：\n{location}\n\n授权完成后我会自动获取邀请码"
            
            sent_priv = False
            try:
                await self.context.send_message(priv_session, MessageChain().message(link_msg))
                sent_priv = True
                yield event.plain_result("✅ 授权链接已私聊发送，请查看私聊消息完成授权")
            except Exception as e:
                logger.warning(f"私聊发送失败: {e}")
                # 私聊失败，群内回复完整链接
                yield event.plain_result(
                    f"🔗 请点击链接完成授权：\n{location}\n\n"
                    f"（链接较长，建议复制到浏览器打开）\n"
                    f"授权完成后我会自动获取邀请码"
                )
            
            # 3. 轮询等待授权完成（最多 120 秒）
            for i in range(24):
                await _asyncio.sleep(5)
                try:
                    async with aiohttp.ClientSession() as s2:
                        ck = {"Cookie": f"PHPSESSID={phpsessid}"}
                        async with s2.get(HELP_BASE + "/user/invite_api?action=info",
                            headers={**ck, "X-Requested-With": "XMLHttpRequest"}) as resp:
                            text = await resp.text()
                        if '"success"' in text:
                            info = json.loads(text)
                            if info.get("success"):
                                # 授权完成！获取 CSRF 创建邀请码
                                async with aiohttp.ClientSession() as s3:
                                    ck3 = {"Cookie": f"PHPSESSID={phpsessid}"}
                                    async with s3.get(HELP_BASE + "/user/invite", headers=ck3) as resp:
                                        body = await resp.text()
                                        m = _re.search(r'id="csrfToken" value="([^"]+)"', body)
                                    if m:
                                        csrf = m.group(1)
                                        async with s3.post(HELP_BASE + "/user/invite_api?action=create",
                                            json={"csrf_token": csrf},
                                            headers={"Cookie": f"PHPSESSID={phpsessid}", "X-Requested-With": "XMLHttpRequest"}) as resp2:
                                            text2 = await resp2.text()
                                        try:
                                            data2 = json.loads(text2)
                                            if data2.get("success"):
                                                code = data2.get("invite", {}).get("code") or data2.get("code", "")
                                                yield event.plain_result(
                                                    f"✅ 邀请码申请成功！\n\n"
                                                    f"邀请码：{code}\n"
                                                    f"注册地址：{HELP_BASE}/register?code={code}"
                                                )
                                            else:
                                                yield event.plain_result(f"❌ {data2.get('msg', '申请失败')}")
                                        except:
                                            yield event.plain_result(f"❌ API 返回异常：{text2[:100]}")
                                    else:
                                        yield event.plain_result("❌ 授权成功但无法获取 CSRF token")
                                return
                except Exception:
                    pass
            
            yield event.plain_result("⏰ 授权超时，请重新发送 /邀请码")
        except Exception as exc:
            yield event.plain_result(f"❌ 异常：{err(exc)}")
    @filter.command("help_login", alias={"登录帮助站", "帮助站登录"})
    async def help_login(self, event: AstrMessageEvent):
        """配置帮助站登录 session"""
        msg = event.message_str.strip()
        # 提取 session 值
        session_val = msg.replace("/help_login", "").replace("登录帮助站", "").replace("帮助站登录", "").strip()
        if session_val:
            try:
                from astrbot.core.star.config import update_config
                update_config("astrbot_plugin_aicue_forum_config", "help_session", session_val)
                self.config["help_session"] = session_val
                yield event.plain_result("✅ 帮助站 session 已保存！现在可以使用 /邀请码 了")
            except Exception as e:
                yield event.plain_result(f"❌ 保存失败：{err(e)}")
        else:
            yield event.plain_result(
                "📋 请按以下步骤操作：\n\n"
                "1. 浏览器打开 https://help.aicue.top/user/invite\n"
                "2. 用论坛账号登录并授权\n"
                "3. 按 F12 → Application → Cookies → help.aicue.top\n"
                "4. 复制 PHPSESSID 的值\n"
                "5. 发送：/help_login 你的PHPSESSID值"
            )

    @filter.command("my_invite_codes", alias={"我的邀请码"})
    async def my_invite_codes(self, event: AstrMessageEvent):
        """查看我申请的邀请码及使用状态"""
        help_session = str(self.cfg("help_session", "")).strip()
        if not help_session:
            yield event.plain_result("❌ 未配置帮助站 session，请先发 /邀请码 自动登录")
            return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                ck = {"Cookie": f"PHPSESSID={help_session}"}
                async with s.get(HELP_BASE + "/user/invite_api?action=info",
                    headers={**ck, "X-Requested-With": "XMLHttpRequest"}) as resp:
                    data = await resp.json(content_type=None)
                if data.get("success"):
                    stats = data.get("stats", {})
                    codes = data.get("invites", data.get("codes", data.get("invite_codes", [])))
                    total = stats.get('total_codes', len(codes) if codes else '?')
                    used_codes = [c.get('code', '?') for c in codes if c.get('used_count', 0) > 0 or c.get('used', c.get('is_used', False))]
                    remaining = data.get('remaining', '?')
                    if used_codes:
                        if len(used_codes) > 4:
                            parts = []
                            for i in range(0, len(used_codes), 4):
                                parts.append("，".join(used_codes[i:i+4]))
                            used_str = "，" + "\n".join(parts)
                        else:
                            used_str = "，".join(used_codes)
                    else:
                        used_str = "无"
                    lines = [
                        f"邀请码总计：{total}个",
                        f"已使用：{used_str}",
                        f"剩余配额：{remaining}",
                    ]
                    yield event.plain_result("\n".join(lines))
                else:
                    yield event.plain_result(f"❌ {data.get('msg', '查询失败')}")
        except Exception as exc:
            yield event.plain_result(f"❌ 查询异常：{err(exc)}")
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
