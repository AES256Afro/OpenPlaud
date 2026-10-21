"""Pieces shared by OpenPlaud and Plaud2Claude: one Plaud session, the headless Plaud sign-in,
page passwords and the same-origin check. Importing this module starts nothing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import plaud_ai as pa

PLAUD_CALLBACK = "http://127.0.0.1:8199/auth/callback"


class PlaudSession:
    """One Plaud MCP process shared by every request, used by one caller at a time."""

    def __init__(self, list_days: int = 30):
        self.lock = threading.Lock()
        self.list_days = list_days
        self._plaud: pa.Plaud | None = None
        self._user: tuple[float, str | None] = (0.0, None)
        self._recordings: tuple[float, list[dict]] = (0.0, [])

    def get(self) -> pa.Plaud:
        if self._plaud is None or not self._plaud.alive():
            self._plaud = pa.Plaud(pa.Plaud.default_command(pa.load_config()))
        return self._plaud

    def forget(self) -> None:
        self._user = (0.0, None)
        self._recordings = (0.0, [])

    def cached_user(self) -> str | None:
        return self._user[1]

    def user(self) -> str | None:
        """Who is signed in (cached), or None. Raises TimeoutError while another caller holds the session."""
        stamp, who = self._user
        if time.time() - stamp < 300:
            return who
        if not self.lock.acquire(timeout=5):
            raise TimeoutError
        try:
            info = json.loads(self.get().call("get_current_user"))
            who = info.get("email") or info.get("nickname") or "your Plaud account"
        except pa.NotSignedIn:
            who = None
        except (pa.PlaudError, ValueError, AttributeError):
            return who  # a passing error: keep what we knew rather than showing "signed out" for 5 minutes
        finally:
            self.lock.release()
        self._user = (time.time(), who)
        return who

    def recordings(self) -> list[dict]:
        stamp, recs = self._recordings
        if time.time() - stamp < 60:
            return recs
        if not self.lock.acquire(timeout=5):
            raise TimeoutError
        try:
            since = (date.today() - timedelta(days=self.list_days)).isoformat()
            recs = pa.list_recordings(self.get(), since, None, None)
        finally:
            self.lock.release()
        recs.sort(key=lambda r: pa.recorded_at(r) or datetime.min, reverse=True)
        self._recordings = (time.time(), recs)
        return recs

    def transcript(self, file_id: str) -> list[str] | None:
        if not self.lock.acquire(timeout=5):
            return None
        try:
            text = pa.fetch_transcript(self.get(), file_id)
            return text.splitlines() if text else []
        except pa.PlaudError:
            return None
        finally:
            self.lock.release()


class PlaudSignIn:
    """Runs Plaud's `login` tool, captures the sign-in link it tries to open, and completes the
    callback from inside the container with the address the owner pastes back."""

    def __init__(self, plaud: PlaudSession, url_file: Path):
        self.plaud = plaud
        self.url_file = url_file
        self.state = {"status": "idle", "url": None, "message": None}
        self.thread: threading.Thread | None = None

    def reset(self) -> None:
        self.state = {"status": "idle", "url": None, "message": None}

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        if self.plaud.lock.locked():
            self.state = {"status": "failed", "url": None,
                          "message": "Plaud is busy with a check right now. Try signing in again when it finishes."}
            return
        self.url_file.unlink(missing_ok=True)
        self.state = {"status": "starting", "url": None, "message": None}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        for _ in range(60):
            if self.url_file.exists() and self.url_file.read_text().strip():
                self.state.update(status="waiting", url=self.url_file.read_text().strip())
                return
            if self.state["status"] in ("success", "failed"):
                return
            time.sleep(0.25)
        if self.state["status"] == "starting":
            self.state.update(status="failed", message="Plaud didn't produce a sign-in link. Try again.")

    def _run(self) -> None:
        with self.plaud.lock:
            try:
                text = self.plaud.get().call("login")
                ok = "Successfully" in text or "Already logged in" in text
                self.state.update(status="success" if ok else "failed", message=text)
            except pa.PlaudError as err:
                self.state.update(status="failed", message=str(err).split("\n")[0])
        self.plaud.forget()

    def complete(self, pasted: str) -> None:
        query = urllib.parse.urlsplit(pasted.strip()).query or pasted.strip().lstrip("?")
        params = urllib.parse.parse_qs(query)
        if "code" not in params or "state" not in params:
            self.state["message"] = "That address has no sign-in code in it. Paste the full address from the browser's address bar."
            return
        if self.state["status"] != "waiting":
            self.state["message"] = "The sign-in expired (Plaud allows 2 minutes). Start again."
            return
        try:
            with urllib.request.urlopen(f"{PLAUD_CALLBACK}?{query}", timeout=30):
                pass
        except urllib.error.URLError as err:
            self.state["message"] = f"Couldn't finish the sign-in: {err.reason}"
            return
        except (http.client.HTTPException, ValueError):
            self.state["message"] = "That address isn't valid. Copy it again from the browser's address bar."
            return
        if self.thread:
            self.thread.join(timeout=30)

    def sign_out(self) -> None:
        with self.plaud.lock:
            try:
                self.plaud.get().call("logout")
            except pa.PlaudError:
                pass
        self.plaud.forget()
        self.reset()

    def page_state(self) -> dict:
        """What the page shows about the Plaud sign-in: signed_in, waiting, busy or signed_out."""
        st = self.state
        out = {"state": "signed_out", "who": None, "url": st.get("url"), "message": st.get("message"), "failed": st["status"] == "failed"}
        if st["status"] in ("starting", "waiting"):
            out["state"] = "waiting" if st.get("url") else "busy"
            return out
        try:
            who = self.plaud.user()
            if who:
                out.update(state="signed_in", who=who)
        except TimeoutError:
            who = self.plaud.cached_user()
            out.update(state="signed_in" if who else "busy", who=who)
        return out


class Passwords:
    """The page's own password and its signed session cookie.

    With a `secret_file`, cookies are signed with a random secret kept in the data folder (made on first use),
    so a copied cookie can't be used to guess the password offline, and `end_all()` (Lock) replaces the
    secret, which signs out every browser at once. Changing the password also ends every session."""

    def __init__(self, password: str, salt: bytes, days: int = 30, secret_file: Path | None = None):
        self.password = password
        self.days = days
        self.salt = salt
        self.secret_file = secret_file
        self.key = self._key()

    def _key(self) -> bytes:
        base = self.salt + self.password.encode()
        if not self.secret_file:
            return hashlib.sha256(base).digest()
        try:
            secret = self.secret_file.read_bytes()
        except FileNotFoundError:
            secret = b""
        if len(secret) < 32:
            secret = self._new_secret()
        return hmac.new(secret, base, "sha256").digest()

    def _new_secret(self) -> bytes:
        secret = os.urandom(32)
        self.secret_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.secret_file.with_name(self.secret_file.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secret)
        os.replace(tmp, self.secret_file)
        return secret

    def end_all(self) -> None:
        """Sign out every browser (only with a secret file; otherwise just this one, by its cookie)."""
        if self.secret_file:
            self._new_secret()
            self.key = self._key()

    _guard = threading.Lock()

    def check(self, given: str) -> bool:
        """Compare a typed password. Wrong guesses are answered one at a time, a second apart, however many
        connections send them, so guessing can't be sped up by sending in parallel."""
        if bool(self.password) and hmac.compare_digest(given.encode(), self.password.encode()):
            return True
        with Passwords._guard:  # only wrong guesses wait in line; the right password never does
            time.sleep(1)
        return False

    def make(self) -> str:
        expires = str(int(time.time()) + self.days * 86400)
        return f"{expires}.{hmac.new(self.key, expires.encode(), 'sha256').hexdigest()}"

    def valid(self, token: str) -> bool:
        if not self.password:
            return True
        expires, _, mac = (token or "").partition(".")
        if not expires.isdigit() or int(expires) < time.time():
            return False
        return hmac.compare_digest(mac, hmac.new(self.key, expires.encode(), "sha256").hexdigest())


