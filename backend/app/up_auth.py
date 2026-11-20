"""Standalone password auth for the /up upload hub.

Deliberately independent from the main TelePlay site: a single password plus a
security question, hashed with PBKDF2, with server side session tokens. State
lives in /app/session (the telegram_session volume) so it survives rebuilds.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_DIR = Path(os.environ.get("UP_AUTH_DIR", "/app/session"))
AUTH_FILE = STATE_DIR / "up-auth.json"
SESSION_FILE = STATE_DIR / "up-sessions.json"

PBKDF2_ROUNDS = 200_000
SESSION_TTL_HOURS = int(os.environ.get("UP_SESSION_HOURS", "720"))  # 30 days
MAX_FAILURES = 5
LOCK_SECONDS = 60

_lock = threading.Lock()
_failures = {"count": 0, "until": 0.0}


# --------------------------------------------------------------- primitives
def _hash_secret(secret, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return {
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(digest).decode(),
        "rounds": PBKDF2_ROUNDS,
    }


def _verify_secret(secret, record):
    if not record or not record.get("salt") or not record.get("hash"):
        return False
    try:
        salt = base64.b64decode(record["salt"])
        rounds = int(record.get("rounds") or PBKDF2_ROUNDS)
        digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(digest, base64.b64decode(record["hash"]))
    except Exception:  # noqa: BLE001
        return False


def _read_json(path, default):
    try:
        if path.is_file():
            data = json.loads(path.read_text() or "{}")
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot read %s: %s", path, exc)
    return default


def _write_json(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.error("cannot write %s: %s", path, exc)
        raise


# ------------------------------------------------------------------- state
def load_auth():
    return _read_json(AUTH_FILE, {})


def is_configured():
    data = load_auth()
    return bool(data.get("password")) and bool(data.get("question"))


def setup(password, question, answer):
    """Create the initial password + security question."""
    with _lock:
        data = load_auth()
        if data.get("password"):
            raise ValueError("already configured")
        if len(password or "") < 6:
            raise ValueError("password must be at least 6 characters")
        if not question or not answer:
            raise ValueError("security question and answer are required")
        _write_json(
            AUTH_FILE,
            {
                "password": _hash_secret(password),
                "question": question.strip(),
                "answer": _hash_secret(answer.strip().lower()),
                "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            },
        )
        return True


def lock_remaining():
    with _lock:
        remaining = _failures["until"] - time.time()
        return int(remaining) if remaining > 0 else 0


def _register_failure():
    with _lock:
        _failures["count"] += 1
        if _failures["count"] >= MAX_FAILURES:
            _failures["until"] = time.time() + LOCK_SECONDS
            _failures["count"] = 0


def _reset_failures():
    with _lock:
        _failures["count"] = 0
        _failures["until"] = 0.0


def verify_password(password):
    wait = lock_remaining()
    if wait:
        raise ValueError("too many attempts, retry in %ds" % wait)
    data = load_auth()
    ok = _verify_secret(password or "", data.get("password"))
    if not ok:
        _register_failure()
    else:
        _reset_failures()
    return ok


def verify_answer(answer):
    wait = lock_remaining()
    if wait:
        raise ValueError("too many attempts, retry in %ds" % wait)
    data = load_auth()
    ok = _verify_secret((answer or "").strip().lower(), data.get("answer"))
    if not ok:
        _register_failure()
    else:
        _reset_failures()
    return ok


def change_password(old_password, answer, new_password):
    """Change the password: requires BOTH the old password and the security answer."""
    if not verify_password(old_password):
        raise ValueError("old password incorrect")
    if not verify_answer(answer):
        raise ValueError("security answer incorrect")
    if len(new_password or "") < 6:
        raise ValueError("new password must be at least 6 characters")
    with _lock:
        data = load_auth()
        data["password"] = _hash_secret(new_password)
        data["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        _write_json(AUTH_FILE, data)
    revoke_all_sessions()
    return True


def reset_password(answer, new_password):
    """Forgot-password path: the security answer alone is enough."""
    if not verify_answer(answer):
        raise ValueError("security answer incorrect")
    if len(new_password or "") < 6:
        raise ValueError("new password must be at least 6 characters")
    with _lock:
        data = load_auth()
        data["password"] = _hash_secret(new_password)
        data["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        _write_json(AUTH_FILE, data)
    revoke_all_sessions()
    return True


# ---------------------------------------------------------------- sessions
def _load_sessions():
    return _read_json(SESSION_FILE, {})


def _save_sessions(data):
    _write_json(SESSION_FILE, data)


def create_session():
    token = secrets.token_urlsafe(32)
    with _lock:
        sessions = _load_sessions()
        now = datetime.utcnow()
        # drop expired entries
        alive = {}
        for key, meta in sessions.items():
            try:
                exp = datetime.fromisoformat((meta or {}).get("expires", ""))
            except Exception:  # noqa: BLE001
                continue
            if exp > now:
                alive[key] = meta
        expires = now + timedelta(hours=SESSION_TTL_HOURS)
        alive[token] = {"expires": expires.isoformat(), "created": now.isoformat()}
        _save_sessions(alive)
    return token, expires.isoformat()


def check_session(token):
    if not token:
        return False
    with _lock:
        sessions = _load_sessions()
        meta = sessions.get(token)
        if not meta:
            return False
        try:
            exp = datetime.fromisoformat(meta.get("expires", ""))
        except Exception:  # noqa: BLE001
            return False
        if exp <= datetime.utcnow():
            sessions.pop(token, None)
            _save_sessions(sessions)
            return False
        return True


def revoke_session(token):
    with _lock:
        sessions = _load_sessions()
        if token in sessions:
            sessions.pop(token, None)
            _save_sessions(sessions)


def revoke_all_sessions():
    with _lock:
        _save_sessions({})

# ----------------------------------------------------------------- API keys
APIKEY_FILE = STATE_DIR / "up-apikeys.json"


def _hash_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def list_api_keys():
    data = _read_json(APIKEY_FILE, {})
    keys = data.get("keys") or []
    return [
        {
            "id": k.get("id"),
            "label": k.get("label"),
            "created": k.get("created"),
            "last_used": k.get("last_used"),
        }
        for k in keys
    ]


def create_api_key(label):
    """Create a machine token. The plaintext is returned once and never stored."""
    key = "tp_" + secrets.token_hex(16)
    with _lock:
        data = _read_json(APIKEY_FILE, {})
        keys = data.get("keys") or []
        keys.append(
            {
                "id": secrets.token_hex(6),
                "label": (label or "api")[:40],
                "hash": _hash_key(key),
                "created": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "last_used": None,
            }
        )
        data["keys"] = keys
        _write_json(APIKEY_FILE, data)
    return key


def verify_api_key(key):
    if not key:
        return False
    digest = _hash_key(key)
    with _lock:
        data = _read_json(APIKEY_FILE, {})
        keys = data.get("keys") or []
        for k in keys:
            stored = k.get("hash") or ""
            if stored and hmac.compare_digest(stored, digest):
                k["last_used"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
                _write_json(APIKEY_FILE, data)
                return True
    return False


def revoke_api_key(key_id):
    with _lock:
        data = _read_json(APIKEY_FILE, {})
        keys = data.get("keys") or []
        kept = [k for k in keys if k.get("id") != key_id]
        if len(kept) == len(keys):
            return False
        data["keys"] = kept
        _write_json(APIKEY_FILE, data)
    return True
