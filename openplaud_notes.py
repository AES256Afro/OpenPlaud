"""Notes on disk: folders, safe paths, and what the page reads out of each note.

Nothing here deletes or overwrites a note: moves land on a free name (see plaud_ai.unique_path).
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import unicodedata
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

import plaud_ai as pa

# Names start with a letter or digit (any script), so none is hidden or "..". Folder names can't end in .md.
FILLERS = {"\u115f", "\u1160", "\u3164", "\uffa0"}  # Hangul fillers: letters to Unicode, but they show as nothing


class NamePattern:
    """A file or folder name: starts with a letter or digit (any script), then letters, digits, marks (accents,
    vowel signs), spaces, dots, dashes and underscores. So no name is hidden, "..", or invisible."""

    def __init__(self, limit: int, folder: bool = False):
        self.limit, self.folder = limit, folder

    def fullmatch(self, name: str) -> bool:
        if not name or len(name) > self.limit or (self.folder and name.lower().endswith(".md")):
            return False
        if unicodedata.category(name[0])[0] not in "LN" or any(ch in FILLERS for ch in name):
            return False
        return all(ch in " ._-" or unicodedata.category(ch)[0] in "LN" or unicodedata.category(ch) in ("Mn", "Mc")
                   for ch in name)


FOLDER_NAME = NamePattern(60, folder=True)
PATH_PART = NamePattern(200)
# Every change to the notes folder (moves, renames, ticks) happens one at a time.
NOTES_LOCK = threading.RLock()
ACTION_LINE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*)$")
PLAUD_ID_LINE = re.compile(r"Plaud file ID:\s*`([A-Za-z0-9_-]+)`")


def inside(root: Path, rel: str) -> Path | None:
    """The path `rel` names under `root`, or None when it is malformed or would leave it."""
    rel = (rel or "").rstrip("/")
    if rel.startswith("/"):
        return None
    if rel and not all(PATH_PART.fullmatch(part) for part in rel.split("/")):
        return None
    path = (root / rel).resolve()
    return path if path == root or root in path.parents else None


def rel_of(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def note_url(root: Path, path: Path) -> str:
    return "/notes/" + urllib.parse.quote(rel_of(root, path))


def folder_url(rel: str, **extra: str) -> str:
    params = {k: v for k, v in {"folder": rel, **extra}.items() if v}
    return ("/?" + urllib.parse.urlencode(params) if params else "/") + "#notes"


def all_folders(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(rel_of(root, p) for p in root.rglob("*")
                  if p.is_dir() and not any(s.startswith(".") for s in p.relative_to(root).parts))


def all_notes(root: Path) -> list[Path]:
    if not root.exists():
        return []
    found = []
    for p in root.rglob("*.md"):
        if any(s.startswith(".") for s in p.relative_to(root).parts):
            continue
        try:
            if p.is_file():
                found.append((p.stat().st_mtime, p))
        except OSError:  # moved away while listing
            continue
    return [p for _, p in sorted(found, key=lambda pair: pair[0], reverse=True)]


def note_for(root: Path, entry: dict | None, rid: str | None = None) -> Path | None:
    """Where a processed recording's note is now, following it if it was moved outside the app. A note found by
    its file name only counts if it names the same recording (`rid`), so two same-named notes never get mixed up."""
    if not entry or not entry.get("file"):
        return None
    path = Path(entry["file"]).resolve()
    if path.exists() and root in path.parents:
        return path
    for candidate in sorted(root.rglob(path.name)):
        try:
            if rid is None or plaud_id(candidate.read_text()) == rid:
                return candidate
        except OSError:
            continue
    return None


MOVES_LOG = "moves.log"


def _repoint(old: Path, new: Path, folder: bool = False) -> None:
    """Keep each recording's link to its note when the note (or a folder holding it) moves."""
    old, new = old.resolve(), new.resolve()
    with pa.editing_state() as state:
        for entry in state["processed"].values():
            path = Path(entry["file"]).resolve() if entry.get("file") else None
            if not path:
                continue
            if path == old:
                entry["file"] = str(new)
            elif folder and old in path.parents:
                entry["file"] = str(new / path.relative_to(old))


def _log_move(root: Path, kind: str, src: Path, dst: Path) -> str:
    move_id = uuid.uuid4().hex[:12]
    with (root.parent / MOVES_LOG).open("a") as f:
        f.write(json.dumps({"id": move_id, "at": datetime.now().isoformat(timespec="seconds"), "kind": kind,
                            "from": rel_of(root, src) if src != root else "", "to": rel_of(root, dst)}) + "\n")
    return move_id


