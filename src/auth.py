"""
auth.py — OAuth credential handling for the Claude Usage widget.

Two credential sources, in priority order:
  1. The widget's OWN store (~/.config/claude-usage-widget/auth.json) — created
     by the in-widget "Authorize" login. The widget refreshes this itself, so it
     keeps working even when Claude Code isn't running. This is what lets a
     friend use the app standalone.
  2. Claude Code's credentials (~/.claude/.credentials.json) — used READ-ONLY as
     a fallback for people who already have Claude Code. We never refresh or
     rewrite these (refreshing rotates the refresh token and would break Claude
     Code's own login).

get_token() returns (access_token, status) where status is one of:
  "ok"       — have a valid token
  "expired"  — have credentials but the token is dead and we can't refresh it
  "offline"  — refresh attempt failed due to a network error
  "no_auth"  — no credentials at all (friend hasn't logged in)

Token refresh is serialized across processes with an flock: the GUI widget and
statusd both call get_token(), and the refresh grant ROTATES the refresh token,
so two concurrent refreshes would leave the loser with a dead token.

OAuth endpoints / client_id were taken from the Claude Code bundle. This uses
the public Claude Code OAuth client; each user authorizes their OWN account to
view their OWN usage.
"""

import os
import json
import time
import base64
import fcntl
import hashlib
import secrets
import urllib.request
import urllib.error
import urllib.parse

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = "user:profile user:inference"

STORE_DIR = os.path.expanduser("~/.config/claude-usage-widget")
STORE_PATH = os.path.join(STORE_DIR, "auth.json")
LOCK_PATH = os.path.join(STORE_DIR, ".refresh.lock")
CLAUDE_CREDS = os.path.expanduser("~/.claude/.credentials.json")

_SKEW = 120  # treat token as expired this many seconds early

# which credential source the last get_token() actually used; source() reports
# this so the footer matches reality (e.g. widget login dead -> CC fallback)
_last_source = None


# ----------------------------- storage --------------------------------------
def _ensure_store_dir() -> None:
    os.makedirs(STORE_DIR, mode=0o700, exist_ok=True)
    try:  # tighten dirs created by older versions with the default umask
        os.chmod(STORE_DIR, 0o700)
    except OSError:
        pass


def _read_store() -> dict | None:
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _write_store(d: dict) -> None:
    _ensure_store_dir()
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE_PATH)


def logout() -> None:
    global _last_source
    _last_source = None  # next get_token() re-resolves the source
    try:
        os.remove(STORE_PATH)
    except OSError:
        pass


# An access token can be revoked server-side (password change, deauthorized
# device) while still locally unexpired — without help, get_token() would
# keep serving the same dead token for up to an hour before the refresh /
# Claude Code fallback path ever runs. invalidate() is rate-limited so that
# a server-side problem that 401s VALID tokens can't put us in a refresh
# loop (each refresh rotates the token — that would be a 30s rotation storm).
_INVALIDATE_MIN_INTERVAL = 600
_last_invalidate = 0.0


