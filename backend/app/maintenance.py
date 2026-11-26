"""每日维护任务。

两件事，都是「每天扫描一次、超过 24 小时才处理」：
1. 清理 `.resume/` 下超过 24 小时没有进展的续传碎片（中断上传留下的 .part）。
   24 小时内的碎片保留，方便浏览器续传。
2. 清理 /downloads 下超过 24 小时的空目录（aria2 为每个任务建目录，但从不删除）。
   判定统一为「是否仍被任何下载器（aria2 / mldonkey）引用」：被引用就跳过，等任务停掉再过 24 小时才删。

另外提供 prune_empty_parent()：上传成功并删除源文件后，顺手把因此变空的父目录删掉。
"""

import asyncio
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

UPLOAD_TMP_DIR = Path(os.environ.get("UP_TMP_DIR", "/tmp/tp-uploads"))
RESUME_DIR = UPLOAD_TMP_DIR / ".resume"
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOADS_DIR", "/downloads"))

STALE_SECONDS = 24 * 3600

INTERVAL_SECONDS = 24 * 3600

_task = None


def _purge_resume_parts():
    """删除超过 24 小时的 .part 续传碎片，返回删除个数。"""
    if not RESUME_DIR.is_dir():
        return 0
    now = time.time()
    removed = 0
    for part in RESUME_DIR.glob("*.part"):
        try:
            if now - part.stat().st_mtime > STALE_SECONDS:
                part.unlink()
                removed += 1
                logger.info("maintenance: removed stale resume part %s", part.name)
        except OSError as exc:
            logger.warning("maintenance: cannot remove %s: %s", part, exc)
    return removed


async def _aria2_referenced_dirs():
    """aria2 当前（活动/等待/已停止列表里）仍指向的目录集合。"""
    from .routers.up import aria2_call

    dirs = set()
    calls = (
        ("aria2.tellActive", None),
        ("aria2.tellWaiting", [0, 2000]),
        ("aria2.tellStopped", [0, 2000]),
    )
    for method, params in calls:
        try:
            tasks = await aria2_call(method, params) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("maintenance: %s failed: %s", method, exc)
            continue
        for task in tasks:
            base = (task.get("dir") or "").strip()
            if base:
                dirs.add(os.path.realpath(base))
            for item in task.get("files") or []:
                path = (item.get("path") or "").strip()
                if path:
                    dirs.add(os.path.realpath(os.path.dirname(path)))
            bt = (task.get("bittorrent") or {}).get("info") or {}
            name = bt.get("name")
            if name and base:
                dirs.add(os.path.realpath(os.path.join(base, name)))
    return dirs


async def _mldonkey_referenced_dirs():
    """mldonkey(ed2k) 仍有任务时，它正在使用的目录集合（与 aria2 同一套判定）。"""
    from .routers.up import _mld_vd_rows, ED2K_DONE_DIR, ED2K_TEMP_DIR, ED2K_INCOMING_DIR

    dirs = set()
    try:
        rows = await asyncio.to_thread(_mld_vd_rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("maintenance: mldonkey vd failed: %s", exc)
        return dirs
    if not rows:
        return dirs
    for d in (ED2K_TEMP_DIR, ED2K_INCOMING_DIR, ED2K_DONE_DIR):
        dirs.add(os.path.realpath(str(d)))
    return dirs


async def _referenced_dirs():
    """所有下载器仍引用的目录并集（aria2 + mldonkey）。"""
    dirs = set()
    for probe in (_aria2_referenced_dirs, _mldonkey_referenced_dirs):
        try:
            dirs |= await probe()
        except Exception as exc:  # noqa: BLE001
            logger.warning("maintenance: %s failed: %s", probe.__name__, exc)
    return dirs


def _purge_empty_dirs(referenced):
    """删除超过 24 小时、且没有被 aria2 任务引用的空目录。"""
    if not DOWNLOAD_DIR.is_dir():
        return 0
    now = time.time()
    removed = 0
    root = os.path.realpath(str(DOWNLOAD_DIR))
    for dirpath, dirnames, _files in os.walk(root, topdown=False):
        for name in dirnames:
            path = os.path.join(dirpath, name)
            try:
                if os.listdir(path):
                    continue
                if now - os.path.getmtime(path) < STALE_SECONDS:
                    continue
                if os.path.realpath(path) in referenced:
                    continue
                os.rmdir(path)
                removed += 1
                logger.info("maintenance: removed empty dir %s", path)
            except OSError:
                continue
    return removed


async def daily_maintenance():
    """启动后先跑一次，之后每 24 小时跑一次。"""
    while True:
        try:
            parts = _purge_resume_parts()
            referenced = await _referenced_dirs()
            dirs = _purge_empty_dirs(referenced)
            if parts or dirs:
                logger.info(
                    "daily maintenance done: %d resume parts, %d empty dirs", parts, dirs
                )
            else:
                logger.info("daily maintenance done: nothing to clean")
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily maintenance failed: %s", exc)
        await asyncio.sleep(INTERVAL_SECONDS)


def start_maintenance():
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(daily_maintenance())
        logger.info("daily maintenance task started")


def stop_maintenance():
    global _task
    if _task is not None:
        _task.cancel()
        _task = None


def prune_empty_parent(path, stop_at=None):
    """删除 path 的父目录，直至 stop_at（根目录本身不删）。空目录以外的会直接失败。"""
    root = os.path.realpath(str(stop_at or DOWNLOAD_DIR))
    try:
        candidate = os.path.dirname(os.path.realpath(str(path)))
    except OSError:
        return False
    removed = False
    while candidate.startswith(root + os.sep):
        try:
            if os.listdir(candidate):
                return removed
            os.rmdir(candidate)
            removed = True
            candidate = os.path.dirname(candidate)
        except OSError:
            return removed
    return removed