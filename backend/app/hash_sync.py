"""
Content-hash registry for the telegram library.

- computes sha256 for files stored in telegram (streams, never writes to disk)
- caches hashes per channel message id
- reconciles the database with the channel: deletes rows whose message is gone,
  re-points rows when the same content appears under a new message, and adopts
  files that exist on telegram but not in the database
"""
import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from sqlalchemy import delete, select, update

from .config import get_settings
from .database import async_session
from .models import File, User
from .streaming import stream_file as _stream_file
from . import telegram as telegram_module

logger = logging.getLogger(__name__)
settings = get_settings()

CACHE_FILE = Path(os.environ.get("UP_HASH_CACHE_FILE", "/app/session/tg-hashes.json"))

STATUS = {
    "running": False,
    "phase": "",
    "total": 0,
    "deleted": 0,
    "renamed": 0,
    "adopted": 0,
    "hashed": 0,
    "last_run": None,
    "last_error": "",
}

_cache = {}
_cache_loaded = False


def _load_cache():
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        if CACHE_FILE.is_file():
            data = json.loads(CACHE_FILE.read_text() or "{}")
            if isinstance(data, dict):
                _cache.update({str(k): v for k, v in data.items()})
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot read hash cache: %s", exc)


def _save_cache():
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(_cache))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot write hash cache: %s", exc)


def cache_get(message_id):
    _load_cache()
    return _cache.get(str(message_id))


def cache_put(message_id, sha256, name, size):
    _load_cache()
    _cache[str(message_id)] = {"sha256": sha256, "name": name, "size": int(size or 0)}
    _save_cache()


def media_of(message):
    if message is None:
        return None
    return message.document or message.video or message.audio or message.photo


def media_name(message, media):
    for candidate in (media, getattr(message, "video", None), getattr(message, "document", None),
                      getattr(message, "audio", None)):
        name = getattr(candidate, "file_name", None)
        if name:
            return name
    return "photo-%s.jpg" % getattr(message, "id", "0")


async def message_sha256(message, size):
    """sha256 of a telegram message, cached by message id."""
    cached = cache_get(message.id)
    if cached and cached.get("sha256"):
        return cached["sha256"]
    digest = hashlib.sha256()
    last = max(0, int(size or 0) - 1)
    client = getattr(telegram_module, "tg_client", None)
    if client is None:
        raise RuntimeError("Telegram client is not started")
    async for chunk in _stream_file(client, message, 0, last):
        digest.update(chunk)
    value = digest.hexdigest()
    media = media_of(message)
    cache_put(message.id, value, media_name(message, media), size)
    STATUS["hashed"] = STATUS.get("hashed", 0) + 1
    return value


async def find_by_hash(sha256):
    if not sha256:
        return None
    async with async_session() as db:
        row = await db.execute(select(File).where(File.sha256 == sha256).limit(1))
        return row.scalar_one_or_none()


def _type_for(media, name):
    mime = getattr(media, "mime_type", "") or ""
    lower = (name or "").lower()
    if mime.startswith("video/") or lower.endswith((".mp4", ".mkv", ".avi", ".mov", ".webm")):
        return "video"
    if mime.startswith("audio/") or lower.endswith((".mp3", ".flac", ".m4a", ".wav", ".ogg")):
        return "audio"
    if mime.startswith("image/") or lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return "image"
    return "document"


async def sync_once(adopt=False):
    """Reconcile the database with the telegram channel.

    Bots cannot enumerate channel history (messages.GetHistory is bot-invalid),
    so this walks the database rows and checks them against the channel by
    message id: missing message -> drop the row, changed name -> sync the name.
    Hashing is a separate job (/up/api/hash/backfill).
    """
    if STATUS["running"]:
        return STATUS
    STATUS.update({"running": True, "phase": "compare", "total": 0, "deleted": 0,
                   "renamed": 0, "adopted": 0, "hashed": 0, "last_error": ""})
    started = time.time()
    try:
        client = getattr(telegram_module, "tg_client", None)
        if client is None:
            raise RuntimeError("Telegram client is not started")
        channel = settings.telegram_storage_channel_id

        async with async_session() as db:
            rows = (await db.execute(select(File))).scalars().all()
        STATUS["total"] = len(rows)

        ids = [int(r.channel_message_id) for r in rows]
        found = {}
        for start_idx in range(0, len(ids), 100):
            batch = ids[start_idx:start_idx + 100]
            try:
                messages = await client.get_messages(channel, batch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("batch message lookup failed: %s", exc)
                raise
            for msg in messages or []:
                if msg is None:
                    continue
                if media_of(msg) is None:
                    continue
                found[int(msg.id)] = msg

        updates, deletes = [], []
        for row in rows:
            mid = int(row.channel_message_id)
            msg = found.get(mid)
            if msg is None:
                deletes.append(row.id)
                continue
            name = media_name(msg, media_of(msg))
            if getattr(row, "name_locked", False):
                continue  # user renamed it on purpose
            if name and name != row.file_name:
                updates.append((row.id, {"file_name": name}))
                STATUS["renamed"] += 1

        STATUS["phase"] = "apply"
        if updates or deletes:
            async with async_session() as db:
                for row_id, values in updates:
                    await db.execute(update(File).where(File.id == row_id).values(**values))
                if deletes:
                    await db.execute(delete(File).where(File.id.in_(deletes)))
                await db.commit()

        STATUS["deleted"] = len(deletes)
        STATUS["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        logger.info(
            "hash sync done in %.1fs: checked=%s deleted=%s renamed=%s",
            time.time() - started, STATUS["total"], STATUS["deleted"], STATUS["renamed"],
        )
        return STATUS
    except Exception as exc:  # noqa: BLE001
        STATUS["last_error"] = str(exc)[:300]
        logger.error("hash sync failed: %s", exc)
        return STATUS
    finally:
        STATUS["running"] = False
        STATUS["phase"] = ""


