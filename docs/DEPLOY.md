# Deploying Thread to Fly.io

One always-on machine holds every account, each with its own folder on a volume.

**Cost:** roughly $2–3 a month: a shared-cpu-1x / 256 MB machine plus a 1 GB volume (Fly.io pricing, October 2026).

## 1. Account and CLI

```bash
brew install flyctl
fly auth login
```

Turn on two-step sign-in for your Fly.io and GitHub accounts: anyone who can deploy can read the app's secrets.

## 2. Name the app

Pick a name; the address becomes `https://<name>.fly.dev`. Set it in `fly.toml` in two places: `app = "..."`
and `THREAD_PUBLIC_URL`.

## 3. Create the app and volume

```bash
fly launch --no-deploy --copy-config --name <name> --region ewr
fly volumes create thread_data --region ewr --size 1
```

Decline any offer of Postgres or Redis, and keep the existing `fly.toml`.

## 4. Set the secret key

`THREAD_SECRET_KEY` encrypts Canvas tokens, feed links, Google sign-ins and TOTP secrets. The server refuses to
start without it. Generate it in your own terminal and store a copy in a password manager:

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
fly secrets set THREAD_SECRET_KEY=<key> --app <name>
```

If the key is lost, saved Canvas and Google connections can't be decrypted (users reconnect), and accounts with
two-step sign-in need `thread-admin twofa-off`. Notes themselves aren't encrypted with it. Plain files from before
encryption are rewritten sealed the first time they're read.

Optional: `THREAD_OPERATOR` and `THREAD_SUPPORT_EMAIL` fill in the operator name and contact on `/privacy`,
`/terms` and `/support`.

## 5. Deploy

```bash
./scripts/deploy.sh
```

## 6. First account

Sign-up is invite-only, including for the operator. On the 256 MB machine, call the installed binary directly;
`uv run` inside the container uses too much memory.

```bash
fly ssh console -C "/app/.venv/bin/thread-admin invite me"
```

Open the printed `/join/<code>` link and pick an email and a password (12+ characters).

## 7. Integrations

- **Google Calendar:** create a **Web application** OAuth client in Google Cloud Console with
  `https://<name>.fly.dev/google/callback` as a redirect URI, and upload its JSON to
  `/data/config/google-client.json`. While the consent screen is in Testing, add each user under Test users.
- **Canvas:** each user pastes their Calendar Feed link (or an access token) under Settings → Connected accounts.

## 8. Connect an AI app

In Claude: Settings → Connectors → Add custom connector → `https://<name>.fly.dev/mcp`, then sign in with the
Thread account. ChatGPT connects the same way through its developer-mode connectors.

## Operations

| Task | Command |
|---|---|
| Status / logs | `fly status`, `fly logs` |
| Restart | `fly machine restart` |
| Invite someone | `fly ssh console -C "/app/.venv/bin/thread-admin invite 'NOTE'"` |
| List accounts | `fly ssh console -C "/app/.venv/bin/thread-admin users"` |
| Temporary password | `fly ssh console -C "/app/.venv/bin/thread-admin reset EMAIL"` |
| Turn off someone's 2FA | `fly ssh console -C "/app/.venv/bin/thread-admin twofa-off EMAIL"` |
| Bug reports | `fly ssh console -C "/app/.venv/bin/thread-admin feedback"` |

Every support reset is logged to `/data/config/admin-log.jsonl` and shown in the user's Recent sign-ins. Only act
on a request that comes from the account's own email address.

## Tear down

```bash
fly apps destroy <name>
```

This deletes every account's data. Users can download theirs first from Settings → Your data.
