"""本地缩略图生成。

图片现在按「文档」上传给 Telegram（避免照片通道的二次压缩），因此 Telegram
不再为图片生成内置缩略图。这里用容器内的 ffmpeg 从原图生成小尺寸 JPEG 并缓存
在 /app/session/thumbs 下，供 /api/stream/{id}/thumbnail 直接返回。
"""

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

THUMB_DIR = Path(os.environ.get("UP_THUMB_DIR", "/app/session/thumbs"))
MAX_SIDE = int(os.environ.get("UP_THUMB_SIDE", "360"))


def thumb_path(file_id):
    return THUMB_DIR / ("%d.jpg" % int(file_id))


def make_thumb(src, file_id, max_side=None):
    """把 src 缩成最长边 max_side 的 JPEG，返回缓存路径（失败返回 None）。"""
    side = max_side or MAX_SIDE
    out = thumb_path(file_id)
    try:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        vf = "scale=%d:%d:force_original_aspect_ratio=decrease" % (side, side)
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-v", "error", "-y",
                "-i", str(src), "-vf", vf, "-frames:v", "1",
                "-q:v", "4", str(out),
            ],
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            logger.warning(
                "thumb failed for %s: %s", src,
                proc.stderr.decode("utf-8", "ignore")[-200:],
            )
            return None
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("thumb error for %s: %s", src, exc)
        return None


async def ensure_thumb_from_message(file_id, channel_message_id, get_message):
    """缓存缺失时，从 Telegram 取回原图并生成缩略图。"""
    from . import telegram

    cached = thumb_path(file_id)
    if cached.exists():
        return cached

    tmp = THUMB_DIR / (".%d.src" % int(file_id))
    try:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        message = await get_message(channel_message_id)
        client = getattr(telegram, "tg_client", None)
        if message is None or client is None:
            return None
        await client.download_media(message, file_name=str(tmp))
        if not tmp.exists():
            return None
        import asyncio

        return await asyncio.get_running_loop().run_in_executor(
            None, make_thumb, tmp, file_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("thumb download failed for file %s: %s", file_id, exc)
        return None
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass