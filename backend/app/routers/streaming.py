"""
Streaming API endpoints for media playback.
"""
import re
import logging
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from slowapi import Limiter
from slowapi.util import get_remote_address

from ..database import get_db
from ..models import File, User
from ..auth import get_current_user, get_scope, stream_auth
from ..visibility import file_visible
from .. import telegram
from .. import thumbs
from ..telegram import get_message_from_channel
from ..streaming import stream_file as stream_file_generator

# Logger for internal debugging (not exposed to users)
logger = logging.getLogger(__name__)

# Rate limiter for public endpoints
limiter = Limiter(key_func=get_remote_address)

async def _download_token_allows(db, folder_id: int, file) -> bool:
    """A download token may only reach files inside the folder it was issued for."""
    if not folder_id or file.folder_id is None:
        return False
    from sqlalchemy import text as _text
    row = await db.execute(_text("""
        WITH RECURSIVE sub AS (
            SELECT id FROM folders WHERE id = :root
            UNION ALL
            SELECT f.id FROM folders f JOIN sub s ON f.parent_id = s.id
        ) SELECT 1 FROM sub WHERE id = :fid LIMIT 1
    """), {"root": folder_id, "fid": file.folder_id})
    return row.first() is not None

router = APIRouter(prefix="/stream", tags=["Streaming"])


def parse_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    """Parse HTTP Range header for video seeking support."""
    if not range_header:
        return 0, file_size - 1
    
    match = re.match(r'bytes=(\d+)-(\d*)', range_header)
    if not match:
        return 0, file_size - 1
    
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else file_size - 1
    
    return start, min(end, file_size - 1)


