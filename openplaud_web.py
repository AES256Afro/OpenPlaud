"""OpenPlaud web app: sign in to Plaud, see recordings, summarize them, read and organize the notes,
and (optionally) send each recording to Claude as its own session.

Settings come from the environment (BoxPilot's app settings): see README.md for the list. The page is
drawn by openplaud_ui in one of ten looks; OPENPLAUD_LOOK sets the default and each browser can pick its own.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.parse
from collections import deque
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import claude_routine as cr
import openplaud_core as core
import openplaud_notes as nt
import openplaud_ui as ui
import plaud_ai as pa

PORT = int(os.environ.get("OPENPLAUD_PORT", "8795"))
PASSWORD = os.environ.get("OPENPLAUD_PASSWORD", "")
SYNC_EVERY_MIN = int(os.environ.get("OPENPLAUD_SYNC_EVERY", "60") or 0)
SYNC_DAYS = int(os.environ.get("OPENPLAUD_SYNC_DAYS", "7") or 7)
LIST_DAYS = int(os.environ.get("OPENPLAUD_LIST_DAYS", "30") or 30)
DEFAULT_TASK = os.environ.get("OPENPLAUD_TASK", "summary")
DEFAULT_LOOK = os.environ.get("OPENPLAUD_LOOK", "inbox")
LONG_MINUTES = int(os.environ.get("OPENPLAUD_LONG_MINUTES", "0") or 0)
LONG_MODEL = os.environ.get("OPENPLAUD_LONG_MODEL", "")
MONTHLY_CAP = float(os.environ.get("OPENPLAUD_MONTHLY_CAP", "0") or 0)
ACTIVITY_LOG = pa.DATA_DIR / "activity.log"
CLAUDE_URL = os.environ.get("CLAUDE_ROUTINE_URL", "")
CLAUDE_TOKEN = os.environ.get("CLAUDE_ROUTINE_TOKEN", "")
AUTH_URL_FILE = Path(os.environ.get("OPENPLAUD_AUTH_URL_FILE", "/tmp/openplaud-auth-url"))
COOKIE = "openplaud_session"
LOOK_COOKIE = "openplaud_look"
SESSION_DAYS = 30
PROVIDER_LABELS = {"claude": "Claude", "chatgpt": "ChatGPT", "local": "Local model"}
TASK_LABELS = {"summary": "Meeting notes", "actions": "Action items", "followup": "Follow-up email"}

plaud = core.PlaudSession(LIST_DAYS)
signin = core.PlaudSignIn(plaud, AUTH_URL_FILE)
passwords = core.Passwords(PASSWORD, b"openplaud-session:", SESSION_DAYS, pa.DATA_DIR / ".session-secret")


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def claude_on() -> bool:
    return cr.configured(CLAUDE_URL, CLAUDE_TOKEN)


# ---------------------------------------------------------------- sending to Claude

def claude_state(state: dict) -> dict:
    c = state.setdefault("claude", {})
    for key in ("sent", "failed", "inflight"):
        c.setdefault(key, {})
    c.setdefault("fires", [])
    return c


def settle_claude_inflight() -> int:
    """Sends under way when the app stopped have an unknown outcome: never repeated automatically."""
    with pa.editing_state() as state:
        c = claude_state(state)
        for rid, info in c["inflight"].items():
            c["failed"][rid] = {"error": "The app stopped while this was being sent. Check claude.ai/code for its session before "
                                         "sending it again.", "retryable": False, "at": info.get("at") or datetime.now().isoformat()}
        count = len(c["inflight"])
        c["inflight"] = {}
    return count


def send_to_claude(rec: dict, note_path: Path, transcript: str, log, manual: bool = False) -> None:
    """Fire the scheduled task with this recording's note and transcript. Automatic sends cover only
    recordings made after sending was switched on, each once; a manual send always goes. The attempt is
    saved before the request goes out, so a crash mid-send is never followed by a second session."""
    if not claude_on():
        return
    rid, name = pa.rec_id(rec), str(rec.get("name") or pa.rec_id(rec))
    try:
        text = cr.build_payload(name, pa.recorded_at(rec).strftime("%Y-%m-%d %H:%M") if pa.recorded_at(rec) else "?",
                                pa.duration(rec), rid, [("Notes written by OpenPlaud", note_path.read_text())], transcript)
    except OSError as err:
        log(f"✗ Claude: {name}: couldn't read its note ({err.__class__.__name__}); not sent")
        return
    with pa.editing_state() as state:
        c = claude_state(state)
        if not manual:
            since = datetime.fromisoformat(c["since"]) if c.get("since") else None
            when = pa.recorded_at(rec)
            if rid in c["sent"] or rid in c["inflight"] or (since and when and when < since):
                return
            if c["failed"].get(rid, {}).get("retryable") is False:
                return  # the last send may have reached Claude: only the owner sends it again
        budget = cr.FireBudget([float(s) for s in c["fires"]])
        wait = c.get("not_before") or 0
        if wait > time.time() or not budget.available():
            why = (f"Claude asked to wait until {datetime.fromtimestamp(wait):%H:%M}" if wait > time.time()
                   else "Claude's hourly limit for this task is nearly used")
            c["failed"][rid] = {"error": f"Waiting: {why}. Sent on a later check.", "retryable": True, "at": datetime.now().isoformat(), "name": name}
            log(f"… Claude: {why.lower()}, {name} waits for the next check")
            return
        budget.spend()
        c["fires"] = budget.stamps
        c["inflight"][rid] = {"at": datetime.now().isoformat()}
    result, failure = None, None
    try:
        result = cr.fire(CLAUDE_URL, CLAUDE_TOKEN, text)
    except cr.RoutineError as err:
        failure = {"error": str(err), "retryable": err.retryable, "at": datetime.now().isoformat()}
        retry_after = err.retry_after
    except Exception as err:  # the request went out and the answer was unreadable: outcome unknown
        failure = {"error": f"Claude's answer couldn't be read ({type(err).__name__}). Check claude.ai/code before sending it again.",
                   "retryable": False, "at": datetime.now().isoformat()}
        retry_after = None
    with pa.editing_state() as state:
        c = claude_state(state)
        c["inflight"].pop(rid, None)
        if failure:
            c["failed"][rid] = dict(failure, name=name)
            if retry_after:
                c["not_before"] = time.time() + min(retry_after, 3600)
        else:
            c["sent"][rid] = {"session_url": result["session_url"], "at": datetime.now().isoformat(), "note": str(note_path)}
            c["failed"].pop(rid, None)
    log(f"✗ Claude: {name}: {failure['error']}" if failure else f"→ Claude: {name} · {result['session_url']}")


# ---------------------------------------------------------------- background jobs

class Jobs:
    def __init__(self):
        self.queue: queue.Queue = queue.Queue()
        self.log: deque[str] = deque(maxlen=300)
        self.current: str | None = None
        self.active_id: str | None = None
        self.pending: set[tuple] = set()
        self.adding = threading.Lock()
        self.last_sync: str | None = None
        self.next_sync: datetime | None = None
        threading.Thread(target=self._worker, daemon=True).start()
        if SYNC_EVERY_MIN > 0:
            threading.Thread(target=self._scheduler, daemon=True).start()

    def say(self, message: str) -> None:
        line = f"{now()}  {message}"
        self.log.append(line)
        try:
            if ACTIVITY_LOG.exists() and ACTIVITY_LOG.stat().st_size > 1_000_000:
                ACTIVITY_LOG.rename(ACTIVITY_LOG.with_name(f"activity-{datetime.now():%Y%m%d-%H%M%S}.log"))
            with ACTIVITY_LOG.open("a") as f:
                f.write(f"{datetime.now():%Y-%m-%d} {line}\n")
        except OSError:
            pass

    def load_log(self) -> None:
        try:
            lines = ACTIVITY_LOG.read_text().splitlines()[-200:]
        except OSError:
            return
        self.log.extend(line.split(" ", 1)[1] if " " in line else line for line in lines)

    def add(self, job: tuple) -> bool:
        with self.adding:
            if job in self.pending:
                return False
            self.pending.add(job)
            self.queue.put(job)
            return True

    def queued_ids(self) -> set[str]:
        with self.adding:
            return {job[1] for job in self.pending if job[0] in ("process", "claude")}

    def _scheduler(self) -> None:
        self.next_sync = datetime.now() + timedelta(seconds=30)
        time.sleep(30)
        while True:
            self.next_sync = datetime.now() + timedelta(minutes=SYNC_EVERY_MIN)
            self.add(("sync",))
            time.sleep(SYNC_EVERY_MIN * 60)

    def _worker(self) -> None:
        while True:
            job = self.queue.get()
            self.current = {"sync": "Syncing new recordings", "process": "Summarizing a recording", "claude": "Sending to Claude"}[job[0]]
            self.active_id = job[1] if job[0] != "sync" else None
            try:
                self._run(job)
            except pa.NotSignedIn:
                self.say("Not signed in to Plaud. Sign in from the page to continue.")
            except pa.CapReached as err:
                self.say(f"… {err}")
            except Exception as err:
                self.say(f"✗ {type(err).__name__}: {err}")
            finally:
                with self.adding:
                    self.pending.discard(job)
                self.current = None
                self.active_id = None
                plaud.forget()

    def _on_done(self, manual: bool = False):
        return lambda rec, path, transcript: send_to_claude(rec, path, transcript, self.say, manual=manual)

    def _run(self, job: tuple) -> None:
        cfg = pa.load_config()
        backend = pa.make_backend(cfg, cfg["backend"])
        out_dir = pa.notes_dir(cfg)
        with plaud.lock:
            p = plaud.get()
            if job[0] == "sync":
                self.say(f"Sync started (last {SYNC_DAYS} days, {backend.name})")
                written, failures = pa.sync(p, backend, SYNC_DAYS, pa.TASKS[DEFAULT_TASK], DEFAULT_TASK, out_dir, log=self.say,
                                            on_start=lambda rec: setattr(self, "active_id", pa.rec_id(rec)), on_done=self._on_done(),
                                            pick=lambda rec: route(cfg, rec))
                self.last_sync = datetime.now().strftime("%-d %b %H:%M")
                self.say(f"Sync finished: {len(written)} new, {failures} failed")
                self._retry_claude(p, out_dir)
            elif job[0] == "process":
                _, file_id, task, spec, custom = job
                instructions, label = instructions_for(task, custom)
                rec = pa.unwrap_file(p.call_json("get_file", file_id=file_id))
                chosen = pa.backend_for(cfg, spec) if spec else route(cfg, rec) or backend
                pa.process_and_record(p, chosen, rec, instructions, label, out_dir, log=self.say, on_done=self._on_done())
            else:
                _, file_id = job
                state = pa.load_state()
                if file_id in claude_state(state)["sent"]:
                    self.say(f"– {file_id} is already in Claude; not sent again")
                    return
                rec = pa.unwrap_file(p.call_json("get_file", file_id=file_id))
                note = nt.note_for(out_dir, state["processed"].get(file_id))
                if note:
                    send_to_claude(rec, note, pa.fetch_transcript(p, file_id), self.say, manual=True)
                else:
                    pa.process_and_record(p, backend, rec, pa.TASKS[DEFAULT_TASK], DEFAULT_TASK, out_dir, log=self.say,
                                          on_done=self._on_done(manual=True))

    def _retry_claude(self, p: pa.Plaud, out_dir: Path) -> None:
        """Send again what Claude refused with a rate limit or overload; errors that need the owner wait for them."""
        if not claude_on():
            return
        state = pa.load_state()
        for rid, failure in list(claude_state(state)["failed"].items()):
            if not failure.get("retryable"):
                continue
            note = nt.note_for(out_dir, state["processed"].get(rid))
            if not note:
                continue
            rec = pa.unwrap_file(p.call_json("get_file", file_id=rid))
            send_to_claude(rec, note, pa.fetch_transcript(p, rid), self.say, manual=True)


jobs = Jobs()
jobs.load_log()


def route(cfg: dict, rec: dict):
    """The long-recording rule: recordings longer than OPENPLAUD_LONG_MINUTES use OPENPLAUD_LONG_MODEL."""
    if LONG_MINUTES and LONG_MODEL and (rec.get("duration") or 0) > LONG_MINUTES * 60_000:
        return pa.backend_for(cfg, LONG_MODEL)
    return None


def saved_prompts() -> dict:
    return pa.load_state().get("prompts", {})


def instructions_for(task: str, custom: str = "") -> tuple[str, str]:
    """What to ask for, and the name the note gets: a built-in task, a saved prompt, or a one-off prompt."""
    if task in pa.TASKS:
        return pa.TASKS[task], task
    if task.startswith("prompt:") and task[7:] in saved_prompts():
        return saved_prompts()[task[7:]], pa.slug(task[7:]).lower()[:30] or "custom"
    if task == "custom" and custom.strip():
        return custom.strip(), "custom"
    raise ValueError("Unknown task.")


def model_choices(cfg: dict) -> list[tuple[str, str]]:
    """The models a run can pick: the default, plus every provider with what it needs configured."""
    choices = [("", f"Default ({model_label(cfg)})")]
    if os.environ.get("ANTHROPIC_API_KEY") or cfg["backend"] == "claude":
        choices += [(f"claude:{m}", f"Claude {name}") for m, name in
                    (("claude-haiku-5-5", "Haiku 5.5"), ("claude-sonnet-5-5", "Sonnet 5.5"), ("claude-opus-5-5", "Opus 5.5"))]
    if os.environ.get("OPENAI_API_KEY"):
        choices.append((f"chatgpt:{cfg['chatgpt']['model']}", f"ChatGPT {cfg['chatgpt']['model']}"))
    if cfg["backend"] == "local" or os.environ.get("OLLAMA_URL"):
        choices.append((f"local:{cfg['local']['model']}", f"Local {cfg['local']['model']}"))
    return choices


# ---------------------------------------------------------------- what a page shows

def transcript_for(file_id: str | None, may_ask_plaud: bool) -> list[str] | None:
    """A recording's transcript with the owner's speaker names: the stored copy, else Plaud (and keep a copy)."""
    if not file_id:
        return None
    text = pa.load_transcript(file_id)
    if text is None and may_ask_plaud:
        lines = plaud.transcript(file_id)
        if lines:
            text = "\n".join(lines)
            pa.save_transcript(file_id, text)
    if text is None:
        return None
    return pa.apply_speakers(text, pa.load_state().get("speakers", {}).get(file_id)).splitlines()


def speakers_for(file_id: str | None) -> dict | None:
    """Plaud's speaker labels in a recording, each with the name the owner gave it (if any)."""
    raw = pa.load_transcript(file_id) if file_id else None
    if not raw:
        return None
    labels = []
    for line in raw.splitlines():
        m = re.match(r"^\[[^\]]*\] ([^:]+): ", line)
        if m and m.group(1) not in labels:
            labels.append(m.group(1))
    names = pa.load_state().get("speakers", {}).get(file_id, {})
    return {"id": file_id, "labels": [(label, names.get(label, "")) for label in labels[:20]]}


