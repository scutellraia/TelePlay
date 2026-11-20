"""Reusable Telegram upload module.

Uploads a local file to the configured Telegram storage channel through the bot
client, registers the resulting media in the database, and optionally deletes
the local copy. Both the aria2 completion worker and the web upload endpoint use
this module, so the logic lives here only once.
"""

import logging
import mimetypes
import asyncio
import hashlib
import os
import re
from pathlib import Path

from sqlalchemy import select

from .config import get_settings
from .database import async_session
from .models import File, User
from . import telegram as telegram_module
from . import thumbs

logger = logging.getLogger(__name__)
settings = get_settings()

VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts", ".m4v", ".wmv",
    ".rmvb", ".mpg", ".mpeg", ".3gp", ".m2ts",
}
AUDIO_EXT = {".mp3", ".flac", ".wav", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".ape"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def sanitize_filename(name):
    """Return a filesystem/Telegram safe basename."""
    name = os.path.basename(str(name))
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()
    name = name.lstrip(".")
    return name[:200] or "unnamed"


def guess_media_type(path):
    """Return (file_type, mime_type) for the given path."""
    p = Path(path)
    mime, _ = mimetypes.guess_type(p.name)
    mime = mime or ""
    ext = p.suffix.lower()
    if ext in VIDEO_EXT or mime.startswith("video/"):
        return "video", mime or "video/mp4"
    if ext in AUDIO_EXT or mime.startswith("audio/"):
        return "audio", mime or "audio/mpeg"
    if ext in IMAGE_EXT or mime.startswith("image/"):
        return "image", mime or "application/octet-stream"
    return "document", mime or "application/octet-stream"


async def _resolve_user(db, telegram_id=None):
    """Find the DB user for a telegram id, falling back to any known user."""
    candidate = telegram_id
    if candidate is None:
        auth_ids = settings.auth_users
        if auth_ids:
            candidate = auth_ids[0]
    if candidate is not None:
        res = await db.execute(select(User).where(User.telegram_id == candidate))
        user = res.scalar_one_or_none()
        if user:
            return user
    res = await db.execute(select(User).limit(1))
    user = res.scalar_one_or_none()
    if user:
        return user
    user = User(telegram_id=candidate or 0, username="uploader", first_name="Auto Upload")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def resolve_db_user(db, telegram_id=None):
    """Public helper: the DB user that uploads are attributed to."""
    return await _resolve_user(db, telegram_id)


UPLOAD_RETRIES = int(os.environ.get("UP_UPLOAD_RETRIES", "3"))
UPLOAD_RETRY_DELAY = int(os.environ.get("UP_UPLOAD_RETRY_DELAY", "10"))


async def _send_with_retry(label, sender):
    """Run a Telegram send with exponential backoff; FloodWait is honoured."""
    attempt = 0
    delay = UPLOAD_RETRY_DELAY
    while True:
        attempt += 1
        try:
            return await sender()
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            wait = delay
            if "FloodWait" in name:
                wait = int(getattr(exc, "value", 0) or delay) + 2
            if attempt > UPLOAD_RETRIES:
                logger.error("%s still failing after %d attempts: %s", label, attempt, exc)
                raise
            logger.warning("%s attempt %d/%d failed (%s: %s), retrying in %ss",
                           label, attempt, UPLOAD_RETRIES, name, exc, wait)
            await asyncio.sleep(wait)
            delay = min(delay * 2, 300)


def sha256_of_file(path, chunk_size=4 * 1024 * 1024):
    """Stream a local file through sha256 (no extra disk usage)."""
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


async def upload_path_to_telegram(
    path,
    telegram_id=None,
    folder_id=None,
    delete_after=True,
    caption=None,
    progress_cb=None,
    sha256=None,
):
    """Upload one local file to the storage channel, register it, delete local file.

    Returns a dict with the created DB file id plus the media metadata.
    Raises on upload failure so callers can decide what to do with the file.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    # NOTE: the client is assigned at startup by telegram.build_clients(), so it
    # must be read from the module (a `from ... import tg_client` would freeze the
    # initial None value and never see the live client).
    client = getattr(telegram_module, "tg_client", None)
    if client is None:
        raise RuntimeError("Telegram client is not started")

    local_size = p.stat().st_size
    clean_name = sanitize_filename(caption or p.name)
    file_type, mime_type = guess_media_type(p)
    channel = settings.telegram_storage_channel_id

    attempts = []
    if file_type == "video":
        attempts.append((
            "video",
            lambda: client.send_video(
                channel, str(p), caption=clean_name, supports_streaming=True,
                progress=progress_cb,
            ),
        ))
    elif file_type == "audio":
        attempts.append((
            "audio",
            lambda: client.send_audio(channel, str(p), caption=clean_name, progress=progress_cb),
        ))
    elif file_type == "image":
        # 图片一律按「文档」上传：Telegram 的照片通道会把图片重新编码（长边压到
        # 1280、JPEG 重压），原始字节无法保留。文档通道按原文件原样存储。
        # 缩略图在本地用 ffmpeg 生成并缓存（见 app/thumbs.py）。
        pass
    attempts.append((
        "document",
        lambda: client.send_document(channel, str(p), caption=clean_name, progress=progress_cb),
    ))

    sent = None
    for kind, sender in attempts:
        try:
            sent = await _send_with_retry("%s upload %s" % (kind, clean_name), sender)
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s upload failed for %s: %s", kind, clean_name, exc)
            sent = None
    if sent is None:
        raise RuntimeError("Telegram upload failed for %s" % clean_name)

    media = getattr(sent, "video", None) or getattr(sent, "audio", None)
    media = media or getattr(sent, "document", None) or getattr(sent, "photo", None)
    thumbs = getattr(media, "thumbs", None) if media is not None else None

    stored_type = file_type
    if media is not None and getattr(media, "file_id", None) is None:
        stored_type = "document"

    content_hash = sha256
    if not content_hash:
        try:
            content_hash = sha256_of_file(p)
        except OSError:
            content_hash = None

    info = {
        "sha256": content_hash,
        "file_id": getattr(media, "file_id", "") or "",
        "file_unique_id": getattr(media, "file_unique_id", "") or "",
        "channel_message_id": sent.id,
        "file_name": clean_name,
        "file_size": getattr(media, "file_size", local_size) or local_size,
        "mime_type": getattr(media, "mime_type", None) or mime_type,
        "file_type": stored_type,
        "duration": getattr(media, "duration", None),
        "width": getattr(media, "width", None),
        "height": getattr(media, "height", None),
        "thumbnail_file_id": thumbs[0].file_id if thumbs else None,
    }

    async with async_session() as db:
        user = await _resolve_user(db, telegram_id)
        record = File(user_id=user.id, folder_id=folder_id, **info)
        db.add(record)
        await db.commit()
        await db.refresh(record)
        db_id = record.id

    result = {"id": db_id}
    result.update(info)

    # 图片按文档上传后 Telegram 不再生成内置缩略图，这里用本地原图直接生成缓存。
    if file_type == "image" and not info.get("thumbnail_file_id"):
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, thumbs.make_thumb, p, db_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("thumbnail generation failed for %s: %s", p.name, exc)

    if delete_after:
        try:
            p.unlink()
            logger.info("deleted local file %s after upload", p)
            # 上传完成、源文件删除后，若父目录因此变空就顺手删掉（aria2 从不清理目录）
            try:
                from .maintenance import prune_empty_parent

                prune_empty_parent(p)
            except Exception as exc:  # noqa: BLE001
                logger.debug("prune empty parent failed for %s: %s", p, exc)
        except OSError as exc:
            logger.warning("could not delete %s: %s", p, exc)

    return result

# ---------------------------------------------------------------- splitting
# Telegram bots cannot upload a single file larger than 2000 MB, so videos that
# exceed it are cut into playable parts before upload.
TELEGRAM_LIMIT_BYTES = int(os.environ.get("UP_TG_LIMIT_BYTES", str(2000 * 1024 * 1024)))
SEGMENT_LIMIT_BYTES = int(os.environ.get("UP_SEGMENT_LIMIT_BYTES", str(1900 * 1024 * 1024)))


async def _ffprobe_duration(path):
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _err = await proc.communicate()
        return float(out.decode("utf-8", "ignore").strip())
    except Exception as exc:  # noqa: BLE001
        logger.warning("ffprobe failed for %s: %s", path, exc)
        return None


def _part_files(out_dir, stem, suffix):
    """列出某个文件切出来的分片。

    不能用 Path.glob：文件名里可能有 [ ] * ?，glob 会当成通配符，
    以 '[' 开头的名字（字幕组标记）会永远匹配不到。
    """
    prefix = stem + ".part"
    try:
        return sorted(
            x for x in out_dir.iterdir()
            if x.is_file() and x.name.startswith(prefix) and x.name.endswith(suffix)
        )
    except OSError:
        return []


async def split_video(path, limit_bytes=None):
    """Cut a video into playable parts, each below the telegram limit.

    Uses stream copy (no re-encode): fast, lossless. Segment length is derived
    from duration and size, and retried with a smaller factor if any part is
    still too big.
    """
    import asyncio as _asyncio  # local alias keeps the module import list tidy

    p = Path(path)
    limit = limit_bytes or SEGMENT_LIMIT_BYTES
    size = p.stat().st_size
    duration = await _ffprobe_duration(p)
    if not duration or duration <= 0:
        raise RuntimeError("cannot probe duration; refusing to split blindly")

    out_dir = p.parent
    for factor in (1.0, 0.90, 0.80, 0.65, 0.50, 0.35):
        for old in _part_files(out_dir, p.stem, p.suffix):
            try:
                old.unlink()
            except OSError:
                pass
        seg_time = max(10, int(duration * (limit / size) * factor))
        pattern = str(out_dir / (p.stem + ".part%03d" + p.suffix))
        cmd = [
            "ffmpeg", "-hide_banner", "-v", "error", "-i", str(p),
            "-c", "copy", "-map", "0",
            "-f", "segment", "-segment_time", str(seg_time),
            "-reset_timestamps", "1", pattern,
        ]
        proc = await _asyncio.create_subprocess_exec(
            *cmd, stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.PIPE
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + err.decode("utf-8", "ignore")[-400:])
        parts = _part_files(out_dir, p.stem, p.suffix)
        if not parts:
            raise RuntimeError("ffmpeg produced no parts")
        biggest = max(x.stat().st_size for x in parts)
        logger.info(
            "split %s -> %d parts, biggest %.0f MB (factor %.2f, seg %ss)",
            p.name, len(parts), biggest / 1048576.0, factor, seg_time,
        )
        if biggest < TELEGRAM_LIMIT_BYTES:
            return parts
    raise RuntimeError("splitting still produced an oversized part")


async def upload_file_auto(path, telegram_id=None, folder_id=None, delete_after=True, progress_cb=None, sha256=None):
    """Upload a file, splitting it first when it is a video above the bot limit.

    Returns {"parts": n, "files": [ ...file dicts... ]}.
    """
    p = Path(path)
    total = p.stat().st_size
    if total > TELEGRAM_LIMIT_BYTES and guess_media_type(p)[0] == "video":
        logger.info("file %s is %.0f MB, splitting before upload", p.name, total / 1048576.0)
        parts = await split_video(p)
        results = []
        all_ok = True
        try:
            for idx, part in enumerate(parts, 1):
                logger.info("uploading part %d/%d: %s", idx, len(parts), part.name)
                # A retry re-splits the file, so the same part may already be
                # stored: check the part hash before spending bandwidth on it.
                part_digest = None
                try:
                    part_digest = sha256_of_file(part)
                except OSError:
                    part_digest = None
                if part_digest:
                    existing = None
                    try:
                        from . import hash_sync as _hash_sync
                        existing = await _hash_sync.find_by_hash(part_digest)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("part dedupe check failed: %s", exc)
                    if existing is not None:
                        logger.info(
                            "skip duplicate part %s (already stored as id=%s)",
                            part.name, existing.id,
                        )
                        results.append({
                            "id": existing.id,
                            "file_name": existing.file_name,
                            "duplicate": True,
                        })
                        continue
                results.append(
                    await upload_path_to_telegram(
                        str(part), telegram_id=telegram_id,
                        folder_id=folder_id, delete_after=True,
                        progress_cb=progress_cb, sha256=part_digest,
                    )
                )
        except Exception:
            all_ok = False
            raise
        finally:
            # part files are always removed; the original is kept when the upload
            # failed so it can be retried from the web UI.
            for part in parts:
                try:
                    Path(part).unlink()
                except OSError:
                    pass
            if all_ok and delete_after:
                try:
                    p.unlink()
                except OSError:
                    pass
        return {"parts": len(results), "files": results}

    result = await upload_path_to_telegram(
        str(path), telegram_id=telegram_id, folder_id=folder_id, delete_after=delete_after,
        progress_cb=progress_cb, sha256=sha256,
    )
    return {"parts": 1, "files": [result]}
