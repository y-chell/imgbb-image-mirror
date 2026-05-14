import os
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from .client import ImgbbClient
from .parser import (
    parse_albums_from_page,
    parse_xchina_albums_from_page,
    parse_image_pages_from_album,
    parse_taotu_image_pages,
    parse_xchina_photo_pages,
    parse_original_url,
    parse_xchina_original_url,
    parse_next_page_url,
    parse_album_name,
    parse_taotu_album_name,
    parse_xchina_album_name,
)
from .metadata import (
    load_metadata,
    save_metadata,
    create_metadata,
    add_image_entry,
    mark_downloaded,
    is_downloaded,
)

__all__ = [
    "scrape_album_list", "download_album",
    "collect_image_pages", "resolve_original_urls",
    "is_direct_album_url", "build_album_from_url",
    "get_download_headers",
]

logger = logging.getLogger(__name__)
XCHINA_IMAGE_REFERER = "https://xchina.co/"


def _is_xchina_url(url: str) -> bool:
    return "xchina.co" in urlparse(url).netloc


def _is_taotu_url(url: str) -> bool:
    return "taotu.org" in urlparse(url).netloc


def _is_xchina_album_url(url: str) -> bool:
    return _is_xchina_url(url) and re.search(r"/photo/id-[^.]+\.html", url) is not None


def _is_taotu_album_url(url: str) -> bool:
    return _is_taotu_url(url) and re.search(r"/[^/]+/[^/]+/.+/?$", urlparse(url).path) is not None


def _is_xchina_model_url(url: str) -> bool:
    return _is_xchina_url(url) and re.search(r"/model/id-[^.]+\.html", url) is not None


def _is_xchina_listing_url(url: str) -> bool:
    return _is_xchina_url(url) and re.search(r"/photos/(model-[^/]+|[^/]+)(?:/\d+)?\.html", url) is not None


def _xchina_album_id_from_url(url: str) -> str:
    match = re.search(r"/photo/id-([^.]+)\.html", url)
    return match.group(1) if match else ""


def _xchina_model_listing_url(url: str) -> str:
    match = re.search(r"/model/id-([^.]+)\.html", url)
    if not match:
        return url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/photos/model-{match.group(1)}.html"


def is_direct_album_url(url: str) -> bool:
    return _is_xchina_album_url(url) or _is_taotu_album_url(url) or "/album/" in urlparse(url).path


def build_album_from_url(client: ImgbbClient, url: str, browser=None) -> dict:
    if _is_xchina_album_url(url):
        if browser:
            album, _ = browser.extract_xchina_manifest(url)
            return album
        html = client.fetch(url)
        album_id = _xchina_album_id_from_url(url)
        return {
            "id": album_id,
            "name": parse_xchina_album_name(html) or album_id,
            "url": url,
            "thumb": "",
            "source": "xchina",
        }

    if _is_taotu_album_url(url):
        html = client.fetch(url)
        album_id = url.rstrip("/").split("/")[-1]
        return {
            "id": album_id,
            "name": parse_taotu_album_name(html) or album_id,
            "url": url,
            "thumb": "",
            "source": "taotu",
        }

    album_id = url.rstrip("/").split("/")[-1]
    album = {
        "id": album_id,
        "name": album_id,
        "url": url,
        "thumb": "",
        "source": "imgbb",
    }
    html = client.fetch(url)
    name = parse_album_name(html)
    if name:
        album["name"] = name
    return album


def get_download_headers(url: str) -> dict | None:
    if "img.xchina.io/photos2/" in url:
        return {"Referer": XCHINA_IMAGE_REFERER}
    return None


