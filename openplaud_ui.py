"""OpenPlaud's page, drawn in one of ten looks.

Every look shows the same data and offers the same actions; only the layout and style differ.
The server builds one context dict per request (see openplaud_web.build_context) and a look renders it.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from collections import OrderedDict
from datetime import date, timedelta

e = html.escape

LOOKS = OrderedDict([
    ("inbox", "Inbox"), ("timeline", "Timeline"), ("actions", "Action Center"), ("command", "Command"),
    ("board", "Pipeline board"), ("reader", "Reader"), ("library", "Library"), ("calendar", "Calendar"),
    ("phone", "Phone"), ("console", "Console"),
])

_LIGHT_BADGES = {"ok_bg": "#e6f4ec", "ok_ink": "#17683a", "warn_bg": "#fff1dc", "warn_ink": "#8a4b00",
                 "bad_ink": "#b42318", "info_bg": "#e9eefc", "info_ink": "#1d3aa3"}
_DARK_BADGES = {"ok_bg": "#13301f", "ok_ink": "#7ee2a8", "warn_bg": "#33260d", "warn_ink": "#ffc35c",
                "bad_ink": "#ff8a7a", "info_bg": "#1d2540", "info_ink": "#a9bcff"}

THEMES = {
    "inbox": dict(_LIGHT_BADGES, bg="#f6f7f9", panel="#ffffff", panel2="#fbfbfc", ink="#16181d", muted="#5b6270",
                  line="#e3e6eb", accent="#2f55d4", accent_ink="#ffffff", radius="10px",
                  font="Inter, system-ui, sans-serif", display="Inter, system-ui, sans-serif", mono="ui-monospace, monospace",
                  fonts="family=Inter:wght@400;500;600;700"),
    "timeline": dict(_LIGHT_BADGES, bg="#f7f5f0", panel="#fffdf8", panel2="#f1ede4", ink="#1d1f1c", muted="#5f625a",
                     line="#e6e1d5", accent="#1f6f5c", accent_ink="#ffffff", radius="16px",
                     font="'DM Sans', system-ui, sans-serif", display="Fraunces, Georgia, serif", mono="ui-monospace, monospace",
                     fonts="family=Fraunces:opsz,wght@9..144,500;9..144,650&family=DM+Sans:wght@400;500;600"),
    "actions": dict(_DARK_BADGES, bg="#11161c", panel="#171e26", panel2="#1c242e", ink="#e8edf2", muted="#9aa6b2",
                    line="#2d3743", accent="#f5a524", accent_ink="#1a1205", radius="12px",
                    font="'IBM Plex Sans', system-ui, sans-serif", display="'IBM Plex Sans', system-ui, sans-serif",
                    mono="'IBM Plex Mono', ui-monospace, monospace",
                    fonts="family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500"),
    "command": dict(_DARK_BADGES, bg="#0b0b0f", panel="#15151c", panel2="#1d1d27", ink="#e9e8f0", muted="#9a97ae",
                    line="#2c2b38", accent="#8b7cff", accent_ink="#0b0b0f", radius="10px",
                    font="Inter, system-ui, sans-serif", display="'JetBrains Mono', ui-monospace, monospace",
                    mono="'JetBrains Mono', ui-monospace, monospace",
                    fonts="family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500;600"),
    "board": dict(_LIGHT_BADGES, bg="#eef0f3", panel="#ffffff", panel2="#e3e6ea", ink="#17191c", muted="#4e5560",
                  line="#cfd4db", accent="#0b6bcb", accent_ink="#ffffff", radius="12px",
                  font="Manrope, system-ui, sans-serif", display="Manrope, system-ui, sans-serif", mono="ui-monospace, monospace",
                  fonts="family=Manrope:wght@500;600;700;800"),
    "reader": dict(_LIGHT_BADGES, bg="#ffffff", panel="#faf9f7", panel2="#f3f1ed", ink="#1a1a1a", muted="#555555",
                   line="#e7e5e1", accent="#1e3a8a", accent_ink="#ffffff", radius="10px",
                   font="Inter, system-ui, sans-serif", display="Newsreader, Georgia, serif", mono="ui-monospace, monospace",
                   fonts="family=Newsreader:opsz,wght@6..72,400;6..72,600&family=Inter:wght@400;500;600"),
    "library": dict(_LIGHT_BADGES, bg="#fbfbfa", panel="#ffffff", panel2="#f3f3f0", ink="#1b1c1d", muted="#66696d",
                    line="#e6e6e2", accent="#0f766e", accent_ink="#ffffff", radius="14px",
                    font="Figtree, system-ui, sans-serif", display="Figtree, system-ui, sans-serif", mono="ui-monospace, monospace",
                    fonts="family=Figtree:wght@400;500;600;700"),
    "calendar": dict(_LIGHT_BADGES, bg="#ffffff", panel="#fafafa", panel2="#eef0ff", ink="#18181b", muted="#52525b",
                     line="#e4e4e7", accent="#4f46e5", accent_ink="#ffffff", radius="12px",
                     font="'Plus Jakarta Sans', system-ui, sans-serif", display="'Plus Jakarta Sans', system-ui, sans-serif",
                     mono="ui-monospace, monospace", fonts="family=Plus+Jakarta+Sans:wght@400;500;600;700"),
    "phone": dict(_DARK_BADGES, bg="#101211", panel="#1b1f1c", panel2="#232824", ink="#eef2ec", muted="#a9b2a6",
                  line="#2b302c", accent="#b6f36a", accent_ink="#101211", radius="18px",
                  font="'Space Grotesk', system-ui, sans-serif", display="'Space Grotesk', system-ui, sans-serif",
                  mono="ui-monospace, monospace", fonts="family=Space+Grotesk:wght@400;500;600;700"),
    "console": dict(_DARK_BADGES, bg="#0c1222", panel="#111a30", panel2="#0c1222", ink="#e5ecf6", muted="#8b9bb4",
                    line="#23304d", accent="#22d3ee", accent_ink="#04202a", radius="14px",
                    font="Sora, system-ui, sans-serif", display="Sora, system-ui, sans-serif",
                    mono="'IBM Plex Mono', ui-monospace, monospace",
                    fonts="family=Sora:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500"),
}

STATUS = {
    "ready": ("Notes ready", "ok"), "filed": ("Filed", "info"), "writing": ("Writing notes…", "warn"),
    "queued": ("Queued", "warn"), "no_transcript": ("No transcript yet", "none"), "new": ("New", "info"),
    "failed": ("Failed · retried next sync", "warn"),
}
FILTER_TESTS = {
    "": lambda r: True, "unread": lambda r: r.get("unread"), "actions": lambda r: r.get("actions_open", 0) > 0,
    "new": lambda r: r["status"] in ("new", "queued", "writing"), "waiting": lambda r: r["status"] == "no_transcript",
    "failed": lambda r: r["status"] == "failed" or bool(r.get("claude_error")), "filed": lambda r: r["status"] == "filed",
}
FILTER_LABELS = [("", "All"), ("unread", "Unread"), ("actions", "Has action items"), ("new", "No notes yet"),
                 ("waiting", "Waiting on Plaud"), ("failed", "Failed"), ("filed", "Filed")]

BASE_CSS = """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--font)}
a{color:var(--accent)} a:hover{opacity:.85}
h1,h2,h3{font-family:var(--display);line-height:1.2}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.muted{color:var(--muted)} .mono{font-family:var(--mono)}
.btn{font:inherit;font-size:14px;min-height:38px;padding:6px 14px;border-radius:calc(var(--radius) * .7);border:1px solid var(--line);
  background:var(--panel);color:var(--ink);cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:6px}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
