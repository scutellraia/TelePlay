"""Upload / download hub mounted at /up.

Auth is standalone: a password (plus a security question) stored hashed in
/app/session, with server side session tokens. The main site's JWT is not used.

Routes:
  GET  /up                              -> page
  ---- auth ----
  GET  /up/api/auth/status              -> configured / authenticated / question
  POST /up/api/auth/setup               -> first run: password + question + answer
  POST /up/api/auth/login               -> password -> session token
  POST /up/api/auth/logout              -> drop session
  POST /up/api/auth/change              -> old password + answer -> new password
  POST /up/api/auth/reset               -> answer -> new password
  ---- data (session required) ----
  GET  /up/api/status                   -> aria2 + disk
  GET  /up/api/server                   -> host metrics (cpu/mem/disk/net/...)
  POST /up/api/torrent                  -> add magnet/url
  GET  /up/api/tasks                    -> aria2 tasks
  POST /up/api/task/{gid}/{action}       -> pause/unpause/remove/forget
  POST /up/api/upload                   -> upload local file -> telegram -> delete
  GET  /up/api/processed                -> recently pushed files
  GET/POST /up/api/settings             -> reconcile interval
  ---- aria2 hook ----
  POST /up/api/hooks/complete
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from urllib.parse import quote
import shutil
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from .. import hash_sync, up_auth
from ..config import get_settings
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import async_session, get_db
from ..models import File as StoredFile, Folder
from ..tg_upload import resolve_db_user, sha256_of_file, upload_file_auto, upload_path_to_telegram

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()

ARIA2_RPC_URL = os.environ.get("ARIA2_RPC_URL", "http://aria2:6800/jsonrpc")
ARIA2_RPC_SECRET = os.environ.get("ARIA2_RPC_SECRET", "")
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
UPLOAD_TMP_DIR = Path(os.environ.get("UPLOAD_TMP_DIR", "/tmp/tp-uploads"))
STATE_FILE = Path(os.environ.get("UP_STATE_FILE", "/tmp/tp-up-state.json"))
STATUS_FILE = Path(os.environ.get("UP_STATUS_FILE", "/app/status/status.json"))
STATUS_MAX_AGE = int(os.environ.get("UP_STATUS_MAX_AGE", "30"))

# 0 = 不限制（只受磁盘可用空间约束）；需要时用 UP_MAX_UPLOAD_BYTES 覆盖
MAX_UPLOAD_BYTES = int(os.environ.get("UP_MAX_UPLOAD_BYTES", "0"))

DEFAULT_RECONCILE_SECONDS = int(os.environ.get("ARIA2_RECONCILE_SECONDS", "300"))
SETTINGS_FILE = Path(os.environ.get("UP_SETTINGS_FILE", "/app/session/up-settings.json"))
RECONCILE_CHOICES = [0, 60, 300, 600, 1800, 3600, 7200]
SYNC_CHOICES = [0,
                24 * 3600,        # 1 天
                3 * 24 * 3600,    # 3 天
                7 * 24 * 3600,    # 7 天
                15 * 24 * 3600,   # 15 天
                30 * 24 * 3600]   # 30 天
DEFAULT_SYNC_SECONDS = int(os.environ.get("UP_SYNC_SECONDS", "3600"))
UP_HOOK_SECRET = os.environ.get("UP_HOOK_SECRET", "")

_processed_gids = {}

_active_uploads = set()
UPLOAD_STALE_SECONDS = int(os.environ.get("UP_UPLOAD_STALE_SECONDS", "3600"))


def _sweep_tmp_uploads():
    """Remove orphaned temp uploads left by cancelled requests or restarts."""
    if not UPLOAD_TMP_DIR.is_dir():
        return 0
    cutoff = time.time() - UPLOAD_STALE_SECONDS
    removed = 0
    for path in UPLOAD_TMP_DIR.iterdir():
        try:
            if not path.is_file() or str(path) in _active_uploads:
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("swept %d orphaned upload file(s)", removed)
    return removed


_poller_task = None


# ------------------------------------------------------------------ helpers
def _now():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def load_settings():
    try:
        if SETTINGS_FILE.is_file():
            data = json.loads(SETTINGS_FILE.read_text() or "{}")
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read settings: %s", exc)
    return {}


def save_settings(data):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not write settings: %s", exc)


def current_sync_seconds():
    try:
        value = int(load_settings().get("sync_interval_seconds", DEFAULT_SYNC_SECONDS))
    except Exception:  # noqa: BLE001
        value = DEFAULT_SYNC_SECONDS
    return max(0, min(value, 30 * 24 * 3600))


def current_reconcile_seconds():
    try:
        value = int(load_settings().get("reconcile_seconds", DEFAULT_RECONCILE_SECONDS))
    except Exception:  # noqa: BLE001
        value = DEFAULT_RECONCILE_SECONDS
    return max(0, min(value, 86400))


def _dir_size(path):
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except Exception:  # noqa: BLE001
        pass
    return total


def _token_from_headers(authorization, cookie_token):
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    elif cookie_token:
        token = cookie_token.strip()
    return token


def require_session(
    authorization: str = Header(default=None),
    x_up_token: str = Header(default=None),
):
    """Standalone session check for every data endpoint."""
    token = _token_from_headers(authorization, x_up_token)
    if not up_auth.check_session(token):
        raise HTTPException(status_code=401, detail="not authenticated")
    return token

def require_write_auth(
    authorization: str = Header(default=None),
    x_up_token: str = Header(default=None),
    x_api_key: str = Header(default=None),
):
    """Write endpoints accept either a browser session or a machine API key."""
    token = _token_from_headers(authorization, x_up_token)
    if up_auth.check_session(token):
        return token
    if x_api_key and up_auth.verify_api_key(x_api_key):
        return "apikey"
    raise HTTPException(status_code=401, detail="not authenticated")


# --------------------------------------------------------------- auth routes
@router.get("/api/auth/status")
async def auth_status(authorization: str = Header(default=None), x_up_token: str = Header(default=None)):
    token = _token_from_headers(authorization, x_up_token)
    configured = up_auth.is_configured()
    question = up_auth.load_auth().get("question") if configured else None
    return {
        "configured": configured,
        "authenticated": up_auth.check_session(token),
        "question": question,
    }


@router.post("/api/auth/setup")
async def auth_setup(payload: dict):
    if up_auth.is_configured():
        raise HTTPException(status_code=400, detail="already configured")
    body = payload or {}
    try:
        up_auth.setup(
            body.get("password") or "",
            body.get("question") or "",
            body.get("answer") or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    token, expires = up_auth.create_session()
    return {"ok": True, "token": token, "expires": expires}


@router.post("/api/auth/login")
async def auth_login(payload: dict):
    if not up_auth.is_configured():
        raise HTTPException(status_code=400, detail="password not configured yet")
    try:
        ok = up_auth.verify_password((payload or {}).get("password") or "")
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=401, detail="wrong password")
    token, expires = up_auth.create_session()
    return {"ok": True, "token": token, "expires": expires}


@router.post("/api/auth/logout")
async def auth_logout(token: str = Depends(require_session)):
    up_auth.revoke_session(token)
    return {"ok": True}


@router.post("/api/auth/change")
async def auth_change(payload: dict, token: str = Depends(require_session)):
    body = payload or {}
    try:
        up_auth.change_password(
            body.get("old_password") or "",
            body.get("security_answer") or "",
            body.get("new_password") or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    new_token, expires = up_auth.create_session()
    return {"ok": True, "token": new_token, "expires": expires}


@router.post("/api/auth/reset")
async def auth_reset(payload: dict):
    if not up_auth.is_configured():
        raise HTTPException(status_code=400, detail="password not configured yet")
    body = payload or {}
    try:
        up_auth.reset_password(body.get("security_answer") or "", body.get("new_password") or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}




DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker1.bt.moack.co.kr:80/announce",
    "https://tracker.tamersunion.org:443/announce",
]


def _magnet_from_hash(info_hash):
    """Build a magnet that already carries public trackers."""
    base = "magnet:?xt=urn:btih:" + info_hash
    for tr in DEFAULT_TRACKERS:
        base += "&tr=" + quote(tr, safe="")
    return base

MAGNET_RE = re.compile(r"magnet:\?[A-Za-z0-9:/?&=#._~%+\-]+", re.IGNORECASE)


def _extract_targets(raw):
    """Pull every magnet link out of pasted text and fall back to hash / url.

    Handles text with Chinese characters or other noise around the links, and
    returns all of them so one paste can create several tasks.
    """
    text = (raw or "").strip()
    if not text:
        return []

    found = [m.rstrip(")。，,;；、]）") for m in MAGNET_RE.findall(text)]
    unique = []
    for m in found:
        if m and m not in unique:
            unique.append(m)
    if unique:
        return unique

    hash_hits = re.findall(r"[0-9a-fA-F]{40}", text)
    if hash_hits:
        magnets = []
        for item in hash_hits:
            magnet = _magnet_from_hash(item.lower())
            if magnet not in magnets:
                magnets.append(magnet)
        return magnets

    url_hits = re.findall(r"https?://[A-Za-z0-9:/?&=#._~%+\-]+", text)
    if url_hits:
        urls = []
        for item in url_hits:
            cleaned = item.rstrip(")。，,;；、]）")
            if cleaned and cleaned not in urls:
                urls.append(cleaned)
        return urls

    single = _normalize_magnet(text)
    return [single] if single else []


HASH40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
HASH32_RE = re.compile(r"^[A-Za-z2-7]{32}$")


def _normalize_magnet(raw):
    """Accept a bare info hash as well as a full magnet / http(s) link."""
    value = (raw or "").strip()
    if not value:
        return ""
    if HASH40_RE.match(value):
        return _magnet_from_hash(value.lower())
    if HASH32_RE.match(value) and re.search(r"[G-Zg-z]", value):
        return _magnet_from_hash(value.upper())
    return value


# ---------------------------------------------------- torrent target folders
TORRENT_FOLDER_FILE = Path(os.environ.get("UP_TORRENT_FOLDERS_FILE", "/app/session/up-torrent-folders.json"))


def _load_torrent_folders():
    try:
        if TORRENT_FOLDER_FILE.is_file():
            data = json.loads(TORRENT_FOLDER_FILE.read_text() or "{}")
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot read torrent folders: %s", exc)
    return {}


def _save_torrent_folders(data):
    try:
        TORRENT_FOLDER_FILE.parent.mkdir(parents=True, exist_ok=True)
        TORRENT_FOLDER_FILE.write_text(json.dumps(data))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot write torrent folders: %s", exc)


def _remember_torrent_folder(gid, folder_id):
    if not gid:
        return
    data = _load_torrent_folders()
    if folder_id:
        data[str(gid)] = int(folder_id)
    else:
        data.pop(str(gid), None)
    _save_torrent_folders(data)


def _forget_torrent_folder(gid):
    data = _load_torrent_folders()
    if data.pop(str(gid), None) is not None:
        _save_torrent_folders(data)


def _torrent_folder_id(gid):
    """Folder id remembered for a gid, or None."""
    try:
        value = _load_torrent_folders().get(str(gid))
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------ live upload view
def _idle_state():
    return {
        "active": False, "name": "", "size": 0, "done": 0, "percent": 0,
        "speed": 0, "folder": "", "started": None, "parts": 1,
    }


# Two independent lanes so a 5 GB video never blocks a stream of small images.
LANES = ("small", "big")
# sha256 -> queue key for uploads that are queued but not yet stored
_inflight_hashes = {}
LANE_SPLIT_BYTES = int(os.environ.get("UP_LANE_SPLIT_BYTES", str(300 * 1024 * 1024)))

_upload_states = {lane: _idle_state() for lane in LANES}
_speed_refs = {lane: {"t": 0.0, "b": 0} for lane in LANES}
_queue_view = []


def _lane_for_size(size):
    try:
        return "big" if int(size or 0) > LANE_SPLIT_BYTES else "small"
    except (TypeError, ValueError):
        return "small"


class UploadCancelled(Exception):
    """Raised inside the progress callback to abort a running upload."""


_upload_stop = {"small": False, "big": False}


def _reset_stop(lane):
    _upload_stop[lane] = False


def _start_active(lane, name, size, folder, parts=1):
    _reset_stop(lane)
    now = time.time()
    _upload_states[lane].update({
        "active": True, "name": name, "size": int(size or 0), "done": 0,
        "percent": 0, "speed": 0, "folder": folder or "根目录",
        "started": _now(), "parts": parts, "lane": lane,
    })
    _speed_refs[lane]["t"] = now
    _speed_refs[lane]["b"] = 0


def _track_progress(lane, current, total):
    if _upload_stop.get(lane):
        raise UploadCancelled("stopped by user")
    state = _upload_states[lane]
    ref = _speed_refs[lane]
    now = time.time()
    current = int(current or 0)
    total = int(total or state.get("size") or 0)
    state["done"] = current
    if total:
        state["size"] = total
        state["percent"] = round(min(100.0, current * 100.0 / total), 1)
    dt = now - ref["t"]
    if dt >= 0.5 and current >= ref["b"]:
        instant = (current - ref["b"]) / dt
        previous = state.get("speed") or 0
        state["speed"] = int(previous * 0.5 + instant * 0.5) if previous else int(instant)
        ref["t"] = now
        ref["b"] = current


def _stop_active(lane):
    _upload_states[lane].update(_idle_state())


async def _folder_name_by_id(folder_id):
    if not folder_id:
        return "根目录"
    try:
        async with async_session() as db:
            row = await db.execute(select(Folder).where(Folder.id == folder_id))
            folder = row.scalar_one_or_none()
            return folder.name if folder else "#%d" % folder_id
    except Exception as exc:  # noqa: BLE001
        logger.debug("folder name lookup failed: %s", exc)
        return "#%d" % folder_id


async def _folder_name_for_gid(gid):
    return await _folder_name_by_id(await _folder_for_gid(gid))


def _queued_entry(gid):
    for item in _queue_view:
        if item.get("gid") == str(gid):
            return item
    return None


def _drop_queue_view(gid):
    """Remove every entry for this gid (the hook and the poller may both queue it)."""
    _queue_view[:] = [i for i in _queue_view if i.get("gid") != str(gid)]


def _propagate_torrent_folder(gid, task):
    """Copy the chosen folder from a metadata gid to its real download gids.

    A magnet runs twice in aria2: the metadata stub keeps the gid returned when
    the task was added, then the actual download gets a brand new gid. Without
    this the folder choice would be lost for everything downloaded via magnet.
    """
    children = (task or {}).get("followedBy") or []
    if not children:
        return []
    folder_id = _torrent_folder_id(gid)
    if not folder_id:
        return []
    inherited = []
    for child in children:
        child_gid = child if isinstance(child, str) else (child or {}).get("gid")
        if not child_gid or child_gid == gid:
            continue
        _remember_torrent_folder(child_gid, folder_id)
        inherited.append(str(child_gid))
    if inherited:
        logger.info(
            "folder %s inherited from gid %s to %s", folder_id, gid, ",".join(inherited)
        )
    return inherited


async def _folder_for_gid(gid):
    """Per task folder, else the global default, else root (None)."""
    remembered = _torrent_folder_id(gid)
    if remembered:
        return remembered

    # A magnet's real download has its own gid; if it finished before the
    # metadata item was processed, walk up "following" to find the folder that
    # was chosen when the task was added.
    try:
        link = await aria2_call("aria2.tellStatus", [gid, ["following", "followedBy"]])
    except Exception as exc:  # noqa: BLE001
        logger.debug("tellStatus(following) failed for %s: %s", gid, exc)
        link = None
    if link:
        parent = link.get("following")
        if parent:
            inherited = _torrent_folder_id(parent)
            if inherited:
                _remember_torrent_folder(gid, inherited)
                logger.info("folder %s adopted from parent gid %s", inherited, parent)
                return inherited
        _propagate_torrent_folder(gid, link)

    spec = load_settings().get("aria2_folder")
    if not spec:
        return None
    async with async_session() as db:
        try:
            return await _resolve_folder_id(db, spec, False)
        except HTTPException:
            logger.warning("aria2 default folder %r not found, uploading to root", spec)
            return None


# ------------------------------------------------------------- aria2 client
async def aria2_call(method, params=None):
    payload = {"jsonrpc": "2.0", "id": "teleplay-up", "method": method}
    call_params = []
    if ARIA2_RPC_SECRET:
        call_params.append("token:" + ARIA2_RPC_SECRET)
    call_params.extend(params or [])
    payload["params"] = call_params
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.post(ARIA2_RPC_URL, json=payload)
        data = resp.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data.get("result")


# ------------------------------------------------------- completion worker
def _load_state():
    global _processed_gids
    try:
        if STATE_FILE.is_file():
            _processed_gids = json.loads(STATE_FILE.read_text() or "{}")
    except Exception:  # noqa: BLE001
        _processed_gids = {}


def _save_state():
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(_processed_gids))
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------- failed upload store
FAILED_FILE = Path(os.environ.get("UP_FAILED_FILE", "/app/session/up-failed.json"))
FAILED_MAX = 100


def _load_failed():
    try:
        if FAILED_FILE.is_file():
            data = json.loads(FAILED_FILE.read_text() or "[]")
            if isinstance(data, list):
                return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot read failed list: %s", exc)
    return []


def _save_failed(items):
    try:
        FAILED_FILE.parent.mkdir(parents=True, exist_ok=True)
        FAILED_FILE.write_text(json.dumps(items[:FAILED_MAX], indent=2))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot write failed list: %s", exc)


def _record_failure(path, error, gid=None, folder_id=None):
    """Keep a failed upload in the retry list (the file itself is left on disk)."""
    p = Path(str(path))
    entry = {
        "id": "%d-%d" % (int(time.time()), abs(hash(str(p))) % 100000),
        "path": str(p),
        "name": p.name,
        "size": p.stat().st_size if p.is_file() else 0,
        "error": str(error)[:300],
        "time": _now(),
        "gid": gid,
        "folder_id": folder_id,
    }
    items = [i for i in _load_failed() if i.get("path") != str(p)]
    items.insert(0, entry)
    _save_failed(items)
    logger.warning("kept failed upload for retry: %s", p.name)
    return entry


def _drop_failure(entry_id):
    items = _load_failed()
    kept = [i for i in items if i.get("id") != entry_id]
    if len(kept) == len(items):
        return None
    removed = [i for i in items if i.get("id") == entry_id][0]
    _save_failed(kept)
    return removed


PADDING_FILE_RE = re.compile(r"_+padding_file_\d+_+", re.IGNORECASE)

# Files that torrent authors add for advertising: never worth storing.
JUNK_EXT_ALWAYS = {".html", ".htm", ".url", ".lnk", ".mht", ".mhtml"}
JUNK_EXT_SMALL = {".txt", ".md", ".nfo", ".log"}
JUNK_SMALL_BYTES = 512 * 1024
JUNK_NAME_RE = re.compile(
    r"(扫码|最新情報|最新情报|網址發布|网址发布|发布器|加群|广告|廣告|sukebei|1024-APP)",
    re.IGNORECASE,
)


# Short advertising clips that torrent authors bundle with the real content:
#   "台湾uu美少女直播 20年信誉保证服务全球.mp4"  13.8 MB
#   "宇宙最丰富完整.mp4"                        25.8 MB
#   "社 區 最 新 情 報.mp4"                     15.1 MB
# The marker check ignores spaces and only applies to small files, so normal
# content that merely mentions 直播 (e.g. a 47 MB themed PDF) is kept.
AD_NAME_RE = re.compile(
    r"(最丰富|最豐富|最新情報|最新情报|信誉保证|信譽保證|服务全球|服務全球|"
    r"网址发布|網址發布|扫码下载|掃碼下載)",
    re.IGNORECASE,
)
AD_MAX_BYTES = 100 * 1024 * 1024


def _is_ad_clip(name, size=None):
    if size is not None and int(size) > AD_MAX_BYTES:
        return False
    compact = re.sub(r"\s+", "", str(name or ""))
    return bool(AD_NAME_RE.search(compact))


def _is_junk_name(name, size=None):
    """True for padding files, ad pages and other meaningless extras.

    Name markers only apply to small files: real videos are often named after
    the release site (odnbt.com@xxx.mp4, javdb.com@xxx.mp4) and must be kept.
    """
    raw = str(name or "")
    lower = raw.lower()
    ext = os.path.splitext(lower)[1]
    if PADDING_FILE_RE.search(raw):
        return True
    if ext in JUNK_EXT_ALWAYS:
        return True
    small = size is None or int(size) <= JUNK_SMALL_BYTES
    if ext in JUNK_EXT_SMALL and small:
        return True
    if small and JUNK_NAME_RE.search(raw):
        return True
    if _is_ad_clip(raw, size):
        return True
    return False


def _is_junk_upload(path):
    """Padding files, ad files and empty files must never reach telegram."""
    p = Path(str(path))
    try:
        size = p.stat().st_size
    except OSError:
        size = None
    if size == 0:
        return True
    return _is_junk_name(p.name, size)


ARIA2_MIN_VIDEO_BYTES = int(os.environ.get("UP_ARIA2_MIN_VIDEO_BYTES", str(30 * 1024 * 1024)))
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts",
                    ".m4v", ".webm", ".mpg", ".mpeg", ".rmvb", ".rm"}


def _is_aria2_small_video(path):
    """Torrent bundles almost always attach tiny ad clips as separate videos."""
    p = Path(str(path))
    try:
        size = p.stat().st_size
    except OSError:
        return False
    if size <= 0 or size >= ARIA2_MIN_VIDEO_BYTES:
        return False
    return p.suffix.lower() in VIDEO_EXTENSIONS


async def _upload_completed_task(task):
    gid = task.get("gid")
    files = task.get("files") or []
    paths = [Path(item.get("path") or "") for item in files]
    def _skip_aria2(path):
        """aria2 only: junk files plus videos below the ad-clip threshold."""
        return _is_junk_upload(path) or _is_aria2_small_video(path)

    # drop junk (and tiny ad videos) from disk, never upload them
    for item in files:
        candidate = Path(item.get("path") or "")
        if candidate.is_file() and _skip_aria2(candidate):
            reason = "junk" if _is_junk_upload(candidate) else "small video"
            try:
                candidate.unlink()
                logger.info("removed %s file %s", reason, candidate.name)
            except OSError:
                pass
    paths = [p for p in paths if p.is_file() and not _skip_aria2(p)]
    if not paths:
        _processed_gids[gid] = {"status": "no-file", "time": _now()}
        _save_state()
        return

    pushed, failed = [], []
    folder_name = await _folder_name_for_gid(gid)
    target_folder = await _folder_for_gid(gid)
    for candidate in paths:
        lane = _lane_for_size(candidate.stat().st_size)
        try:
            digest = None
            try:
                digest = sha256_of_file(candidate)
            except OSError:
                digest = None
            if digest:
                duplicate = await hash_sync.find_by_hash(digest)
                if duplicate is not None:
                    logger.info("skip duplicate %s (already id=%s)", candidate.name, duplicate.id)
                    try:
                        candidate.unlink()
                    except OSError:
                        pass
                    pushed.append({
                        "path": str(candidate), "duplicate": True,
                        "existing_id": duplicate.id, "sha256": digest,
                    })
                    continue
            _start_active(lane, candidate.name, candidate.stat().st_size, folder_name)
            res = await upload_file_auto(
                str(candidate), folder_id=target_folder, delete_after=True,
                progress_cb=lambda cur, tot, _l=lane: _track_progress(_l, cur, tot),
            )
            pushed.append({
                "path": str(candidate),
                "parts": res["parts"],
                "file_ids": [f.get("id") for f in res["files"]],
                "name": (res["files"][0].get("file_name") if res["files"] else candidate.name),
            })
        except Exception as exc:  # noqa: BLE001
            logger.error("upload failed for %s: %s", candidate, exc)
            failed.append({"path": str(candidate), "error": str(exc)[:300]})
            _record_failure(candidate, exc, gid=gid, folder_id=target_folder)
        finally:
            _stop_active(lane)

    if not failed:
        try:
            await aria2_call("aria2.removeDownloadResult", [gid])
        except Exception as exc:  # noqa: BLE001
            logger.warning("removeDownloadResult failed for %s: %s", gid, exc)
        _forget_torrent_folder(gid)
        _processed_gids[gid] = {"status": "done", "time": _now(), "files": pushed}
    else:
        _processed_gids[gid] = {"status": "partial", "time": _now(), "files": pushed, "failed": failed}
    _save_state()


async def handle_gid(gid):
    if not gid or gid in _processed_gids:
        return {"ok": False, "reason": "already handled"}
    try:
        task = await aria2_call(
            "aria2.tellStatus",
            [gid, ["gid", "status", "files", "totalLength", "completedLength",
                   "followedBy", "following"]],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("tellStatus failed for %s: %s", gid, exc)
        return {"ok": False, "reason": "aria2 unavailable"}
    if not task:
        return {"ok": False, "reason": "not found"}
    _propagate_torrent_folder(gid, task)
    if task.get("status") != "complete":
        return {"ok": False, "reason": "not complete"}
    queued = await enqueue_upload(gid)
    return {"ok": True, "queued": queued}


async def poll_once():
    active = await aria2_call("aria2.tellActive") or []
    waiting = await aria2_call("aria2.tellWaiting", [0, 100]) or []
    stopped = await aria2_call("aria2.tellStopped", [0, 100]) or []
    for task in list(active) + list(waiting) + list(stopped):
        _propagate_torrent_folder(task.get("gid"), task)
        if task.get("status") != "complete":
            continue
        gid = task.get("gid")
        if not gid:
            continue
        await enqueue_upload(gid)


async def _reconcile_loop():
    _load_state()
    logger.info("aria2 reconcile loop started (rpc=%s)", ARIA2_RPC_URL)
    tick = 0
    sweep_tick = 0
    while True:
        await asyncio.sleep(5)
        sweep_tick += 5
        if sweep_tick >= 600:
            sweep_tick = 0
            try:
                _sweep_tmp_uploads()
            except Exception as exc:  # noqa: BLE001
                logger.debug("tmp sweep failed: %s", exc)
        interval = current_reconcile_seconds()
        if interval <= 0:
            tick = 0
            continue
        tick += 5
        if tick < interval:
            continue
        tick = 0
        try:
            await poll_once()
        except Exception as exc:  # noqa: BLE001
            logger.debug("aria2 reconcile failed: %s", exc)


_upload_queue = None
_queued_gids = set()
_queue_task = None
UPLOAD_QUEUE_RETRIES = int(os.environ.get("UP_QUEUE_RETRIES", "2"))


_queues = {lane: None for lane in LANES}
_queue_seq = 0


def _next_seq():
    global _queue_seq
    _queue_seq += 1
    return _queue_seq


def _get_queue(lane):
    if _queues.get(lane) is None:
        _queues[lane] = asyncio.Queue()
    return _queues[lane]


def _is_metadata_task(task):
    """True for the magnet metadata stub (no real files, nothing to upload)."""
    if not task:
        return False
    files = task.get("files") or []
    for f in files:
        name = os.path.basename(f.get("path") or "")
        if name.startswith("[METADATA]"):
            return True
    info = ((task or {}).get("bittorrent") or {}).get("info") or {}
    if not info and (task or {}).get("bittorrent") is not None:
        return True
    return False


def _will_upload(path_str, length):
    """Would this aria2 file actually be uploaded? (mirrors the completion filter)"""
    name = os.path.basename(path_str or "")
    try:
        size = int(length or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return False
    if _is_junk_name(name, size):
        return False
    if size < ARIA2_MIN_VIDEO_BYTES and Path(name).suffix.lower() in VIDEO_EXTENSIONS:
        return False
    return True


async def enqueue_upload(gid):
    """Put a completed gid on the matching upload lane (deduplicated)."""
    if not gid:
        return False
    if gid in _processed_gids or gid in _queued_gids:
        return False

    try:
        probe = await aria2_call(
            "aria2.tellStatus", [gid, ["files", "bittorrent", "totalLength"]]
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("enqueue probe failed for %s: %s", gid, exc)
        probe = None
    if _is_metadata_task(probe):
        _processed_gids[str(gid)] = {"status": "metadata-skip", "time": _now()}
        _save_state()
        logger.info("skipped metadata stub gid=%s", gid)
        return False

    task = probe or {}
    files = task.get("files") or []
    real_files = [f for f in files if _will_upload(f.get("path"), f.get("length"))]
    name, size = "", 0
    if real_files:
        # use the biggest file for the label: files[0] is often a tiny ad clip
        biggest = max(real_files, key=lambda f: int(f.get("length") or 0))
        name = os.path.basename(biggest.get("path") or "")
        if len(real_files) > 1:
            name += "（共 %d 个文件）" % len(real_files)
        size = sum(int(f.get("length") or 0) for f in real_files)
    if not size:
        size = int(task.get("totalLength") or 0)
    if not name:
        info = (task.get("bittorrent") or {}).get("info") or {}
        name = info.get("name") or ""

    lane = _lane_for_size(size)
    _queued_gids.add(gid)
    await _get_queue(lane).put((_next_seq(), str(gid)))
    if not _queued_entry(gid):
        _queue_view.append({
            "gid": str(gid),
            "name": name or str(gid),
            "size": size,
            "folder": await _folder_name_for_gid(gid),
            "lane": lane,
        })
    logger.info(
        "queued upload gid=%s lane=%s (small=%d big=%d)",
        gid, lane, _get_queue("small").qsize(), _get_queue("big").qsize(),
    )
    return True


async def _process_queued(gid, lane):
    """Upload one queued task, retrying the whole task on partial failure."""
    attempts = 0
    while True:
        attempts += 1
        try:
            task = await aria2_call(
                "aria2.tellStatus",
                [gid, ["gid", "status", "files", "totalLength", "completedLength",
                       "followedBy", "following"]],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("queued gid %s: tellStatus failed (%s)", gid, exc)
            task = None
        if not task:
            logger.warning("queued gid %s not found in aria2, dropping", gid)
            return
        _propagate_torrent_folder(gid, task)
        if task.get("status") != "complete":
            logger.info("queued gid %s status=%s, dropping", gid, task.get("status"))
            return
        if gid in _processed_gids:
            return

        _drop_queue_view(gid)
        await _upload_completed_task(task)
        status = (_processed_gids.get(gid) or {}).get("status")
        if status == "partial" and attempts <= UPLOAD_QUEUE_RETRIES:
            wait = 10 * attempts
            logger.warning(
                "gid %s partially uploaded, retrying in %ss (attempt %d/%d)",
                gid, wait, attempts, UPLOAD_QUEUE_RETRIES + 1,
            )
            await asyncio.sleep(wait)
            _processed_gids.pop(gid, None)
            continue
        return


FILE_JOBS_FILE = Path(os.environ.get("UP_FILE_JOBS_FILE", "/app/session/up-file-jobs.json"))


def _load_file_jobs():
    try:
        if FILE_JOBS_FILE.is_file():
            data = json.loads(FILE_JOBS_FILE.read_text() or "[]")
            if isinstance(data, list):
                return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot read file jobs: %s", exc)
    return []


def _save_file_jobs(items):
    try:
        FILE_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE_JOBS_FILE.write_text(json.dumps(items, indent=2))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot write file jobs: %s", exc)


def _remember_file_job(path, folder_id, size):
    items = [x for x in _load_file_jobs() if x.get("path") != str(path)]
    items.append({"path": str(path), "folder_id": folder_id, "size": int(size or 0)})
    _save_file_jobs(items)


def _forget_file_job(path):
    items = _load_file_jobs()
    keep = [x for x in items if x.get("path") != str(path)]
    if len(keep) != len(items):
        _save_file_jobs(keep)


async def enqueue_file_upload(path, folder_id, size=0, sha256=None):
    """Queue a local file for the telegram upload worker (returns immediately)."""
    p = Path(str(path))
    key = "file:" + str(p)
    folder = await _folder_name_by_id(folder_id)
    lane = _lane_for_size(size)
    _queue_view.append({
        "gid": key,
        "name": p.name,
        "size": int(size or 0),
        "folder": folder,
        "lane": lane,
    })
    _queued_gids.add(key)
    _active_uploads.add(str(p))
    _remember_file_job(p, folder_id, size)
    if sha256:
        _inflight_hashes[sha256] = key
    await _get_queue(lane).put((_next_seq(), {
        "path": str(p),
        "name": p.name,
        "folder_id": folder_id,
        "key": key,
        "lane": lane,
        "sha256": sha256,
    }))
    logger.info("queued file %s (%.1f MB) for telegram upload", p.name, (size or 0) / 1048576.0)
    return key


async def _process_file(item, lane):
    """Upload one queued local file (from the API or the web page)."""
    path = Path(str(item.get("path") or ""))
    name = item.get("name") or path.name
    folder_id = item.get("folder_id")
    if not path.is_file():
        logger.warning("queued file %s vanished, skipping", name)
        return
    folder_name = await _folder_name_by_id(folder_id)
    _start_active(lane, name, path.stat().st_size, folder_name)
    try:
        await upload_file_auto(
            str(path), folder_id=folder_id, delete_after=True,
            progress_cb=lambda cur, tot, _l=lane: _track_progress(_l, cur, tot),
            sha256=item.get("sha256"),
        )
        logger.info("queued file %s uploaded to telegram", name)
    except Exception as exc:  # noqa: BLE001
        logger.error("queued file %s failed: %s", name, exc)
        _record_failure(path, exc, folder_id=folder_id)
    finally:
        _stop_active(lane)
        _active_uploads.discard(str(path))
        _forget_file_job(path)


async def _lane_worker(lane):
    queue = _get_queue(lane)
    logger.info("upload worker started (lane=%s, serial)", lane)
    while True:
        entry = await queue.get()
        item = entry[1] if isinstance(entry, tuple) and len(entry) == 2 else entry
        key = ""
        try:
            if isinstance(item, dict):
                key = str(item.get("key") or ("file:" + str(item.get("path") or "")))
                await _process_file(item, lane)
            else:
                key = str(item)
                await _process_queued(key, lane)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("queue item %s crashed: %s", key, exc)
        finally:
            _queued_gids.discard(key)
            # The view mirrors the backlog: once an item leaves the queue for any
            # reason (uploaded, not complete, error) it must leave the view too.
            _drop_queue_view(key)
            queue.task_done()


_queue_health = {"stall": {lane: 0 for lane in LANES}}
_lane_tasks = {}


async def _queue_watchdog():
    """Restart a stalled lane worker (telegram can block forever)."""
    while True:
        await asyncio.sleep(30)
        try:
            for lane in LANES:
                queue = _get_queue(lane)
                pending = queue.qsize()
                idle = not _upload_states[lane].get("active")
                if pending > 0 and idle:
                    _queue_health["stall"][lane] += 1
                    logger.warning(
                        "lane %s stalled: %d pending, no active upload (%d/2)",
                        lane, pending, _queue_health["stall"][lane],
                    )
                    if _queue_health["stall"][lane] >= 2:
                        _queue_health["stall"][lane] = 0
                        old = _lane_tasks.get(lane)
                        if old is not None and not old.done():
                            old.cancel()
                            try:
                                await old
                            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                                pass
                        _lane_tasks[lane] = asyncio.create_task(_lane_worker(lane))
                        logger.warning("restarted the stalled lane worker (%s)", lane)
                else:
                    _queue_health["stall"][lane] = 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("queue watchdog error: %s", exc)


async def _sync_loop():
    """Periodic telegram <-> database reconciliation (interval from settings)."""
    tick = 0
    while True:
        await asyncio.sleep(5)
        interval = current_sync_seconds()
        if interval <= 0:
            tick = 0
            continue
        tick += 5
        if tick < interval:
            continue
        tick = 0
        try:
            await hash_sync.sync_once()
        except Exception as exc:  # noqa: BLE001
            logger.debug("sync loop error: %s", exc)


async def _initial_poll(delay=8):
    """Recover work lost while the service was down (restart/deploy)."""
    await asyncio.sleep(delay)
    try:
        _load_state()
        await poll_once()

        # complete aria2 tasks whose files are still on disk: force a re-queue
        recovered = 0
        stopped = await aria2_call("aria2.tellStopped", [0, 200]) or []
        for task in stopped:
            if task.get("status") != "complete":
                continue
            files = task.get("files") or []
            alive = [
                f for f in files
                if Path(f.get("path") or "").is_file() and not _is_junk_upload(f.get("path"))
            ]
            if not alive:
                continue
            gid = str(task.get("gid"))
            _processed_gids.pop(gid, None)
            if await enqueue_upload(gid):
                recovered += 1

        # queued api/web uploads that were persisted before the restart
        for job in _load_file_jobs():
            path = Path(str(job.get("path") or ""))
            if path.is_file():
                await enqueue_file_upload(path, job.get("folder_id"), job.get("size"))
            else:
                _forget_file_job(path)

        logger.info("initial scan done (aria2 re-queued: %d, file jobs re-queued: %d)",
                    recovered, len(_load_file_jobs()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("initial scan failed: %s", exc)


def start_worker():
    global _poller_task
    if _poller_task is None or _poller_task.done():
        _poller_task = asyncio.create_task(_reconcile_loop())
        asyncio.create_task(_initial_poll())
        asyncio.create_task(_queue_watchdog())
        asyncio.create_task(_sync_loop())
        if os.environ.get("UP_AUTOSTART_HASH_BACKFILL", "1") == "1":
            async def _resume_backfill():
                await asyncio.sleep(20)
                try:
                    async with async_session() as db:
                        pending = (await db.execute(
                            select(StoredFile).where(StoredFile.sha256.is_(None)).limit(1)
                        )).scalar_one_or_none()
                    if pending is not None:
                        await _hash_backfill()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("backfill autostart skipped: %s", exc)
            asyncio.create_task(_resume_backfill())
    for lane in LANES:
        task = _lane_tasks.get(lane)
        if task is None or task.done():
            _lane_tasks[lane] = asyncio.create_task(_lane_worker(lane))
    return _poller_task


async def stop_worker():
    global _poller_task
    for lane in list(_lane_tasks):
        task = _lane_tasks.pop(lane, None)
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    if _poller_task is not None:
        _poller_task.cancel()
        try:
            await _poller_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _poller_task = None


# ------------------------------------------------------------------- routes

REL_SEG_RE = re.compile(r"^[^/\\:*?\"<>|]{1,200}$")


def _rel_dir_segments(relpath):
    """Split a client relative path into its directory segments (filename dropped)."""
    raw = str(relpath or "").replace("\\", "/").strip()
    parts = [p for p in raw.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=400, detail="relpath must not contain '..'")
    if len(parts) > 32:
        raise HTTPException(status_code=400, detail="relpath is too deep")
    dirs = parts[:-1] if len(parts) > 1 else []
    for seg in dirs:
        if not REL_SEG_RE.match(seg):
            raise HTTPException(status_code=400, detail="invalid folder name in relpath: %s" % seg)
    return dirs


async def _ensure_relpath(db, base_folder_id, relpath):
    """Create (or reuse) the folder chain for a dropped folder and return its id."""
    dirs = _rel_dir_segments(relpath)
    if not dirs:
        return base_folder_id
    user = await resolve_db_user(db)
    parent = base_folder_id
    for name in dirs:
        stmt = select(Folder).where(Folder.name == name, Folder.user_id == user.id)
        stmt = stmt.where(Folder.parent_id == parent) if parent is not None else stmt.where(Folder.parent_id.is_(None))
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = Folder(user_id=user.id, parent_id=parent, name=name[:255])
            db.add(row)
            await db.flush()
            logger.info("created subfolder %s under %s", row.name, parent)
        parent = row.id
    await db.commit()
    return parent


async def _resolve_folder_id(db, spec, create=False):
    """Turn a folder id or a folder name into a folder id (None = root)."""
    if spec is None or not str(spec).strip():
        return None
    spec = str(spec).strip()
    user = await resolve_db_user(db)
    if spec.isdigit():
        res = await db.execute(
            select(Folder).where(Folder.id == int(spec), Folder.user_id == user.id)
        )
        folder = res.scalar_one_or_none()
        if folder:
            return folder.id
        raise HTTPException(status_code=404, detail="folder id %s not found" % spec)
    res = await db.execute(
        select(Folder).where(Folder.name == spec, Folder.user_id == user.id)
    )
    folder = res.scalar_one_or_none()
    if folder:
        return folder.id
    if create:
        folder = Folder(user_id=user.id, name=spec[:255])
        db.add(folder)
        await db.commit()
        await db.refresh(folder)
        logger.info("created folder %s (%s) on upload", folder.name, folder.id)
        return folder.id
    raise HTTPException(
        status_code=404,
        detail="folder '%s' not found; pass create_folder=1 to create it" % spec,
    )


@router.get("")
@router.get("/")
async def up_page():
    page = Path(__file__).resolve().parent.parent / "static" / "up.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="up.html missing")
    return FileResponse(
        str(page),
        media_type="text/html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/api/status")
async def up_status(token: str = Depends(require_session)):
    try:
        stat = await aria2_call("aria2.getGlobalStat") or {}
        version = await aria2_call("aria2.getVersion") or {}
        reachable = True
    except Exception as exc:  # noqa: BLE001
        stat, version, reachable = {}, {}, False
        logger.debug("aria2 status unavailable: %s", exc)

    usage = shutil.disk_usage(str(DOWNLOAD_DIR) if DOWNLOAD_DIR.exists() else "/")
    return {
        "aria2": reachable,
        "version": (version or {}).get("version"),
        "download_speed": int(stat.get("downloadSpeed") or 0),
        "upload_speed": int(stat.get("uploadSpeed") or 0),
        "active": int(stat.get("numActive") or 0),
        "waiting": int(stat.get("numWaiting") or 0),
        "stopped": int(stat.get("numStopped") or 0),
        "disk": {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "downloads_bytes": _dir_size(DOWNLOAD_DIR) if DOWNLOAD_DIR.exists() else 0,
        },
        "limits": {"max_upload_bytes": MAX_UPLOAD_BYTES},
        "upload_queue": {
            "pending": sum(_get_queue(lane).qsize() for lane in LANES),
            "busy": sum(1 for lane in LANES if _upload_states[lane].get("active")),
            "lanes": {
                lane: {
                    "pending": _get_queue(lane).qsize(),
                    "busy": bool(_upload_states[lane].get("active")),
                }
                for lane in LANES
            },
        },
    }


@router.get("/api/server")
async def up_server(token: str = Depends(require_session)):
    """Host metrics written by the teleplay-status service on the host."""
    if not STATUS_FILE.is_file():
        return {"available": False, "reason": "collector not installed"}
    try:
        data = json.loads(STATUS_FILE.read_text() or "{}")
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": "unreadable: %s" % exc}
    try:
        ts = datetime.fromisoformat(str(data.get("ts", "")).replace("Z", ""))
        age = (datetime.utcnow() - ts).total_seconds()
    except Exception:  # noqa: BLE001
        age = 9999
    data["available"] = age <= STATUS_MAX_AGE
    data["age"] = int(age)
    return data


@router.post("/api/hooks/complete")
async def hook_complete(payload: dict, x_hook_secret: str = Header(default=None)):
    if UP_HOOK_SECRET and x_hook_secret != UP_HOOK_SECRET:
        raise HTTPException(status_code=403, detail="bad hook secret")
    gid = (payload or {}).get("gid")
    if not gid:
        raise HTTPException(status_code=400, detail="gid required")
    queued = await enqueue_upload(gid)
    logger.info("download completion hook received for gid=%s (queued=%s)", gid, queued)
    return {"ok": True, "gid": gid, "queued": queued}


@router.get("/api/settings")
async def up_get_settings(token: str = Depends(require_session)):
    return {
        "reconcile_seconds": current_reconcile_seconds(),
        "default_reconcile_seconds": DEFAULT_RECONCILE_SECONDS,
        "choices": RECONCILE_CHOICES,
        "aria2_folder": load_settings().get("aria2_folder") or "",
        "sync_interval_seconds": current_sync_seconds(),
        "sync_choices": SYNC_CHOICES,
        "direct_upload_url": os.environ.get("UP_DIRECT_URL", "http://13.54.30.110/up"),
    }


@router.post("/api/settings")
async def up_set_settings(payload: dict, token: str = Depends(require_session)):
    raw = (payload or {}).get("reconcile_seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="reconcile_seconds must be an integer")
    if value < 0 or value > 86400:
        raise HTTPException(status_code=400, detail="reconcile_seconds must be between 0 and 86400")
    data = load_settings()
    data["reconcile_seconds"] = value
    body = payload or {}
    if "aria2_folder" in body:
        data["aria2_folder"] = str(body.get("aria2_folder") or "").strip()
    if "sync_interval_seconds" in body:
        try:
            sync_value = int(body.get("sync_interval_seconds"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="sync_interval_seconds must be an integer")
        if sync_value < 0 or sync_value > 30 * 24 * 3600:
            raise HTTPException(status_code=400, detail="sync interval must be between 0 and 30 days")
        data["sync_interval_seconds"] = sync_value
    save_settings(data)
    logger.info("settings updated: reconcile=%ss aria2_folder=%r", value, data.get("aria2_folder"))
    return {
        "ok": True,
        "reconcile_seconds": value,
        "aria2_folder": data.get("aria2_folder") or "",
        "sync_interval_seconds": int(data.get("sync_interval_seconds", DEFAULT_SYNC_SECONDS)),
    }


@router.post("/api/torrent")
async def up_add_torrent(
    payload: dict,
    token: str = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    targets = _extract_targets((payload or {}).get("url"))
    if not targets:
        raise HTTPException(status_code=400, detail="url is required")
    for target in targets:
        if not (
            target.startswith("magnet:")
            or target.startswith("http://")
            or target.startswith("https://")
        ):
            raise HTTPException(
                status_code=400,
                detail="only magnet, a 40-char info hash, or an http(s) link is supported",
            )
    folder_id = await _resolve_folder_id(
        db, (payload or {}).get("folder"), bool((payload or {}).get("create_folder"))
    )
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    gids = []
    for target in targets:
        try:
            gid = await aria2_call("aria2.addUri", [[target], {"dir": str(DOWNLOAD_DIR)}])
        except Exception as exc:  # noqa: BLE001
            logger.warning("aria2 rejected %s: %s", target[:80], exc)
            if not gids:
                raise HTTPException(status_code=502, detail="aria2 error: " + str(exc)[:200])
            continue
        _remember_torrent_folder(gid, folder_id)
        gids.append(gid)
    if not gids:
        raise HTTPException(status_code=502, detail="no task was accepted by aria2")
    return {
        "ok": True,
        "gid": gids[0],
        "gids": gids,
        "count": len(gids),
        "folder_id": folder_id,
    }


@router.get("/api/tasks")
async def up_tasks(token: str = Depends(require_session), db: AsyncSession = Depends(get_db)):
    try:
        active = await aria2_call("aria2.tellActive") or []
        waiting = await aria2_call("aria2.tellWaiting", [0, 50]) or []
        stopped = await aria2_call("aria2.tellStopped", [0, 50]) or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="aria2 error: " + str(exc)[:200])

    folder_names = {}
    try:
        rows = await db.execute(select(Folder))
        for f in rows.scalars().all():
            folder_names[f.id] = f.name
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot list folders for task labels: %s", exc)

    default_spec = (load_settings().get("aria2_folder") or "").strip()
    default_label = ""
    if default_spec:
        if default_spec.isdigit() and int(default_spec) in folder_names:
            default_label = folder_names[int(default_spec)]
        else:
            default_label = default_spec

    assigned = _load_torrent_folders()

    def folder_label(gid):
        raw = assigned.get(str(gid))
        if raw:
            try:
                fid = int(raw)
            except (TypeError, ValueError):
                return str(raw)
            return folder_names.get(fid, "#%d" % fid)
        if default_label:
            return default_label + "（默认）"
        return "根目录"

    def task_name(task, files):
        """任务名用目录名（多文件 torrent），而不是第一个文件名。"""
        bt_name = ((task.get("bittorrent") or {}).get("info") or {}).get("name") or ""
        bt_name = str(bt_name).strip()
        if bt_name:
            return bt_name
        paths = [f.get("path") or "" for f in files if f.get("path")]
        if not paths:
            return task.get("gid")
        if len(paths) == 1:
            base = os.path.basename(paths[0])
            if base.startswith("[METADATA]") or len(base) >= 24 and all(c in "0123456789abcdef" for c in base.lower()):
                return "磁力链接"
            return base
        try:
            common = os.path.commonpath(paths)
        except ValueError:
            common = ""
        base = os.path.basename(common) if common else ""
        if base:
            return base
        dirs = {os.path.dirname(p) for p in paths}
        if len(dirs) == 1:
            return os.path.basename(dirs.pop()) or task.get("gid")
        return "（%d 个文件）" % len(paths)

    def shape(task, bucket):
        files = task.get("files") or []
        name = task_name(task, files)
        total = int(task.get("totalLength") or 0)
        done = int(task.get("completedLength") or 0)
        return {
            "gid": task.get("gid"),
            "bucket": bucket,
            "status": task.get("status"),
            "name": name,
            "total": total,
            "done": done,
            "percent": round(done * 100.0 / total, 1) if total else 0,
            "speed": int(task.get("downloadSpeed") or 0),
            "connections": int(task.get("connections") or 0),
            "error": task.get("errorMessage") or "",
            "files": len(files),
            "folder": folder_label(task.get("gid")),
        }

    items = [shape(t, "active") for t in active]
    items += [shape(t, "waiting") for t in waiting]
    items += [shape(t, "stopped") for t in stopped]
    return {"tasks": items}


@router.get("/api/task/{gid}/files")
async def up_task_files(gid: str, token: str = Depends(require_session)):
    """Per-file list of an aria2 task (to pick finished files manually)."""
    try:
        task = await aria2_call("aria2.tellStatus", [gid, ["gid", "status", "dir", "files"]])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="aria2 error: %s" % str(exc)[:200])
    if not task or not task.get("gid"):
        raise HTTPException(status_code=404, detail="task not found")
    items = []
    for f in task.get("files") or []:
        path = f.get("path") or ""
        length = int(f.get("length") or 0)
        done = int(f.get("completedLength") or 0)
        items.append({
            "index": int(f.get("index", -1)),
            "name": os.path.basename(path),
            "path": path,
            "length": length,
            "done": done,
            "complete": bool(length) and done >= length,
            "selected": (f.get("selected") == "true"),
        })
    return {"gid": gid, "status": task.get("status"), "dir": task.get("dir"), "files": items}


async def _manual_upload_file(path: Path, folder_id, folder_label):
    lane = _lane_for_size(path.stat().st_size)
    try:
        _start_active(lane, path.name, path.stat().st_size, folder_label)
        res = await upload_file_auto(
            str(path), folder_id=folder_id, delete_after=False,
            progress_cb=lambda cur, tot, _l=lane: _track_progress(_l, cur, tot),
        )
        name = (res.get("files") or [{}])[0].get("file_name") or path.name
        logger.info("manual upload finished: %s -> %s", path.name, name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("manual upload failed: %s (%s)", path.name, exc)
    finally:
        _stop_active(lane)


@router.post("/api/task/{gid}/upload-file")
async def up_task_upload_file(gid: str, payload: dict,
                              token: str = Depends(require_session),
                              db: AsyncSession = Depends(get_db)):
    """Upload one already-finished file of a task, leaving the task untouched."""
    index = int(payload.get("index", -1))
    folder_spec = payload.get("folder")
    try:
        task = await aria2_call("aria2.tellStatus", [gid, ["files", "dir", "status"]])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="aria2 error: %s" % str(exc)[:200])
    target = None
    for f in (task or {}).get("files") or []:
        if int(f.get("index", -1)) == index:
            target = f
            break
    if target is None:
        raise HTTPException(status_code=404, detail="file not found in task")
    length = int(target.get("length") or 0)
    done = int(target.get("completedLength") or 0)
    if not length or done < length:
        raise HTTPException(status_code=400, detail="该文件还没下载完成")
    path = Path(target.get("path") or "")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在：%s" % path)
    folder_id = await _resolve_folder_id(db, folder_spec, True) if folder_spec else await _folder_for_gid(gid)
    folder_label = await _folder_name_by_id(folder_id)
    asyncio.create_task(_manual_upload_file(path, folder_id, folder_label))
    return {"ok": True, "queued": True, "name": path.name, "folder": folder_label}


@router.post("/api/task/{gid}/{action}")
async def up_task_action(gid: str, action: str, token: str = Depends(require_session)):
    mapping = {
        "pause": "aria2.pause",
        "unpause": "aria2.unpause",
        "remove": "aria2.forceRemove",
        "forget": "aria2.removeDownloadResult",
    }
    method = mapping.get(action)
    if not method:
        raise HTTPException(status_code=400, detail="unsupported action")
    try:
        await aria2_call(method, [gid])
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:200]
        logger.warning("aria2 %s failed for %s: %s", method, gid, message)

    # Removal is idempotent: a finished/removed task cannot be force-removed, so
    # fall back to dropping the download result and finally report success.
    if action == "remove":
        try:
            await aria2_call("aria2.removeDownloadResult", [gid])
            return {"ok": True, "note": "removed download result"}
        except Exception as exc:  # noqa: BLE001
            logger.info("removeDownloadResult also failed for %s: %s", gid, exc)
        return {"ok": True, "note": "already gone"}
    if action == "forget":
        return {"ok": True, "note": "already gone"}
    if action == "unpause":
        return {"ok": True, "note": "nothing to resume"}
    raise HTTPException(status_code=502, detail="aria2 error: " + message)


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _claimed_hash(value):
    """Normalise a client supplied sha256 header, or None."""
    candidate = (value or "").strip().lower()
    return candidate if HEX64_RE.match(candidate) else None


async def _duplicate_response(sha256):
    existing = await hash_sync.find_by_hash(sha256)
    if existing is None:
        if sha256 in _inflight_hashes:
            return {
                "ok": True, "duplicate": True, "sha256": sha256, "pending": True,
                "message": "该文件已在上传队列中，无需重复上传",
            }
        return None
    return {
        "ok": True,
        "duplicate": True,
        "sha256": sha256,
        "existing": {
            "id": existing.id,
            "file_name": existing.file_name,
            "file_size": existing.file_size,
            "folder_id": existing.folder_id,
        },
        "message": "该文件已存在",
    }


@router.post("/api/upload")
async def up_upload(
    file: UploadFile = File(...),
    folder: str = Query(default=None, description="folder id or name; empty = root"),
    create_folder: int = Query(default=0, description="1 = create the folder if missing"),
    wait: int = Query(default=0, description="1 = block until telegram upload finishes"),
    relpath: str = Query(default=None, description="relative path of a dropped folder"),
    x_content_sha256: str = Header(default=None, description="client computed sha256"),
    token: str = Depends(require_write_auth),
    db: AsyncSession = Depends(get_db),
):
    folder_id = await _resolve_folder_id(db, folder, bool(create_folder))
    if relpath:
        folder_id = await _ensure_relpath(db, folder_id, relpath)
    claimed = _claimed_hash(x_content_sha256)
    if claimed:
        duplicate = await _duplicate_response(claimed)
        if duplicate is not None:
            return duplicate
    UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "upload.bin").name
    target = UPLOAD_TMP_DIR / safe_name
    written = 0
    _active_uploads.add(str(target))
    finished = False
    try:
        with open(target, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if MAX_UPLOAD_BYTES and written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="file too large")
                out.write(chunk)
        digest = claimed
        if not digest:
            try:
                digest = sha256_of_file(target)
            except OSError:
                digest = None
        if digest and not claimed:
            existing = await hash_sync.find_by_hash(digest)
            if existing is not None:
                finished = True
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                return {
                    "ok": True,
                    "duplicate": True,
                    "sha256": digest,
                    "existing": {
                        "id": existing.id,
                        "file_name": existing.file_name,
                        "file_size": existing.file_size,
                        "folder_id": existing.folder_id,
                    },
                    "message": "该文件已存在",
                }
        if wait:
            _wait_lane = _lane_for_size(written)
            _start_active(_wait_lane, target.name, written, await _folder_name_by_id(folder_id))
            try:
                res = await upload_file_auto(
                    str(target), folder_id=folder_id, delete_after=True,
                    progress_cb=lambda cur, tot, _l=_wait_lane: _track_progress(_l, cur, tot),
                )
            finally:
                _stop_active(_wait_lane)
            finished = True
            return {
                "ok": True,
                "parts": res["parts"],
                "file": (res["files"][0] if res["files"] else None),
                "files": res["files"],
            }
        queue_key = await enqueue_file_upload(target, folder_id, written, sha256=digest)
        finished = True
        return {
            "ok": True,
            "queued": True,
            "bytes": written,
            "name": target.name,
            "folder_id": folder_id,
            "queue_key": queue_key,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="upload failed: " + str(exc)[:300])
    finally:
        if not finished:
            _active_uploads.discard(str(target))
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass



# ------------------------------------------------------- resumable uploads
RESUME_DIR = UPLOAD_TMP_DIR / ".resume"
RESUME_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@router.get("/api/upload/resume")
async def up_upload_resume(key: str = Query(...), token: str = Depends(require_write_auth)):
    """How many bytes of this upload session does the server already have?"""
    if not RESUME_KEY_RE.match(key or ""):
        raise HTTPException(status_code=400, detail="invalid key")
    part = RESUME_DIR / (key + ".part")
    received = part.stat().st_size if part.is_file() else 0
    return {"ok": True, "key": key, "received": received, "exists": part.is_file()}


async def _finish_resumed_upload(target, folder_id, written, digest, claimed, wait):
    """Same tail as a one-shot upload: dedupe then queue (or upload now)."""
    if digest and not claimed:
        existing = await hash_sync.find_by_hash(digest)
        if existing is not None:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return {
                "ok": True, "duplicate": True, "sha256": digest,
                "existing": {
                    "id": existing.id, "file_name": existing.file_name,
                    "file_size": existing.file_size, "folder_id": existing.folder_id,
                },
                "message": "该文件已存在",
            }
    if wait:
        lane = _lane_for_size(written)
        _start_active(lane, target.name, written, await _folder_name_by_id(folder_id))
        try:
            res = await upload_file_auto(
                str(target), folder_id=folder_id, delete_after=True,
                progress_cb=lambda cur, tot, _l=lane: _track_progress(_l, cur, tot),
                sha256=digest,
            )
        finally:
            _stop_active(lane)
        return {
            "ok": True, "bytes": written, "parts": res["parts"],
            "file": (res["files"][0] if res["files"] else None),
            "files": res["files"],
        }
    queue_key = await enqueue_file_upload(target, folder_id, written, sha256=digest)
    return {
        "ok": True, "queued": True, "bytes": written, "name": target.name,
        "folder_id": folder_id, "queue_key": queue_key,
    }


async def _handle_resume(raw_request, key, offset, size, name, folder_id, claimed, wait):
    if not RESUME_KEY_RE.match(key or ""):
        raise HTTPException(status_code=400, detail="invalid key")
    if size <= 0:
        raise HTTPException(status_code=400, detail="size is required for resumable uploads")
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    part = RESUME_DIR / (key + ".part")
    current = part.stat().st_size if part.is_file() else 0
    if offset > current:
        raise HTTPException(
            status_code=409,
            detail={"message": "offset ahead of server", "received": current},
        )
    if offset < current:
        with open(part, "r+b") as fh:
            fh.truncate(offset)
        current = offset
    written = current
    with open(part, "ab") as out:
        async for chunk in raw_request.stream():
            if not chunk:
                continue
            written += len(chunk)
            if written > size:
                raise HTTPException(status_code=400, detail="more bytes than the declared size")
            out.write(chunk)
    if written < size:
        return {"ok": True, "partial": True, "received": written, "size": size}

    # assembled: move into the normal temp dir and hand it to the queue
    safe_name = Path(name).name or "upload.bin"
    target = UPLOAD_TMP_DIR / safe_name
    try:
        part.replace(target)
    except OSError:
        shutil.copyfile(part, target)
        part.unlink(missing_ok=True)
    digest = claimed
    if not digest:
        try:
            digest = sha256_of_file(target)
        except OSError:
            digest = None
    logger.info("resumed upload finished: %s (%d bytes)", safe_name, written)
    return await _finish_resumed_upload(target, folder_id, written, digest, claimed, wait)


@router.put("/api/upload/raw")
async def up_upload_raw(
    request: Request,
    name: str = Query(..., description="file name"),
    folder: str = Query(default=None, description="folder id or name; empty = root"),
    create_folder: int = Query(default=0, description="1 = create the folder if missing"),
    wait: int = Query(default=0, description="1 = block until telegram upload finishes"),
    check_only: int = Query(default=0, description="1 = only check the hash, no body needed"),
    relpath: str = Query(default=None, description="relative path of a dropped folder, e.g. A/B/C.mp4"),
    key: str = Query(default=None, description="resume session key: enables resumable upload"),
    offset: int = Query(default=0, description="resume: byte offset to continue from"),
    size: int = Query(default=0, description="resume: total file size"),
    x_content_sha256: str = Header(default=None, description="client computed sha256 (enables pre-check)"),
    token: str = Depends(require_write_auth),
    db: AsyncSession = Depends(get_db),
):
    folder_id = await _resolve_folder_id(db, folder, bool(create_folder))
    if relpath:
        folder_id = await _ensure_relpath(db, folder_id, relpath)
    """Stream a raw request body straight to Telegram (no multipart overhead)."""
    # Pre-check: reject a duplicate before a single byte of the body is read.
    claimed = _claimed_hash(x_content_sha256)
    if check_only:
        if not claimed:
            raise HTTPException(status_code=400, detail="check_only requires X-Content-SHA256")
        duplicate = await _duplicate_response(claimed)
        return duplicate or {"ok": True, "duplicate": False, "sha256": claimed}
    if claimed:
        duplicate = await _duplicate_response(claimed)
        if duplicate is not None:
            logger.info("pre-check hit for %s (sha256=%s)", name, claimed[:12])
            return duplicate
    if key:
        return await _handle_resume(request, key, offset, size, name, folder_id, claimed, bool(wait))
    UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(name).name or "upload.bin"
    target = UPLOAD_TMP_DIR / safe_name
    written = 0
    _active_uploads.add(str(target))
    finished = False
    try:
        with open(target, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if MAX_UPLOAD_BYTES and written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="file too large")
                out.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="empty body")
        digest = claimed
        if not digest:
            try:
                digest = sha256_of_file(target)
            except OSError:
                digest = None
        if digest and not claimed:
            existing = await hash_sync.find_by_hash(digest)
            if existing is not None:
                finished = True
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                return {
                    "ok": True,
                    "duplicate": True,
                    "sha256": digest,
                    "existing": {
                        "id": existing.id,
                        "file_name": existing.file_name,
                        "file_size": existing.file_size,
                        "folder_id": existing.folder_id,
                    },
                    "message": "该文件已存在",
                }
        if wait:
            _wait_lane = _lane_for_size(written)
            _start_active(_wait_lane, target.name, written, await _folder_name_by_id(folder_id))
            try:
                res = await upload_file_auto(
                    str(target), folder_id=folder_id, delete_after=True,
                    progress_cb=lambda cur, tot, _l=_wait_lane: _track_progress(_l, cur, tot),
                )
            finally:
                _stop_active(_wait_lane)
            finished = True
            return {
                "ok": True,
                "bytes": written,
                "parts": res["parts"],
                "file": (res["files"][0] if res["files"] else None),
                "files": res["files"],
            }
        queue_key = await enqueue_file_upload(target, folder_id, written, sha256=digest)
        finished = True
        return {
            "ok": True,
            "queued": True,
            "bytes": written,
            "name": target.name,
            "folder_id": folder_id,
            "queue_key": queue_key,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="upload failed: " + str(exc)[:300])
    finally:
        if not finished:
            _active_uploads.discard(str(target))
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass



@router.get("/api/folders")
async def up_folders(token: str = Depends(require_write_auth), db: AsyncSession = Depends(get_db)):
    """List folders so scripts can pick a target."""
    user = await resolve_db_user(db)
    res = await db.execute(select(Folder).where(Folder.user_id == user.id).order_by(Folder.name))
    return {
        "folders": [
            {"id": f.id, "name": f.name, "parent_id": f.parent_id}
            for f in res.scalars().all()
        ]
    }


@router.get("/api/keys")
async def list_keys(token: str = Depends(require_session)):
    return {"keys": up_auth.list_api_keys()}


@router.post("/api/keys")
async def create_key(payload: dict, token: str = Depends(require_session)):
    label = ((payload or {}).get("label") or "api").strip()[:40]
    new_key = up_auth.create_api_key(label)
    return {"ok": True, "key": new_key, "label": label}


@router.delete("/api/keys/{key_id}")
async def delete_key(key_id: str, token: str = Depends(require_session)):
    if not up_auth.revoke_api_key(key_id):
        raise HTTPException(status_code=404, detail="key not found")
    return {"ok": True}

# ------------------------------------------------------------- content hashes
HASH_BACKFILL = {"running": False, "done": 0, "failed": 0, "total": 0, "current": "", "started": None}


async def _hash_backfill(limit=0, folder_id=None):
    """Compute sha256 for files already stored in telegram (streams, no temp file)."""
    from ..streaming import stream_file as _stream
    from ..telegram import get_message_from_channel, tg_client

    HASH_BACKFILL.update({
        "running": True, "done": 0, "failed": 0, "total": 0, "current": "", "started": _now(),
    })
    try:
        async with async_session() as db:
            stmt = select(StoredFile).where(StoredFile.sha256.is_(None)).order_by(StoredFile.file_size)
            if folder_id:
                stmt = stmt.where(StoredFile.folder_id == int(folder_id))
            rows = (await db.execute(stmt)).scalars().all()
        if limit:
            rows = rows[:limit]
        HASH_BACKFILL["total"] = len(rows)
        logger.info("hash backfill started for %d file(s)", len(rows))

        for file in rows:
            # let uploads win: pause while either lane is busy
            while any(_upload_states[lane]["active"] for lane in _upload_states):
                await asyncio.sleep(5)
            HASH_BACKFILL["current"] = file.file_name
            try:
                message = await get_message_from_channel(file.channel_message_id)
                if message is None:
                    raise RuntimeError("message not found")
                digest = hashlib.sha256()
                last = max(0, int(file.file_size) - 1)
                async for chunk in _stream(tg_client, message, 0, last):
                    digest.update(chunk)
                async with async_session() as db:
                    await db.execute(
                        update(StoredFile).where(StoredFile.id == file.id).values(sha256=digest.hexdigest())
                    )
                    await db.commit()
                HASH_BACKFILL["done"] += 1
            except Exception as exc:  # noqa: BLE001
                HASH_BACKFILL["failed"] += 1
                logger.warning("hash backfill failed for %s: %s", file.file_name, exc)
    finally:
        HASH_BACKFILL["running"] = False
        HASH_BACKFILL["current"] = ""
        logger.info(
            "hash backfill finished: done=%s failed=%s total=%s",
            HASH_BACKFILL["done"], HASH_BACKFILL["failed"], HASH_BACKFILL["total"],
        )


@router.post("/api/hash/backfill")
async def up_hash_backfill(
    limit: int = Query(default=0),
    folder: int = Query(default=0, description="only hash files in this folder id"),
    token: str = Depends(require_session),
):
    """Start (or check) the sha256 backfill for files already in telegram."""
    if HASH_BACKFILL.get("running"):
        return {"ok": True, "started": False, "status": dict(HASH_BACKFILL)}
    asyncio.create_task(_hash_backfill(limit, folder or None))
    return {"ok": True, "started": True, "status": dict(HASH_BACKFILL)}


@router.get("/api/hash/status")
async def up_hash_status(token: str = Depends(require_session)):
    return dict(HASH_BACKFILL)


@router.post("/api/sync/run")
async def up_sync_run(token: str = Depends(require_session)):
    """Run one reconciliation pass now."""
    if hash_sync.STATUS.get("running"):
        return {"ok": True, "started": False, "status": dict(hash_sync.STATUS)}
    asyncio.create_task(hash_sync.sync_once())
    return {"ok": True, "started": True, "status": dict(hash_sync.STATUS)}


@router.get("/api/sync/status")
async def up_sync_status(token: str = Depends(require_session)):
    return dict(hash_sync.STATUS)


@router.post("/api/upload/stop")
async def up_upload_stop(lane: str = Query(default="all"), token: str = Depends(require_session)):
    """Abort the upload currently running on a lane (or all lanes)."""
    if lane in ("all", "", None):
        _upload_stop["small"] = True
        _upload_stop["big"] = True
    elif lane in _upload_stop:
        _upload_stop[lane] = True
    else:
        raise HTTPException(status_code=400, detail="lane must be small, big or all")
    logger.warning("upload stop requested (lane=%s)", lane)
    return {"ok": True, "stop": dict(_upload_stop)}


@router.get("/api/queue")
async def up_queue(token: str = Depends(require_session)):
    """Live view of both telegram upload lanes."""
    pending = [i for i in _queue_view if i.get("gid") not in _processed_gids]
    if len(pending) != len(_queue_view):
        _queue_view[:] = pending

    actives = []
    for lane in LANES:
        state = _upload_states[lane]
        if state.get("active"):
            item = dict(state)
            item["lane"] = lane
            actives.append(item)

    return {
        "active": actives[0] if actives else None,
        "actives": actives,
        "pending": list(pending),
        "pending_count": len(pending),
        "lanes": {
            lane: {
                "pending": _get_queue(lane).qsize(),
                "busy": bool(_upload_states[lane].get("active")),
                "limit_bytes": LANE_SPLIT_BYTES,
            }
            for lane in LANES
        },
    }


@router.get("/api/failed")
async def up_failed(token: str = Depends(require_session)):
    items = []
    for entry in _load_failed():
        item = dict(entry)
        p = Path(str(entry.get("path") or ""))
        item["exists"] = p.is_file()
        item["size"] = p.stat().st_size if p.is_file() else 0
        items.append(item)
    return {"items": items}


@router.post("/api/failed/retry")
async def up_failed_retry(payload: dict, token: str = Depends(require_session)):
    target_id = (payload or {}).get("id")
    todo = [i for i in _load_failed() if not target_id or i.get("id") == target_id]
    if not todo:
        raise HTTPException(status_code=404, detail="nothing to retry")
    results = []
    for entry in todo:
        p = Path(str(entry.get("path") or ""))
        if not p.is_file():
            results.append({"id": entry.get("id"), "ok": False, "error": "file missing"})
            continue
        try:
            res = await upload_file_auto(
                str(p), folder_id=entry.get("folder_id"), delete_after=True
            )
            _drop_failure(entry.get("id"))
            results.append({
                "id": entry.get("id"),
                "ok": True,
                "file_ids": [f.get("id") for f in res["files"]],
            })
            logger.info("retry succeeded for %s", p.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("retry failed for %s: %s", p.name, exc)
            _record_failure(p, exc, folder_id=entry.get("folder_id"))
            results.append({"id": entry.get("id"), "ok": False, "error": str(exc)[:200]})
    return {"ok": True, "results": results}


@router.delete("/api/failed/{entry_id}")
async def up_failed_delete(
    entry_id: str,
    purge: int = Query(default=1, description="1 = also delete the file on disk"),
    token: str = Depends(require_session),
):
    removed = _drop_failure(entry_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="entry not found")
    if purge:
        p = Path(str(removed.get("path") or ""))
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001
            logger.warning("could not delete %s: %s", p, exc)
    return {"ok": True}


@router.get("/api/processed")
async def up_processed(limit: int = 20, token: str = Depends(require_session),
                       db: AsyncSession = Depends(get_db)):
    """最近入库的文件（直接查库，覆盖网页/API/aria2 所有上传途径）。"""
    rows = await db.execute(
        select(StoredFile, Folder)
        .outerjoin(Folder, Folder.id == StoredFile.folder_id)
        .order_by(StoredFile.created_at.desc())
        .limit(max(1, min(limit, 100)))
    )
    items = []
    for f, folder in rows.all():
        items.append({
            "status": "done",
            "name": f.file_name,
            "size": f.file_size,
            "time": f.created_at.strftime("%Y-%m-%d %H:%M:%S") if f.created_at else "",
            "folder": folder.name if folder else "根目录",
            "files": [{"id": f.id, "name": f.file_name, "size": f.file_size}],
        })
    return {"items": items}
