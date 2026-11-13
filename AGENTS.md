# AGENTS.md

Instructions for coding agents (Claude Code, Codex, Cursor, Copilot, Gemini CLI) working in this repo.
Humans: start with [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## What this is
Thread is a memory for AI apps, stored as plain Markdown notes, served over MCP, with an
Obsidian-style map and Canvas LMS + Google Calendar tools for students. Python 3.12, managed with `uv`.

## Setup
```bash
uv sync                    # installs from uv.lock; Python 3.12 is picked up from .python-version
uv run pytest -q           # ~280 tests, about 1 minute, no network or accounts needed
```
If the tests pass, the checkout is good. Nothing else (API keys, Canvas, Google) is needed to run or test Thread.

## Try it out (step by step)
Always use a throwaway notes folder so nothing touches a real `~/thread-brain`:
```bash
export THREAD_BRAIN_DIR="$(mktemp -d)"
```

1. **See the tools.** Open the MCP Inspector against the stdio server and call `remember`, then `search_memory`:
   ```bash
   npx @modelcontextprotocol/inspector uv run thread
   ```
   The note appears as a `.md` file in `$THREAD_BRAIN_DIR`.
2. **Use it from Claude Code.** Register the local checkout, then ask the AI to remember something and recall it
   in a new session:
   ```bash
   claude mcp add thread-dev -e THREAD_BRAIN_DIR="$THREAD_BRAIN_DIR" -- uv run --directory "$PWD" thread
   ```
3. **See the map.** Fill the folder with synthetic notes and open the single-user web app:
   ```bash
   uv run python scale-lab/gen.py "$THREAD_BRAIN_DIR" 300
   uv run thread-home                                  # http://127.0.0.1:8765
   ```
4. **Try the multi-account server** (sign-in, invites, OAuth for AI apps) with a throwaway account in a temp dir:
   ```bash
   scripts/preview-online.sh                           # http://127.0.0.1:8771, prints the login + an invite code
   ```
   Ctrl+C stops it and deletes the temp account.

Canvas and Google Calendar need the tester's own credentials; skip them unless the task is about those
integrations. Their tests use fakes, so `pytest` covers them without real accounts.

## Commands
```bash
uv run pytest -q                                    # all tests
uv run pytest tests/test_brain.py -q                # one file
uv run pytest -q -k oauth                           # tests matching a name
uv run thread                                       # MCP server over stdio
uv run thread-home                                  # single-user web app, http://127.0.0.1:8765
uv run thread-online                                # multi-account server (needs THREAD_SECRET_KEY, see .env.example)
```

## Layout
- `src/thread/server.py`: MCP tools and the instructions sent to the AI; `main_online` builds the full app.
- `src/thread/brain.py`: the note store (read/write notes, `[[wikilinks]]`, note index, map graph).
- `src/thread/auth.py`, `accounts.py`: sign-in, sessions, OAuth 2.1 provider, `thread-admin` CLI.
- `src/thread/home.py`: web routes and JSON API. `src/thread/web/`: the front end (plain JS + vendored d3).
- `src/thread/canvas*.py`, `gcal.py`: integrations. `sealed.py`: encryption at rest. `twofactor.py`: TOTP.
- `extension/`: Chrome extension (chat backup). `scale-lab/`: synthetic data + load checks.
- `tests/`: pytest. External services are faked; no test touches the network.

## Rules
- Every bug fix gets a regression test. Every new MCP tool gets tests and a read-only/destructive annotation.
- Tool descriptions and server instructions are prompts the AI reads: keep them short and literal.
- Text read from notes, Canvas or chats goes back to the AI fenced as data. Keep that fencing.
- Don't weaken security code (CSP, CSRF checks, rate limits, the SSRF guard, token hashing, Fernet sealing)
  without a test showing the new behavior is still safe. See `docs/SECURITY-threat-model.md`.
- No new dependencies without a reason in the PR description.
- Run `uv run pytest -q` before saying a change works.

## Never
- Commit `.env`, real tokens, real notes, `accounts.json`, or anything under a brain/data folder.
- Point tests or scripts at a real `~/thread-brain`; use `tmp_path`, a `mktemp -d` folder, or `scale-lab/gen.py`.
- Hardcode a deployment URL, email address or invite code. Use `THREAD_PUBLIC_URL` and the env vars in `.env.example`.
- Run `fly deploy` or touch a live server from an agent session.
