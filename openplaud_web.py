"""OpenPlaud web app: sign in to Plaud, see recordings, summarize them, read the notes.

Settings come from the environment (BoxPilot's app settings): see README.md for the list.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import plaud_ai as pa

PORT = int(os.environ.get("OPENPLAUD_PORT", "8795"))
PASSWORD = os.environ.get("OPENPLAUD_PASSWORD", "")
SYNC_EVERY_MIN = int(os.environ.get("OPENPLAUD_SYNC_EVERY", "60") or 0)
SYNC_DAYS = int(os.environ.get("OPENPLAUD_SYNC_DAYS", "7") or 7)
LIST_DAYS = int(os.environ.get("OPENPLAUD_LIST_DAYS", "30") or 30)
DEFAULT_TASK = os.environ.get("OPENPLAUD_TASK", "summary")
AUTH_URL_FILE = Path(os.environ.get("OPENPLAUD_AUTH_URL_FILE", "/tmp/openplaud-auth-url"))
PLAUD_CALLBACK = "http://127.0.0.1:8199/auth/callback"
COOKIE = "openplaud_session"
SESSION_DAYS = 30
PROVIDER_LABELS = {"claude": "Claude", "chatgpt": "ChatGPT", "local": "Local model"}
TASK_LABELS = {"summary": "Meeting notes", "actions": "Action items", "followup": "Follow-up email"}


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------- Plaud session

class PlaudSession:
    """One Plaud MCP process shared by every request, used by one caller at a time."""

    def __init__(self):
        self.lock = threading.Lock()
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

    def user(self) -> str | None:
        """Who is signed in (cached), or None. Raises TimeoutError while a sign-in holds the session."""
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
        except pa.PlaudError:
            who = None
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
            since = (date.today() - timedelta(days=LIST_DAYS)).isoformat()
            recs = pa.list_recordings(self.get(), since, None, None)
        finally:
            self.lock.release()
        recs.sort(key=lambda r: pa.recorded_at(r) or datetime.min, reverse=True)
        self._recordings = (time.time(), recs)
        return recs


plaud = PlaudSession()


# ---------------------------------------------------------------- sign-in to Plaud (headless OAuth)

class PlaudSignIn:
    """Runs Plaud's `login` tool, captures the sign-in link it tries to open, and completes the
    callback from inside the container with the address the owner pastes back."""

    def __init__(self):
        self.state = {"status": "idle", "url": None, "message": None}
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        AUTH_URL_FILE.unlink(missing_ok=True)
        self.state = {"status": "starting", "url": None, "message": None}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        for _ in range(60):
            if AUTH_URL_FILE.exists() and AUTH_URL_FILE.read_text().strip():
                self.state.update(status="waiting", url=AUTH_URL_FILE.read_text().strip())
                return
            if self.state["status"] in ("success", "failed"):
                return
            time.sleep(0.25)
        if self.state["status"] == "starting":
            self.state.update(status="failed", message="Plaud didn't produce a sign-in link. Try again.")

    def _run(self) -> None:
        with plaud.lock:
            try:
                text = plaud.get().call("login")
                ok = "Successfully" in text or "Already logged in" in text
                self.state.update(status="success" if ok else "failed", message=text)
            except pa.PlaudError as e:
                self.state.update(status="failed", message=str(e).split("\n")[0])
        plaud.forget()

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
        except urllib.error.URLError as e:
            self.state["message"] = f"Couldn't finish the sign-in: {e}"
            return
        if self.thread:
            self.thread.join(timeout=30)


signin = PlaudSignIn()


# ---------------------------------------------------------------- background jobs

class Jobs:
    def __init__(self):
        self.queue: queue.Queue = queue.Queue()
        self.log: deque[str] = deque(maxlen=300)
        self.current: str | None = None
        self.pending: set[tuple] = set()
        self.last_sync: str | None = None
        threading.Thread(target=self._worker, daemon=True).start()
        if SYNC_EVERY_MIN > 0:
            threading.Thread(target=self._scheduler, daemon=True).start()

    def say(self, message: str) -> None:
        self.log.append(f"{now()}  {message}")

    def add(self, job: tuple) -> bool:
        if job in self.pending:
            return False
        self.pending.add(job)
        self.queue.put(job)
        return True

    def _scheduler(self) -> None:
        time.sleep(30)
        while True:
            self.add(("sync",))
            time.sleep(SYNC_EVERY_MIN * 60)

    def _worker(self) -> None:
        while True:
            job = self.queue.get()
            self.current = "Syncing new recordings" if job[0] == "sync" else f"Summarizing {job[1]}"
            try:
                self._run(job)
            except pa.NotSignedIn:
                self.say("Not signed in to Plaud. Sign in from the page to continue.")
                plaud.forget()
            except Exception as e:
                self.say(f"✗ {type(e).__name__}: {e}")
            finally:
                self.pending.discard(job)
                self.current = None
                plaud.forget()

    def _run(self, job: tuple) -> None:
        cfg = pa.load_config()
        backend = pa.make_backend(cfg, cfg["backend"])
        out_dir = pa.notes_dir(cfg)
        with plaud.lock:
            p = plaud.get()
            if job[0] == "sync":
                self.say(f"Sync started (last {SYNC_DAYS} days, {backend.name})")
                written, failures = pa.sync(p, backend, SYNC_DAYS, pa.TASKS[DEFAULT_TASK], DEFAULT_TASK, out_dir, log=self.say)
                self.last_sync = datetime.now().strftime("%Y-%m-%d %H:%M")
                self.say(f"Sync finished: {len(written)} new, {failures} failed")
            else:
                _, file_id, task = job
                rec = pa.unwrap_file(p.call_json("get_file", file_id=file_id))
                pa.process_and_record(p, backend, rec, pa.TASKS[task], task, out_dir, log=self.say)


jobs = Jobs()


# ---------------------------------------------------------------- sessions (the page's own password)

def _session_key() -> bytes:
    return hashlib.sha256(b"openplaud-session:" + PASSWORD.encode()).digest()


def make_session() -> str:
    expires = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{hmac.new(_session_key(), expires.encode(), 'sha256').hexdigest()}"


def valid_session(token: str) -> bool:
    expires, _, mac = token.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(mac, hmac.new(_session_key(), expires.encode(), "sha256").hexdigest())


# ---------------------------------------------------------------- rendering

CSS = """
:root { --bg:#f6f5f2; --panel:#fff; --ink:#1d1d1b; --muted:#6b6a66; --line:#e3e1dc; --accent:#3b5bdb; --ok:#2b8a3e; --bad:#c92a2a; }
@media (prefers-color-scheme: dark) { :root { --bg:#16171a; --panel:#1f2024; --ink:#ececea; --muted:#9a9a96; --line:#2e3036; --accent:#7c94ff; --ok:#69db7c; --bad:#ff8787; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 20px 16px 60px; }
header { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:baseline; justify-content:space-between; margin-bottom:16px; }
h1 { font-size:22px; margin:0; } h2 { font-size:16px; margin:0 0 10px; }
a { color:var(--accent); }
ul.folders { list-style:none; padding:0; margin:0 0 12px; display:flex; flex-wrap:wrap; gap:8px 20px; } td { overflow-wrap:anywhere; } td:first-child { white-space:nowrap; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
.muted { color:var(--muted); } .ok { color:var(--ok); } .bad { color:var(--bad); }
.meta { display:flex; flex-wrap:wrap; gap:6px 18px; font-size:14px; }
button, .button { font:inherit; padding:6px 12px; border-radius:7px; border:1px solid var(--line); background:var(--panel); color:var(--ink); cursor:pointer; text-decoration:none; display:inline-block; }
button.primary, .button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
input[type=text], input[type=password], select { font:inherit; padding:6px 8px; border-radius:7px; border:1px solid var(--line); background:var(--bg); color:var(--ink); }
input[type=text] { width:100%; }
form.inline { display:inline-flex; gap:6px; align-items:center; margin:0; }
table { width:100%; border-collapse:collapse; font-size:14px; }
td, th { text-align:left; padding:7px 6px; border-top:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:500; border-top:none; }
.scroll { overflow-x:auto; }
pre.log { background:var(--bg); border:1px solid var(--line); border-radius:7px; padding:10px; max-height:260px; overflow:auto; white-space:pre-wrap; font-size:13px; margin:0; }
.note h2 { font-size:17px; margin-top:22px; } .note h3 { font-size:15px; } .note li { margin:3px 0; }
.flash { padding:10px 14px; border-radius:8px; border:1px solid var(--line); background:var(--panel); margin-bottom:16px; }
@media (max-width: 640px) {
  table, tbody, tr, td { display:block; width:100%; }
  tr.head { display:none; }
  tr { border-top:1px solid var(--line); padding:10px 0; }
  td { border:none; padding:2px 0; }
  td:first-child { color:var(--muted); font-size:13px; }
  td.hide-sm { display:none; }
}
"""


def page(title: str, body: str, refresh: bool = False) -> str:
    script = """<script>
setTimeout(async function poll(){ try { const r = await fetch('api/status'); const s = await r.json();
 const el = document.getElementById('log'); if (el) el.textContent = s.log.join('\\n');
 if (s.busy || s.signin === 'waiting') { setTimeout(poll, 3000); } else if (document.body.dataset.busy) { location.reload(); } } catch(e) {} }, 3000);
</script>""" if refresh else ""
    busy = ' data-busy="1"' if refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body{busy}><main>{body}</main>{script}</body></html>"""


def inline_md(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def render_markdown(md: str) -> str:
    out, para, in_list = [], [], False

    def flush():
        nonlocal para, in_list
        if para:
            out.append(f"<p>{inline_md(' '.join(para))}</p>")
            para = []
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in md.splitlines():
        line = html.escape(raw.rstrip())
        if not line.strip():
            flush()
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)", line)
        item = re.match(r"^\s*[-*]\s+(\[( |x)\]\s+)?(.*)", line)
        if heading:
            flush()
            level = min(len(heading.group(1)) + 1, 5)
            out.append(f"<h{level}>{inline_md(heading.group(2))}</h{level}>")
        elif item:
            if para:
                out.append(f"<p>{inline_md(' '.join(para))}</p>")
                para = []
            if not in_list:
                out.append("<ul>")
                in_list = True
            box = "" if item.group(1) is None else ("☑ " if item.group(2) == "x" else "☐ ")
            out.append(f"<li>{box}{inline_md(item.group(3))}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            para.append(line)
    flush()
    return "\n".join(out)


def model_label(cfg: dict) -> str:
    provider = cfg["backend"]
    model = {"claude": cfg["claude"]["model"], "chatgpt": cfg["chatgpt"]["model"], "local": cfg["local"]["model"]}.get(provider, "?")
    return f"{PROVIDER_LABELS.get(provider, provider)} · {model}"


# ---------------------------------------------------------------- notes and folders (nothing is ever deleted)

FOLDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}")
PATH_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,199}")


def inside(root: Path, rel: str) -> Path | None:
    """The path `rel` names under `root`, or None when it is malformed or would leave it."""
    rel = rel.strip("/")
    if rel and not all(PATH_PART.fullmatch(part) for part in rel.split("/")):
        return None
    path = (root / rel).resolve()
    return path if path == root or root in path.parents else None


def rel_of(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def note_url(root: Path, path: Path) -> str:
    return "/notes/" + urllib.parse.quote(rel_of(root, path))


def folder_url(rel: str) -> str:
    return "/?" + urllib.parse.urlencode({"folder": rel}) + "#notes" if rel else "/#notes"


def all_folders(root: Path) -> list[str]:
    return sorted(rel_of(root, p) for p in root.rglob("*") if p.is_dir() and not any(s.startswith(".") for s in p.relative_to(root).parts))


def note_for(root: Path, entry: dict | None) -> Path | None:
    """Where a processed recording's note is now, following it if it was moved by hand."""
    if not entry or not entry.get("file"):
        return None
    path = Path(entry["file"]).resolve()
    if path.exists() and root in path.parents:
        return path
    return next(iter(sorted(root.rglob(path.name))), None)


def move_note(root: Path, note: Path, folder: Path) -> Path:
    target = pa.unique_path(folder / note.name)
    note.rename(target)
    state = pa.load_state()
    for entry in state["processed"].values():
        if entry.get("file") and Path(entry["file"]).resolve() == note.resolve():
            entry["file"] = str(target)
    pa.save_state(state)
    return target


def notes_panel(root: Path, current: str) -> str:
    e = html.escape
    here = inside(root, current) or root
    current = rel_of(root, here) if here != root else ""
    crumbs = ['<a href="/#notes">Notes</a>']
    for i, part in enumerate(current.split("/") if current else []):
        crumbs.append(f'<a href="{e(folder_url("/".join(current.split("/")[:i + 1])))}">{e(part)}</a>')
    subfolders = sorted(p for p in here.iterdir() if p.is_dir() and not p.name.startswith(".")) if here.exists() else []
    notes = sorted(here.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if here.exists() else []
    folders = [""] + all_folders(root)
    folder_items = "".join(
        f'<li>📁 <a href="{e(folder_url(rel_of(root, p)))}">{e(p.name)}</a> <span class="muted">{sum(1 for _ in p.rglob("*.md"))}</span></li>'
        for p in subfolders)
    note_rows = []
    for note in notes:
        options = "".join(f'<option value="{e(f)}">{e(f or "Top level")}</option>' for f in folders if f != current)
        move = (f"""<form class="inline" method="post" action="/notes/move"><input type="hidden" name="note" value="{e(rel_of(root, note))}">
<input type="hidden" name="back" value="{e(current)}"><select name="dest" aria-label="Move to">{options}</select><button>Move</button></form>""") if options else ""
        note_rows.append(f'<tr><td><a href="{e(note_url(root, note))}">{e(note.stem)}</a></td><td>{move}</td></tr>')
    body = (f"<ul class='folders'>{folder_items}</ul>" if folder_items else "") + (
        f"<div class='scroll'><table>{''.join(note_rows)}</table></div>" if note_rows
        else '<p class="muted">No notes in this folder.</p>')
    return f"""<div class="panel" id="notes"><div class="meta" style="justify-content:space-between;margin-bottom:8px">
<h2 style="margin:0">{' / '.join(crumbs)}</h2>
<form class="inline" method="post" action="/folders"><input type="hidden" name="parent" value="{e(current)}">
<input type="text" name="name" placeholder="New folder" required maxlength="60" style="width:160px" aria-label="New folder name"><button>Create</button></form></div>
{body}<p class="muted">New notes arrive in the top level. Moving a note never replaces another; nothing here is ever deleted.</p></div>"""


def dashboard(flash: str | None, folder: str = "") -> str:
    cfg = pa.load_config()
    root = pa.notes_dir(cfg)
    e = html.escape
    parts = [f"""<header><h1>OpenPlaud</h1><div class="meta muted"><span>{e(model_label(cfg))}</span>
<span>{'Auto-sync every ' + str(SYNC_EVERY_MIN) + ' min' if SYNC_EVERY_MIN else 'Auto-sync off'}</span></div></header>"""]
    if flash:
        parts.append(f'<div class="flash">{e(flash)}</div>')

    st = signin.state
    who, busy_signin = None, False
    if st["status"] not in ("starting", "waiting"):
        try:
            who = plaud.user()
        except TimeoutError:
            busy_signin = True
    if who:
        parts.append(f"""<div class="panel meta"><span>Plaud: <span class="ok">signed in</span> as {e(who)}</span>
<form class="inline" method="post" action="plaud/signout"><button>Sign out of Plaud</button></form>
<form class="inline" method="post" action="test"><button>Test {e(PROVIDER_LABELS.get(cfg['backend'], 'model'))}</button></form></div>""")
    elif st["status"] == "waiting" and st["url"]:
        parts.append(f"""<div class="panel"><h2>Sign in to Plaud</h2>
<ol><li><a class="button primary" href="{e(st['url'])}" target="_blank" rel="noopener noreferrer">Open the Plaud sign-in page</a> and approve access.</li>
<li>Your browser then goes to a <code>localhost:8199</code> address that doesn't load. That's expected.
Copy that whole address from the address bar and paste it here within 2 minutes:</li></ol>
<form method="post" action="plaud/callback"><input type="text" name="url" placeholder="http://localhost:8199/auth/callback?code=…" required autofocus>
<p><button class="primary">Finish signing in</button></p></form>{f'<p class="bad">{e(st["message"])}</p>' if st.get("message") else ''}</div>""")
    elif busy_signin:
        parts.append('<div class="panel">Plaud is busy. Reload in a moment.</div>')
    else:
        msg = f'<p class="bad">{e(st["message"])}</p>' if st["status"] == "failed" and st.get("message") else ""
        parts.append(f"""<div class="panel"><h2>Sign in to Plaud</h2><p>OpenPlaud reads your recordings through Plaud's official connector.</p>
{msg}<form method="post" action="plaud/signin"><button class="primary">Start Plaud sign-in</button></form></div>""")

    busy = bool(jobs.current) or not jobs.queue.empty()
    parts.append(f"""<div class="panel"><div class="meta" style="justify-content:space-between;margin-bottom:8px">
<h2 style="margin:0">Activity</h2><span class="muted">{e(jobs.current or ('Last sync ' + jobs.last_sync if jobs.last_sync else 'Idle'))}</span></div>
<pre class="log" id="log">{e(chr(10).join(jobs.log) or 'Nothing yet.')}</pre></div>""")

    if who:
        try:
            recs = plaud.recordings()
        except TimeoutError:
            recs = None
        except pa.PlaudError as err:
            recs = None
            parts.append(f'<div class="flash bad">{e(str(err))}</div>')
        done = pa.load_state()["processed"]
        task_options = "".join(f'<option value="{k}"{" selected" if k == DEFAULT_TASK else ""}>{v}</option>' for k, v in TASK_LABELS.items())
        rows = []
        for r in recs or []:
            rid = pa.rec_id(r)
            when = pa.recorded_at(r)
            note = note_for(root, done.get(rid))
            status = f'<a href="{e(note_url(root, note))}">Notes</a>' if note else '<span class="muted">Not yet</span>'
            rows.append(f"""<tr><td>{e(when.strftime('%Y-%m-%d %H:%M') if when else '?')}</td><td class="hide-sm">{e(pa.duration(r))}</td>
<td>{e(r.get('name') or rid)}</td><td>{status}</td><td><form class="inline" method="post" action="process">
<input type="hidden" name="id" value="{e(rid)}"><select name="task" aria-label="Task">{task_options}</select><button>Run</button></form></td></tr>""")
        table = ("<div class='scroll'><table><tr class='head'><th>Recorded</th><th class='hide-sm'>Length</th><th>Name</th><th>Notes</th><th></th></tr>"
                 + "".join(rows) + "</table></div>") if rows else f'<p class="muted">{"Loading…" if recs is None else f"No recordings in the last {LIST_DAYS} days."}</p>'
        parts.append(f"""<div class="panel"><div class="meta" style="justify-content:space-between;margin-bottom:8px">
<h2 style="margin:0">Recordings · last {LIST_DAYS} days</h2><form class="inline" method="post" action="sync"><button class="primary">Sync now</button></form></div>{table}</div>""")

    parts.append(notes_panel(root, folder))
    if PASSWORD:
        parts.append('<form method="post" action="logout"><button>Sign out of OpenPlaud</button></form>')
    return page("OpenPlaud", "".join(parts), refresh=busy or st["status"] == "waiting")


def login_page(error: str = "") -> str:
    err = f'<p class="bad">{html.escape(error)}</p>' if error else ""
    return page("OpenPlaud sign-in", f"""<div class="panel" style="max-width:360px;margin:60px auto"><h1>OpenPlaud</h1>{err}
<form method="post" action="login"><p><input type="password" name="password" placeholder="Password" required autofocus style="width:100%"></p>
<button class="primary">Sign in</button></form><p class="muted">The password is in BoxPilot, on this app's card.</p></div>""")


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "OpenPlaud"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, body: str | bytes, ctype: str = "text/html; charset=utf-8", headers: dict | None = None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        # Not no-referrer: under it browsers send "Origin: null" on every form post, same-site included.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str = "./", flash: str | None = None, headers: dict | None = None):
        if flash:
            to, hash_, fragment = to.partition("#")
            to += ("&" if "?" in to else "?") + urllib.parse.urlencode({"msg": flash}) + hash_ + fragment
        self.send_response(303)
        self.send_header("Location", to)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authed(self) -> bool:
        if not PASSWORD:
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and valid_session(value):
                return True
        return False

    def _form(self) -> dict:
        length = min(int(self.headers.get("Content-Length") or 0), 16384)
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}

    def _same_origin(self) -> bool:
        site = self.headers.get("Sec-Fetch-Site")
        if site:
            return site in ("same-origin", "none")
        origin = self.headers.get("Origin")
        if not origin:
            return True
        # Behind Tailscale Serve the Host header can be the backend's; the public name is forwarded.
        return urllib.parse.urlsplit(origin).netloc in {self.headers.get("Host"), self.headers.get("X-Forwarded-Host")}

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if not self._authed():
            return self._send(200 if path == "/login" else 401, login_page())
        if path == "/":
            query = urllib.parse.parse_qs(url.query)
            return self._send(200, dashboard(query.get("msg", [None])[0], query.get("folder", [""])[0]))
        if path == "/api/status":
            body = {"busy": bool(jobs.current) or not jobs.queue.empty(), "current": jobs.current,
                    "log": list(jobs.log)[-80:], "signin": signin.state["status"]}
            return self._send(200, json.dumps(body), "application/json")
        if path.startswith("/notes/"):
            return self._note(path.removeprefix("/notes/"), "raw" in urllib.parse.parse_qs(url.query))
        self._send(404, page("Not found", '<p>Not found. <a href="/">Back</a></p>'))

    def _note(self, rel: str, raw: bool):
        root = pa.notes_dir(pa.load_config())
        target = inside(root, urllib.parse.unquote(rel))
        if not target or target.suffix != ".md" or not target.is_file():
            return self._send(404, page("Not found", '<p>No such note. <a href="/#notes">Back</a></p>'))
        text = target.read_text()
        if raw:
            return self._send(200, text, "text/markdown; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{target.name}"'})
        folder = rel_of(root, target.parent) if target.parent != root else ""
        back = html.escape(folder_url(folder))
        body = f"""<p><a href="{back}">← {html.escape(folder or "OpenPlaud")}</a> · <a href="{html.escape(note_url(root, target))}?raw=1">Download Markdown</a></p>
<div class="panel note">{render_markdown(text)}</div>"""
        self._send(200, page(target.name, body))

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if not self._same_origin():
            return self._send(403, "Cross-site request refused.", "text/plain")
        form = self._form()
        if path == "/login":
            if PASSWORD and hmac.compare_digest(form.get("password", "").encode(), PASSWORD.encode()):
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                cookie = f"{COOKIE}={make_session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}{secure}"
                return self._redirect("./", headers={"Set-Cookie": cookie})
            time.sleep(1)
            return self._send(401, login_page("Wrong password."))
        if not self._authed():
            return self._send(401, login_page())
        if path == "/logout":
            return self._redirect("./", headers={"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
        if path == "/plaud/signin":
            signin.start()
            return self._redirect("../", signin.state.get("message") if signin.state["status"] == "failed" else None)
        if path == "/plaud/callback":
            signin.complete(form.get("url", ""))
            ok = signin.state["status"] == "success"
            return self._redirect("../", "Signed in to Plaud." if ok else signin.state.get("message") or "Sign-in didn't finish.")
        if path == "/plaud/signout":
            with plaud.lock:
                try:
                    plaud.get().call("logout")
                except pa.PlaudError:
                    pass
            plaud.forget()
            signin.state = {"status": "idle", "url": None, "message": None}
            return self._redirect("../", "Signed out of Plaud.")
        if path == "/folders":
            root = pa.notes_dir(pa.load_config())
            parent, name = inside(root, form.get("parent", "")), form.get("name", "").strip()
            if not parent or not FOLDER_NAME.fullmatch(name):
                return self._redirect(folder_url(form.get("parent", "")), "Folder names use letters, numbers, spaces, dots, dashes and underscores.")
            (parent / name).mkdir(parents=True, exist_ok=True)
            return self._redirect(folder_url(rel_of(root, parent / name)), f"Folder {name} is ready.")
        if path == "/notes/move":
            root = pa.notes_dir(pa.load_config())
            note, dest = inside(root, form.get("note", "")), inside(root, form.get("dest", ""))
            if not note or note.suffix != ".md" or not note.is_file() or not dest or not dest.is_dir():
                return self._redirect(folder_url(form.get("back", "")), "That note or folder doesn't exist.")
            moved = move_note(root, note, dest)
            return self._redirect(folder_url(form.get("back", "")), f"Moved to {rel_of(root, dest) if dest != root else 'the top level'} as {moved.name}.")
        if path == "/sync":
            added = jobs.add(("sync",))
            return self._redirect("./", "Sync queued." if added else "A sync is already queued.")
        if path == "/process":
            file_id, task = form.get("id", ""), form.get("task", DEFAULT_TASK)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", file_id) or task not in pa.TASKS:
                return self._redirect("./", "Unknown recording or task.")
            jobs.add(("process", file_id, task))
            return self._redirect("./", "Queued.")
        if path == "/test":
            cfg = pa.load_config()
            try:
                result = pa.make_backend(cfg, cfg["backend"]).check()
            except Exception as err:
                result = f"{PROVIDER_LABELS.get(cfg['backend'])}: {err}"
            return self._redirect("./", result)
        self._send(404, "Not found", "text/plain")


def main() -> None:
    cfg = pa.load_config()
    if cfg["backend"] not in pa.PROVIDERS:
        raise SystemExit(f"OPENPLAUD_PROVIDER must be one of {', '.join(pa.PROVIDERS)}")
    if DEFAULT_TASK not in pa.TASKS:
        raise SystemExit(f"OPENPLAUD_TASK must be one of {', '.join(pa.TASKS)}")
    pa.notes_dir(cfg).mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"OpenPlaud listening on :{PORT} ({model_label(cfg)})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
