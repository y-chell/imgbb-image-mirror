"""站点适配器（SiteAdapter）。

历史上 xchina / taotu / imgbb 的分支判断散落在 downloader.py 的各处
``_is_xchina_url`` / ``_is_taotu_album_url`` 等 helper 里，每加一个站点
都要在主流程插 if-else。这里把它们收敛成统一接口，主流程只负责：

1. ``select_adapter(url)`` 找到匹配的 adapter
2. 调 adapter 的方法做抓取/下载

downloader.py 仍然保留 ``scrape_album_list`` / ``download_album`` 等门面
函数（内部委托给 adapter），对外签名不变，__main__.py 和测试无需改动。
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from .client import ImgbbClient
from .parser import (
    parse_album_name,
    parse_albums_from_page,
    parse_image_pages_from_album,
    parse_next_page_url,
    parse_original_url,
    parse_taotu_album_name,
    parse_taotu_image_pages,
    parse_xchina_album_name,
    parse_xchina_albums_from_page,
    parse_xchina_original_url,
    parse_xchina_photo_pages,
)

XCHINA_IMAGE_REFERER = "https://xchina.co/"


def xchina_download_headers(url: str) -> dict | None:
    """img.xchina.io 的图（含 photos2 缩略图 CDN）必须带站内 Referer，否则 403。"""
    if "img.xchina.io/photos" in url:
        return {"Referer": XCHINA_IMAGE_REFERER}
    return None


def _host_is(url: str, host: str) -> bool:
    """精确匹配 host 本身或其子域，不做子串匹配（避免 evil-xchina.com 误路由）。"""
    hostname = urlparse(url).hostname or ""
    return hostname == host or hostname.endswith("." + host)


class SiteAdapter:
    """站点适配器接口。子类实现各站点的抓取/下载策略。

    所有方法都不持有状态，可安全单例复用。browser 参数可空：需要浏览器
    上下文的站点在 browser 为 None 时应回退到 client 直连路径。
    """

    source: str = "site"

    def matches(self, url: str) -> bool:
        raise NotImplementedError

    def is_album_url(self, url: str) -> bool:
        """是否单个相册 URL（而非列表页）。"""
        return False

    def build_album(self, client: ImgbbClient, url: str, browser=None) -> dict:
        raise NotImplementedError

    def scrape_albums(
        self, client: ImgbbClient, url: str, max_pages: int = 100, browser=None
    ) -> list[dict]:
        raise NotImplementedError

    def collect_image_pages(self, client: ImgbbClient, album_url: str, browser=None) -> list[dict]:
        raise NotImplementedError

    def resolve_originals(
        self, client: ImgbbClient, image_pages: list[dict], workers: int = 4, browser=None
    ) -> list[str]:
        raise NotImplementedError

    def _resolve_with_pool(
        self, client: ImgbbClient, image_pages: list[dict], workers: int
    ) -> list[str]:
        """并发逐页解析原图（子类实现 _resolve_one），失败条目回退缩略图。"""
        total = len(image_pages)
        urls: list[str | None] = [None] * total
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(self._resolve_one, client, img): i for i, img in enumerate(image_pages)
            }
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    urls[idx] = future.result()
                except Exception:
                    urls[idx] = None
        return [u or image_pages[i].get("thumb", "") for i, u in enumerate(urls)]

    def _resolve_one(self, client: ImgbbClient, img: dict) -> str | None:
        raise NotImplementedError

    def download_one(
        self, client: ImgbbClient, image_page: dict, dest: str, album: dict, browser=None
    ) -> bool:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# xchina
# ---------------------------------------------------------------------------


def _xchina_canonical_album_url(url: str) -> str:
    """去掉相册/series 的分页后缀：/photo/id-x/7.html -> /photo/id-x.html。

    相册 ID、state 文件名都以首页 URL 为准，否则带页码的 ID 会把
    state 文件写进嵌套目录，且下次用干净 URL 会重复上传整套。
    """
    return re.sub(r"(/(?:photo/id|photos/series)-[^./]+)/\d+\.html$", r"\1.html", url)


def _xchina_album_id(url: str) -> str:
    m = re.search(r"/photo/id-([^.]+)\.html", url)
    if not m:
        m = re.search(r"/photos/series-([^.]+)\.html", url)
    return m.group(1) if m else ""


def _xchina_series_url_id(url: str) -> str | None:
    """series 相册（2026 改版新模板）返回其 id，其余 URL 返回 None。"""
    m = re.search(r"/photos/series-([^.]+)\.html", url)
    return m.group(1) if m else None


def _xchina_model_listing_url(url: str) -> str:
    m = re.search(r"/model/id-([^.]+)\.html", url)
    if not m:
        return url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/photos/model-{m.group(1)}.html"


def _is_xchina_model_url(url: str) -> bool:
    return _host_is(url, "xchina.co") and re.search(r"/model/id-[^.]+\.html", url) is not None


def _is_xchina_listing_url(url: str) -> bool:
    return (
        _host_is(url, "xchina.co")
        and re.search(r"/photos/(model-[^/]+|[^/]+)(?:/\d+)?\.html", url) is not None
    )


class XchinaAdapter(SiteAdapter):
    source = "xchina"

    def matches(self, url: str) -> bool:
        return _host_is(url, "xchina.co")

    def is_album_url(self, url: str) -> bool:
        return _host_is(url, "xchina.co") and (
            re.search(r"/photo/id-[^.]+\.html", url) is not None
            or _xchina_series_url_id(url) is not None
        )

    def build_album(self, client: ImgbbClient, url: str, browser=None) -> dict:
        url = _xchina_canonical_album_url(url)
        if self.is_album_url(url) and browser:
            album, _ = browser.extract_xchina_manifest(url)
            return album
        html = client.fetch(url)
        album_id = _xchina_album_id(url)
        return {
            "id": album_id,
            "name": parse_xchina_album_name(html) or album_id,
            "url": url,
            "thumb": "",
            "source": self.source,
        }

    def scrape_albums(
        self, client: ImgbbClient, url: str, max_pages: int = 100, browser=None
    ) -> list[dict]:
        all_albums: list[dict] = []
        current = _xchina_model_listing_url(url) if _is_xchina_model_url(url) else url
        page = 1
        while current and page <= max_pages:
            html = browser.fetch_html(current, wait_ms=500) if browser else client.fetch(current)
            albums = parse_xchina_albums_from_page(html, current)
            if not albums:
                break
            all_albums.extend(albums)
            next_url = (
                parse_next_page_url(html, current)
                if (_is_xchina_listing_url(current) or _is_xchina_model_url(url))
                else None
            )
            if next_url and next_url != current:
                current = next_url
                page += 1
            else:
                break
        return all_albums

    def collect_image_pages(self, client: ImgbbClient, album_url: str, browser=None) -> list[dict]:
        album_url = _xchina_canonical_album_url(album_url)
        if browser and not _xchina_series_url_id(album_url):
            _, photo_pages = browser.extract_xchina_manifest(album_url)
            return photo_pages
        fetch = browser.fetch_html if browser else client.fetch
        html = fetch(album_url)
        entries = parse_xchina_photo_pages(html, album_url)
        if entries and not entries[0].get("direct_url"):
            return entries  # 旧模板：photoShow 链接一次拿全
        return self._collect_series_subpages(fetch, html, album_url, entries)

    def _collect_series_subpages(
        self, fetch, first_html: str, album_url: str, entries: list[dict]
    ) -> list[dict]:
        """series 相册内容分页（/photos/series-{id}/{n}.html），逐页收集去重。

        每页只内嵌十几张预览图，原图不在任何页面 HTML 里，只能逐条从预览
        URL 推导；分页总数取自页面上出现的最大页码。
        """
        sid = _xchina_series_url_id(album_url)
        if not sid:
            return entries
        collected = list(entries)
        seen = {e["direct_url"] for e in collected if e.get("direct_url")}
        parsed = urlparse(album_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        page_nums = {
            int(n) for n in re.findall(rf"/photos/series-{sid}/(\d+)\.html", first_html)
        }
        for n in sorted(p for p in page_nums if p > 1):
            html = fetch(f"{base}/photos/series-{sid}/{n}.html")
            for entry in parse_xchina_photo_pages(html, album_url):
                if entry["direct_url"] not in seen:
                    seen.add(entry["direct_url"])
                    collected.append(entry)
        return collected

    def resolve_originals(
        self, client: ImgbbClient, image_pages: list[dict], workers: int = 4, browser=None
    ) -> list[str]:
        if image_pages and image_pages[0].get("direct_url"):
            return [str(img["direct_url"]) for img in image_pages]
        if browser:
            resolved = browser.resolve_xchina_originals(image_pages)
            return [u or image_pages[i].get("thumb", "") for i, u in enumerate(resolved)]
        # 无 browser 时退回 client 逐页解析
        return self._resolve_with_pool(client, image_pages, workers)

    def _resolve_one(self, client: ImgbbClient, img: dict) -> str | None:
        html = client.fetch(img["page_url"])
        return parse_xchina_original_url(html)

    def download_one(
        self, client: ImgbbClient, image_page: dict, dest: str, album: dict, browser=None
    ) -> bool:
        if not browser:
            # xchina 需要 referer，无 browser 时尝试直连
            url = image_page.get("direct_url") or image_page.get("page_url", "")
            return client.download(url, dest, headers=xchina_download_headers(url))
        try:
            if image_page.get("direct_url"):
                browser.download_xchina_direct(
                    image_page["direct_url"],
                    image_page.get("referer_url", album["url"]),
                    dest,
                )
            else:
                browser.download_xchina_image(image_page["page_url"], dest)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# taotu
# ---------------------------------------------------------------------------


def _is_taotu_album_url(url: str) -> bool:
    return (
        _host_is(url, "taotu.org")
        and re.search(r"/[^/]+/[^/]+/.+/?$", urlparse(url).path) is not None
    )


class TaotuAdapter(SiteAdapter):
    source = "taotu"

    def matches(self, url: str) -> bool:
        return _host_is(url, "taotu.org")

    def is_album_url(self, url: str) -> bool:
        return _is_taotu_album_url(url)

    def build_album(self, client: ImgbbClient, url: str, browser=None) -> dict:
        html = client.fetch(url)
        album_id = url.rstrip("/").split("/")[-1]
        return {
            "id": album_id,
            "name": parse_taotu_album_name(html) or album_id,
            "url": url,
            "thumb": "",
            "source": self.source,
        }

    def scrape_albums(
        self, client: ImgbbClient, url: str, max_pages: int = 100, browser=None
    ) -> list[dict]:
        # taotu 目前只支持直接相册 URL，无列表翻页
        return []

    def collect_image_pages(self, client: ImgbbClient, album_url: str, browser=None) -> list[dict]:
        html = client.fetch(album_url)
        return parse_taotu_image_pages(html, album_url)

    def resolve_originals(
        self, client: ImgbbClient, image_pages: list[dict], workers: int = 4, browser=None
    ) -> list[str]:
        return [img.get("direct_url") or img.get("page_url", "") for img in image_pages]

    def download_one(
        self, client: ImgbbClient, image_page: dict, dest: str, album: dict, browser=None
    ) -> bool:
        url = image_page.get("direct_url") or image_page.get("page_url", "")
        return client.download(url, dest)


# ---------------------------------------------------------------------------
# fisharchive (fisharchive.pages.dev: DeepSeek 同人表情收藏)
# ---------------------------------------------------------------------------


class FishArchiveAdapter(SiteAdapter):
    """fisharchive.pages.dev 整站是一个相册，图片清单在 stickers/manifest.json。

    manifest 里的 original/preview/large 是相对站点根目录的路径（不是相对
    manifest 所在的 stickers/ 目录），拼 URL 时必须以 scheme://netloc/ 为基准。
    相册名固定为 deepseek（该站内容即 DeepSeek 鲸鱼娘同人表情）。
    """

    source = "fisharchive"

    def matches(self, url: str) -> bool:
        return _host_is(url, "fisharchive.pages.dev")

    def is_album_url(self, url: str) -> bool:
        return self.matches(url)

    @staticmethod
    def _site_base(url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}/"

    def _fetch_manifest(self, client: ImgbbClient, url: str) -> list[dict]:
        base = self._site_base(url)
        return json.loads(client.fetch(base + "stickers/manifest.json"))

    def build_album(self, client: ImgbbClient, url: str, browser=None) -> dict:
        stickers = self._fetch_manifest(client, url)
        return {
            "id": "fisharchive",
            "name": "deepseek",
            "url": self._site_base(url),
            "thumb": "",
            "count": len(stickers),
            "source": self.source,
        }

    def scrape_albums(
        self, client: ImgbbClient, url: str, max_pages: int = 100, browser=None
    ) -> list[dict]:
        # 整站只有一个相册，列表翻页语义不适用
        return []

    def collect_image_pages(self, client: ImgbbClient, album_url: str, browser=None) -> list[dict]:
        base = self._site_base(album_url)
        stickers = self._fetch_manifest(client, album_url)
        return [
            {
                "page_url": "",
                "thumb": base + s["preview"] if s.get("preview") else "",
                "alt": s.get("filename", ""),
                "source": self.source,
                "direct_url": base + s["original"],
            }
            for s in stickers
            if s.get("original")
        ]

    def resolve_originals(
        self, client: ImgbbClient, image_pages: list[dict], workers: int = 4, browser=None
    ) -> list[str]:
        return [img.get("direct_url", "") for img in image_pages]

    def download_one(
        self, client: ImgbbClient, image_page: dict, dest: str, album: dict, browser=None
    ) -> bool:
        return client.download(image_page.get("direct_url", ""), dest)


# ---------------------------------------------------------------------------
# imgbb
# ---------------------------------------------------------------------------


class ImgbbAdapter(SiteAdapter):
    source = "imgbb"

    def matches(self, url: str) -> bool:
        return _host_is(url, "ibb.co") or _host_is(url, "imgbb.com")

    def is_album_url(self, url: str) -> bool:
        return "/album/" in urlparse(url).path

    def build_album(self, client: ImgbbClient, url: str, browser=None) -> dict:
        album_id = url.rstrip("/").split("/")[-1]
        album = {
            "id": album_id,
            "name": album_id,
            "url": url,
            "thumb": "",
            "source": self.source,
        }
        html = client.fetch(url)
        name = parse_album_name(html)
        if name:
            album["name"] = name
        return album

    def scrape_albums(
        self, client: ImgbbClient, url: str, max_pages: int = 100, browser=None
    ) -> list[dict]:
        all_albums: list[dict] = []
        current: str | None = url
        page = 1
        while current and page <= max_pages:
            html = client.fetch(current)
            albums = parse_albums_from_page(html)
            if not albums:
                break
            all_albums.extend(albums)
            current = parse_next_page_url(html, url)
            page += 1
        return all_albums

    def collect_image_pages(self, client: ImgbbClient, album_url: str, browser=None) -> list[dict]:
        all_pages: list[dict] = []
        current = album_url
        while current:
            html = client.fetch(current)
            pages = parse_image_pages_from_album(html)
            if not pages:
                break
            all_pages.extend(pages)
            next_url = parse_next_page_url(html, album_url)
            if next_url and next_url != current:
                current = next_url
            else:
                break
        return all_pages

    def resolve_originals(
        self, client: ImgbbClient, image_pages: list[dict], workers: int = 4, browser=None
    ) -> list[str]:
        return self._resolve_with_pool(client, image_pages, workers)

    def _resolve_one(self, client: ImgbbClient, img: dict) -> str | None:
        html = client.fetch(img["page_url"])
        return parse_original_url(html)

    def download_one(
        self, client: ImgbbClient, image_page: dict, dest: str, album: dict, browser=None
    ) -> bool:
        return client.download(image_page.get("thumb", ""), dest)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

# 顺序敏感：xchina / taotu / fisharchive 先于 imgbb（imgbb 的 matches 较宽）
_ADAPTERS: list[SiteAdapter] = [
    XchinaAdapter(),
    TaotuAdapter(),
    FishArchiveAdapter(),
    ImgbbAdapter(),
]


def select_adapter(url: str) -> SiteAdapter | None:
    for adapter in _ADAPTERS:
        if adapter.matches(url):
            return adapter
    return None