def model_label(cfg: dict) -> str:
    provider = cfg["backend"]
    model = {"claude": cfg["claude"]["model"], "chatgpt": cfg["chatgpt"]["model"], "local": cfg["local"]["model"]}.get(provider, "?")
    return f"{PROVIDER_LABELS.get(provider, provider)} · {model}"


def read_note(root: Path, path: Path) -> dict:
    text = path.read_text()
    folder = nt.rel_of(root, path.parent) if path.parent != root else ""
    return {"rel": nt.rel_of(root, path), "url": nt.note_url(root, path), "folder": folder, "back": nt.folder_url(folder),
            "title": nt.note_title(text, path.stem), "html": ui.render_markdown(text), "text": text, "stem": path.stem,
            "plaud_id": nt.plaud_id(text)}


FILTERS = {key: test for key, test in ui.FILTER_TESTS.items() if key}


def note_day(text: str, path: Path):
    """The day a note's recording was made (its "- Date:" line), else the day the file was written."""
    m = re.search(r"^- Date: (\d{4}-\d{2}-\d{2})", text, re.M)
    try:
        return date.fromisoformat(m.group(1)) if m else datetime.fromtimestamp(path.stat().st_mtime).date()
    except (ValueError, OSError):
        return None


def query_undo(here: str, raw_path: str) -> str:
    """The ?undo= move id on a page address, if any (kept out of `here` so it isn't repeated)."""
    value = urllib.parse.parse_qs(urllib.parse.urlsplit(raw_path).query).get("undo", [""])[0]
    return value if re.fullmatch(r"[0-9a-f]{12}", value) else ""


