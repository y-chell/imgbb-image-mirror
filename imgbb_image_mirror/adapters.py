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


def _host_is(url: str, host: str) -> bool:
    return host in urlparse(url).netloc


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

    def download_one(
        self, client: ImgbbClient, image_page: dict, dest: str, album: dict, browser=None
    ) -> bool:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# xchina
# ---------------------------------------------------------------------------


def _xchina_album_id(url: str) -> str:
    m = re.search(r"/photo/id-([^.]+)\.html", url)
    return m.group(1) if m else ""


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
        return _host_is(url, "xchina.co") and re.search(r"/photo/id-[^.]+\.html", url) is not None

    def build_album(self, client: ImgbbClient, url: str, browser=None) -> dict:
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
        if browser:
            _, photo_pages = browser.extract_xchina_manifest(album_url)
            return photo_pages
        html = client.fetch(album_url)
        return parse_xchina_photo_pages(html, album_url)

    def resolve_originals(
        self, client: ImgbbClient, image_pages: list[dict], workers: int = 4, browser=None
    ) -> list[str]:
        total = len(image_pages)
        urls: list[str | None] = [None] * total
        if image_pages and image_pages[0].get("direct_url"):
            return [str(img["direct_url"]) for img in image_pages]
        if browser:
            resolved = browser.resolve_xchina_originals(image_pages)
            return [u or image_pages[i].get("thumb", "") for i, u in enumerate(resolved)]
        # 无 browser 时退回 client 逐页解析
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
        html = client.fetch(img["page_url"])
        return parse_xchina_original_url(html)

    def download_one(
        self, client: ImgbbClient, image_page: dict, dest: str, album: dict, browser=None
    ) -> bool:
        if not browser:
            # xchina 需要 referer，无 browser 时尝试直连
            url = image_page.get("direct_url") or image_page.get("page_url", "")
            return client.download(
                url,
                dest,
                headers={"Referer": XCHINA_IMAGE_REFERER}
                if "img.xchina.io/photos" in url
                else None,
            )
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
# imgbb
# ---------------------------------------------------------------------------


class ImgbbAdapter(SiteAdapter):
    source = "imgbb"

    def matches(self, url: str) -> bool:
        p = urlparse(url)
        return "ibb.co" in p.netloc or "imgbb.com" in p.netloc

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
        html = client.fetch(img["page_url"])
        return parse_original_url(html)

    def download_one(
        self, client: ImgbbClient, image_page: dict, dest: str, album: dict, browser=None
    ) -> bool:
        return client.download(image_page.get("thumb", ""), dest)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

# 顺序敏感：xchina / taotu 先于 imgbb（imgbb 的 matches 较宽）
_ADAPTERS: list[SiteAdapter] = [
    XchinaAdapter(),
    TaotuAdapter(),
    ImgbbAdapter(),
]


def select_adapter(url: str) -> SiteAdapter | None:
    for adapter in _ADAPTERS:
        if adapter.matches(url):
            return adapter
    return None
