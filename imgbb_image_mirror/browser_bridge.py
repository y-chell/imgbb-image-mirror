import os
import re
from playwright.sync_api import sync_playwright

from .parser import parse_xchina_album_name, parse_xchina_photo_pages, parse_xchina_original_url


def _read_devtools_endpoint() -> str:
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data", "DevToolsActivePort"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome Dev", "User Data", "DevToolsActivePort"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome Beta", "User Data", "DevToolsActivePort"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome SxS", "User Data", "DevToolsActivePort"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
        if len(lines) >= 2:
            port = lines[0]
            ws_path = lines[1]
            return f"ws://127.0.0.1:{port}{ws_path}"
    raise RuntimeError("未找到 Chrome DevToolsActivePort，请先开启 Chrome 远程调试")


class BrowserBridge:
    def __init__(self, browser_url: str = ""):
        self.browser_url = browser_url or _read_devtools_endpoint()
        self._playwright = None
        self._browser = None
        self._context = None
        self._asset_page = None

    def __enter__(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(self.browser_url)
        if self._browser.contexts:
            self._context = self._browser.contexts[0]
        else:
            raise RuntimeError("连接到 Chrome 成功，但没有可用 context")
        return self

    def __exit__(self, *args):
        if self._asset_page:
            self._asset_page.close()
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def _new_page(self):
        return self._context.new_page()

    def _get_asset_page(self, referer_url: str):
        if self._asset_page is None or self._asset_page.is_closed():
            self._asset_page = self._new_page()
            self._asset_page.goto(referer_url, wait_until="networkidle", timeout=30000)
            return self._asset_page
        current = self._asset_page.url or ""
        if not current.startswith("https://xchina.co/"):
            self._asset_page.goto(referer_url, wait_until="networkidle", timeout=30000)
        return self._asset_page

    def fetch_html(self, url: str, wait_ms: int = 1000) -> str:
        page = self._new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)
            return page.content()
        finally:
            page.close()

    def extract_xchina_album(self, url: str) -> tuple[dict, list[dict]]:
        html = self.fetch_html(url)
        photo_pages = parse_xchina_photo_pages(html, url)
        album_id_match = re.search(r"/photo/id-([^.]+)\.html", url)
        album_id = album_id_match.group(1) if album_id_match else ""
        album = {
            "id": album_id,
            "name": parse_xchina_album_name(html) or album_id,
            "url": url,
            "thumb": "",
            "count": len(photo_pages),
            "source": "xchina",
        }
        return album, photo_pages

    def extract_xchina_manifest(self, url: str) -> tuple[dict, list[dict]]:
        album, photo_pages = self.extract_xchina_album(url)
        if not photo_pages:
            return album, photo_pages

        page = self._new_page()
        try:
            first_url = photo_pages[0]["page_url"]
            page.goto(first_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(200)
            html = page.content()
            original = parse_xchina_original_url(html)
            if not original:
                raise RuntimeError(f"无法解析首张原图: {first_url}")

            title = page.title()
            total_match = re.search(r"\((\d+)/(\d+)\)", title)
            total = int(total_match.group(2)) if total_match else len(photo_pages)
            album["count"] = total

            direct_match = re.search(r"(https://img\.xchina\.io/photos2/[^/]+/)(\d+)(\.[a-zA-Z0-9]+)$", original)
            if not direct_match:
                return album, photo_pages

            prefix = direct_match.group(1)
            ext = direct_match.group(3)
            image_pages = [{
                "page_url": "",
                "thumb": "",
                "alt": f"{idx:04d}{ext}",
                "source": "xchina",
                "direct_url": f"{prefix}{idx:04d}{ext}",
                "referer_url": url,
            } for idx in range(1, total + 1)]
            return album, image_pages
        finally:
            page.close()

    def resolve_xchina_originals(self, image_pages: list[dict]) -> list[str]:
        page = self._new_page()
        try:
            originals = []
            for image in image_pages:
                page.goto(image["page_url"], wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(300)
                html = page.content()
                original = parse_xchina_original_url(html)
                if not original:
                    original = page.evaluate(
                        """() => {
                            const preload = document.querySelector('link[rel="preload"][as="image"]');
                            if (preload?.href) return preload.href;
                            const img = document.querySelector('img[src*="img.xchina.io/photos2/"]');
                            if (img?.src) return img.src;
                            const html = document.documentElement.outerHTML;
                            const match = html.match(/"contentUrl"\\s*:\\s*"(https:\\\\/\\\\/img\\.xchina\\.io\\\\/photos2\\\\/[^"]+)"/);
                            if (match) return JSON.parse('"' + match[1] + '"');
                            return null;
                        }"""
                    )
                if not original:
                    raise RuntimeError(f"无法解析原图: {image['page_url']}")
                originals.append(original)
            return originals
        finally:
            page.close()

    def download_xchina_image(self, photo_page_url: str, dest: str) -> str:
        page = self._new_page()
        try:
            with page.expect_response(
                lambda resp: "img.xchina.io/photos2/" in resp.url and resp.request.resource_type == "image",
                timeout=30000,
            ) as image_info:
                page.goto(photo_page_url, wait_until="networkidle", timeout=30000)
            image_response = image_info.value
            content = image_response.body()
            if not content:
                raise RuntimeError(f"图片响应为空: {photo_page_url}")
            with open(dest, "wb") as f:
                f.write(content)
            return image_response.url
        finally:
            page.close()

    def download_xchina_direct(self, image_url: str, referer_url: str, dest: str) -> str:
        page = self._get_asset_page(referer_url)
        with page.expect_response(
            lambda resp: resp.url == image_url and resp.request.resource_type == "image",
            timeout=30000,
        ) as image_info:
            page.evaluate(
                """(url) => {
                    let img = document.getElementById('__codex_xchina_img');
                    if (!img) {
                        img = document.createElement('img');
                        img.id = '__codex_xchina_img';
                        document.body.innerHTML = '';
                        document.body.appendChild(img);
                    }
                    img.src = url;
                }""",
                image_url,
            )
        image_response = image_info.value
        content = image_response.body()
        if not content:
            raise RuntimeError(f"图片响应为空: {image_url}")
        with open(dest, "wb") as f:
            f.write(content)
        return image_response.url

    def get_imgbb_session(self) -> tuple[str, str]:
        page = self._new_page()
        try:
            page.goto("https://imgbb.com/", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(500)
            cookies = self._context.cookies(["https://imgbb.com/"])
            cookie_map = {item["name"]: item["value"] for item in cookies}
            cookie = "; ".join(
                f"{name}={cookie_map[name]}"
                for name in ("LID", "PHPSESSID")
                if name in cookie_map
            )
            auth_token = page.evaluate(
                """() => {
                    const html = document.documentElement.outerHTML;
                    const match = html.match(/auth_token\\s*=\\s*\\"([a-f0-9]+)\\"/) ||
                                  html.match(/name=\\"auth_token\\"[^>]*value=\\"([a-f0-9]+)\\"/);
                    return match ? match[1] : null;
                }"""
            )
            if not cookie or not auth_token:
                raise RuntimeError("无法从当前 Chrome 会话读取 imgbb 登录态")
            return cookie, auth_token
        finally:
            page.close()