def scrape_album_list(client: ImgbbClient, start_url: str,
                      max_pages: int = 100, browser=None) -> list[dict]:
    if _is_xchina_url(start_url):
        all_albums = []
        url = _xchina_model_listing_url(start_url) if _is_xchina_model_url(start_url) else start_url
        page = 1

        while url and page <= max_pages:
            logger.info(f"[页 {page}] 获取相册列表: {url}")
            html = browser.fetch_html(url, wait_ms=500) if browser else client.fetch(url)
            albums = parse_xchina_albums_from_page(html, url)
            if not albums:
                break
            all_albums.extend(albums)
            logger.info(f"  找到 {len(albums)} 个相册 (累计 {len(all_albums)})")
            next_url = parse_next_page_url(html, url) if (_is_xchina_listing_url(url) or _is_xchina_model_url(start_url)) else None
            if next_url and next_url != url:
                url = next_url
                page += 1
            else:
                break

        return all_albums

    all_albums = []
    url = start_url
    page = 1

    while url and page <= max_pages:
        logger.info(f"[页 {page}] 获取相册列表: {url}")
        html = client.fetch(url)
        albums = parse_albums_from_page(html)
        if not albums:
            break
        all_albums.extend(albums)
        logger.info(f"  找到 {len(albums)} 个相册 (累计 {len(all_albums)})")
        url = parse_next_page_url(html, start_url)
        page += 1

    return all_albums


def collect_image_pages(client: ImgbbClient, album_url: str, browser=None) -> list[dict]:
    """翻页收集相册内所有图片的单图页面信息"""
    if _is_xchina_album_url(album_url):
        if browser:
            _, photo_pages = browser.extract_xchina_manifest(album_url)
            return photo_pages
        html = client.fetch(album_url)
        return parse_xchina_photo_pages(html, album_url)
    if _is_taotu_album_url(album_url):
        html = client.fetch(album_url)
        return parse_taotu_image_pages(html, album_url)

    all_pages = []
    url = album_url
    page_num = 1

    while url:
        html = client.fetch(url)
        pages = parse_image_pages_from_album(html)
        if not pages:
            break
        all_pages.extend(pages)
        next_url = parse_next_page_url(html, album_url)
        if next_url and next_url != url:
            page_num += 1
            logger.debug(f"  相册第 {page_num} 页...")
            url = next_url
        else:
            break

    return all_pages


def resolve_original_urls(client: ImgbbClient, image_pages: list[dict],
                          workers: int = 4, browser=None) -> list[str]:
    """并发获取每张图片的原图链接，失败时回退到缩略图。"""
    total = len(image_pages)
    original_urls = [None] * total

    if image_pages and image_pages[0].get("source") == "xchina" and browser:
        if image_pages[0].get("direct_url"):
            original_urls = [img["direct_url"] for img in image_pages]
        else:
            original_urls = browser.resolve_xchina_originals(image_pages)
        logger.info(f"  原图链接: {len(original_urls)}/{total} 张")
        return original_urls
    if image_pages and image_pages[0].get("source") == "taotu":
        original_urls = [img.get("direct_url") or img["page_url"] for img in image_pages]
        logger.info(f"  原图链接: {len(original_urls)}/{total} 张")
        return original_urls

    with ThreadPoolExecutor(max_workers=workers) as pool:
        def _get_original(img):
            html = client.fetch(img["page_url"])
            if img.get("source") == "xchina":
                return parse_xchina_original_url(html)
            return parse_original_url(html)

        future_map = {pool.submit(_get_original, img): i
                      for i, img in enumerate(image_pages)}
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                url = future.result()
            except Exception:
                url = None
            original_urls[idx] = url if url else image_pages[idx]["thumb"]

    resolved = sum(1 for i, u in enumerate(original_urls)
                   if u and u != image_pages[i]["thumb"])
    logger.info(f"  原图链接: {resolved}/{total} 张")
    return original_urls


def _resolve_filenames(image_pages: list[dict], original_urls: list[str]) -> list[str]:
    """构建文件名：序号前缀 + 原始文件名"""
    filenames = []
    seen = {}
    for idx, img in enumerate(image_pages):
        raw = img["alt"].strip() if img["alt"].strip() else ""
        if not raw and original_urls[idx]:
            raw = os.path.basename(original_urls[idx].split("?")[0])
        if not raw:
            raw = f"image{idx+1}.jpg"
        base, ext = os.path.splitext(raw)
        if not ext:
            ext = ".jpg"
        candidate = f"{idx+1:03d}_{base}{ext}"
        if candidate in seen:
            seen[candidate] += 1
            candidate = f"{idx+1:03d}_{base}_{seen[candidate]}{ext}"
        else:
            seen[candidate] = 1
        filenames.append(candidate)
    return filenames