def move_history(root: Path, limit: int = 50) -> list[dict]:
    path = root.parent / MOVES_LOG
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines()[-500:]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries[::-1][:limit]


def _place(src: Path, wanted: Path) -> Path:
    """Move a file to `wanted`, or to the first free -2, -3… name beside it. Never replaces a file: the new
    name is claimed with a hard link (which fails if the name exists) before the old one is removed."""
    for _ in range(1000):
        target = pa.unique_path(wanted)
        try:
            os.link(src, target)
        except FileExistsError:
            continue
        except OSError:  # a filesystem without hard links: fall back to a checked rename (under NOTES_LOCK)
            if os.path.lexists(target):
                continue
            src.rename(target)
            return target
        src.unlink()
        return target
    raise ValueError(f"Couldn't find a free name for {wanted.name}.")


def move_note(root: Path, note: Path, folder: Path) -> tuple[Path, str]:
    """Move a note into a folder. A name that is taken gets a -2 suffix; nothing is replaced.
    Returns the new path and the move's id (for undo)."""
    with NOTES_LOCK:
        if note.parent.resolve() == folder.resolve():
            raise ValueError("That note is already in that folder.")
        target = _place(note, folder / note.name)
        _repoint(note, target)
        return target, _log_move(root, "note", note, target)


def undo_move(root: Path, move_id: str) -> Path | None:
    """Put a moved note or folder back where it was (under a free name if that spot is taken now)."""
    with NOTES_LOCK:
        entry = next((m for m in move_history(root, 500) if m["id"] == move_id), None)
        if not entry:
            return None
        current, original = inside(root, entry["to"]), inside(root, entry["from"])
        if not current or not current.exists() or not original:
            return None
        if current.is_dir() and (original == current or current in original.parents):
            return None
        original.parent.mkdir(parents=True, exist_ok=True)
        is_dir = current.is_dir()
        if is_dir:
            back = _free_dir(original)
            current.rename(back)
        else:
            back = _place(current, original)
        _repoint(current, back, folder=is_dir)
        _log_move(root, "undo", current, back)
        return back


def _free_dir(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while path.with_name(f"{path.name}-{n}").exists():
        n += 1
    return path.with_name(f"{path.name}-{n}")


def rename_folder(root: Path, folder: Path, new_name: str) -> tuple[Path, str]:
    """Rename a folder. Refuses a name already used beside it rather than merging two folders."""
    if folder == root or not FOLDER_NAME.fullmatch(new_name):
        raise ValueError("Folder names use letters, numbers, spaces, dots, dashes and underscores.")
    with NOTES_LOCK:
        target = folder.parent / new_name
        if target.exists():
            raise ValueError(f"There is already a folder called {new_name} here.")
        folder.rename(target)
        _repoint(folder, target, folder=True)
        return target, _log_move(root, "folder", folder, target)


def move_folder(root: Path, folder: Path, dest: Path) -> tuple[Path, str]:
    """Move a folder into another one (never into itself). A taken name gets a -2 suffix."""
    if folder == root or dest == folder or folder in dest.parents:
        raise ValueError("A folder can't be moved into itself.")
    if folder.parent == dest:
        raise ValueError("That folder is already there.")
    with NOTES_LOCK:
        target = _free_dir(dest / folder.name)
        folder.rename(target)
        _repoint(folder, target, folder=True)
        return target, _log_move(root, "folder", folder, target)


def set_action(path: Path, line_no: int, expected: str, done: bool) -> bool:
    """Tick or untick one action item in a note, changing only that line's box, and only if the line
    still reads as the page showed it. Written through a temporary file, so the note is never half-saved."""
    with NOTES_LOCK:
        with path.open(newline="") as f:  # keep the note's own line endings
            lines = f.read().split("\n")
        if not 0 <= line_no < len(lines):
            return False
        m = ACTION_LINE.match(lines[line_no].rstrip("\r"))
        if not m or re.sub(r"\*\*|`", "", m.group(2)).strip() != expected:
            return False
        lines[line_no] = re.sub(r"\[( |x|X)\]", "[x]" if done else "[ ]", lines[line_no], count=1)
        mode = stat.S_IMODE(path.stat().st_mode)  # keep the note's own permissions
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", newline="") as f:
                os.fchmod(f.fileno(), mode)
                f.write("\n".join(lines))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)  # only our own unfinished temporary file
        return True