@router.get("/{file_id}")
async def stream_file(
    file_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth=Depends(stream_auth),
    download: int = Query(0, description="Set to 1 to force download"),
):
    current_user, scope, token_payload = auth
    """Stream file from Telegram with range request support for seeking."""
    # Get file from database
    result = await db.execute(
        select(File).where(File.id == file_id, File.user_id == current_user.id)
    )
    file = result.scalar_one_or_none()
    
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    if token_payload.get("type") == "download":
        if not await _download_token_allows(db, token_payload.get("dlf"), file):
            raise HTTPException(status_code=404, detail="File not found")
    elif not await file_visible(db, current_user.id, file.folder_id, scope):
        raise HTTPException(status_code=404, detail="File not found")

    file_size = file.file_size
    
    # Parse range header
    range_header = request.headers.get("range")
    from_bytes, until_bytes = parse_range_header(range_header, file_size)
    
    # Validate range
    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return Response(
            status_code=416,
            content="416: Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    
    req_length = until_bytes - from_bytes + 1
    
    # Get message from channel
    message = await get_message_from_channel(file.channel_message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found in channel")
    
    async def file_streamer():
        """Generator that streams file chunks from Telegram MTProto."""
        async for chunk in stream_file_generator(
            telegram.tg_client,
            message,
            from_bytes,
            until_bytes
        ):
            yield chunk
    
    # Determine content disposition
    mime_type = file.mime_type or "application/octet-stream"
    previewable = (
        "video/" in mime_type or "audio/" in mime_type
        or "image/" in mime_type or mime_type == "application/pdf"
        or mime_type.startswith("text/")
    )
    disposition = "attachment" if download else ("inline" if previewable else "attachment")
    
    from urllib.parse import quote
    encoded_filename = quote(file.file_name)
    
    headers = {
        "Content-Type": mime_type,
        "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
        "Content-Length": str(req_length),
        "Content-Disposition": f"{disposition}; filename*=utf-8''{encoded_filename}",
        "Accept-Ranges": "bytes",
    }
    
    return StreamingResponse(
        file_streamer(),
        status_code=206 if range_header else 200,
        media_type=mime_type,
        headers=headers
    )


@router.get("/{file_id}/thumbnail")
async def get_thumbnail(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    auth=Depends(stream_auth),
):
    current_user, scope, token_payload = auth
    """Get file thumbnail."""
    # Get file from database
    result = await db.execute(
        select(File).where(File.id == file_id, File.user_id == current_user.id)
    )
    file = result.scalar_one_or_none()
    
    if not file:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    if token_payload.get("type") == "download":
        if not await _download_token_allows(db, token_payload.get("dlf"), file):
            raise HTTPException(status_code=404, detail="Thumbnail not found")
    elif not await file_visible(db, current_user.id, file.folder_id, scope):
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    # 图片现在按「文档」上传，Telegram 不生成内置缩略图。这里优先用本地缓存的
    # 缩略图（上传时由 ffmpeg 生成）；缓存缺失时从 Telegram 取原图临时生成。
    if not file.thumbnail_file_id:
        is_image = (file.file_type or "").lower() == "image" or (file.mime_type or "").startswith("image/")
        if not is_image:
            raise HTTPException(status_code=404, detail="Thumbnail not found")
        cached = thumbs.thumb_path(file.id)
        if not cached.exists():
            await thumbs.ensure_thumb_from_message(file.id, file.channel_message_id, get_message_from_channel)
        if cached.exists():
            return Response(content=cached.read_bytes(), media_type="image/jpeg")
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    
    try:
        # Get the message and download thumbnail
        message = await get_message_from_channel(file.channel_message_id)
        if not message:
            raise HTTPException(status_code=404, detail="Message not found")

        # Extract thumbnail object
        thumbnail = None
        if message.video and message.video.thumbs:
            thumbnail = message.video.thumbs[0]
        elif message.document and message.document.thumbs:
            thumbnail = message.document.thumbs[0]
        elif message.audio and message.audio.thumbs:
            thumbnail = message.audio.thumbs[0]
        elif message.photo:
            # A photo size file_id cannot be downloaded on its own; the message
            # is required. Inline small photos as their own preview, and let
            # larger ones fall back to the generic icon.
            sizes = getattr(message.photo, "sizes", None) or []
            best = sizes[-1] if sizes else None
            if best is not None and (best.file_size or 0) <= 1500000:
                data = await telegram.tg_client.download_media(message, in_memory=True)
                return Response(content=data.getvalue(), media_type="image/jpeg")
            raise HTTPException(status_code=404, detail="Thumbnail not available")
            
        if not thumbnail:
            # Try using the file_id directly if stored (fallback)
            if file.thumbnail_file_id:
                try:
                    thumb_bytes = await telegram.tg_client.download_media(file.thumbnail_file_id, in_memory=True)
                    return Response(content=thumb_bytes.getvalue(), media_type="image/jpeg")
                except Exception:
                    pass
            raise HTTPException(status_code=404, detail="Thumbnail not found in message")
        
        # Download thumbnail to memory
        thumb_bytes = await telegram.tg_client.download_media(thumbnail.file_id, in_memory=True)
        
        return Response(
            content=thumb_bytes.getvalue(),
            media_type="image/jpeg"
        )
    except Exception as e:
        # Log error internally, don't expose details to users
        logger.error(f"Thumbnail error for file {file_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get thumbnail")


@router.get("/s/{public_hash}")
@limiter.limit("60/minute")  # Rate limit public streaming to prevent abuse
async def stream_public_file(
    public_hash: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    download: int = Query(0, description="Set to 1 to force download"),
):
    """Stream file via public link (no auth required)."""
    # Get file by hash
    result = await db.execute(select(File).where(File.public_hash == public_hash))
    file = result.scalar_one_or_none()
    
    if not file:
        raise HTTPException(status_code=404, detail="File not found or link revoked")
        
    file_size = file.file_size
    
    # Parse range header
    range_header = request.headers.get("range")
    from_bytes, until_bytes = parse_range_header(range_header, file_size)
    
    # Validate range
    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return Response(
            status_code=416,
            content="416: Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    
    req_length = until_bytes - from_bytes + 1
    
    # Get message from channel
    message = await get_message_from_channel(file.channel_message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found in channel")
    
    async def file_streamer():
        """Generator that streams file chunks from Telegram MTProto."""
        async for chunk in stream_file_generator(
            telegram.tg_client,
            message,
            from_bytes,
            until_bytes
        ):
            yield chunk
    
    # Determine content disposition
    mime_type = file.mime_type or "application/octet-stream"
    previewable = (
        "video/" in mime_type or "audio/" in mime_type
        or "image/" in mime_type or mime_type == "application/pdf"
        or mime_type.startswith("text/")
    )
    disposition = "attachment" if download else ("inline" if previewable else "attachment")
    
    from urllib.parse import quote
    encoded_filename = quote(file.file_name)
    
    headers = {
        "Content-Type": mime_type,
        "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
        "Content-Length": str(req_length),
        "Content-Disposition": f"{disposition}; filename*=utf-8''{encoded_filename}",
        "Accept-Ranges": "bytes",
    }
    
    return StreamingResponse(
        file_streamer(),
        status_code=206 if range_header else 200,
        media_type=mime_type,
        headers=headers
    )
