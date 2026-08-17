"""镜像编排：把"列表 -> 原图 -> 上传/下载 -> 状态落盘"的流程从 CLI 入口剥离。

历史上 ``__main__.py`` 把 CLI 解析、配置加载、上传/下载编排、队列 worker、
续传状态全部揉在一起，单文件 500+ 行。这里抽出两个入口：

- ``download_albums``: 只下载图片到本地（原 ``_download_with_progress``）
- ``mirror_albums``: 镜像上传到 imgbb（原 ``_mirror_with_progress``）

以及它们依赖的纯逻辑 helper：

- ``build_upload_name`` / ``mirror_state_path``
- ``prepare_mirror_metadata`` / ``get_cached_original_urls``
- ``count_uploaded`` / ``save_mirror_album``
- ``download_to_temp`` / ``upload_one`` / ``upload_worker``

对外行为（CLI、配置、进度条、输出格式、state 文件格式）与重构前完全一致，
仅做了文件拆分 + 去掉前缀下划线变成公开 API。测试可直接 import 这些函数。
"""

import logging
import os
import queue
import random
import tempfile
import threading
import time
from contextlib import nullcontext
from urllib.parse import urlparse

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from .client import ImgbbClient
from .downloader import (
    collect_image_pages,
    download_album,
    resolve_original_urls,
)
from .metadata import create_metadata, load_metadata_file, mark_uploaded, save_metadata_file

logger = logging.getLogger(__name__)

console = Console()


def nullcontext_bridge():
    """BrowserBridge 不需要时占位用的空 context。"""
    return nullcontext(None)


def build_upload_name(image_info: dict, resolved_url: str, index: int) -> str:
    """构建上传到 imgbb 的文件名：优先用原图直链的文件名，否则序号兜底。"""
    if image_info.get("direct_url"):
        parsed = os.path.basename(urlparse(image_info["direct_url"]).path)
        if parsed:
            return parsed
    if resolved_url:
        parsed = os.path.basename(urlparse(resolved_url).path)
        if parsed:
            return parsed
    return f"{index + 1:04d}.jpg"


def mirror_state_path(output_dir: str, album: dict) -> str:
    """镜像 state 文件路径：{output}/_mirror_state/{source}_{album_id}.json"""
    state_dir = os.path.join(output_dir, "_mirror_state")
    source = album.get("source", "site")
    return os.path.join(state_dir, f"{source}_{album['id']}.json")


def prepare_mirror_metadata(
    state_path: str, album: dict, source_url: str, image_pages: list[dict], original_urls: list[str]
) -> tuple[dict, list[str]]:
    """构建镜像 metadata，保留上次已 uploaded 的条目（断点续传）。

    每次运行都重建 images 列表（因为源站图片顺序/数量可能变），但 status==uploaded
    的旧条目按 index 复用到新列表，避免重复上传。
    """
    current_names = [
        build_upload_name(image_pages[idx], original_urls[idx], idx)
        for idx in range(len(image_pages))
    ]
    previous = load_metadata_file(state_path) or {}
    previous_images = {
        img.get("index"): img for img in previous.get("images", []) if isinstance(img, dict)
    }

    metadata = create_metadata(album, source_url)
    metadata["state_version"] = 2
    metadata["storage"] = "mirror_resume"
    metadata["mirror_album_id"] = previous.get("mirror_album_id", "")
    metadata["mirror_album_url"] = previous.get("mirror_album_url", "")
    metadata["mirror_status"] = previous.get("mirror_status", "pending")
    metadata["images"] = []

    for idx, image in enumerate(image_pages):
        old = previous_images.get(idx, {})
        entry = {
            "index": idx,
            "original_url": original_urls[idx],
            "thumb_url": image.get("thumb", ""),
            "page_url": image.get("page_url", ""),
            "filename": current_names[idx],
            "alt": image.get("alt", ""),
            "size": old.get("size", 0),
            "status": "pending",
            "uploaded_url": "",
            "viewer_url": "",
            "uploaded_thumb": "",
        }
        if old.get("status") == "uploaded":
            entry["status"] = "uploaded"
            entry["uploaded_url"] = old.get("uploaded_url", "")
            entry["viewer_url"] = old.get("viewer_url", "")
            entry["uploaded_thumb"] = old.get("uploaded_thumb", "")
        metadata["images"].append(entry)

    save_metadata_file(state_path, metadata)
    return metadata, current_names


def get_cached_original_urls(state_path: str, total: int) -> list[str] | None:
    """若上次已抓到全部原图链接且数量一致，按 index 顺序还原，避免重复抓取。"""
    previous = load_metadata_file(state_path) or {}
    if len(previous.get("images", [])) != total:
        return None
    ordered = sorted(
        (img for img in previous["images"] if isinstance(img, dict) and "index" in img),
        key=lambda img: img["index"],
    )
    if len(ordered) != total:
        return None
    original_urls = [img.get("original_url", "") for img in ordered]
    if any(not url for url in original_urls):
        return None
    return original_urls


def count_uploaded(metadata: dict) -> int:
    return sum(1 for image in metadata.get("images", []) if image.get("status") == "uploaded")


def save_mirror_album(metadata: dict, state_path: str, album_info: dict, completed: bool = False):
    """更新镜像相册 ID/URL/状态并落盘。"""
    metadata["mirror_album_id"] = album_info.get("id", metadata.get("mirror_album_id", ""))
    metadata["mirror_album_url"] = album_info.get("url", metadata.get("mirror_album_url", ""))
    metadata["mirror_status"] = "completed" if completed else "uploading"
    save_metadata_file(state_path, metadata)


def download_to_temp(browser, album: dict, image_info: dict, upload_name: str) -> str:
    """通过浏览器会话下载单张图片到临时文件，返回临时路径。调用方负责删除。"""
    suffix = os.path.splitext(upload_name)[1] or ".jpg"
    tmp_path = ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
    try:
        if image_info.get("direct_url"):
            browser.download_xchina_direct(
                image_info["direct_url"],
                image_info.get("referer_url", album["url"]),
                tmp_path,
            )
        else:
            browser.download_xchina_image(image_info["page_url"], tmp_path)
        return tmp_path
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def upload_one(uploader, task: dict, album_id: str) -> tuple[dict | None, str]:
    """执行单次上传，返回 (result, error)。task 由主循环构建。"""
    try:
        if task["type"] == "file":
            return uploader.upload_file(
                task["file_path"],
                album_id,
                name=task["title"],
                upload_filename=task["upload_name"],
            ), ""
        result = uploader.upload_image(task["url"], album_id, name=task["title"])
        if not result:
            return None, "upload returned empty result"
        return result, ""
    except Exception as exc:
        return None, str(exc)


def upload_worker(uploader, album_id: str, upload_queue, result_queue, max_retries: int = 2):
    """上传 worker，单次运行内对失败图片做带 jitter 的有限次重试。

    max_retries 是"额外重试次数"，即总尝试次数 = 1 + max_retries。
    """
    while True:
        task = upload_queue.get()
        try:
            if task is None:
                return

            result = None
            error = ""
            attempts = 1 + max(0, max_retries)
            for attempt in range(attempts):
                result, error = upload_one(uploader, task, album_id)
                if result:
                    break
                if attempt < attempts - 1:
                    # 指数退避 + jitter，避免与源站限速周期锁相
                    backoff = (2**attempt) + random.uniform(0, 1.0)
                    time.sleep(backoff)
            if task.get("file_path") and os.path.exists(task["file_path"]):
                os.unlink(task["file_path"])

            result_queue.put(
                {
                    "index": task["index"],
                    "result": result,
                    "error": error,
                }
            )
        finally:
            upload_queue.task_done()