def same_origin(headers) -> bool:
    site = headers.get("Sec-Fetch-Site")
    if site:
        return site in ("same-origin", "none")
    origin = headers.get("Origin")
    if not origin:
        return True
    # Behind Tailscale Serve the Host header can be the backend's; the public name is forwarded.
    return urllib.parse.urlsplit(origin).netloc in {headers.get("Host"), headers.get("X-Forwarded-Host")}


def cookie(headers, name: str) -> str | None:
    for part in headers.get("Cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


SAFE_BACK = re.compile(r"/(?![/\\])[A-Za-z0-9/_.~%?=&#+,:@!-]*")


def safe_back(value: str) -> str:
    """A path on this site to return to; anything else (another site, a backslash, a line break) becomes "/"."""
    return value if SAFE_BACK.fullmatch(value or "") else "/"


def host_allowed(headers, extra: str = "") -> bool:
    """Whether the request names this server: an IP address, localhost, a single-label (MagicDNS) name, a
    tailnet name (*.ts.net) or a host listed in `extra` (comma-separated). Refuses DNS-rebinding pages, which
    reach the server under the attacker's own domain name."""
    allowed = {_hostname(h) for h in (extra or os.environ.get("OPENPLAUD_ALLOWED_HOSTS", "")).split(",") if h.strip()}
    for value in (headers.get("Host"), headers.get("X-Forwarded-Host")):
        if value is None:
            continue
        if not _host_ok(_hostname(value), allowed):
            return False
    return True


def _hostname(value: str) -> str:
    try:
        return (urllib.parse.urlsplit("//" + value.strip()).hostname or "").rstrip(".")
    except ValueError:
        return ""


def _host_ok(host: str, allowed: set) -> bool:
    if not host:
        return False
    if host in allowed or host == "localhost" or host.endswith((".ts.net", ".localhost", ".local")) or "." not in host:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def plaud_health(token_file: Path | None = None) -> dict | None:
    """How long the saved Plaud sign-in lasts, read from its dates only (never the token itself).
    The access token renews itself from the refresh token; when the refresh token lapses, sign in again."""
    path = token_file or Path.home() / ".plaud" / "tokens-mcp.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    refresh = data.get("refresh_token") or ""
    try:
        part = refresh.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("exp")
    except (IndexError, ValueError, TypeError):
        exp = None
    if not exp:
        return {"until": None, "days": None, "warn": False}
    until = datetime.fromtimestamp(exp)
    days = (until - datetime.now()).total_seconds() / 86400
    return {"until": until.strftime("%-d %b %Y"), "days": round(days, 1), "warn": days < 3}