def download_album(client: ImgbbClient, album: dict, output_dir: str,
                   source_url: str = "", workers: int = 4,
                   thumb_only: bool = False, progress_cb=None, browser=None) -> dict:
    """下载单个相册的图片。

    thumb_only: True 时只下载缩略图。
    progress_cb: 可选回调 (event, **kwargs)，用于 rich 进度条集成。
    """
    name = re.sub(r'[<>:"/\\|?*]', '_', album["name"]).strip()
    album_dir = os.path.join(output_dir, name)
    os.makedirs(album_dir, exist_ok=True)

    logger.info(f"[相册] {album['name']}")
    logger.info(f"  URL: {album['url']}")

    # 收集所有图片页面（含翻页）
    image_pages = collect_image_pages(client, album["url"], browser=browser)
    total = len(image_pages)
    logger.info(f"  发现 {total} 张图片，正在获取原图链接...")

    if not image_pages:
        return {"name": name, "url": album["url"], "total": 0, "success": 0, "failed": 0}

    # 加载或创建 metadata
    meta = load_metadata(album_dir)
    if meta and len(meta.get("images", [])) == total:
        logger.debug("  使用已有 metadata")
    else:
        meta = create_metadata(album, source_url)

    # 获取下载链接
    if thumb_only:
        original_urls = [img["thumb"] for img in image_pages]
        logger.info(f"  缩略图模式: {total} 张")
    else:
        original_urls = resolve_original_urls(client, image_pages, workers, browser=browser)

    # 构建文件名
    filenames = _resolve_filenames(image_pages, original_urls)

    # 重建 metadata images 列表
    if not meta["images"]:
        for idx, img in enumerate(image_pages):
            add_image_entry(meta, idx, original_urls[idx],
                            img["thumb"], img["page_url"],
                            filenames[idx], img["alt"])
        save_metadata(album_dir, meta)

    # 并发下载
    success = 0
    failed = 0

    if progress_cb:
        progress_cb("album_start", total=total, name=name)

    def _download_one(idx: int) -> bool:
        if is_downloaded(meta, idx, album_dir):
            return True
        url = original_urls[idx]
        dest = os.path.join(album_dir, filenames[idx])
        if image_pages[idx].get("source") == "xchina" and browser:
            try:
                if image_pages[idx].get("direct_url"):
                    browser.download_xchina_direct(
                        image_pages[idx]["direct_url"],
                        image_pages[idx].get("referer_url", album["url"]),
                        dest,
                    )
                else:
                    browser.download_xchina_image(image_pages[idx]["page_url"], dest)
                ok = True
            except Exception as e:
                logger.warning(f"下载失败: {os.path.basename(dest)}: {e}")
                ok = False
        else:
            ok = client.download(url, dest, headers=get_download_headers(url))
        if ok:
            size = os.path.getsize(dest)
            mark_downloaded(meta, idx, size)
        return ok

    if image_pages and image_pages[0].get("source") == "xchina" and browser:
        for i in range(total):
            if _download_one(i):
                success += 1
            else:
                failed += 1
            if progress_cb:
                progress_cb("image_done", success=success, failed=failed)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_download_one, i): i for i in range(total)}
            for future in as_completed(futures):
                if future.result():
                    success += 1
                else:
                    failed += 1
                if progress_cb:
                    progress_cb("image_done", success=success, failed=failed)

    # 保存最终 metadata
    save_metadata(album_dir, meta)

    if progress_cb:
        progress_cb("album_done", success=success, failed=failed)

    logger.info(f"  完成: {success}/{total} 成功, {failed} 失败")
    return {"name": name, "url": album["url"], "total": total, "success": success, "failed": failed}