def invalidate(token: str) -> None:
    """The API rejected `token` (401/403) though it looked locally valid:
    expire the widget store now so the next get_token() goes through refresh
    and, if the grant is dead too, the Claude Code fallback. Claude Code's
    own credentials are never touched (read-only — CC refreshes them)."""
    global _last_invalidate
    now = time.time()
    if now - _last_invalidate < _INVALIDATE_MIN_INTERVAL:
        return
    _last_invalidate = now
    try:
        _ensure_store_dir()
        # same lock as _refresh: otherwise we could overwrite a token that a
        # concurrent refresh just rotated. Inside the lock, only expire the
        # store if it still holds the rejected token.
        with open(LOCK_PATH, "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            store = _read_store()
            if store and store.get("access_token") == token:
                store["expires_at"] = 0
                _write_store(store)
    except OSError:
        pass


def have_widget_login() -> bool:
    return _read_store() is not None


# ----------------------------- http helpers ---------------------------------
def _post_token(payload: dict) -> tuple[dict | None, str | None]:
    """POST to the token endpoint. Returns (response, err) where err is None
    on success, "http" when the server rejected the grant (dead/revoked
    token, bad code), and "net" for network problems (offline, DNS, timeout).
    """
    data = json.dumps(payload).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "claude-usage-widget",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        # Only a definitive 4xx means the grant itself was rejected. 429 and
        # 5xx are transient server states, not a verdict on the credentials —
        # treating them as "http" would flash a false "session expired".
        if e.code == 429 or e.code >= 500:
            return None, "net"
        return None, "http"
    except (urllib.error.URLError, OSError, ValueError):
        return None, "net"


def _store_from_token_response(resp: dict, fallback_refresh=None) -> dict:
    return {
        "access_token": resp["access_token"],
        "refresh_token": resp.get("refresh_token") or fallback_refresh,
        "expires_at": time.time() + int(resp.get("expires_in", 3600)),
        "scope": resp.get("scope", SCOPES),
    }


# ----------------------------- token access ---------------------------------
def _refresh(store: dict) -> tuple[dict | None, str | None]:
    """Refresh the widget's own token, serialized across processes.

    Holds an exclusive flock for the duration: if the GUI and statusd both
    notice expiry at the same moment, the second one waits, then re-reads the
    store and finds the fresh token instead of re-spending the (rotated)
    refresh token. Returns (new_store, err) — err as in _post_token.
    """
    _ensure_store_dir()
    with open(LOCK_PATH, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        # someone may have refreshed while we waited for the lock
        cur = _read_store() or store
        if (cur.get("access_token")
                and time.time() < cur.get("expires_at", 0) - _SKEW):
            return cur, None
        if cur.get("refresh_dead"):
            return None, "http"
        rt = cur.get("refresh_token") or store.get("refresh_token")
        if not rt:
            return None, "http"
        resp, err = _post_token({
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": CLIENT_ID,
        })
        if not resp or "access_token" not in resp:
            if err != "net":
                # the grant was definitively rejected — flag the store so we
                # (and statusd) stop re-POSTing a dead refresh token on every
                # retry cycle; re-login replaces the store and clears this
                cur["refresh_dead"] = True
                _write_store(cur)
            return None, (err or "http")
        new = _store_from_token_response(resp, fallback_refresh=rt)
        _write_store(new)
        return new, None


def _claude_code_token() -> tuple[str | None, str]:
    """Claude Code's credentials, READ-ONLY (never refreshed/rewritten)."""
    try:
        with open(CLAUDE_CREDS, "r", encoding="utf-8") as fh:
            cc = json.load(fh)["claudeAiOauth"]
        if time.time() < cc.get("expiresAt", 0) / 1000.0 - _SKEW:
            return cc["accessToken"], "ok"
        return None, "expired"
    except (OSError, KeyError, json.JSONDecodeError):
        return None, "no_auth"


def get_token():
    """Return (access_token | None, status)."""
    global _last_source
    store = _read_store()
    if store and store.get("access_token"):
        _last_source = "widget"
        if time.time() < store.get("expires_at", 0) - _SKEW:
            return store["access_token"], "ok"
        if store.get("refresh_token") and not store.get("refresh_dead"):
            new, err = _refresh(store)
            if new:
                return new["access_token"], "ok"
            if err == "net":
                # transient network problem, not a dead login — report offline
                # so the UI keeps cached data instead of the re-auth banner
                return None, "offline"
        # widget login is dead — fall back to Claude Code creds if usable
        tok, st = _claude_code_token()
        if tok:
            _last_source = "claude-code"
            return tok, "ok"
        return None, "expired"

    tok, st = _claude_code_token()
    _last_source = "claude-code" if st != "no_auth" else "none"
    return tok, st


def source() -> str:
    """Which credential source is active: 'widget', 'claude-code', or 'none'.
    Reflects what the last get_token() actually used; before the first call it
    falls back to a static guess."""
    if _last_source is not None:
        return _last_source
    if have_widget_login():
        return "widget"
    if os.path.exists(CLAUDE_CREDS):
        return "claude-code"
    return "none"


# ----------------------------- login flow -----------------------------------
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def begin_login() -> dict:
    """Start a PKCE login. Returns {url, verifier, state}; open `url` in a
    browser, let the user authorize, then pass the displayed code to
    finish_login()."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(32))
    params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    return {"url": url, "verifier": verifier, "state": state}


def finish_login(code: str, verifier: str, state: str) -> tuple[bool, str]:
    """Exchange the pasted authorization code for tokens. The manual flow shows
    the code as `CODE#STATE`; we split it and verify the state matches the one
    we generated (CSRF guard). Returns (ok, message)."""
    code = (code or "").strip()
    if not code:
        return False, "empty code"
    recv_state = state
    if "#" in code:
        code, recv_state = code.split("#", 1)
        if recv_state != state:
            return False, ("code doesn't match this login attempt — "
                           "use the newest login window and try again")
    resp, err = _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "state": recv_state,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })
    if not resp or "access_token" not in resp:
        if err == "net":
            return False, "network error — check your connection and retry"
        return False, "exchange failed (check the code and try again)"
    _write_store(_store_from_token_response(resp))
    return True, "connected"