def download_albums(client: ImgbbClient, albums: list[dict], cfg, args, browser=None) -> list[dict]:
    """下载模式：把相册图片下载到本地目录，带进度条。返回每个相册的统计。"""
    results = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        disable=args.quiet,
    ) as progress:
        album_task = progress.add_task("相册总进度", total=len(albums))
        img_task = progress.add_task("当前相册", total=0, visible=False)

        for i, album in enumerate(albums):
            progress.update(album_task, description=f"[{i + 1}/{len(albums)}] {album['name'][:40]}")
            progress.update(img_task, completed=0, total=0, visible=False)

            def on_progress(event, **kwargs):
                if event == "album_start":
                    progress.update(
                        img_task,
                        total=kwargs["total"],
                        completed=0,
                        visible=True,
                        description=f"下载: {kwargs['name'][:30]}",
                    )
                elif event == "image_done":
                    progress.update(img_task, completed=kwargs["success"] + kwargs["failed"])
                elif event == "album_done":
                    progress.update(img_task, visible=False)

            result = download_album(
                client,
                album,
                cfg.output,
                source_url=args.url,
                workers=cfg.workers,
                progress_cb=on_progress,
                browser=browser,
            )
            results.append(result)
            progress.advance(album_task)

    total_imgs = sum(r["total"] for r in results)
    total_ok = sum(r["success"] for r in results)
    total_fail = sum(r["failed"] for r in results)
    console.print(
        f"\n[bold green]完成[/bold green] 相册: {len(results)}, "
        f"图片: {total_imgs}, 成功: {total_ok}, 失败: {total_fail}"
    )
    return results