def mark_read(note_text: str) -> None:
    rid = nt.plaud_id(note_text)
    if not rid:
        return
    if rid in pa.load_state().get("read", {}):
        return
    with pa.editing_state() as state:
        state.setdefault("read", {}).setdefault(rid, datetime.now().isoformat(timespec="seconds"))


def build_context(look: str, query: dict, flash: str | None, here: str) -> dict:
    cfg = pa.load_config()
    root = pa.notes_dir(cfg)
    root.mkdir(parents=True, exist_ok=True)
    here_dir = nt.inside(root, query.get("folder", ""))
    here_dir = here_dir if here_dir and here_dir.is_dir() else root
    folder = nt.rel_of(root, here_dir) if here_dir != root else ""
    plaud_state = signin.page_state()

    # Every note once: titles, summaries and action items for the looks that show them.
    notes = []
    for path in nt.all_notes(root):
        try:
            text = path.read_text()
        except OSError:
            continue
        notes.append({"path": path, "rel": nt.rel_of(root, path), "url": nt.note_url(root, path), "stem": path.stem,
                      "folder": nt.rel_of(root, path.parent) if path.parent != root else "", "title": nt.note_title(text, path.stem),
                      "summary": nt.note_summary(text), "actions": nt.note_actions(text, with_lines=True), "plaud_id": nt.plaud_id(text),
                      "day": note_day(text, path)})
    by_path = {n["path"].resolve(): n for n in notes}

    state = pa.load_state()
    sent = claude_state(state)["sent"]
    failed = claude_state(state)["failed"]
    recordings, recordings_error = None, None
    if plaud_state["state"] == "signed_in":
        try:
            raw = plaud.recordings()
            queued = jobs.queued_ids()
            recordings = []
            for r in raw:
                rid = pa.rec_id(r)
                when = pa.recorded_at(r)
                path = nt.note_for(root, state["processed"].get(rid))
                note = by_path.get(path.resolve()) if path else None
                if rid == jobs.active_id:
                    status = "writing"
                elif rid in queued:
                    status = "queued"
                elif note:
                    status = "filed" if note["folder"] else "ready"
                elif rid in state.get("failed", {}):
                    status = "stuck" if state["failed"][rid].get("count", 0) >= pa.MAX_FAILURES else "failed"
                elif rid in state.get("no_transcript", {}):
                    status = "no_transcript"
                else:
                    status = "new"
                recordings.append({
                    "id": rid, "name": r.get("name") or rid, "when": when, "day": when.date() if when else None,
                    "when_s": when.strftime("%Y-%m-%d %H:%M") if when else "?", "time_s": when.strftime("%-I:%M %p") if when else "",
                    "duration": pa.duration(r), "status": status,
                    "note_url": note["url"] if note else None, "note_rel": note["rel"] if note else None,
                    "note_folder": note["folder"] if note else "", "summary": note["summary"] if note else "",
                    "actions_open": sum(1 for a in note["actions"] if not a["done"]) if note else 0,
                    "claude_url": sent.get(rid, {}).get("session_url"), "claude_error": failed.get(rid, {}).get("error"),
                    "claude_unsure": failed.get(rid, {}).get("retryable") is False,
                    "unread": bool(note) and rid not in state.get("read", {}),
                    "error": state.get("failed", {}).get(rid, {}).get("error")})
        except TimeoutError:
            recordings_error = "Plaud is busy with a sync. The list refreshes when it's done."
        except pa.PlaudError as err:
            recordings_error = str(err)

    counts: dict[str, int] = {}
    for n in notes:
        parts = n["folder"].split("/") if n["folder"] else []
        for i in range(1, len(parts) + 1):
            counts["/".join(parts[:i])] = counts.get("/".join(parts[:i]), 0) + 1
    folders = nt.all_folders(root)
    folder_counts = [{"rel": f, "name": f.rsplit("/", 1)[-1], "count": counts.get(f, 0), "url": nt.folder_url(f)} for f in folders]
    subfolders = [fc for fc in folder_counts if (fc["rel"].rsplit("/", 1)[0] if "/" in fc["rel"] else "") == folder]

    crumbs = [("Notes", nt.folder_url(""))]
    parts = folder.split("/") if folder else []
    for i, part in enumerate(parts):
        crumbs.append((part, nt.folder_url("/".join(parts[: i + 1]))))

    actions = [{"text": a["text"], "done": a["done"], "line": a["line"], "note_rel": n["rel"], "note_url": n["url"],
                "note_title": n["title"], "day": n["day"]} for n in notes for a in n["actions"]]

    show = query.get("show", "")
    all_recordings = recordings
    if recordings is not None and show in FILTERS:
        recordings = [r for r in recordings if FILTERS[show](r)]
    search = None
    if query.get("q", "").strip():
        titles = {r["id"]: r["name"] for r in all_recordings or []}
        search = nt.search(root, query["q"], titles)
        for hit in search:
            if hit["kind"] == "transcript":
                path = nt.note_for(root, state["processed"].get(hit["id"]))
                hit["url"] = nt.note_url(root, path) if path else None

    selected = None
    if query.get("note"):
        path = nt.inside(root, query["note"])
        if path and path.suffix == ".md" and path.is_file():
            selected = read_note(root, path)
            mark_read(selected["text"])
    transcript = None
    if look == "reader" and selected:
        transcript = transcript_for(nt.plaud_id(selected["text"]), plaud_state["state"] == "signed_in")

    try:
        week = max(-52, min(52, int(query.get("week", "0"))))
    except ValueError:
        week = 0

    return {
        "look": look, "flash": flash, "here": here, "folder": folder, "week": week, "rec": query.get("rec", ""),
        "provider": PROVIDER_LABELS.get(cfg["backend"], cfg["backend"]), "model": model_label(cfg),
        "sync_every": SYNC_EVERY_MIN, "sync_days": SYNC_DAYS, "list_days": LIST_DAYS,
        "last_sync": jobs.last_sync, "next_sync": jobs.next_sync.strftime("%-I:%M %p") if jobs.next_sync else None,
        "busy": bool(jobs.current) or not jobs.queue.empty(), "current": jobs.current, "log": list(jobs.log)[-200:],
        "password": bool(PASSWORD), "plaud": plaud_state, "claude_on": claude_on(),
        "recordings": recordings, "recordings_error": recordings_error,
        "folders": folders, "folder_counts": folder_counts, "subfolders": subfolders, "breadcrumbs": crumbs,
        "notes_here": [n for n in notes if n["folder"] == folder], "recent_notes": notes,
        "actions": actions, "counts": {"notes": len(notes)},
        "tasks": TASK_LABELS, "default_task": DEFAULT_TASK, "note": selected, "transcript": transcript,
        "show": show if show in FILTERS else "", "q": query.get("q", ""), "search": search, "undo": query.get("undo", ""),
        "all_recordings": all_recordings, "moves": nt.move_history(root, 10),
        "models": model_choices(cfg), "prompts": sorted(saved_prompts()), "spend": pa.month_spend(state), "cap": MONTHLY_CAP,
        "health": core.plaud_health() if plaud_state["state"] == "signed_in" else None,
        "speakers": speakers_for(nt.plaud_id(selected["text"])) if selected and look == "reader" else None,
        "long_rule": f"Longer than {LONG_MINUTES} min → {LONG_MODEL}" if LONG_MINUTES and LONG_MODEL else "",
    }


