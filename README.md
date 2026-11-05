<!-- mcp-name: io.github.astribus/thread-memory -->
<p align="center">
  <img src="brand/thread-logo-400.png" width="96" alt="Thread logo">
</p>

<h1 align="center">Thread</h1>

<p align="center">
  <b>A memory any AI can read and write, kept as plain Markdown files you own.</b><br>
  An MCP server for Claude, ChatGPT and Cursor, with an Obsidian-style map and tools for students' Canvas deadlines and Google Calendar.
</p>

<p align="center">
  <a href="https://github.com/Astribus/thread-memory/actions/workflows/tests.yml"><img src="https://github.com/Astribus/thread-memory/actions/workflows/tests.yml/badge.svg" alt="tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.12+-blue" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/MCP-27%20tools-black" alt="27 MCP tools">
</p>

![Thread's map view, with synthetic notes](docs/images/map-desktop.png)

## Try it in 30 seconds

You need [uv](https://docs.astral.sh/uv/getting-started/installation/). Nothing else to install or sign up for.

**Claude Code**
```bash
claude mcp add thread -- uvx --from git+https://github.com/Astribus/thread-memory thread
```

**Claude Desktop, Cursor, or any MCP client** (add to the client's MCP config file):
```json
{
  "mcpServers": {
    "thread": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Astribus/thread-memory", "thread"]
    }
  }
}
```

Then tell the AI something like *"remember that my stats exam is on Dec 10"* and, in a new chat,
*"what do you know about my exams?"*. Notes land in `~/thread-brain` as Markdown (set `THREAD_BRAIN_DIR` to
use another folder, e.g. an Obsidian vault). To see the map:

```bash
uvx --from git+https://github.com/Astribus/thread-memory thread-home   # http://127.0.0.1:8765
```

ChatGPT and claude.ai connect to remote servers only. For those, [self-host the online server](#self-host-the-online-server).

## What it does

- **Memory over MCP.** 27 tools (`remember`, `search_memory`, `read_note`, `catch_up`, `log_chat`, `whats_due`,
  `read_syllabus`, `add_to_calendar`, ...) let an AI save and recall notes in the signed-in user's own folder.
  Each tool carries read-only/destructive annotations.
- **Plain files.** One Markdown note per topic with `[[wikilinks]]`, readable and editable in any editor or Obsidian.
- **The map.** Every note is a dot, every `[[wikilink]]` a thread. A d3-force simulation matched to Obsidian's
  graph view, drawn on a single HTML canvas; it holds 60 fps while zooming and dragging at 3,000 notes under a 4×
  CPU throttle.
- **Canvas LMS.** Deadlines from the student's calendar feed link or an access token; syllabus text pulled from
  the syllabus page, wiki pages and PDFs; read-only tools for announcements, assignments, pages and modules.
- **Google Calendar.** Read upcoming events and add new ones the user asks for.
- **Fingerprint and voice.** Short, visible notes about how the user works and writes, ranked by recency, so the
  AI can answer the way they like.
- **Chat backup.** An optional Chrome extension keeps the last 10 days of ChatGPT/Claude chats, searchable from
  the AI.
- **Accounts (online server).** Invite-only sign-up, OAuth 2.1 for AI apps, optional authenticator-app two-step sign-in.

| Map on a phone (dark) | |
|---|---|
| <img src="docs/images/map-iphone-dark.png" width="260" alt="Thread map on an iPhone in dark mode"> | The phone layout is portrait-only: a bottom tab bar (Home · Map · Search · Me), pinch to zoom, drag a dot to pull its neighbours. |

## How it compares

- **Built-in ChatGPT / Claude memory:** locked to one app, and you can't see the whole thing as files. Thread works
  with any MCP client and is just a folder.
- **[Basic Memory](https://github.com/basicmachines-co/basic-memory):** the closest project (Markdown notes over
  MCP, works with Obsidian). It's a general-purpose knowledge base under AGPL-3.0. Thread is MIT and adds
  student tools (Canvas, syllabus, calendar), a built-in map, and a multi-account server with OAuth for
  ChatGPT/claude.ai.

> **Status:** a personal project that works and is tested (280 tests, CI on every push). Issues and small pull
> requests are welcome; it's maintained on a best-effort basis.

## Architecture

```mermaid
flowchart LR
    subgraph Clients
        AI["Claude / ChatGPT<br/>(MCP client)"]
        Web["Browser<br/>(map, notes, settings)"]
        Ext["Chrome extension<br/>(chat backup)"]
    end
    subgraph Server["thread-online (Starlette + uvicorn)"]
        OAuth["OAuth 2.1 server<br/>auth.py"]
        MCP["MCP server /mcp<br/>server.py"]
        Home["Web app + JSON API<br/>home.py"]
        WS["Workspaces<br/>one folder per account"]
    end
    subgraph Storage["Fly volume /data"]
        Brain["users/&lt;id&gt;/brain/*.md"]
        Secrets["sealed tokens<br/>(Fernet)"]
        Accts["accounts.json<br/>(hashes only)"]
    end
    Canvas["Canvas LMS"]
    GCal["Google Calendar"]

    AI -- "Bearer token" --> MCP
    AI -- "authorize / token" --> OAuth
    Web -- "session cookie" --> Home
    Ext -- "backup key" --> Home
    MCP --> WS
    Home --> WS
    OAuth --> Accts
    WS --> Brain
    WS --> Secrets
    WS --> Canvas
    WS --> GCal
```

- `src/thread/server.py`: MCP tools and the instructions handed to the AI; `main_online` builds the full app.
- `src/thread/auth.py`, `accounts.py`: sign-in, sessions, the OAuth provider, `thread-admin`.
- `src/thread/home.py`: web routes and JSON API for the home screen.
- `src/thread/brain.py`: reading and writing notes, links, the cached note index and the map graph.
- `src/thread/canvas*.py`, `gcal.py`: integrations. `sealed.py`: encryption at rest. `twofactor.py`: TOTP.
- `src/thread/web/`: the single-page front end (`app.js`, `style.css`, `index.html`, vendored d3).
- `extension/`: the Chrome extension.

## Tech stack

Python 3.12 · [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 2.2 · Starlette 1.7 · uvicorn ·
httpx · cryptography (Fernet) · segno (QR codes) · pypdf · Google API client · d3 7.9 (force simulation, zoom) ·
HTML Canvas 2D · pytest · Playwright (visual checks) · Docker · Fly.io · uv.

## Develop

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest -q                                    # the test suite
THREAD_BRAIN_DIR=/tmp/my-brain uv run thread-home   # single-user web app at http://127.0.0.1:8765
uv run thread                                       # MCP server over stdio (Claude Code, Claude Desktop)
```

`thread-home` and `thread` use `~/thread-brain` unless `THREAD_BRAIN_DIR` is set.

To try the multi-account online server on your laptop with a throwaway account:

```bash
scripts/preview-online.sh
```

Synthetic data for screenshots and load tests:

```bash
uv run python scale-lab/gen.py /tmp/synth 300
uv run python scale-lab/serve.py /tmp/synth 8800
```

## Self-host the online server

The online server runs as one Fly.io machine with a 1 GB volume (about $2–3 a month at the time of writing).
See [docs/DEPLOY.md](docs/DEPLOY.md). It refuses to start without `THREAD_SECRET_KEY`; [.env.example](.env.example)
lists every setting.

## Security

- Passwords are scrypt hashes; sessions, OAuth tokens, invite codes and backup keys are stored only as SHA-256
  hashes.
- Canvas tokens, feed links, Google refresh tokens and TOTP secrets are encrypted at rest with Fernet, keyed by a
  server secret that never touches the volume.
- Refresh-token rotation with reuse detection: a replayed refresh token revokes the whole chain.
- Per-account lockout plus a per-IP limit keyed on `Fly-Client-IP`, not the spoofable `X-Forwarded-For`.
- Optional TOTP (RFC 6238, ±1 step, replay-protected) with scrypt-hashed recovery codes, and step-up
  re-authentication before sensitive actions.
- Strict CSP with per-response nonces, HSTS, CSRF checks on every form POST, an SSRF guard on outbound fetches,
  upload size caps.
- Stored text returned to the AI is fenced and labelled as data, to blunt prompt injection.

See [SECURITY.md](SECURITY.md) and [docs/SECURITY-threat-model.md](docs/SECURITY-threat-model.md).

## Tests

```bash
uv run pytest -q
```

About 280 tests cover the note store, the MCP tools, sign-in and OAuth flows, two-step sign-in, encryption,
Canvas and Calendar parsing (with fakes), the security fixes (one regression test each), and scale checks.

## Lessons learned

The hard problem wasn't storage or retrieval, it was capture. Everything Thread knows has to be written by
the AI calling `remember` or `log_chat`, and in practice the AI apps rarely did:

- Claude and ChatGPT ship their own built-in memory, and when the model decides to remember something it
  reaches for the first-party tool.
- Some clients load connector tools lazily, so Thread's tools and instructions often aren't in context when a
  chat starts, and "call `catch_up` first" never fires.
- Over two weeks of daily use, the server received no new notes from either test account.

A memory layer that depends on the model choosing to call it loses to the host app's own memory. Capture has to
happen without the model's cooperation (a browser extension, an export import, or owning the chat surface), and
on phones, where most student chats happen, the native apps allow none of those. That's why this stayed a
project.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bugs and ideas: [open an issue](https://github.com/Astribus/thread-memory/issues/new/choose).
Security problems: report privately, see [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE). Third-party components are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