def search(root: Path, query: str, titles: dict[str, str], limit: int = 40) -> list[dict]:
    """Notes and transcripts containing every word of the query. `titles` maps Plaud ids to recording names."""
    words = [w.lower() for w in query.split() if w.strip()][:8]
    if not words:
        return []
    results = []
    for path in all_notes(root):
        text = path.read_text()
        low = text.lower()
        if all(w in low for w in words):
            results.append({"kind": "note", "title": note_title(text, path.stem), "url": note_url(root, path),
                            "where": rel_of(root, path.parent) if path.parent != root else "Top level",
                            "snippet": _snippet(text, words[0])})
    tdir = pa.TRANSCRIPTS_DIR
    if tdir.exists():
        for path in sorted(tdir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True):
            text = path.read_text()
            low = text.lower()
            if not all(w in low for w in words):
                continue
            line = next((l for l in text.splitlines() if words[0] in l.lower()), "")
            stamp = line[1:line.find(" ")] if line.startswith("[") else ""
            results.append({"kind": "transcript", "title": titles.get(path.stem, path.stem), "id": path.stem, "stamp": stamp,
                            "snippet": _snippet(line or text, words[0])})
            if len(results) >= limit:
                break
    return results[:limit]


def _snippet(text: str, word: str, width: int = 160) -> str:
    flat = re.sub(r"\s+", " ", re.sub(r"[#*`>|]", "", text))
    i = flat.lower().find(word)
    start = max(0, i - width // 3) if i >= 0 else 0
    out = flat[start:start + width].strip()
    return ("…" if start else "") + out + ("…" if start + width < len(flat) else "")


def note_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def note_summary(text: str, limit: int = 260) -> str:
    """The first paragraph under "Summary", else the first prose paragraph after the metadata."""
    lines = text.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if re.match(r"^#{1,3}\s+Summary\b", line)), None)
    candidates = lines[start:] if start is not None else lines
    para: list[str] = []
    for line in candidates:
        stripped = line.strip()
        if not stripped:
            if para:
                break
            continue
        if stripped.startswith(("#", "- ", "* ", "|", "```")):
            if para:
                break
            continue
        para.append(stripped)
    summary = re.sub(r"\*\*|`", "", " ".join(para))
    return summary if len(summary) <= limit else summary[:limit].rsplit(" ", 1)[0] + "…"


def note_actions(text: str, with_lines: bool = False) -> list[dict]:
    out = []
    for i, line in enumerate(text.split("\n")):
        m = ACTION_LINE.match(line.rstrip("\r"))
        if m:
            item = {"done": m.group(1).lower() == "x", "text": re.sub(r"\*\*|`", "", m.group(2)).strip()}
            if with_lines:
                item["line"] = i
            out.append(item)
    return out


def plaud_id(text: str) -> str | None:
    m = PLAUD_ID_LINE.search(text)
    return m.group(1) if m else None


def weekly_digest(root: Path, today, days: int = 7) -> tuple[str, int]:
    """A digest of the notes for recordings made in the last `days` days: one line each, then every open action
    item. Built from the notes themselves (no model, no cost). Returns (markdown, number of notes)."""
    from datetime import date, timedelta
    since = today - timedelta(days=days - 1)
    picked = []
    for path in all_notes(root):
        try:
            text = path.read_text()
        except OSError:
            continue
        if text.startswith("# Weekly digest"):
            continue
        m = re.search(r"^- Date: (\d{4}-\d{2}-\d{2})", text, re.M)
        try:
            day = date.fromisoformat(m.group(1)) if m else None
        except ValueError:
            day = None
        if day and since <= day <= today:
            picked.append((day, path, text))
    picked.sort(key=lambda item: item[0])
    lines = [f"# Weekly digest {since:%-d %b} – {today:%-d %b %Y}", "",
             f"- Date: {today.isoformat()}", f"- Notes: {len(picked)}", "", "## Recordings"]
    open_items = []
    for day, path, text in picked:
        title = note_title(text, path.stem)
        rel = rel_of(root, path)
        summary = note_summary(text, 200)
        label = title.replace("[", "(").replace("]", ")")
        lines.append(f"- {day:%a %-d %b} · [{label}](/notes/{urllib.parse.quote(rel)})" + (f": {summary}" if summary else ""))
        open_items += [(a["text"], title) for a in note_actions(text) if not a["done"]]
    if not picked:
        lines.append("No notes for recordings from these days.")
    lines += ["", "## Open action items"]
    # Plain bullets, not checkboxes: the items live in their own notes, and ticking happens there.
    lines += [f"- {item} _(from {title})_" for item, title in open_items] or ["None."]
    return "\n".join(lines) + "\n", len(picked)