def with_undo(url: str, move_id: str) -> str:
    base, hash_, fragment = url.partition("#")
    return base + ("&" if "?" in base else "?") + urllib.parse.urlencode({"undo": move_id}) + hash_ + fragment


def claude_context(look: str, flash: str | None) -> dict:
    state = pa.load_state()
    c = claude_state(state)
    return {"look": look, "flash": flash, "here": "/claude", "folder": "", "folders": [], "claude_on": claude_on(),
            "since": c.get("since"), "sent": len(c["sent"]), "failed": c["failed"],
            "fires_hour": len([s for s in c["fires"] if float(s) > time.time() - 3600]), "prompt": cr.SUGGESTED_PROMPT,
            "url_ok": bool(CLAUDE_URL) and cr.configured(CLAUDE_URL, "x"), "token_set": bool(CLAUDE_TOKEN.strip())}


# ---------------------------------------------------------------- HTTP

class BodyTooLarge(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenPlaud"
    timeout = 60  # a client that stops sending is dropped instead of holding a thread

    def log_message(self, fmt, *args):
        pass

    def _look(self) -> str:
        chosen = core.cookie(self.headers, LOOK_COOKIE)
        if chosen in ui.LOOKS:
            return chosen
        return DEFAULT_LOOK if DEFAULT_LOOK in ui.LOOKS else "inbox"

    def _send(self, status: int, body: str | bytes, ctype: str = "text/html; charset=utf-8", headers: dict | None = None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Not no-referrer: under it browsers send "Origin: null" on every form post, same-site included.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src https://fonts.gstatic.com; script-src 'unsafe-inline'; form-action 'self'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str = "/", flash: str | None = None, headers: dict | None = None):
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
        return passwords.valid(core.cookie(self.headers, COOKIE) or "")

    def _form(self) -> dict:
        """The posted form; a field sent more than once (selected notes) keeps every value under "<name>[]"."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= 65536:
            raise BodyTooLarge
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"), keep_blank_values=True)
        form = {k: v[0] for k, v in fields.items()}
        form.update({f"{k}[]": v for k, v in fields.items()})
        return form

    def _bare_ctx(self) -> dict:
        return {"look": self._look(), "flash": None, "here": "/", "folder": "", "folders": []}

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if not core.host_allowed(self.headers):
            return self._send(421, "Unknown host name. Add it to OPENPLAUD_ALLOWED_HOSTS to use it.", "text/plain")
        if not self._authed():
            return self._send(200 if path == "/login" else 401, ui.login_page(self._bare_ctx()))
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        kept = {k: v for k, v in query.items() if k not in ("msg", "undo")}
        here = url.path + ("?" + urllib.parse.urlencode(kept) if kept else "")
        if path == "/":
            return self._send(200, ui.dashboard(build_context(self._look(), query, query.get("msg"), here)))
        if path == "/claude":
            return self._send(200, ui.claude_page(claude_context(self._look(), query.get("msg"))))
        if path == "/api/status":
            body = {"busy": bool(jobs.current) or not jobs.queue.empty(), "current": jobs.current,
                    "log": list(jobs.log)[-80:], "signin": signin.state["status"]}
            return self._send(200, json.dumps(body), "application/json")
        if path.startswith("/notes/"):
            return self._note(urllib.parse.unquote(path.removeprefix("/notes/")), "raw" in query, query.get("msg"), here)
        self._send(404, ui.page(self._bare_ctx(), "Not found", '<p style="padding:20px">Not found. <a href="/">Back</a></p>'))

    def _note(self, rel: str, raw: bool, flash: str | None, here: str):
        root = pa.notes_dir(pa.load_config())
        target = nt.inside(root, rel)
        if not target or target.suffix != ".md" or not target.is_file():
            return self._send(404, ui.page(self._bare_ctx(), "Not found", '<p style="padding:20px">No such note. <a href="/#notes">Back</a></p>'))
        if raw:
            return self._send(200, target.read_text(), "text/markdown; charset=utf-8",
                              {"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(target.name)}"})
        look = self._look()
        note = read_note(root, target)
        mark_read(note["text"])
        cfg = pa.load_config()
        ctx = {"look": look, "flash": flash, "here": here, "folder": note["folder"], "folders": nt.all_folders(root),
               "undo": query_undo(here, self.path),
               "models": model_choices(cfg), "speakers": speakers_for(note["plaud_id"]) if look == "reader" else None}
        with_transcript = look == "reader"
        transcript = transcript_for(nt.plaud_id(note["text"]), True) if with_transcript else None
        self._send(200, ui.note_page(ctx, note, transcript, with_transcript))

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if not core.same_origin(self.headers) or not core.host_allowed(self.headers):
            return self._send(403, "Cross-site request refused.", "text/plain")
        try:
            form = self._form()
        except BodyTooLarge:
            return self._send(413, "Request too large.", "text/plain")
        back = core.safe_back(form.get("back", "/"))
        if path == "/login":
            if passwords.check(form.get("password", "")):
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                cookie = f"{COOKIE}={passwords.make()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}{secure}"
                return self._redirect("/", headers={"Set-Cookie": cookie})
            return self._send(401, ui.login_page(self._bare_ctx(), "Wrong password."))
        if not self._authed():
            return self._send(401, ui.login_page(self._bare_ctx()))
        if path == "/logout":
            passwords.end_all()
            return self._redirect("/", headers={"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
        if path == "/look":
            look = form.get("look", "")
            if look not in ui.LOOKS:
                return self._redirect(back, "Unknown look.")
            return self._redirect(back, headers={"Set-Cookie": f"{LOOK_COOKIE}={look}; Path=/; SameSite=Strict; Max-Age={365 * 86400}"})
        if path == "/plaud/signin":
            signin.start()
            return self._redirect("/", signin.state.get("message") if signin.state["status"] == "failed" else None)
        if path == "/plaud/callback":
            signin.complete(form.get("url", ""))
            ok = signin.state["status"] == "success"
            return self._redirect("/", "Signed in to Plaud." if ok else signin.state.get("message") or "Sign-in didn't finish.")
        if path == "/plaud/signout":
            signin.sign_out()
            return self._redirect("/", "Signed out of Plaud.")
        if path == "/folders":
            root = pa.notes_dir(pa.load_config())
            parent, name = nt.inside(root, form.get("parent", "")), form.get("name", "").strip()
            if not parent or not parent.is_dir() or not nt.FOLDER_NAME.fullmatch(name):
                return self._redirect(nt.folder_url(form.get("parent", "")), "Folder names use letters, numbers, spaces, dots, dashes and underscores.")
            (parent / name).mkdir(parents=True, exist_ok=True)
            return self._redirect(nt.folder_url(nt.rel_of(root, parent / name)), f"Folder {name} is ready.")
        if path == "/notes/move":
            root = pa.notes_dir(pa.load_config())
            note, dest = nt.inside(root, form.get("note", "")), nt.inside(root, form.get("dest", ""))
            if not note or note.suffix != ".md" or not note.is_file() or not dest or not dest.is_dir():
                return self._redirect(back, "That note or folder doesn't exist.")
            try:
                moved, move_id = nt.move_note(root, note, dest)
            except ValueError as err:
                return self._redirect(back, str(err))
            where = nt.rel_of(root, dest) if dest != root else "the top level"
            if back.startswith("/notes/"):
                back = nt.note_url(root, moved)
            elif "note=" in back:
                back = "/?" + urllib.parse.urlencode({"note": nt.rel_of(root, moved)})
            return self._redirect(with_undo(back, move_id), f"Moved to {where} as {moved.name}.")
        if path == "/notes/move-many":
            root = pa.notes_dir(pa.load_config())
            dest = nt.inside(root, form.get("dest", ""))
            picked = [nt.inside(root, rel) for rel in form.get("notes[]", [])[:200]]
            picked = [n for n in picked if n and n.suffix == ".md" and n.is_file() and dest and n.parent != dest]
            if not dest or not dest.is_dir() or not picked:
                return self._redirect(back, "Pick at least one note and a folder.")
            for note in picked:
                nt.move_note(root, note, dest)
            where = nt.rel_of(root, dest) if dest != root else "the top level"
            return self._redirect(back, f"Moved {len(picked)} notes to {where}. Recent moves can be undone below.")
        if path == "/notes/undo":
            root = pa.notes_dir(pa.load_config())
            restored = nt.undo_move(root, form.get("id", ""))
            if not restored:
                return self._redirect(back, "That move can't be undone: what it moved isn't where it was put any more.")
            if restored.is_file() and back.startswith("/notes/"):
                back = nt.note_url(root, restored)
            elif restored.is_file() and "note=" in back:
                back = "/?" + urllib.parse.urlencode({"note": nt.rel_of(root, restored)})
            return self._redirect(back, f"Put back as {nt.rel_of(root, restored)}.")
        if path in ("/folders/rename", "/folders/move"):
            root = pa.notes_dir(pa.load_config())
            folder = nt.inside(root, form.get("folder", ""))
            if not folder or folder == root or not folder.is_dir():
                return self._redirect(back, "That folder doesn't exist.")
            try:
                if path == "/folders/rename":
                    moved, move_id = nt.rename_folder(root, folder, form.get("name", "").strip())
                else:
                    dest = nt.inside(root, form.get("dest", ""))
                    if not dest or not dest.is_dir():
                        return self._redirect(back, "That folder doesn't exist.")
                    moved, move_id = nt.move_folder(root, folder, dest)
            except ValueError as err:
                return self._redirect(back, str(err))
            return self._redirect(with_undo(nt.folder_url(nt.rel_of(root, moved)), move_id), f"Folder is now {nt.rel_of(root, moved)}.")
        if path == "/actions/toggle":
            root = pa.notes_dir(pa.load_config())
            note = nt.inside(root, form.get("note", ""))
            try:
                line = int(form.get("line", "-1"))
            except ValueError:
                line = -1
            ok = bool(note and note.suffix == ".md" and note.is_file()) and nt.set_action(note, line, form.get("text", ""), form.get("done") == "1")
            return self._redirect(back, None if ok else "That note changed since the page loaded; reload and try again.")
        if path == "/sync":
            added = jobs.add(("sync",))
            return self._redirect(back, "Checking Plaud for new recordings. Progress shows under Activity." if added else "A check is already queued.")
        if path in ("/process", "/process-many", "/claude/send"):
            ids = form.get("ids[]", []) if path == "/process-many" else [form.get("id", "")]
            ids = [i for i in ids[:100] if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", i)]
            task, spec, custom = form.get("task", DEFAULT_TASK), form.get("model", ""), form.get("prompt", "")[:8000]
            valid_specs = {key for key, _ in model_choices(pa.load_config())}
            try:
                instructions_for(task, custom)
            except ValueError:
                return self._redirect(back, "Unknown task, or the custom prompt is empty.")
            if not ids or spec not in valid_specs:
                return self._redirect(back, "Tick at least one recording first.")
            if path == "/claude/send":
                if not claude_on():
                    return self._redirect("/claude", "Send to Claude isn't set up yet. Follow the steps on this page.")
                jobs.add(("claude", ids[0]))
                return self._redirect(back, "Sending to Claude.")
            for file_id in ids:
                jobs.add(("process", file_id, task, spec, custom if task == "custom" else ""))
            return self._redirect(back, "Writing notes. This page updates when they're ready." if len(ids) == 1 else f"Writing notes for {len(ids)} recordings. This page updates as each is ready.")
        if path == "/speakers":
            file_id = form.get("id", "")
            labels, names = form.get("label[]", [])[:20], form.get("name[]", [])[:20]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", file_id) or len(labels) != len(names):
                return self._redirect(back, "Couldn't save those names.")
            with pa.editing_state() as state:
                mapping = state.setdefault("speakers", {}).setdefault(file_id, {})
                for label, name in zip(labels, names):
                    name = re.sub(r"[\x00-\x1f:\[\]]", "", name).strip()[:40]
                    if name:
                        mapping[label[:40]] = name
                    else:
                        mapping.pop(label[:40], None)
            return self._redirect(back, "Speaker names saved. They show in the transcript and in future runs.")
        if path == "/prompts/save":
            name, text = form.get("name", "").strip()[:40], form.get("prompt", "").strip()[:8000]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}", name) or not text:
                return self._redirect(back, "Give the prompt a short name (letters, numbers, spaces) and some text.")
            with pa.editing_state() as state:
                prompts = state.setdefault("prompts", {})
                taken = name in prompts
                if not taken:
                    prompts[name] = text
            if taken:
                return self._redirect(back, f"There is already a prompt called {name}; pick another name.")
            return self._redirect(back, f"Saved the prompt {name}. It's in every Run menu now.")
        if path == "/test":
            cfg = pa.load_config()
            try:
                result = pa.make_backend(cfg, cfg["backend"]).check()
            except Exception as err:
                result = f"{PROVIDER_LABELS.get(cfg['backend'])}: {err}"
            return self._redirect(back, result)
        self._send(404, "Not found", "text/plain")


def main() -> None:
    cfg = pa.load_config()
    if cfg["backend"] not in pa.PROVIDERS:
        raise SystemExit(f"OPENPLAUD_PROVIDER must be one of {', '.join(pa.PROVIDERS)}")
    if DEFAULT_TASK not in pa.TASKS:
        raise SystemExit(f"OPENPLAUD_TASK must be one of {', '.join(pa.TASKS)}")
    if DEFAULT_LOOK not in ui.LOOKS:
        raise SystemExit(f"OPENPLAUD_LOOK must be one of {', '.join(ui.LOOKS)}")
    pa.notes_dir(cfg).mkdir(parents=True, exist_ok=True)
    unknown = settle_claude_inflight()
    if unknown:
        print(f"{unknown} Claude send(s) were under way when the app stopped; marked as not sent (check Claude).", flush=True)
    if claude_on():
        with pa.editing_state() as state:
            claude_state(state).setdefault("since", datetime.now().isoformat())
    elif CLAUDE_URL or CLAUDE_TOKEN:
        print("Send to Claude is off: the fire URL or token is missing or malformed.", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"OpenPlaud listening on :{PORT} ({model_label(cfg)}, look: {DEFAULT_LOOK}, "
          f"send to Claude: {'on' if claude_on() else 'off'})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
