# Security

## Reporting a problem

Please report vulnerabilities privately through GitHub's "Report a vulnerability" (Security → Advisories) on
this repository rather than in a public issue.

This is a personal project with no hosted service and no bug bounty, so expect best-effort replies.

## What's in place

- **Credentials.** Passwords are scrypt hashes. Sessions, OAuth access/refresh tokens, invite codes and extension
  backup keys are stored only as SHA-256 hashes, so a copied `accounts.json` can't be replayed.
- **Encryption at rest.** Canvas tokens, Canvas feed links, Google refresh tokens and TOTP secrets are sealed with
  Fernet (AES-128-CBC + HMAC-SHA256) using `THREAD_SECRET_KEY`, a deployment secret kept off the data volume.
  The online server refuses to start without it.
- **Sign-in.** Per-account lockout (5 failures / 15 minutes) and a per-IP limit; constant-time comparison for
  unknown emails; optional TOTP two-step sign-in with replay protection and scrypt-hashed recovery codes; step-up
  re-authentication for export, password change, 2FA changes and new backup keys; "sign out everywhere else".
- **OAuth for AI apps.** OAuth 2.1 with PKCE, strict redirect-URI validation (https, loopback, or a custom
  scheme), audience-bound tokens (RFC 8707), refresh-token rotation with reuse detection, and a warning plus a
  typed password for apps that aren't on a short allowlist.
- **Web.** CSP with a per-response script nonce, HSTS, `nosniff`, no framing, Origin checks on every form POST
  (including sign-in and sign-out), SameSite cookies, HTML escaping of note titles.
- **Outbound requests.** Canvas feed fetches are limited to public addresses and re-checked against the address
  actually connected to (SSRF and DNS rebinding), with size caps.
- **Prompt injection.** Notes, backed-up chats and syllabus text returned to the AI are fenced and labelled as
  data, and the instructions tell the AI to treat them as content, not commands.

The threat model is in [docs/SECURITY-threat-model.md](docs/SECURITY-threat-model.md).

## Known gaps

- scrypt runs at N=2^14 to fit a 256 MB machine; OWASP recommends 2^17.
- No email verification or password-reset email (there is no mail provider); support resets go through
  `thread-admin`.
- No passkeys/WebAuthn.
- No key rotation tooling for `THREAD_SECRET_KEY` yet.
