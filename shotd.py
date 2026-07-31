"""帖子截图服务：跑在同时装了 Playwright 和 Caddy 的那台机器上。

截图直接落进 Caddy 的静态目录，只把 JSON 回给 bot，图片不占 bot 的带宽。

    POST /shot  {"url": "https://www.aicue.top/d/123-xxx"}
    -> {"url": "http://图床域名/ab12cd34.jpg", "width": 1280, "height": 964}

依赖：pip install aiohttp playwright && python -m playwright install --with-deps chromium
启动：SHOT_TOKEN=xxx SHOT_IMAGE_BASE=http://图床域名 python shotd.py

注意：本服务走明文 HTTP，Token 在链路上是可见的。请用防火墙只放行 bot 的 IP，
例如 ufw allow from <botIP> to any port 8899，不要对公网敞开。
截图质量与最大高度由本服务的环境变量决定，插件里那两项配置在远端模式下不生效。
"""
import asyncio
import hashlib
import hmac
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from playwright.async_api import async_playwright

TOKEN = os.environ.get("SHOT_TOKEN", "")
IMAGE_DIR = Path(os.environ.get("SHOT_IMAGE_DIR", "/var/www/images"))
IMAGE_BASE = os.environ.get("SHOT_IMAGE_BASE", "http://127.0.0.1:8080").rstrip("/")
ALLOW_HOSTS = {x.strip().lower() for x in os.environ.get("SHOT_ALLOW_HOSTS", "www.aicue.top,flarum.aicue.top").split(",") if x.strip()}
WAIT_SELECTOR = os.environ.get("SHOT_WAIT_SELECTOR", ".Post-body")
KEEP_DAYS = int(os.environ.get("SHOT_KEEP_DAYS", "3"))
MAX_HEIGHT = int(os.environ.get("SHOT_MAX_HEIGHT", "1400"))
QUALITY = int(os.environ.get("SHOT_QUALITY", "70"))
PORT = int(os.environ.get("SHOT_PORT", "8899"))
RESTART_AFTER = int(os.environ.get("SHOT_RESTART_AFTER", "200"))  # 截这么多张就重启浏览器，防内存越吃越多
WIDTH = 1280


async def get_browser(app):
    """专机专用，浏览器常驻复用；断了或截够张数就重新拉起。"""
    browser = app.get("browser")
    if browser is not None and browser.is_connected() and app.get("shots", 0) >= RESTART_AFTER > 0:
        await browser.close()
        browser = None
        app["shots"] = 0
    if browser is None or not browser.is_connected():
        browser = await app["playwright"].chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        app["browser"] = browser
    return browser


async def capture(app, url):
    browser = await get_browser(app)
    page = await browser.new_page(viewport={"width": WIDTH, "height": 900}, device_scale_factor=1)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector(WAIT_SELECTOR, timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            await page.wait_for_timeout(3000)
        try:
            height = int(await page.evaluate("document.body.scrollHeight") or MAX_HEIGHT)
        except Exception:
            height = MAX_HEIGHT
        height = max(400, min(height, MAX_HEIGHT))
        await page.set_viewport_size({"width": WIDTH, "height": height})
        await page.wait_for_timeout(300)
        image_bytes = await page.screenshot(type="jpeg", quality=QUALITY)
        app["shots"] = app.get("shots", 0) + 1
        return image_bytes, height
    finally:
        await page.close()


def cleanup(app):
    """每小时最多清一次，删掉过期图片。"""
    if time.time() - app.get("cleaned", 0) < 3600:
        return
    app["cleaned"] = time.time()
    cutoff = time.time() - KEEP_DAYS * 86400
    for path in list(IMAGE_DIR.glob("*.jpg")) + list(IMAGE_DIR.glob("*.tmp")):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


async def shot(request):
    token = request.headers.get("X-Token") or request.query.get("token", "")
    if not TOKEN or not hmac.compare_digest(token, TOKEN):
        raise web.HTTPUnauthorized(text="token 不正确")
    try:
        payload = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="请求体不是合法 JSON")
    url = str((payload or {}).get("url", "")).strip()
    parsed = urlparse(url)
    # 不校验域名的话，这就是个任人使唤的截图代理，还能拿去探内网
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in ALLOW_HOSTS:
        raise web.HTTPBadRequest(text=f"域名不在白名单：{parsed.hostname}")
    async with request.app["lock"]:
        image_bytes, height = await capture(request.app, url)
    name = hashlib.sha1(image_bytes).hexdigest()[:16] + ".jpg"
    path = IMAGE_DIR / name
    if not path.exists():
        temporary = path.with_suffix(".tmp")
        await asyncio.to_thread(temporary.write_bytes, image_bytes)
        await asyncio.to_thread(temporary.replace, path)
    cleanup(request.app)
    return web.json_response({"url": f"{IMAGE_BASE}/{name}", "width": WIDTH, "height": height})


async def health(request):
    browser = request.app.get("browser")
    return web.json_response({
        "ok": True,
        "dir": str(IMAGE_DIR),
        "base": IMAGE_BASE,
        "hosts": sorted(ALLOW_HOSTS),
        "browser": bool(browser and browser.is_connected()),
        "shots": request.app.get("shots", 0),
    })


async def browser_context(app):
    async with async_playwright() as playwright:
        app["playwright"] = playwright
        yield
        browser = app.get("browser")
        if browser and browser.is_connected():
            await browser.close()


def main():
    if not TOKEN:
        raise SystemExit("必须设置 SHOT_TOKEN，否则任何人都能调用本服务")
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    app = web.Application()
    app["lock"] = asyncio.Lock()  # 串行截图，避免并发把内存吃爆
    app.cleanup_ctx.append(browser_context)
    app.add_routes([web.post("/shot", shot), web.get("/health", health)])
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