def mirror_albums(client: ImgbbClient, albums: list[dict], cfg, args, browser=None) -> list[dict]:
    """镜像模式：爬取原图链接并上传到 imgbb，支持断点续传。

    续传语义：
    - mirror_album_id 已存在 -> 复用相册，不新建
    - 单张图 status==uploaded -> 跳过
    - 单张图 failed/pending -> 上传（upload_worker 内带重试）
    """
    from .uploader import ImgbbUploader

    if not cfg.imgbb.cookie:
        console.print("[red]镜像模式需要 imgbb cookie (--imgbb-cookie 或配置文件)[/red]")
        return []

    results = []

    with ImgbbUploader(
        cookie=cfg.imgbb.cookie, auth_token=cfg.imgbb.auth_token, delay=cfg.delay
    ) as uploader:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=console,
            disable=args.quiet,
        ) as progress:
            album_task = progress.add_task("镜像总进度", total=len(albums))
            img_task = progress.add_task("当前相册", total=0, visible=False)

            for i, album in enumerate(albums):
                album_name = album["name"]
                progress.update(
                    album_task, description=f"[{i + 1}/{len(albums)}] {album_name[:40]}"
                )

                # 收集原图链接
                image_pages = collect_image_pages(client, album["url"], browser=browser)
                total = len(image_pages)
                if not image_pages:
                    progress.advance(album_task)
                    results.append({"name": album_name, "total": 0, "success": 0, "failed": 0})
                    continue

                progress.update(
                    img_task,
                    total=total + 1,
                    completed=0,
                    visible=True,
                    description=f"获取原图: {album_name[:30]}",
                )

                state_path = mirror_state_path(cfg.output, album)
                original_urls = get_cached_original_urls(state_path, total)
                if original_urls:
                    logger.info(f"复用缓存原图链接: {album_name} ({total} 张)")
                else:
                    original_urls = resolve_original_urls(
                        client, image_pages, workers=cfg.workers, browser=browser
                    )
                metadata, upload_names = prepare_mirror_metadata(
                    state_path, album, args.url, image_pages, original_urls
                )
                uploaded_before = count_uploaded(metadata)

                # 在用户 imgbb 创建或复用相册
                try:
                    if metadata.get("mirror_album_id"):
                        new_album = {
                            "id": metadata["mirror_album_id"],
                            "url": metadata.get("mirror_album_url", ""),
                            "name": album_name,
                        }
                        new_album_id = new_album["id"]
                        logger.info(f"继续上传: {album_name} -> {new_album.get('url', '')}")
                    else:
                        new_album = uploader.create_album(album_name)
                        new_album_id = new_album["id"]
                        save_mirror_album(metadata, state_path, new_album)
                        logger.info(f"创建相册: {album_name} -> {new_album['url']}")
                except Exception as e:
                    logger.error(f"创建相册失败 ({album_name}): {e}")
                    progress.advance(album_task)
                    results.append(
                        {"name": album_name, "total": total, "success": 0, "failed": total}
                    )
                    continue

                progress.update(
                    img_task,
                    total=total,
                    completed=uploaded_before,
                    description=f"上传: {album_name[:30]}",
                )

                success = uploaded_before
                failed = 0

                if uploaded_before == total:
                    save_mirror_album(metadata, state_path, new_album, completed=True)
                else:
                    upload_queue: queue.Queue = queue.Queue(maxsize=max(1, min(cfg.workers, 3)))
                    result_queue: queue.Queue = queue.Queue()
                    worker = threading.Thread(
                        target=upload_worker,
                        args=(uploader, new_album_id, upload_queue, result_queue),
                        daemon=True,
                    )
                    worker.start()

                    def _collect_results(
                        block: bool = False,
                        _q=result_queue,
                        _m=metadata,
                        _sp=state_path,
                        _an=album_name,
                        _it=img_task,
                    ):
                        nonlocal success, failed
                        while True:
                            try:
                                item = _q.get(timeout=0.1 if block else 0)
                            except queue.Empty:
                                break
                            if item["result"]:
                                success += 1
                                mark_uploaded(_m, item["index"], item["result"])
                            else:
                                failed += 1
                                mark_uploaded(_m, item["index"], None)
                                logger.warning(
                                    f"上传失败: {_an} #{item['index'] + 1}: {item['error']}"
                                )
                            save_metadata_file(_sp, _m)
                            progress.update(_it, completed=success + failed)

                    pending_indices = [
                        idx
                        for idx in range(total)
                        if metadata["images"][idx].get("status") != "uploaded"
                    ]

                    for idx in pending_indices:
                        upload_name = upload_names[idx]
                        title = os.path.splitext(upload_name)[0]
                        try:
                            if image_pages[idx].get("source") == "xchina":
                                if not browser:
                                    raise RuntimeError("xchina 上传缺少浏览器会话")
                                task = {
                                    "type": "file",
                                    "index": idx,
                                    "title": title,
                                    "upload_name": upload_name,
                                    "file_path": download_to_temp(
                                        browser, album, image_pages[idx], upload_name
                                    ),
                                }
                            else:
                                task = {
                                    "type": "url",
                                    "index": idx,
                                    "title": title,
                                    "upload_name": upload_name,
                                    "url": original_urls[idx],
                                }
                            upload_queue.put(task)
                            _collect_results()
                        except Exception as e:
                            failed += 1
                            mark_uploaded(metadata, idx, None)
                            save_metadata_file(state_path, metadata)
                            logger.warning(f"下载失败: {album_name} #{idx + 1}: {e}")
                            progress.update(img_task, completed=success + failed)

                    upload_queue.put(None)
                    upload_queue.join()
                    worker.join()
                    _collect_results(block=True)

                    if success == total:
                        save_mirror_album(metadata, state_path, new_album, completed=True)
                    else:
                        save_mirror_album(metadata, state_path, new_album, completed=False)

                progress.update(img_task, visible=False)
                progress.advance(album_task)

                logger.info(f"  镜像完成: {album_name} ({success}/{total} 成功, {failed} 失败)")
                results.append(
                    {
                        "name": album_name,
                        "total": total,
                        "success": success,
                        "failed": failed,
                        "album_url": new_album.get("url", ""),
                    }
                )

    total_imgs = sum(r["total"] for r in results)
    total_ok = sum(r["success"] for r in results)
    total_fail = sum(r["failed"] for r in results)
    console.print(
        f"\n[bold green]镜像完成[/bold green] 相册: {len(results)}, "
        f"图片: {total_imgs}, 成功: {total_ok}, 失败: {total_fail}"
    )
    for r in results:
        if r.get("album_url"):
            console.print(f"  {r['name']}: {r['album_url']}")
    return results
