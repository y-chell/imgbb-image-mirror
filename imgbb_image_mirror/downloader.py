"""相册下载门面。

历史实现把站点分支判断和下载逻辑揉在一起。现在站点相关的具体策略
收敛到 ``adapters.py`` 的 ``SiteAdapter``，本模块只保留：

- 站点无关的下载编排（``download_album``）
- 对外门面函数（保持原有签名，``__main__.py`` 和测试无需改动）

门面函数内部按 URL 选择 adapter，委托执行。
"""

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .adapters import XCHINA_IMAGE_REFERER, SiteAdapter, XchinaAdapter, select_adapter
from .client import ImgbbClient
from .metadata import (
    add_image_entry,
    create_metadata,
    is_downloaded,
    load_metadata,
    mark_downloaded,
    save_metadata,
)

__all__ = [
    "scrape_album_list",
    "download_album",
    "collect_image_pages",
    "resolve_original_urls",
    "is_direct_album_url",
    "build_album_from_url",
    "get_download_headers",
]

logger = logging.getLogger(__name__)


def is_direct_album_url(url: str) -> bool:
    adapter = select_adapter(url)
    return bool(adapter and adapter.is_album_url(url))


def build_album_from_url(client: ImgbbClient, url: str, browser=None) -> dict:
    adapter = select_adapter(url)
    if adapter is None:
        raise ValueError(f"不支持的站点: {url}")
    return adapter.build_album(client, url, browser=browser)


def get_download_headers(url: str) -> dict | None:
    if "img.xchina.io/photos" in url:
        return {"Referer": XCHINA_IMAGE_REFERER}
    return None


def scrape_album_list(
    client: ImgbbClient, start_url: str, max_pages: int = 100, browser=None
) -> list[dict]:
    adapter = select_adapter(start_url)
    if adapter is None:
        raise ValueError(f"不支持的站点: {start_url}")
    return adapter.scrape_albums(client, start_url, max_pages, browser=browser)


def collect_image_pages(client: ImgbbClient, album_url: str, browser=None) -> list[dict]:
    adapter = select_adapter(album_url)
    if adapter is None:
        raise ValueError(f"不支持的站点: {album_url}")
    return adapter.collect_image_pages(client, album_url, browser=browser)


def resolve_original_urls(
    client: ImgbbClient, image_pages: list[dict], workers: int = 4, browser=None
) -> list[str]:
    """并发获取每张图片的原图链接，失败时回退到缩略图。"""
    if not image_pages:
        return []
    # 用首张图的 source 字段定位 adapter；兼容无 source 的 imgbb 场景
    source = image_pages[0].get("source", "")
    adapter: SiteAdapter | None = None
    if source == "xchina":
        adapter = XchinaAdapter()
    else:
        # 找出 image_pages 里有 page_url 的，按 URL 路由
        for img in image_pages:
            url = img.get("page_url") or img.get("direct_url") or ""
            if url:
                adapter = select_adapter(url)
                if adapter:
                    break
    if adapter is None:
        # 兜底：无法判定站点，原样回退 thumb
        return [img.get("thumb", "") for img in image_pages]

    original_urls = adapter.resolve_originals(client, image_pages, workers, browser=browser)
    total = len(image_pages)
    resolved = sum(
        1 for i, u in enumerate(original_urls) if u and u != image_pages[i].get("thumb", "")
    )
    logger.info(f"  原图链接: {resolved}/{total} 张")
    return original_urls


def _resolve_filenames(image_pages: list[dict], original_urls: list[str]) -> list[str]:
    """构建文件名：序号前缀 + 原始文件名"""
    filenames = []
    seen: dict[str, int] = {}
    for idx, img in enumerate(image_pages):
        raw = img["alt"].strip() if img["alt"].strip() else ""
        if not raw and original_urls[idx]:
            raw = os.path.basename(original_urls[idx].split("?")[0])
        if not raw:
            raw = f"image{idx + 1}.jpg"
        base, ext = os.path.splitext(raw)
        if not ext:
            ext = ".jpg"
        candidate = f"{idx + 1:03d}_{base}{ext}"
        if candidate in seen:
            seen[candidate] += 1
            candidate = f"{idx + 1:03d}_{base}_{seen[candidate]}{ext}"
        else:
            seen[candidate] = 1
        filenames.append(candidate)
    return filenames


def download_album(
    client: ImgbbClient,
    album: dict,
    output_dir: str,
    source_url: str = "",
    workers: int = 4,
    thumb_only: bool = False,
    progress_cb=None,
    browser=None,
) -> dict:
    """下载单个相册的图片。

    thumb_only: True 时只下载缩略图。
    progress_cb: 可选回调 (event, **kwargs)，用于 rich 进度条集成。
    """
    name = re.sub(r'[<>:"/\\|?*]', "_", album["name"]).strip()
    album_dir = os.path.join(output_dir, name)
    os.makedirs(album_dir, exist_ok=True)

    logger.info(f"[相册] {album['name']}")
    logger.info(f"  URL: {album['url']}")

    adapter = select_adapter(album["url"])
    if adapter is None:
        raise ValueError(f"不支持的站点: {album['url']}")

    # 收集所有图片页面（含翻页）
    image_pages = adapter.collect_image_pages(client, album["url"], browser=browser)
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
        original_urls = [img.get("thumb", "") for img in image_pages]
        logger.info(f"  缩略图模式: {total} 张")
    else:
        original_urls = resolve_original_urls(client, image_pages, workers, browser=browser)

    # 构建文件名
    filenames = _resolve_filenames(image_pages, original_urls)

    # 重建 metadata images 列表
    if not meta["images"]:
        for idx, img in enumerate(image_pages):
            add_image_entry(
                meta,
                idx,
                original_urls[idx],
                img.get("thumb", ""),
                img.get("page_url", ""),
                filenames[idx],
                img.get("alt", ""),
            )
        save_metadata(album_dir, meta)

    # 并发下载
    success = 0
    failed = 0

    if progress_cb:
        progress_cb("album_start", total=total, name=name)

    def _download_one(idx: int) -> bool:
        if is_downloaded(meta, idx, album_dir):
            return True
        dest = os.path.join(album_dir, filenames[idx])
        ok = adapter.download_one(client, image_pages[idx], dest, album, browser=browser)
        if ok:
            size = os.path.getsize(dest)
            mark_downloaded(meta, idx, size)
        return ok

    # xchina 有 browser 时走串行（复用单页避免并发争用浏览器会话）
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
