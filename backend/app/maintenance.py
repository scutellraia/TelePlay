"""每日维护任务。

两件事，都是「每天扫描一次、超过 24 小时才处理」：
1. 清理 `.resume/` 下超过 24 小时没有进展的续传碎片（中断上传留下的 .part）。
   24 小时内的碎片保留，方便浏览器续传。
2. 清理 /downloads 下超过 24 小时的空目录，以及「已从下载列表移除」后遗留的下载文件（文件级判定）（aria2 为每个任务建目录，但从不删除）。
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
MLDONKEY_STATE_DIR = Path(os.environ.get("MLDONKEY_STATE_DIR", "/mldonkey-state"))
ED2K_DONE_DIR = Path(os.environ.get("ED2K_DONE_DIR", str(DOWNLOAD_DIR / "ed2k" / "done")))

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


async def _aria2_active_files():
    """aria2 当前任务仍引用的文件路径。"""
    from .routers.up import aria2_call

    files = set()
    for method, params in (
        ("aria2.tellActive", None),
        ("aria2.tellWaiting", [0, 2000]),
        ("aria2.tellStopped", [0, 2000]),
    ):
        try:
            tasks = await aria2_call(method, params) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("maintenance: %s failed: %s", method, exc)
            continue
        for task in tasks:
            for item in task.get("files") or []:
                path = (item.get("path") or "").strip()
                if path:
                    files.add(os.path.realpath(path))
    return files


def _mldonkey_active_files():
    """mldonkey(files.ini) 里仍被管理的文件路径——等价于 aria2 的 per-task files。"""
    ini = MLDONKEY_STATE_DIR / "files.ini"
    files = set()
    if not ini.is_file():
        return files
    try:
        for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
            head, _, tail = line.partition("=")
            if head.strip() != "file_name":
                continue
            value = tail.strip().strip('"')
            if value:
                files.add(os.path.realpath(value))
    except OSError as exc:  # noqa: BLE001
        logger.warning("maintenance: cannot read files.ini: %s", exc)
    return files


def _uploading_files():
    """上传队列里正在处理的文件（不能被当残留删掉）。"""
    from .routers.up import _load_file_jobs

    files = set()
    try:
        for job in _load_file_jobs() or []:
            if isinstance(job, dict):
                for key in ("path", "file", "local_path"):
                    value = job.get(key)
                    if isinstance(value, str) and value:
                        files.add(os.path.realpath(value))
    except Exception as exc:  # noqa: BLE001
        logger.warning("maintenance: cannot read upload jobs: %s", exc)
    return files


def _purge_orphan_files(referenced_files, uploading_files):
    """删除 /downloads 下「不再被任何下载器引用、也不在上传中」且超过 24h 的文件。

    判定是文件级的：
    - aria2 任务 files[].path、mldonkey files.ini 的 file_name → 仍被引用，绝不动；
    - 上传队列里的文件 → 不动；
    - /downloads/ed2k/done（等待上传）整棵跳过；
    - 其余「无主 + 超 24h」的即历史残留（例如已从下载列表移除的旧任务），删除并顺带清空父目录。
    """
    if not DOWNLOAD_DIR.is_dir():
        return 0
    now = time.time()
    root = os.path.realpath(str(DOWNLOAD_DIR))
    done_root = os.path.realpath(str(ED2K_DONE_DIR))
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        real_dir = os.path.realpath(dirpath)
        if real_dir == done_root or real_dir.startswith(done_root + os.sep):
            dirnames[:] = []
            continue
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                real = os.path.realpath(path)
                if real in referenced_files or real in uploading_files:
                    continue
                if now - os.path.getmtime(real) < STALE_SECONDS:
                    continue
                os.unlink(real)
                removed += 1
                logger.info("maintenance: removed orphan file %s", real)
                prune_empty_parent(real)
            except OSError:
                continue
    return removed


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
            ref_files = await _aria2_active_files()
            ref_files |= _mldonkey_active_files()
            uploading = _uploading_files()
            files = _purge_orphan_files(ref_files, uploading)
            if parts or dirs or files:
                logger.info(
                    "daily maintenance done: %d resume parts, %d empty dirs, %d orphan files",
                    parts, dirs, files,
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