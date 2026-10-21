# OpenPlaud

Run your Plaud recordings through **your** model: Claude, ChatGPT, or a local model such as Qwen on
Ollama. OpenPlaud pulls each recording's transcript from your Plaud account and writes Markdown notes:
meeting notes, action items, or a follow-up email.

The Plaud recorder and app can't be pointed at a custom model, so OpenPlaud works on the transcript after
Plaud syncs it. It reads Plaud through Plaud's official MCP server (`@plaud-ai/mcp`).

There are two ways to run it: as a web app (a container, installable from the BoxPilot App Catalog) or as
a command-line script.

## Web app (Docker / BoxPilot)

```bash
docker run -d -p 8795:8795 -v openplaud:/data \
  --add-host host.docker.internal:host-gateway \
  -e OPENPLAUD_PASSWORD=choose-one -e ANTHROPIC_API_KEY=sk-ant-... \
  ghcr.io/aes256afro/openplaud:0.5.0
```

Open `http://<server>:8795`, sign in, then **Start Plaud sign-in**. Plaud's page sends your browser to a
`localhost:8199` address that doesn't load. Paste that address into OpenPlaud to finish signing in.
After that, recordings sync on a schedule, and each one can also be run by hand.

### Settings

| Variable | What it does | Default |
| --- | --- | --- |
| `OPENPLAUD_PROVIDER` | `claude`, `chatgpt` or `local` | `claude` |
| `ANTHROPIC_API_KEY` | Claude API key | |
| `CLAUDE_MODEL` | Claude model | `claude-haiku-5-5` |
| `OPENAI_API_KEY` | OpenAI API key, for ChatGPT | |
| `OPENAI_MODEL` | OpenAI model | `gpt-5-mini` |
| `OLLAMA_URL` | Ollama address for local models | `http://localhost:11434` |
| `LOCAL_MODEL` | Ollama model name | `qwen3:30b-a3b` |
| `OPENPLAUD_TASK` | What auto-sync writes: `summary`, `actions` or `followup` | `summary` |
| `OPENPLAUD_SYNC_EVERY` | Minutes between automatic syncs; `0` turns it off | `60` |
| `OPENPLAUD_SYNC_DAYS` | How far back each sync looks, in days | `7` |
| `OPENPLAUD_PASSWORD` | Password for the web page; empty means no password | |
| `OPENPLAUD_CHECK_TOKEN` | Secret for the check trigger, `POST /api/check` (see below); empty turns the trigger off | |
| `OPENPLAUD_CHECK_ON_OPEN` | Opening the page checks Plaud when the last check is older than this many minutes; `0` turns it off | `5` |
| `OPENPLAUD_ALLOWED_HOSTS` | Extra host names the page answers to, comma-separated (e.g. `notes.example.org`). IP addresses, `localhost`, `*.local`, single-label names and `*.ts.net` always work; any other name gets *421 Unknown host name*, which blocks DNS-rebinding pages. | |
| `OPENPLAUD_LOOK` | Default look: `inbox`, `timeline`, `actions`, `command`, `board`, `reader`, `library`, `calendar`, `phone` or `console`. Each browser can pick its own from the page. | `inbox` |
| `OPENPLAUD_LIST_DAYS` | How many days of recordings the page lists | `30` |
| `OPENPLAUD_MONTHLY_CAP` | Most to spend on paid models in a calendar month, in dollars; `0` means no cap. A capped month refuses models it can't price. | `0` |
| `OPENPLAUD_PRICES` | Prices for models OpenPlaud doesn't know (ChatGPT ones), in dollars per million input/output tokens: `gpt-5=1.25/10,gpt-5-mini=0.25/2`. Needed for those runs to count toward the cap. | |
| `OPENPLAUD_LONG_MINUTES` / `OPENPLAUD_LONG_MODEL` | Recordings longer than this many minutes use this model instead, e.g. `120` and `claude:claude-sonnet-5-5` | |
| `CLAUDE_ROUTINE_URL` | Fire URL of a Claude scheduled task's API trigger (Send to Claude) | |
| `CLAUDE_ROUTINE_TOKEN` | That trigger's token | |

### Send to Claude

With a Claude scheduled task's API trigger configured, each new recording's notes and transcript start their own
session in your Claude account, visible at claude.ai/code and in the Claude app's Code tab. It runs on your Claude
subscription (Pro, Max, Team or Enterprise). Set it up from the page's **Claude** button, which shows the prompt to
paste into the task. Only recordings made after it is switched on are sent automatically; older ones have a
**Send to Claude** button. Claude allows 30 sends an hour per task, and long transcripts are cut to 65,536 characters.

### Looks

The page comes in ten looks that show the same recordings, notes and actions in different layouts:
**Inbox** (folders, list and reader), **Timeline** (a day-by-day feed), **Action Center** (action items from every
note), **Command** (a keyboard palette on ⌘K / Ctrl-K), **Pipeline board** (columns from new to filed),
**Reader** (notes beside the transcript), **Library** (folder tiles), **Calendar** (a week view), **Phone**
(one column with a tab bar) and **Console** (sync and status tiles). The look menu at the top of the page switches
it for that browser; `OPENPLAUD_LOOK` sets the default for everyone else.

### Notes are never deleted

OpenPlaud has no delete. Running a recording again, or moving a note into a folder that already holds that
name, writes `name-2.md` beside the original. Folders organize notes in `/data/notes`.

Notes, the Plaud sign-in and the record of processed recordings live in `/data`.

## Checking for new recordings right away

Besides the schedule (60 minutes by default), a check starts when:

- you press **Check Plaud now** on the page;
- you open the page and the last check is more than 5 minutes old (`OPENPLAUD_CHECK_ON_OPEN`, minutes; `0` turns it off);
- a recent recording is still waiting on Plaud for its transcript: the app checks again every 3 minutes for up to an hour, so it shows up soon after Plaud finishes;
- something calls the **check trigger**: `POST /api/check` with the header `Authorization: Bearer <OPENPLAUD_CHECK_TOKEN>`. It needs no page password, only that token (16 characters or more; BoxPilot generates one, shown under the app's Secrets).

An iPhone Shortcut that checks after a meeting: add **Get Contents of URL**, set the URL to
`https://<your-box>.ts.net:8795/api/check`, Method **POST**, and a header `Authorization` = `Bearer <your check token>`.
Run it from the Home Screen, the Action button or an automation (for example, when the Plaud app closes).
From a computer: `curl -X POST -H "Authorization: Bearer $TOKEN" https://<your-box>.ts.net:8795/api/check`.

## Command line

```bash
npm install                    # installs the Plaud MCP server locally
uv run plaud_ai.py login       # opens a browser to sign in to Plaud
uv run plaud_ai.py check       # confirms Plaud and each provider are reachable
uv run plaud_ai.py list        # last 7 days; ✓ = already processed
uv run plaud_ai.py sync        # process every new recording from the last 7 days
uv run plaud_ai.py process <file_id> --task actions --print
uv run plaud_ai.py --backend local process <file_id> --prompt "Write a SOAP note from this visit"
```

Provider settings for the command line live in `plaud_ai.toml`; the environment variables above override
it. API keys can also go in a `.env` file next to the script (git-ignored).

Long recordings that exceed a local model's context are summarized in parts, then combined.
