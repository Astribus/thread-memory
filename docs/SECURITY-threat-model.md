# Thread threat model

Update this when a new entry point, integration or kind of stored data is added.

## What we're protecting (assets)

| Asset | Where it lives | Why it matters |
|---|---|---|
| Notes ("memory") | `users/<id>/brain/*.md` on the Fly volume; `~/thread-brain` on a laptop | Everything a student tells their AI: grades, health, money, relationships (privacy is "light" by default) |
| Fingerprint + voice samples | `brain/` (`fingerprint.json`, voice notes) | A profile of how someone thinks and writes; enough to impersonate them in writing |
| Chat backups (10 days) | `users/<id>/chats/` | Whole ChatGPT/Claude conversations, word for word, including deleted ones |
| Account passwords | `accounts.json` (scrypt hashes) | Reused passwords unlock other sites |
| Sessions, OAuth access/refresh tokens, invite codes, backup keys | `accounts.json` (SHA-256 hashes only) | A stolen token = full read/write of someone's memory through their AI |
| Canvas feed URL, Google Calendar refresh token | `users/<id>/` (file mode 600; Fernet-encrypted online) | Canvas feed shows every course and deadline; Google token can write to the calendar |
| Server secrets | Fly secrets (`THREAD_SECRET_KEY`, Google client secret) | Unlocks everything above |

## Who might attack (actors)

1. **Random internet scanner / bot**: hits every public URL, tries common passwords, oversized bodies, SSRF payloads.
2. **Another Thread user (signed in)**: tries to read someone else's notes, chats or tokens (cross-account access).
3. **A malicious website the user visits**: CSRF, login CSRF, clickjacking, tries to talk to the laptop server on 127.0.0.1.
4. **A fake "AI app"**: registers itself through open dynamic client registration (anyone can) with a name like "Claude", phishes a one-tap approval.
5. **Prompt injection**: text inside a note, a backed-up chat, a syllabus PDF or a Canvas item that tells the AI to delete, leak or change things.
6. **A malicious browser extension or page** talking to Thread's extension to steal its backup key.
7. **Someone with the laptop** (lost/stolen, roommate) or with **Fly host access** (Fly staff, a Fly breach, a leaked deploy token).
8. **Supply chain**: a compromised PyPI package or a CDN script.

## Entry points

| Entry point | Auth | Main risks |
|---|---|---|
| `/signin`, `/signup`, `/signup/check`, `/join` | none (invite code for sign-up) | password guessing, spraying, login CSRF, invite brute force |
| `/`, `/api/*`, `/static/*` (home app) | session cookie (HttpOnly, Secure, SameSite=Lax) | XSS via note titles/content, CSRF, cross-account access |
| `/register`, `/authorize`, `/login`, `/token`, `/revoke` (OAuth) | open registration; password or one-tap | malicious redirect URIs, phishing via fake app names, token theft/replay |
| `/mcp` (AI tools) | Bearer token, audience-checked | prompt injection, tool misuse, cross-account reads |
| `/backup/chat` (extension upload) | Bearer backup key, CORS `*` | huge bodies, key theft |
| Canvas feed fetch (server makes outbound HTTP) | — | SSRF into Fly's private network / metadata, DNS rebinding, huge responses |
| Google OAuth callback | state parameter | token mix-up |
| Laptop server `127.0.0.1:8765` | none (Origin + JSON content-type check) | DNS rebinding / drive-by from websites |
| Chrome extension (content scripts on chatgpt.com / claude.ai, popup, background) | backup key in `chrome.storage.local` | message spoofing, key exfiltration |
| `thread-admin` CLI | shell access on the Fly machine | whoever has `fly ssh` has everything |

## Trust boundaries

- Browser ↔ Fly edge (TLS, Fly terminates) ↔ app (plain HTTP inside Fly's network; `Fly-Client-IP` is set by the proxy).
- App ↔ AI client: the AI is **not trusted** to follow instructions found in stored data.
- App ↔ Anthropic/OpenAI: whatever a tool returns is sent to the AI provider. Thread can't encrypt that away.
- One account ↔ another: enforced by `Workspaces.get(user)` and per-user token subjects.