.btn.small{min-height:32px;padding:4px 10px;font-size:13px}
input[type=text],input[type=password],input[type=search],select{font:inherit;font-size:14px;min-height:38px;padding:6px 10px;
  border-radius:calc(var(--radius) * .7);border:1px solid var(--line);background:var(--bg);color:var(--ink)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.spread{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
form.inline{display:inline-flex;gap:6px;align-items:center;margin:0;flex-wrap:wrap}
.badge{display:inline-block;font-size:12px;padding:2px 8px;border-radius:6px;white-space:nowrap}
.badge.ok{background:var(--ok-bg);color:var(--ok-ink)} .badge.warn{background:var(--warn-bg);color:var(--warn-ink)}
.badge.info{background:var(--info-bg);color:var(--info-ink)} .badge.none{border:1px dashed var(--line);color:var(--muted)}
.flash{padding:10px 14px;border-radius:var(--radius);border:1px solid var(--line);background:var(--panel);margin:0 0 14px}
pre.log{background:var(--panel2);border:1px solid var(--line);border-radius:calc(var(--radius) * .7);padding:10px;max-height:260px;
  overflow:auto;white-space:pre-wrap;font:13px/1.5 var(--mono);margin:0}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{text-align:left;padding:8px 6px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;border-top:none}
.note{line-height:1.65} .note h2{font-size:18px;margin:22px 0 8px} .note h3{font-size:16px} .note li{margin:3px 0}
.note code{font-family:var(--mono);font-size:.9em}
.switch select{min-height:34px;font-size:13px}
details>summary{cursor:pointer;color:var(--muted)}
ul.plain{list-style:none;margin:0;padding:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px}
.stack{display:flex;flex-direction:column;gap:12px}
.topbar{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;justify-content:space-between;padding:10px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
.chips{gap:6px} .chip{font-size:13px;padding:4px 10px;border-radius:999px;border:1px solid var(--line);text-decoration:none;color:var(--ink);white-space:nowrap}
.chip.on{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.searchwrap{max-width:1200px;margin:0 auto;padding:0 20px}
@media (max-width:700px){ .hide-sm{display:none} .topbar{padding:10px 14px} }
"""


# ---------------------------------------------------------------- shared pieces

def theme_css(look: str) -> str:
    t = THEMES[look]
    names = ["bg", "panel", "panel2", "ink", "muted", "line", "accent", "accent_ink", "radius", "font", "display", "mono",
             "ok_bg", "ok_ink", "warn_bg", "warn_ink", "bad_ink", "info_bg", "info_ink"]
    return ":root{" + ";".join(f"--{n.replace('_', '-')}:{t[n]}" for n in names) + "}"


def page(ctx: dict, title: str, body: str, extra_css: str = "", poll: bool = False) -> str:
    look = ctx["look"]
    fonts = THEMES[look]["fonts"]
    poll_js = """<script>
setTimeout(async function poll(){try{const r=await fetch('/api/status');const s=await r.json();
const el=document.getElementById('log');if(el)el.textContent=s.log.join('\\n');
if(s.busy||s.signin==='waiting'){setTimeout(poll,3000)}else{location.reload()}}catch(e){}},3000);
</script>""" if poll else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="stylesheet" href="https://fonts.googleapis.com/css2?{fonts}&amp;display=swap">
<style>{theme_css(look)}{BASE_CSS}{extra_css}</style></head><body class="look-{look}">{body}{poll_js}</body></html>"""


def switcher(ctx: dict) -> str:
    opts = "".join(f'<option value="{k}"{" selected" if k == ctx["look"] else ""}>{e(v)}</option>' for k, v in LOOKS.items())
    return (f'<form class="inline switch" method="post" action="/look"><label><span class="sr-only">Look</span>'
            f'<select name="look" onchange="this.form.submit()" aria-label="Look">{opts}</select></label>'
            f'<input type="hidden" name="back" value="{e(ctx.get("here", "/"))}">'
            f'<noscript><button class="btn small">Apply</button></noscript></form>')


def flash(ctx: dict) -> str:
    if not ctx.get("flash"):
        return ""
    undo = (f'<form class="inline" method="post" action="/notes/undo"><input type="hidden" name="id" value="{e(ctx["undo"])}">'
            f'<input type="hidden" name="back" value="{e(ctx.get("here", "/"))}"><button class="btn small">Undo</button></form>') if ctx.get("undo") else ""
    return f'<div class="flash spread" role="status"><span>{e(ctx["flash"])}</span>{undo}</div>'


def topbar(ctx: dict) -> str:
    """Search and filters, the same in every look, with results when there is a query."""
    chips = []
    for key, label in FILTER_LABELS:
        count = "" if key == "" or not ctx.get("all_recordings") else f' <span class="muted">{sum(1 for r in ctx["all_recordings"] if FILTER_TESTS[key](r))}</span>'
        href = "/" if not key else "/?" + urllib.parse.urlencode({"show": key})
        current = ' aria-current="page"' if ctx.get("show", "") == key else ""
        chips.append(f'<a class="chip{" on" if current else ""}" href="{e(href)}"{current}>{e(label)}{count}</a>')
    results = ""
    if ctx.get("search") is not None:
        hits = []
        for h in ctx["search"]:
            where = f'{e(h["where"])}' if h["kind"] == "note" else f'transcript{(" at " + e(h["stamp"])) if h.get("stamp") else ""}'
            title = f'<a href="{e(h["url"])}">{e(h["title"])}</a>' if h.get("url") else e(h["title"])
            hits.append(f'<li style="padding:8px 0;border-top:1px solid var(--line)">{title} <span class="muted" style="font-size:12px">· {where}</span>'
                        f'<div class="muted" style="font-size:13px">{e(h["snippet"])}</div></li>')
        body = "".join(hits) or '<li class="muted" style="padding:8px 0">Nothing matches every word.</li>'
        results = (f'<section class="panel" style="margin-top:10px"><div class="spread"><strong>{len(ctx["search"])} results for “{e(ctx["q"])}”</strong>'
                   f'<a href="/">Clear</a></div><ul class="plain">{body}</ul></section>')
    return f"""<div class="topbar"><form class="inline" method="get" action="/" role="search"><label><span class="sr-only">Search notes and transcripts</span>
<input type="search" name="q" id="search" value="{e(ctx.get('q', ''))}" placeholder="Search notes and transcripts  ( / )" style="width:min(360px,70vw)"></label>
<button class="btn small">Search</button></form><nav class="row chips" aria-label="Filter recordings">{''.join(chips)}</nav></div>{results}"""


def sync_button(label: str = "Sync now", cls: str = "btn primary") -> str:
    return f'<form class="inline" method="post" action="/sync"><button class="{cls}">{e(label)}</button></form>'


def sync_text(ctx: dict) -> str:
    if ctx["current"]:
        return ctx["current"]
    parts = []
    if ctx["last_sync"]:
        parts.append(f"Synced {ctx['last_sync']}")
    parts.append(f"next {ctx['next_sync']}" if ctx["next_sync"] else ("auto-sync off" if not ctx["sync_every"] else "first sync soon"))
    return " · ".join(parts)


def badge(rec: dict) -> str:
    label, kind = STATUS.get(rec["status"], (rec["status"], "none"))
    unread = '<span class="badge info">Unread</span> ' if rec.get("unread") else ""
    title = f' title="{e(rec["error"])}"' if rec.get("error") and rec["status"] == "failed" else ""
    return f'{unread}<span class="badge {kind}"{title}>{e(label)}</span>'


def task_options(ctx: dict) -> str:
    opts = "".join(f'<option value="{k}"{" selected" if k == ctx["default_task"] else ""}>{e(v)}</option>' for k, v in ctx["tasks"].items())
    return opts + "".join(f'<option value="prompt:{e(name)}">{e(name)}</option>' for name in ctx.get("prompts", []))


def model_select(ctx: dict, label: str) -> str:
    models = ctx.get("models") or []
    if len(models) <= 1:
        return '<input type="hidden" name="model" value="">'
    return (f'<select name="model" aria-label="{e(label)}">'
            + "".join(f'<option value="{e(k)}">{e(v)}</option>' for k, v in models) + "</select>")


def run_form(ctx: dict, rec: dict, compact: bool = False) -> str:
    label = "Run again" if rec.get("note_url") else "Run"
    return (f'<form class="inline" method="post" action="/process"><input type="hidden" name="id" value="{e(rec["id"])}">'
            f'<input type="hidden" name="back" value="{e(ctx.get("here", "/"))}">'
            f'<select name="task" aria-label="What to write for {e(rec["name"])}">{task_options(ctx)}</select>'
            f'{model_select(ctx, "Model for " + rec["name"])}'
            f'<button class="btn{" small" if compact else ""}">{label}</button></form>')


def claude_bits(ctx: dict, rec: dict) -> str:
    """Where this recording stands with Claude: a link to its session, or a button to send it."""
    if rec.get("claude_url"):
        return f'<a class="btn small" href="{e(rec["claude_url"])}" target="_blank" rel="noopener noreferrer">Open in Claude ↗</a>'
    if not ctx.get("claude_on"):
        return ""
    err = f'<span class="muted" style="font-size:12px" title="{e(rec["claude_error"])}">Claude: not sent ⓘ</span>' if rec.get("claude_error") else ""
    return (f'{err}<form class="inline" method="post" action="/claude/send"><input type="hidden" name="id" value="{e(rec["id"])}">'
            f'<input type="hidden" name="back" value="{e(ctx.get("here", "/"))}"><button class="btn small">Send to Claude</button></form>')


def move_form(ctx: dict, note_rel: str, current: str) -> str:
    choices = [f for f in [""] + ctx["folders"] if f != current]
    if not choices:
        return ""
    opts = "".join(f'<option value="{e(f)}">{e(f or "Top level")}</option>' for f in choices)
    return (f'<form class="inline" method="post" action="/notes/move"><input type="hidden" name="note" value="{e(note_rel)}">'
            f'<input type="hidden" name="back" value="{e(ctx.get("here", "/"))}">'
            f'<select name="dest" aria-label="Move to">{opts}</select><button class="btn small">Move</button></form>')


def new_folder_form(ctx: dict, placeholder: str = "New folder") -> str:
    return (f'<form class="inline" method="post" action="/folders"><input type="hidden" name="parent" value="{e(ctx["folder"])}">'
            f'<input type="text" name="name" placeholder="{e(placeholder)}" required maxlength="60" aria-label="New folder name" style="width:150px">'
            f'<button class="btn small">Create</button></form>')


def breadcrumbs(ctx: dict) -> str:
    return " / ".join(f'<a href="{e(url)}">{e(label)}</a>' for label, url in ctx["breadcrumbs"])


def plaud_panel(ctx: dict) -> str:
    p = ctx["plaud"]
    if p["state"] == "signed_in":
        return ""
    if p["state"] == "waiting":
        msg = f'<p style="color:var(--bad-ink)">{e(p["message"])}</p>' if p.get("message") else ""
        return f"""<div class="panel" style="margin-bottom:14px"><h2 style="margin-top:0">Sign in to Plaud</h2>
<ol><li><a class="btn primary" href="{e(p['url'])}" target="_blank" rel="noopener noreferrer">Open the Plaud sign-in page</a> and approve access.</li>
<li>Your browser then goes to a <code>localhost:8199</code> address that doesn't load. That's expected. Copy that whole address and paste it here within 2 minutes:</li></ol>
<form method="post" action="/plaud/callback"><input type="text" name="url" placeholder="http://localhost:8199/auth/callback?code=…" required autofocus style="width:100%">
<p><button class="btn primary">Finish signing in</button></p></form>{msg}</div>"""
    if p["state"] == "busy":
        return '<div class="panel" style="margin-bottom:14px">Plaud is busy. Reload in a moment.</div>'
    msg = f'<p style="color:var(--bad-ink)">{e(p["message"])}</p>' if p.get("failed") and p.get("message") else ""
    return f"""<div class="panel" style="margin-bottom:14px"><h2 style="margin-top:0">Sign in to Plaud</h2>
<p>OpenPlaud reads your recordings through Plaud's official connector.</p>{msg}
<form method="post" action="/plaud/signin"><button class="btn primary">Start Plaud sign-in</button></form></div>"""


def account_bits(ctx: dict) -> str:
    p = ctx["plaud"]
    who = f'<span class="muted">Plaud: {e(p["who"])}</span>' if p["state"] == "signed_in" else ""
    signout = '<form class="inline" method="post" action="/plaud/signout"><button class="btn small">Sign out of Plaud</button></form>' if p["state"] == "signed_in" else ""
    test = f'<form class="inline" method="post" action="/test"><button class="btn small">Test {e(ctx["provider"])}</button></form>'
    logout = '<form class="inline" method="post" action="/logout"><button class="btn small">Lock</button></form>' if ctx["password"] else ""
    claude = f'<a class="btn small" href="/claude">Claude: {"on" if ctx.get("claude_on") else "set up"}</a>'
    spend = ""
    cap_text = f" of ${ctx['cap']:.2f}" if ctx.get("cap") else ""
    if ctx.get("spend") or ctx.get("cap"):
        spend = f'<span class="muted">This month: ${ctx.get("spend", 0):.2f}{cap_text} on paid models</span>'
    return f'<div class="row" style="font-size:13px">{who}{signout}{test}{claude}{spend}{logout}</div>'


def activity(ctx: dict, title: str = "Activity", open_: bool = True) -> str:
    log = e("\n".join(ctx["log"]) or "Nothing yet.")
    head = f'<div class="spread" style="margin-bottom:8px"><strong>{e(title)}</strong><span class="muted" style="font-size:13px">{e(sync_text(ctx))}</span></div>'
    return f'<div class="panel">{head}<pre class="log" id="log">{log}</pre></div>' if open_ else \
        f'<details class="panel"><summary>{e(title)} · {e(sync_text(ctx))}</summary><pre class="log" id="log" style="margin-top:10px">{log}</pre></details>'


def recordings_table(ctx: dict, compact: bool = False) -> str:
    recs = ctx["recordings"]
    if recs is None:
        return f'<p class="muted">{e(ctx.get("recordings_error") or "Recordings appear once Plaud is signed in.")}</p>'
    if not recs:
        return f'<p class="muted">No recordings in the last {ctx["list_days"]} days.</p>'
    rows = "".join(
        f'<tr><td style="width:28px"><input type="checkbox" form="bulkrun" name="ids" value="{e(r["id"])}" aria-label="Select {e(r["name"])}"></td>'
        f'<td class="mono" style="white-space:nowrap">{e(r["when_s"])}</td><td class="hide-sm">{e(r["duration"])}</td>'
        f'<td>{open_link(ctx, r)}</td><td>{badge(r)}</td><td><div class="row">{run_form(ctx, r, compact=True)}{claude_bits(ctx, r)}</div></td></tr>' for r in recs)
    bulk = (f'<form id="bulkrun" class="row" method="post" action="/process-many" style="margin:0 0 8px">'
            f'<input type="hidden" name="back" value="{e(ctx.get("here", "/"))}"><span class="muted" style="font-size:13px">Selected recordings:</span>'
            f'<select name="task" aria-label="What to write for the selected recordings">{task_options(ctx)}</select>'
            f'{model_select(ctx, "Model for the selected recordings")}<button class="btn small">Run selected</button></form>')
    return (f'{bulk}<div class="scroll"><table><tr><th><span class="sr-only">Select</span></th><th>Recorded</th><th class="hide-sm">Length</th>'
            f'<th>Name</th><th>Status</th><th></th></tr>{rows}</table></div>')


def open_link(ctx: dict, rec: dict) -> str:
    if not rec.get("note_url"):
        return e(rec["name"])
    href = "/?" + urllib.parse.urlencode({"note": rec["note_rel"]}) if ctx["look"] in ("inbox", "reader") else rec["note_url"]
    return f'<a href="{e(href)}">{e(rec["name"])}</a>'


def notes_list(ctx: dict, with_move: bool = True) -> str:
    rows = []
    for n in ctx["notes_here"]:
        pick = f'<td style="width:28px"><input type="checkbox" form="bulkmove" name="notes" value="{e(n["rel"])}" aria-label="Select {e(n["title"])}"></td>'
        rows.append(f'<tr>{pick}<td><a href="{e(n["url"])}">{e(n["title"])}</a><div class="muted" style="font-size:12px">{e(n["stem"])}</div></td>'
                    f'<td>{move_form(ctx, n["rel"], ctx["folder"]) if with_move else ""}</td></tr>')
    if not rows:
        return '<p class="muted">No notes in this folder.</p>'
    dests = "".join(f'<option value="{e(f)}">{e(f or "Top level")}</option>' for f in [""] + ctx["folders"] if f != ctx["folder"])
    bulk = (f'<form id="bulkmove" class="row" method="post" action="/notes/move-many" style="margin:0 0 8px">'
            f'<input type="hidden" name="back" value="{e(ctx.get("here", "/"))}"><span class="muted" style="font-size:13px">Selected notes:</span>'
            f'<select name="dest" aria-label="Move selected notes to">{dests}</select><button class="btn small">Move selected</button></form>') if dests else ""
    return f'{bulk}<div class="scroll"><table>{"".join(rows)}</table></div>'


def folders_here(ctx: dict) -> str:
    items = "".join(f'<li><a href="{e(f["url"])}">📁 {e(f["name"])}</a> <span class="muted">{f["count"]}</span></li>' for f in ctx["subfolders"])
    return f'<ul class="plain row" style="gap:8px 20px;margin-bottom:12px">{items}</ul>' if items else ""


def folder_tools(ctx: dict) -> str:
    """Rename or move the folder being looked at (never delete)."""
    if not ctx.get("folder"):
        return ""
    name = ctx["folder"].rsplit("/", 1)[-1]
    dests = "".join(f'<option value="{e(f)}">{e(f or "Top level")}</option>' for f in [""] + ctx["folders"]
                    if f != ctx["folder"] and not f.startswith(ctx["folder"] + "/") and f != (ctx["folder"].rsplit("/", 1)[0] if "/" in ctx["folder"] else ""))
    move = (f'<form class="inline" method="post" action="/folders/move"><input type="hidden" name="folder" value="{e(ctx["folder"])}">'
            f'<input type="hidden" name="back" value="{e(ctx.get("here", "/"))}"><select name="dest" aria-label="Move this folder into">{dests}</select>'
            f'<button class="btn small">Move folder</button></form>') if dests else ""
    return (f'<details style="margin:0 0 12px"><summary>Folder options</summary><div class="row" style="margin-top:8px">'
            f'<form class="inline" method="post" action="/folders/rename"><input type="hidden" name="folder" value="{e(ctx["folder"])}">'
            f'<input type="hidden" name="back" value="{e(ctx.get("here", "/"))}"><input type="text" name="name" value="{e(name)}" required maxlength="60" aria-label="New folder name" style="width:160px">'
            f'<button class="btn small">Rename</button></form>{move}</div></details>')


def recent_moves(ctx: dict) -> str:
    moves = ctx.get("moves") or []
    if not moves:
        return ""
    def place(rel: str) -> str:
        return rel.rsplit("/", 1)[0] if "/" in rel else "Top level"

    rows = "".join(f'<li class="spread" style="padding:4px 0"><span class="muted" style="font-size:13px">{e(m["at"][5:16].replace("T", " "))} · '
                   f'{"Put back: " if m["kind"] == "undo" else ""}{e(m["to"].rsplit("/", 1)[-1])}: {e(place(m["from"]))} → {e(place(m["to"]))}</span>'
                   + ("" if m["kind"] == "undo" else f'<form class="inline" method="post" action="/notes/undo"><input type="hidden" name="id" value="{e(m["id"])}">'
                      f'<input type="hidden" name="back" value="{e(ctx.get("here", "/"))}"><button class="btn small">Undo</button></form>') + '</li>' for m in moves)
    return f'<details style="margin-top:10px"><summary>Recent moves</summary><ul class="plain" style="margin-top:6px">{rows}</ul></details>'


def notes_panel(ctx: dict, title: str = "") -> str:
    return f"""<section class="panel" id="notes"><div class="spread" style="margin-bottom:10px">
<h2 style="margin:0;font-size:16px">{title or breadcrumbs(ctx)}</h2>{new_folder_form(ctx)}</div>
{folder_tools(ctx)}{folders_here(ctx)}{notes_list(ctx)}{recent_moves(ctx)}
<p class="muted" style="font-size:13px;margin-bottom:0">New notes arrive in the top level. Moving never replaces a note, every move can be undone, and nothing here is ever deleted.</p></section>"""


def folder_nav(ctx: dict) -> str:
    links = [f'<a class="navlink{" on" if ctx["folder"] == "" else ""}" href="/#notes">All notes <span class="muted">{ctx["counts"]["notes"]}</span></a>']
    for f in ctx["folder_counts"]:
        depth = f["rel"].count("/")
        on = " on" if ctx["folder"] == f["rel"] else ""
        links.append(f'<a class="navlink{on}" style="padding-left:{10 + depth * 14}px" href="{e(f["url"])}">{e(f["name"])} <span class="muted">{f["count"]}</span></a>')
    return "".join(links)


def actions_list(ctx: dict, limit: int | None = None, show_done: bool = False) -> str:
    items = [a for a in ctx["actions"] if show_done or not a["done"]]
    if limit:
        items = items[:limit]
    if not items:
        return '<p class="muted">No open action items.</p>'
    done_style = ' style="text-decoration:line-through;color:var(--muted)"'
    return "".join(
        f'<form class="actrow" method="post" action="/actions/toggle" style="display:flex;gap:10px;align-items:flex-start;padding:8px 0;margin:0">'
        f'<input type="hidden" name="note" value="{e(a["note_rel"])}"><input type="hidden" name="line" value="{a["line"]}">'
        f'<input type="hidden" name="text" value="{e(a["text"])}"><input type="hidden" name="done" value="{"0" if a["done"] else "1"}">'
        f'<input type="hidden" name="back" value="{e(ctx.get("here", "/"))}">'
        f'<input type="checkbox" id="act-{i}"{" checked" if a["done"] else ""} onchange="this.form.submit()" style="margin-top:4px;width:18px;height:18px">'
        f'<label for="act-{i}"><span{done_style if a["done"] else ""}>{e(a["text"])}</span><br>'
        f'<span class="muted" style="font-size:13px">from <a href="{e(a["note_url"])}">{e(a["note_title"])}</a></span></label>'
        f'<noscript><button class="btn small">{"Reopen" if a["done"] else "Done"}</button></noscript></form>'
        for i, a in enumerate(items))


def render_markdown(md: str) -> str:
    out, para, in_list = [], [], False

    def inline(text: str) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    def flush():
        nonlocal para, in_list
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para = []
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in md.splitlines():
        line = e(raw.rstrip())
        if not line.strip():
            flush()
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)", line)
        item = re.match(r"^\s*[-*]\s+(\[( |x|X)\]\s+)?(.*)", line)
        if heading:
            flush()
            level = min(len(heading.group(1)) + 1, 5)
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif item:
            if para:
                out.append(f"<p>{inline(' '.join(para))}</p>")
                para = []
            if not in_list:
                out.append("<ul>")
                in_list = True
            box = "" if item.group(1) is None else ("☑ " if item.group(2).lower() == "x" else "☐ ")
            out.append(f"<li>{box}{inline(item.group(3))}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            para.append(line)
    flush()
    return "\n".join(out)


def transcript_html(lines: list[str] | None) -> str:
    if lines is None:
        return '<p class="muted">The transcript appears here when Plaud can be reached.</p>'
    parts = []
    for line in lines[:400]:
        m = re.match(r"^\[(\S+) - \S+\] ([^:]+): (.*)$", line)
        parts.append(f'<p style="margin:0 0 10px"><span class="mono muted" style="font-size:12px">{e(m.group(1))}</span> '
                     f'<strong>{e(m.group(2))}</strong> — {e(m.group(3))}</p>' if m else f'<p>{e(line)}</p>')
    return "".join(parts) or '<p class="muted">Plaud has no transcript for this recording.</p>'


def header_bar(ctx: dict, title: str = "OpenPlaud", sub: str = "") -> str:
    return f"""<header class="spread" style="padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel)">
<div class="row"><strong style="font-size:18px;font-family:var(--display)">{e(title)}</strong><span class="muted" style="font-size:13px">{e(sub or ctx['model'])}</span></div>
<div class="row"><span class="muted" style="font-size:13px">{e(sync_text(ctx))}</span>{switcher(ctx)}{sync_button()}</div></header>"""


# ---------------------------------------------------------------- the ten looks

def look_inbox(ctx):
    css = """.navlink{display:flex;justify-content:space-between;gap:8px;padding:8px 10px;border-radius:8px;text-decoration:none;color:var(--ink)}
.navlink.on{background:var(--info-bg);color:var(--info-ink);font-weight:600}
.inbox{display:flex;flex-wrap:wrap;min-height:calc(100vh - 66px)}
.inbox nav{flex:1 1 200px;max-width:250px;padding:16px 12px;border-right:1px solid var(--line);background:var(--panel)}
.inbox .list{flex:1 1 320px;max-width:420px;border-right:1px solid var(--line);background:var(--panel2)}
.inbox .list article{padding:14px 16px;border-bottom:1px solid var(--line)}
.inbox main{flex:999 1 460px;min-width:0;padding:24px 32px;background:var(--panel)}"""
    cards = []
    for r in ctx["recordings"] or []:
        sel = r.get("note_rel") and r["note_rel"] == (ctx["note"] or {}).get("rel")
        cards.append(f"""<article style="{'background:var(--panel);box-shadow:inset 3px 0 0 var(--accent)' if sel else ''}">
<div class="spread"><strong>{open_link(ctx, r)}</strong><span class="muted" style="font-size:12px">{e(r['when_s'])}</span></div>
<div class="muted" style="margin-top:4px;font-size:14px">{e(r['summary'] or '')}</div>
<div class="row" style="margin-top:8px">{badge(r)}<span class="muted" style="font-size:12px">{e(r['duration'])}</span>
{f'<span class="badge warn">{r["actions_open"]} actions</span>' if r['actions_open'] else ''}</div>
<div class="row" style="margin-top:8px">{run_form(ctx, r, compact=True)}{claude_bits(ctx, r)}</div></article>""")
    listing = "".join(cards) if ctx["recordings"] else f'<div style="padding:16px">{recordings_table(ctx)}</div>'
    n = ctx["note"]
    if n:
        reading = f"""<div class="spread"><span class="muted" style="font-size:13px">{e(n['folder'] or 'Top level')}</span>
<div class="row"><a class="btn small" href="{e(n['url'])}">Open</a><a class="btn small" href="{e(n['url'])}?raw=1">Download</a>{move_form(ctx, n['rel'], n['folder'])}</div></div>
<div class="note">{n['html']}</div>"""
    else:
        reading = f'<h2 style="margin-top:0">{breadcrumbs(ctx)}</h2>{folders_here(ctx)}{notes_list(ctx)}<div style="margin-top:12px">{new_folder_form(ctx)}</div>'
    body = f"""{header_bar(ctx)}<div class="inbox"><nav aria-label="Folders"><div class="muted" style="font-size:12px;text-transform:uppercase;letter-spacing:.06em;padding:0 10px 6px">Folders</div>
{folder_nav(ctx)}<div style="margin-top:14px">{activity(ctx, open_=False)}</div><div style="margin-top:10px">{account_bits(ctx)}</div></nav>
<section class="list" aria-label="Recordings">{listing}</section>
<main id="notes">{flash(ctx)}{plaud_panel(ctx)}{reading}</main></div>"""
    return body, css


def look_timeline(ctx):
    css = """.tl{max-width:1120px;margin:0 auto;padding:28px 20px 48px;display:flex;flex-wrap:wrap;gap:28px}
.tl .main{flex:999 1 560px;min-width:0} .tl aside{flex:1 1 260px;max-width:330px;display:flex;flex-direction:column;gap:16px}
.tl h1{font-size:38px;margin:0;letter-spacing:-.02em} .tl h2.day{font-weight:500;font-size:20px;color:var(--muted);margin:26px 0 12px}
.tl article{margin-bottom:14px} .tl h3{font-size:22px;margin:6px 0 8px}
.dark{background:#1f2a26;color:#f2efe6;border-radius:var(--radius);padding:20px} .dark a{color:#9fe0c9}"""
    days: dict = {}
    for r in ctx["recordings"] or []:
        days.setdefault(r["day"], []).append(r)
    blocks = []
    for d, recs in days.items():
        label = "Today" if d == date.today() else "Yesterday" if d == date.today() - timedelta(days=1) else (d.strftime("%A") if d else "")
        blocks.append(f'<h2 class="day">{e(label)} · {e(d.strftime("%B %-d") if d else "Unknown date")}</h2>')
        for r in recs:
            blocks.append(f"""<article class="card"><div class="spread muted" style="font-size:13px"><span>{e(r['time_s'])} · {e(r['duration'])}</span>{badge(r)}</div>
<h3>{e(r['name'])}</h3>{f'<p style="margin:0 0 12px">{e(r["summary"])}</p>' if r['summary'] else ''}
<div class="row">{f'<a href="{e(r["note_url"])}" style="font-weight:600">Read notes</a>' if r['note_url'] else ''}
{f'<span class="muted">{r["actions_open"]} open actions</span>' if r['actions_open'] else ''}<span class="row" style="margin-left:auto">{claude_bits(ctx, r)}{run_form(ctx, r, compact=True)}</span></div></article>""")
    feed = "".join(blocks) if ctx["recordings"] else recordings_table(ctx)
    open_count = sum(1 for a in ctx["actions"] if not a["done"])
    body = f"""<div class="tl"><div class="main"><div class="spread"><h1>Your recordings</h1><div class="row">{switcher(ctx)}{sync_button()}</div></div>
<p class="muted">{e(ctx['model'])} · {e(sync_text(ctx))}</p>{flash(ctx)}{plaud_panel(ctx)}{feed}<div style="margin-top:24px">{notes_panel(ctx)}</div></div>
<aside><section class="dark"><div style="font-size:13px;opacity:.8">Open action items</div><div style="font-family:var(--display);font-size:44px;font-weight:650">{open_count}</div>
{actions_list(ctx, limit=4)}</section>{activity(ctx, open_=False)}<div class="panel">{account_bits(ctx)}</div></aside></div>"""
    return body, css


def look_actions(ctx):
    css = """.ac{max-width:1180px;margin:0 auto;padding:26px 20px 40px}
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px} .tile b{display:block;font-size:28px}
.cols{display:flex;flex-wrap:wrap;gap:20px} .cols .main{flex:999 1 560px;min-width:0} .cols aside{flex:1 1 280px;display:flex;flex-direction:column;gap:14px}
.group{font-family:var(--mono);font-size:12px;letter-spacing:.06em;color:var(--accent);padding:12px 0 4px;border-bottom:1px solid var(--line)}
@media (max-width:700px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}"""
    open_items = [a for a in ctx["actions"] if not a["done"]]
    done = len(ctx["actions"]) - len(open_items)
    week_ago = date.today() - timedelta(days=7)
    this_week = [a for a in open_items if a["day"] and a["day"] >= week_ago]
    older = [a for a in open_items if not (a["day"] and a["day"] >= week_ago)]
    attention = [r for r in ctx["recordings"] or [] if r["status"] in ("new", "no_transcript", "writing", "queued")]

    def group(title, items):
        return f'<div class="group">{e(title)}</div>' + (actions_list(dict(ctx, actions=items)) if items else '<p class="muted">None.</p>')

    body = f"""<div class="ac"><div class="spread"><div><div class="mono muted" style="font-size:12px;letter-spacing:.08em">OPENPLAUD / ACTIONS</div>
<h1 style="margin:6px 0 0;font-size:30px">{len(open_items)} open · {len(this_week)} from this week</h1></div><div class="row">{switcher(ctx)}{sync_button()}</div></div>
<div class="tiles"><div class="tile"><span class="muted">Open</span><b style="color:var(--accent)">{len(open_items)}</b></div>
<div class="tile"><span class="muted">Done</span><b style="color:var(--ok-ink)">{done}</b></div>
<div class="tile"><span class="muted">Notes</span><b>{ctx['counts']['notes']}</b></div>
<div class="tile"><span class="muted">Need attention</span><b style="color:var(--warn-ink)">{len(attention)}</b></div></div>
{flash(ctx)}{plaud_panel(ctx)}<div class="cols"><section class="main panel">{group('FROM THE LAST 7 DAYS', this_week)}{group('EARLIER', older)}
<p class="muted" style="font-size:13px">Action items are read from your notes. Checking them off here is coming in v0.3.0; for now they mirror each note.</p></section>
<aside><section class="panel"><strong>Recordings needing attention</strong><ul class="plain" style="margin-top:8px">{''.join(f'<li style="padding:6px 0">{e(r["name"])} {badge(r)}</li>' for r in attention) or '<li class="muted">Nothing waiting.</li>'}</ul></section>
{activity(ctx, open_=False)}<div class="panel">{account_bits(ctx)}</div></aside></div>
<h2 style="margin-top:28px;font-size:18px">Recordings · last {ctx['list_days']} days</h2><div class="panel">{recordings_table(ctx)}</div>
<div style="margin-top:20px">{notes_panel(ctx)}</div></div>"""
    return body, css


def look_command(ctx):
    css = """.cmd{max-width:1100px;margin:0 auto;padding:26px 20px 40px;font-family:var(--mono)}
.cmd .line{display:flex;gap:20px;padding:10px 12px;border-radius:8px;flex-wrap:wrap} .cmd .line:nth-child(odd){background:var(--panel)}
dialog{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:16px;padding:0;width:min(680px,calc(100% - 32px));box-shadow:0 30px 80px rgba(0,0,0,.6)}
dialog::backdrop{background:rgba(0,0,0,.55)} dialog input{width:100%;border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent;font:500 17px var(--mono);padding:16px 18px;min-height:56px}
dialog ul{list-style:none;margin:0;padding:8px;max-height:52vh;overflow:auto} dialog li a,dialog li button{display:block;width:100%;text-align:left;font:inherit;color:var(--ink);background:transparent;border:0;padding:10px 12px;border-radius:8px;text-decoration:none;cursor:pointer}
dialog li.hit a,dialog li.hit button{background:var(--panel2);box-shadow:inset 2px 0 0 var(--accent)} kbd{font-family:var(--mono);font-size:12px;border:1px solid var(--line);border-radius:6px;padding:1px 6px;color:var(--muted)}"""
    cmds = ['<li data-k="sync now refresh"><form method="post" action="/sync"><button>Sync now</button></form></li>']
    for r in ctx["recordings"] or []:
        if r["note_url"]:
            cmds.append(f'<li data-k="open {e(r["name"].lower())}"><a href="{e(r["note_url"])}">Open notes: {e(r["name"])}</a></li>')
        cmds.append(f'<li data-k="run {e(r["name"].lower())}"><form method="post" action="/process"><input type="hidden" name="id" value="{e(r["id"])}">'
                    f'<input type="hidden" name="task" value="{e(ctx["default_task"])}"><button>Run: {e(r["name"])}</button></form></li>')
        if r.get("claude_url"):
            cmds.append(f'<li data-k="claude open {e(r["name"].lower())}"><a href="{e(r["claude_url"])}" target="_blank" rel="noopener noreferrer">Open in Claude: {e(r["name"])}</a></li>')
        elif ctx.get("claude_on"):
            cmds.append(f'<li data-k="claude send {e(r["name"].lower())}"><form method="post" action="/claude/send"><input type="hidden" name="id" value="{e(r["id"])}">'
                        f'<button>Send to Claude: {e(r["name"])}</button></form></li>')
    for f in ctx["folders"]:
        cmds.append(f'<li data-k="go folder {e(f.lower())}"><a href="{e("/?" + urllib.parse.urlencode({"folder": f}) + "#notes")}">Go to folder: {e(f)}</a></li>')
    for k, v in LOOKS.items():
        cmds.append(f'<li data-k="look theme {e(v.lower())}"><form method="post" action="/look"><input type="hidden" name="look" value="{k}"><button>Look: {e(v)}</button></form></li>')
    lines = "".join(f'<div class="line"><span class="muted">{e(r["when_s"])}</span><span style="flex:1">{open_link(ctx, r)}</span>{badge(r)}</div>'
                    for r in ctx["recordings"] or []) or recordings_table(ctx)
    js = """<script>
const d=document.getElementById('palette'),q=document.getElementById('q'),items=[...d.querySelectorAll('li')];
function filt(){const t=q.value.toLowerCase().trim().split(/\\s+/);let first=true;items.forEach(li=>{const ok=t.every(w=>li.dataset.k.includes(w)||li.textContent.toLowerCase().includes(w));li.hidden=!ok;li.classList.toggle('hit',ok&&first);if(ok)first=false;});}
function open_(){d.showModal();q.value='';filt();q.focus();}
document.addEventListener('keydown',ev=>{if((ev.metaKey||ev.ctrlKey)&&ev.key.toLowerCase()==='k'){ev.preventDefault();open_();}});
document.getElementById('openPalette').addEventListener('click',open_);q.addEventListener('input',filt);
q.addEventListener('keydown',ev=>{if(ev.key==='Enter'){ev.preventDefault();const h=d.querySelector('li.hit a,li.hit button');if(h)h.click();}});
</script>"""
    body = f"""<div class="cmd"><div class="spread"><span class="muted">openplaud ~ {e(ctx['model'])} · {e(sync_text(ctx))}</span>
<div class="row"><button class="btn" id="openPalette" type="button">Commands <kbd>⌘K</kbd></button>{switcher(ctx)}{sync_button()}</div></div>
<div style="margin-top:16px">{flash(ctx)}{plaud_panel(ctx)}</div><div style="margin:12px 0 24px">{lines}</div>
<div style="font-family:var(--font)">{notes_panel(ctx)}<div style="margin-top:16px">{activity(ctx)}</div><div class="panel" style="margin-top:16px">{account_bits(ctx)}</div></div></div>
<dialog id="palette" aria-label="Command palette"><label><span class="sr-only">Command</span><input id="q" type="text" placeholder="Type a command: sync, open…, run…, folder…, look…" autocomplete="off"></label>
<ul>{''.join(cmds)}</ul><div class="row muted" style="padding:10px 16px;border-top:1px solid var(--line);font-size:12px"><span>↵ run</span><span>esc close</span><span>⌘K / Ctrl-K anywhere</span></div></dialog>{js}"""
    return body, css


def look_board(ctx):
    css = """.bd{padding:22px} .cols{display:grid;grid-template-columns:repeat(5,minmax(220px,1fr));gap:14px;overflow-x:auto}
.col{background:var(--panel2);border-radius:var(--radius);padding:12px;min-height:200px} .col h2{font-size:14px;margin:2px 4px 12px;display:flex;justify-content:space-between}
.col .card{margin-bottom:10px;padding:12px}"""
    columns = [("New", ["new"]), ("Waiting on Plaud", ["no_transcript"]), ("Writing", ["writing", "queued"]), ("Ready to file", ["ready"]), ("Filed", ["filed"])]
    cols = []
    for title, kinds in columns:
        recs = [r for r in ctx["recordings"] or [] if r["status"] in kinds]
        cards = []
        for r in recs:
            extra = ""
            if r["status"] in ("new", "no_transcript"):
                extra = run_form(ctx, r, compact=True)
            elif r["status"] in ("ready", "filed") and r.get("note_rel"):
                extra = move_form(ctx, r["note_rel"], r.get("note_folder", ""))
            cards.append(f'<div class="card"><div style="font-weight:700">{open_link(ctx, r)}</div><div class="muted" style="font-size:13px;margin:4px 0 8px">'
                         f'{e(r["when_s"])} · {e(r["duration"])}{" · " + e(r["note_folder"]) if r.get("note_folder") else ""}</div>{extra}'
                         f'<div class="row" style="margin-top:6px">{claude_bits(ctx, r)}</div></div>')
        cols.append(f'<section class="col"><h2>{e(title)}<span class="muted">{len(recs)}</span></h2>{"".join(cards)}</section>')
    board = f'<div class="cols">{"".join(cols)}</div>' if ctx["recordings"] is not None else recordings_table(ctx)
    body = f"""<div class="bd"><div class="spread" style="margin-bottom:14px"><h1 style="margin:0;font-size:24px">Recordings pipeline</h1>
<div class="row"><span class="muted" style="font-size:13px">{e(sync_text(ctx))}</span>{new_folder_form(ctx, "New folder to file into")}{switcher(ctx)}{sync_button()}</div></div>
{flash(ctx)}{plaud_panel(ctx)}{board}<p class="muted" style="font-size:13px">Filing only moves a note; nothing is deleted.</p>
<div style="display:flex;flex-wrap:wrap;gap:16px;margin-top:10px"><div style="flex:2 1 480px">{notes_panel(ctx)}</div><div style="flex:1 1 300px">{activity(ctx)}<div class="panel" style="margin-top:12px">{account_bits(ctx)}</div></div></div></div>"""
    return body, css


def look_reader(ctx):
    css = """.rd{display:flex;flex-wrap:wrap;min-height:calc(100vh - 66px)} .rd nav{flex:1 1 220px;max-width:280px;border-right:1px solid var(--line);padding:16px;background:var(--panel)}
.rd article{flex:999 1 520px;min-width:0;padding:28px 40px;max-width:820px} .rd article .note{font-family:var(--display);font-size:19px}
.rd article .note h2,.rd article .note h3{font-family:var(--font);font-size:15px} .rd aside{flex:1 1 320px;border-left:1px solid var(--line);background:var(--panel);padding:20px;min-width:0;max-height:calc(100vh - 66px);overflow:auto}
.rd nav a.on{font-weight:700}"""
    n = ctx["note"]
    items = "".join(f'<li style="padding:6px 0"><a class="{"on" if n and x["rel"] == n["rel"] else ""}" href="{e("/?" + urllib.parse.urlencode({"note": x["rel"]}))}">{e(x["title"])}</a>'
                    f'<div class="muted" style="font-size:12px">{e(x["folder"] or "Top level")}</div></li>' for x in ctx["recent_notes"][:40])
    if n:
        main = f"""<div class="spread muted" style="font-size:13px"><span>{e(n['folder'] or 'Top level')}</span><div class="row"><a class="btn small" href="{e(n['url'])}?raw=1">Download .md</a>{move_form(ctx, n['rel'], n['folder'])}</div></div>
<div class="note">{n['html']}</div>"""
        side = f'<strong>Transcript</strong><div style="margin-top:12px;font-size:14px">{transcript_html(ctx.get("transcript"))}</div>'
    else:
        main = f'<h1 style="font-size:36px">Pick a note to read</h1><p class="muted">Notes are listed on the left, newest first.</p>{recordings_table(ctx)}'
        side = activity(ctx)
    body = f"""{header_bar(ctx)}<div class="rd"><nav aria-label="Notes"><strong>Notes</strong><ul class="plain" style="margin-top:8px">{items or '<li class="muted">No notes yet.</li>'}</ul>
<div style="margin-top:16px">{new_folder_form(ctx)}</div><div style="margin-top:12px">{account_bits(ctx)}</div></nav>
<article>{flash(ctx)}{plaud_panel(ctx)}{main}</article><aside aria-label="Transcript">{side}</aside></div>
<div style="padding:20px">{notes_panel(ctx, "Folders")}</div>"""
    return body, css


def look_library(ctx):
    css = """.lib{padding:24px 28px} .tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px;margin:18px 0 24px}
.tile{display:flex;flex-direction:column;gap:6px;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px;text-decoration:none;color:var(--ink)}"""
    tiles = "".join(f'<a class="tile" href="{e(f["url"])}"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="1.8" aria-hidden="true">'
                    f'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path></svg><strong>{e(f["name"])}</strong>'
                    f'<span class="muted">{f["count"]} notes</span></a>' for f in ctx["subfolders"])
    tiles += f'<div class="tile" style="border-style:dashed">{new_folder_form(ctx, "Folder name")}</div>'
    body = f"""<div class="lib"><div class="spread"><nav aria-label="Breadcrumb" style="font-size:20px;font-weight:700">{breadcrumbs(ctx)}</nav>
<div class="row"><span class="muted" style="font-size:13px">{e(sync_text(ctx))}</span>{switcher(ctx)}{sync_button()}</div></div>
<div style="margin-top:12px">{flash(ctx)}{plaud_panel(ctx)}</div><div class="tiles" id="notes">{tiles}</div>
<div class="panel">{notes_list(ctx)}</div><p class="muted" style="font-size:13px">Moving never replaces a note; nothing here is ever deleted.</p>
<h2 style="font-size:18px;margin-top:26px">Recordings · last {ctx['list_days']} days</h2><div class="panel">{recordings_table(ctx)}</div>
<div style="margin-top:16px">{activity(ctx, open_=False)}</div><div class="panel" style="margin-top:12px">{account_bits(ctx)}</div></div>"""
    return body, css


def look_calendar(ctx):
    css = """.cal{padding:22px 24px} .week{display:grid;grid-template-columns:repeat(7,minmax(120px,1fr));gap:8px;min-width:900px}
.day{background:var(--panel);border-radius:var(--radius);padding:8px;min-height:420px;display:flex;flex-direction:column;gap:8px}
.ev{display:block;background:var(--panel2);border-left:3px solid var(--accent);border-radius:8px;padding:8px 10px;text-decoration:none;color:var(--ink)}
.ev.sel{background:var(--accent);color:var(--accent-ink)} .ev.none{background:transparent;border:1.5px dashed var(--line);color:var(--muted)}
.ev.warn{border-left-color:#f59e0b}"""
    start = date.today() - timedelta(days=date.today().weekday()) + timedelta(weeks=ctx["week"])
    cols = []
    for i in range(7):
        d = start + timedelta(days=i)
        evs = []
        for r in sorted((r for r in ctx["recordings"] or [] if r["day"] == d), key=lambda r: r["when_s"]):
            cls = "sel" if r["id"] == ctx["rec"] else ("none" if r["status"] == "no_transcript" else "warn" if r["status"] in ("writing", "queued", "new") else "")
            href = "/?" + urllib.parse.urlencode({"week": ctx["week"], "rec": r["id"]})
            evs.append(f'<a class="ev {cls}" href="{e(href)}"><div style="font-size:12px;opacity:.8">{e(r["time_s"])} · {e(r["duration"])}</div><strong>{e(r["name"])}</strong></a>')
        cols.append(f'<div><div style="font-weight:600;padding:4px;color:var(--muted)">{e(d.strftime("%a %-d"))}</div><div class="day">{"".join(evs)}</div></div>')
    sel = next((r for r in ctx["recordings"] or [] if r["id"] == ctx["rec"]), None)
    detail = (f"""<div class="muted" style="font-size:13px">{e(sel['when_s'])} · {e(sel['duration'])}</div><h2 style="font-size:18px;margin:4px 0 10px">{e(sel['name'])}</h2>
{badge(sel)}<p>{e(sel['summary'] or 'No notes yet.')}</p><div class="stack">{f'<a href="{e(sel["note_url"])}">Open notes</a>' if sel['note_url'] else ''}{run_form(ctx, sel)}{claude_bits(ctx, sel)}
{move_form(ctx, sel['note_rel'], sel.get('note_folder', '')) if sel.get('note_rel') else ''}</div>""") if sel else '<p class="muted">Pick a recording on the calendar to see it here.</p>'
    nav = lambda w, label: f'<a class="btn small" href="{e("/?" + urllib.parse.urlencode({"week": w}))}">{label}</a>'
    body = f"""<div class="cal"><div class="spread" style="margin-bottom:14px"><div class="row"><h1 style="margin:0;font-size:22px">{e(start.strftime('%b %-d'))} – {e((start + timedelta(days=6)).strftime('%b %-d'))}</h1>
{nav(ctx['week'] - 1, '‹ Earlier')}{nav(0, 'This week')}{nav(ctx['week'] + 1, 'Later ›')}</div><div class="row">{switcher(ctx)}{sync_button()}</div></div>
{flash(ctx)}{plaud_panel(ctx)}<div style="display:flex;flex-wrap:wrap;gap:18px"><div class="scroll" style="flex:999 1 640px;min-width:0"><div class="week">{''.join(cols)}</div></div>
<aside class="panel" style="flex:1 1 280px;max-width:360px;align-self:flex-start">{detail}</aside></div>
<div style="margin-top:20px">{notes_panel(ctx)}</div><div style="margin-top:16px">{activity(ctx, open_=False)}</div><div class="panel" style="margin-top:12px">{account_bits(ctx)}</div></div>"""
    return body, css


def look_phone(ctx):
    css = """.ph{max-width:480px;margin:0 auto;padding:22px 16px 110px} .ph h1{font-size:30px;margin:0}
.ph .card{margin-bottom:12px;border:0} .ph .btn{min-height:44px}
.tabbar{position:fixed;left:0;right:0;bottom:0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));background:var(--panel);border-top:1px solid var(--line);padding:6px 6px 18px}
.tabbar a{display:flex;flex-direction:column;align-items:center;gap:2px;padding:8px 0;color:var(--muted);text-decoration:none;font-size:12px} .tabbar a:first-child{color:var(--accent)}
.ph section{scroll-margin-top:12px} .ph h2{font-size:18px;margin:26px 0 12px}"""
    cards = "".join(f"""<article class="card"><div class="spread muted" style="font-size:13px"><span>{e(r['when_s'])} · {e(r['duration'])}</span>{badge(r)}</div>
<div style="font-weight:600;font-size:17px;margin:4px 0">{open_link(ctx, r)}</div>{f'<div class="muted">{e(r["summary"])}</div>' if r['summary'] else ''}
<div class="row" style="margin-top:10px">{run_form(ctx, r)}{claude_bits(ctx, r)}</div></article>""" for r in ctx["recordings"] or []) or recordings_table(ctx)
    body = f"""<div class="ph"><div class="spread"><h1>Today</h1>{sync_button('Sync')}</div><p class="muted">{e(sync_text(ctx))}</p>
<div class="row" style="margin-bottom:10px">{switcher(ctx)}</div>{flash(ctx)}{plaud_panel(ctx)}
<section id="recordings">{cards}</section><section id="actions"><h2>Action items</h2><div class="card">{actions_list(ctx, limit=10)}</div></section>
<section id="notes-sec"><h2>Notes</h2>{notes_panel(ctx, breadcrumbs(ctx))}</section><section id="activity"><h2>Activity</h2>{activity(ctx)}<div class="card" style="margin-top:12px">{account_bits(ctx)}</div></section></div>
<nav class="tabbar" aria-label="Sections"><a href="#recordings">Recordings</a><a href="#actions">Actions</a><a href="#notes-sec">Notes</a><a href="#activity">Activity</a></nav>"""
    return body, css


def look_console(ctx):
    css = """.con{max-width:1200px;margin:0 auto;padding:24px 20px} .tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px} .tile b{display:block;font-size:18px;margin-top:4px}
.two{display:flex;flex-wrap:wrap;gap:16px} .two>*{flex:1 1 440px;min-width:0} @media (max-width:700px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}"""
    p = ctx["plaud"]
    waiting = sum(1 for r in ctx["recordings"] or [] if r["status"] == "no_transcript")
    queue_n = sum(1 for r in ctx["recordings"] or [] if r["status"] in ("writing", "queued"))
    body = f"""<div class="con"><div class="spread"><div><div class="mono muted" style="font-size:12px">OpenPlaud · {e(ctx['model'])}</div><h1 style="margin:4px 0 0;font-size:26px">Console</h1></div>
<div class="row">{switcher(ctx)}{sync_button()}</div></div>
<div class="tiles"><div class="tile"><span class="muted" style="font-size:12px">Plaud</span><b style="color:{'var(--ok-ink)' if p['state'] == 'signed_in' else 'var(--bad-ink)'}">{'Signed in' if p['state'] == 'signed_in' else 'Not signed in'}</b></div>
<div class="tile"><span class="muted" style="font-size:12px">Sync</span><b>{e(ctx['next_sync'] or ('off' if not ctx['sync_every'] else 'soon'))}</b><span class="muted" style="font-size:12px">{f"every {ctx['sync_every']} min · last {ctx['sync_days']} days" if ctx['sync_every'] else 'automatic sync is off'}</span></div>
<div class="tile"><span class="muted" style="font-size:12px">Working on</span><b>{queue_n} recordings</b><span class="muted" style="font-size:12px">{e(ctx['current'] or 'idle')}</span></div>
<div class="tile"><span class="muted" style="font-size:12px">Waiting on Plaud</span><b>{waiting} recordings</b><span class="muted" style="font-size:12px">no transcript yet · retried each sync</span></div></div>
<p class="muted" style="font-size:13px;margin:-6px 0 14px">This month on paid models: ${ctx.get('spend', 0):.2f}{(" of $" + format(ctx['cap'], '.2f') + " cap") if ctx.get('cap') else " (no cap)"}{(" · " + e(ctx['long_rule'])) if ctx.get('long_rule') else ""}</p>
{flash(ctx)}{plaud_panel(ctx)}<div class="two"><section class="panel"><h2 style="font-size:15px;margin-top:0">Recordings</h2>{recordings_table(ctx)}</section>{activity(ctx, "Recent runs")}</div>
<div style="margin-top:16px">{notes_panel(ctx)}</div><div class="panel" style="margin-top:16px">{account_bits(ctx)}
<p class="muted" style="font-size:13px;margin-bottom:0">Keys, provider, schedule and the default look are set in BoxPilot (OpenPlaud › Settings). Notes are kept forever.</p></div></div>"""
    return body, css


RENDERERS = {"inbox": look_inbox, "timeline": look_timeline, "actions": look_actions, "command": look_command,
             "board": look_board, "reader": look_reader, "library": look_library, "calendar": look_calendar,
             "phone": look_phone, "console": look_console}


def dashboard(ctx: dict) -> str:
    body, css = RENDERERS[ctx["look"]](ctx)
    bar = topbar(ctx)
    if ctx.get("search") is not None:
        bar = bar.replace('</div><section class="panel"', '</div><div class="searchwrap"><section class="panel"', 1) + "</div>"
    return page(ctx, "OpenPlaud", bar + body, css, poll=ctx["busy"] or ctx["plaud"]["state"] == "waiting")


def note_page(ctx: dict, note: dict, transcript: list[str] | None = None, with_transcript: bool = False) -> str:
    side = (f'<aside class="panel" style="flex:1 1 320px;min-width:0;max-height:80vh;overflow:auto"><strong>Transcript</strong>'
            f'<div style="margin-top:12px;font-size:14px">{transcript_html(transcript)}</div></aside>') if with_transcript else ""
    custom = ""
    if note.get("plaud_id"):
        custom = f"""<details class="panel" style="margin-top:16px"><summary>Run this recording with your own prompt</summary>
<form method="post" action="/process" class="stack" style="margin-top:10px"><input type="hidden" name="id" value="{e(note['plaud_id'])}">
<input type="hidden" name="task" value="custom"><input type="hidden" name="back" value="{e(ctx.get('here', '/'))}">
<label for="prompt" class="muted" style="font-size:13px">What should the model write? (A new note is made; this one stays.)</label>
<textarea id="prompt" name="prompt" rows="4" required maxlength="8000" style="font:inherit;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink)" placeholder="Write a SOAP note from this visit."></textarea>
<div class="row">{model_select(ctx, "Model for this prompt")}<button class="btn primary">Run</button>
<label class="muted" style="font-size:13px">Save as <input type="text" name="name" maxlength="40" placeholder="Template name" style="width:150px"></label>
<button class="btn small" formaction="/prompts/save">Save prompt</button></div></form></details>"""
    body = f"""<div style="max-width:{1240 if with_transcript else 860}px;margin:0 auto;padding:22px 20px 48px">
<div class="spread" style="margin-bottom:16px"><a href="{e(note['back'])}">← {e(note['folder'] or 'OpenPlaud')}</a>
<div class="row"><a class="btn small" href="{e(note['url'])}?raw=1">Download Markdown</a>{move_form(ctx, note['rel'], note['folder'])}{switcher(ctx)}</div></div>
{flash(ctx)}<div style="display:flex;flex-wrap:wrap;gap:20px"><div class="panel note" style="flex:999 1 520px;min-width:0">{note['html']}</div>{side}</div>{custom}</div>"""
    return page(ctx, note["title"], body)


def claude_page(ctx: dict) -> str:
    if ctx["claude_on"]:
        status = (f'<p><span class="badge ok">On</span> Recordings made after {e((ctx["since"] or "")[:16].replace("T", " "))} '
                  f'are sent automatically once their notes are written. {ctx["sent"]} sent so far; '
                  f'{ctx["fires_hour"]} of 30 sends used this hour.</p>')
    else:
        missing = [label for label, ok in (("the fire URL", ctx["url_ok"]), ("the token", ctx["token_set"])) if not ok]
        status = f'<p><span class="badge none">Off</span> Add {" and ".join(missing)} in BoxPilot (OpenPlaud › Settings) to turn it on.</p>'
    failures = "".join(f'<li><span class="mono">{e(rid)}</span>: {e(f["error"])}</li>' for rid, f in ctx["failed"].items())
    body = f"""<div style="max-width:820px;margin:0 auto;padding:24px 20px 48px"><div class="spread"><a href="/">← OpenPlaud</a>{switcher(ctx)}</div>
<h1>Send to Claude</h1>{flash(ctx)}{status}
{f'<div class="panel"><strong>Not sent</strong><ul>{failures}</ul><p class="muted" style="font-size:13px;margin-bottom:0">Rate limits and overloads are retried on the next sync. Other errors wait for you: fix the setting, then use Send to Claude on the recording.</p></div>' if failures else ''}
<h2>How it works</h2><p>Each new recording's notes and transcript start a session in a Claude <em>scheduled task</em> on your Claude account. The session appears with your other sessions at claude.ai/code and in the Claude app's Code tab, where you can read it and keep chatting. It uses your Claude subscription, not an API key.</p>
<h2>Set it up once</h2><ol class="stack" style="padding-left:20px">
<li>Open <a href="https://claude.ai/code/routines" target="_blank" rel="noopener noreferrer">claude.ai/code/routines</a> and create a new scheduled task. Paste the prompt below. Under Connectors, keep only what it needs (Plaud, if you added it).</li>
<li>Add an <strong>API</strong> trigger, copy the <strong>Fire URL</strong>, click <strong>Generate token</strong> and copy the token (it is shown once).</li>
<li>In BoxPilot, open OpenPlaud › Settings and paste them into <strong>Claude fire URL</strong> and <strong>Claude token</strong>.</li></ol>
<div class="panel"><div class="spread"><strong>Prompt for the scheduled task</strong></div><pre class="log" style="max-height:none;margin-top:8px">{e(ctx['prompt'])}</pre></div>
<p class="muted" style="font-size:13px">Claude allows 30 sends an hour per task. Long transcripts are cut to fit Claude's 65,536-character limit; the message says where, with the Plaud file ID. A send that times out is never repeated automatically, because every send starts a new session.</p></div>"""
    return page(ctx, "Send to Claude", body)


def login_page(ctx: dict, error: str = "") -> str:
    err = f'<p style="color:var(--bad-ink)">{e(error)}</p>' if error else ""
    return page(ctx, "OpenPlaud sign-in", f"""<div class="panel" style="max-width:360px;margin:60px auto"><h1 style="margin-top:0">OpenPlaud</h1>{err}
<form method="post" action="/login"><p><input type="password" name="password" placeholder="Password" aria-label="Password" required autofocus style="width:100%"></p>
<button class="btn primary">Sign in</button></form><p class="muted">The password is in BoxPilot, on this app's card.</p></div>""")
