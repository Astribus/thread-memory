"""Authentication for online mode: accounts, invite-only sign-up, browser sessions, and the OAuth 2.1
authorization server used by MCP clients.

Clients register dynamically, send the user to /login, and receive a short-lived access token and a
rotating refresh token bound to one account.

- Passwords are stored as scrypt hashes; invite codes, tokens and sessions as SHA-256 hashes.
- Failed sign-ins are throttled; an account locks for 15 minutes after 5 failures.
- Sign-up requires a one-time invite code (`thread-admin invite`).
"""


import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from html import escape
from pathlib import Path

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

# Account storage and the admin CLI live in thread.accounts (no web dependencies).
from thread.security import client_ip
from thread.accounts import (   # noqa: F401  (re-exported)
    ACCESS_TTL, ACCOUNT_COOKIE, LOCK_SECONDS, MAX_FAILURES, AUTH_FILE, CODE_TTL, CONFIG_DIR, EMAIL_RE, LOCKED, MIN_PASSWORD, PENDING_TTL,
    REFRESH_TTL, SESSION_COOKIE, SESSION_TTL, STEP_UP_SECONDS, WRONG, AuthStore, _DUMMY_HASH, _hash_token, admin_cli,
    check_password, hash_password, new_invite_code, normalize_email,
)

# Per-IP failure limit across all accounts, against password spraying.
MAX_IP_FAILURES = 30   # per 15 min; high enough for shared campus NAT
# Known clients get one-tap approval. Any other dynamically registered client must re-enter the
# password and sees a warning, so a client named "Claude" can't phish a signed-in user.
TRUSTED_APP_HOSTS = {"claude.ai", "claude.com", "chatgpt.com", "chat.openai.com", "localhost", "127.0.0.1", "::1"}
MAX_CLIENTS = 2000   # cap on registered clients; tokenless ones are evicted first
BAD_REDIRECT_SCHEMES = {"javascript", "data", "file", "vbscript", "blob", "about"}
# Lifetime of the TOTP step after a correct password.
SECOND_STEP_TTL = 5 * 60
WRONG_CODE = "That code didn't work. Codes change every 30 seconds, so use the newest one."
CODE_LOCKED = "Too many wrong codes. Try again in 15 minutes."


def device_name(user_agent: str) -> str:
    """Coarse browser/OS label, e.g. 'Safari on iPhone'. Nothing else from the user agent is stored."""
    ua = user_agent or ""
    system = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                          ("CrOS", "Chromebook"), ("Mac OS X", "Mac"), ("Macintosh", "Mac"),
                                          ("Windows", "Windows"), ("Linux", "Linux")) if key in ua), "")
    browser = next((name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                           ("FxiOS", "Firefox"), ("CriOS", "Chrome"), ("Chrome/", "Chrome"),
                                           ("Safari/", "Safari")) if key in ua), "")
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown browser"


class Lockout:
    """Wrong-password counting per account, shared by the app sign-in and the browser sign-in, plus a looser
    per-address count keyed "ip:<address>"."""

    def __init__(self):
        self.failures: dict[str, list[float]] = {}

    def locked(self, email: str) -> bool:
        now = time.time()
        key = normalize_email(email)
        self.failures[key] = [t for t in self.failures.get(key, []) if now - t < LOCK_SECONDS]
        limit = MAX_IP_FAILURES if key.startswith("ip:") else MAX_FAILURES
        return len(self.failures[key]) >= limit

    async def failed(self, email: str) -> None:
        self.failures.setdefault(normalize_email(email), []).append(time.time())
        await anyio.sleep(1)   # throttle guessing without blocking the event loop


class LoopbackAwareClient(OAuthClientInformationFull):
    """Client that matches loopback redirect URIs ignoring the port (RFC 8252 §7.3); others match exactly."""

    def validate_redirect_uri(self, redirect_uri):
        if redirect_uri is not None and redirect_uri not in (self.redirect_uris or []):
            if (redirect_uri.scheme == "http" and redirect_uri.host in ("localhost", "127.0.0.1")
                    and any(r.scheme == "http" and r.host == redirect_uri.host and (r.path or "/") == (redirect_uri.path or "/")
                            for r in (self.redirect_uris or []))):
                return redirect_uri
        return super().validate_redirect_uri(redirect_uri)


class ThreadAuthProvider:
    """The OAuth server pieces the MCP SDK needs. Every code and token carries the account it's for."""

    def __init__(self, store: AuthStore, base_url: str):
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.pending: dict[str, tuple[float, OAuthClientInformationFull, AuthorizationParams]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.lockout = Lockout()
        # Pending second factor: ticket hash -> (time, account, purpose). In memory; a restart means re-entering the password.
        self.second_step: dict[str, tuple[float, str, str]] = {}

    # ---- second factor (browser and OAuth sign-in) ----
    def new_ticket(self, user: str, purpose: str) -> str:
        now = time.time()
        self.second_step = {k: v for k, v in self.second_step.items() if now - v[0] < SECOND_STEP_TTL}
        ticket = secrets.token_urlsafe(32)
        self.second_step[_hash_token(ticket)] = (now, user, purpose)
        return ticket

    def ticket_user(self, ticket: str, purpose: str) -> str | None:
        entry = self.second_step.get(_hash_token(ticket or ""))
        if not entry or time.time() - entry[0] >= SECOND_STEP_TTL or entry[2] != purpose:
            return None
        return entry[1]

    async def check_code(self, user: str, code: str, request: Request) -> str | None:
        """None if the code is valid (and consumes it), else an error message. Failures count toward the
        per-account and per-IP limits."""
        ip = "ip:" + client_ip(request)
        if self.lockout.locked("2fa:" + user) or self.lockout.locked(ip):
            return CODE_LOCKED
        if not self.store.check_second_step(user, code):
            await self.lockout.failed("2fa:" + user)
            await self.lockout.failed(ip)
            return WRONG_CODE
        return None

    # ---- dynamic client registration ----
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.store.data["clients"].get(client_id)
        return LoopbackAwareClient.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        from urllib.parse import urlparse
        from mcp.server.auth.provider import RegistrationError
        # Allowed redirect URIs: https, loopback http (native clients), or a custom scheme.
        for uri in client_info.redirect_uris or []:
            u = urlparse(str(uri))
            scheme = (u.scheme or "").lower()
            if scheme in BAD_REDIRECT_SCHEMES or (scheme == "http" and u.hostname not in ("localhost", "127.0.0.1", "::1")):
                raise RegistrationError("invalid_redirect_uri", "Redirect addresses must be https or a local address.")
        clients = self.store.data["clients"]
        if len(clients) >= MAX_CLIENTS:
            # Registration is open, so cap it, evicting clients that never received a token.
            used = {v["client_id"] for kind in ("access", "refresh") for v in self.store.data[kind].values()}
            for cid in [c for c in clients if c not in used][: len(clients) - MAX_CLIENTS + 1]:
                clients.pop(cid)
            if len(clients) >= MAX_CLIENTS:
                raise RegistrationError("invalid_client_metadata", "Too many apps are registered right now. Try later.")
        clients[client_info.client_id] = client_info.model_dump(mode="json")
        self.store.save()

    # ---- login ----
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if now - v[0] < PENDING_TTL}
        request_id = secrets.token_urlsafe(24)
        self.pending[request_id] = (now, client, params)
        return f"{self.base_url}/login?request={request_id}"

    def _same_site_post(self, request: Request) -> bool:
        """True if the POST's Origin is this server."""
        return request.headers.get("origin", "").rstrip("/") == self.base_url

    async def login_page(self, request: Request) -> Response:
        """OAuth consent page: one tap with an existing session, otherwise email and password."""
        expired = "This sign-in link expired. Start again from your AI app."
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            if request_id not in self.pending:
                return HTMLResponse(login_html(expired), status_code=400)
            _, client, params = self.pending[request_id]
            app_name, back_to = client.client_name or "An app", _host(params.redirect_uri)
            user = self.store.session_user(request.cookies.get(SESSION_COOKIE))
            if user and request.query_params.get("other") != "1" and back_to in TRUSTED_APP_HOSTS:
                return HTMLResponse(continue_html(request_id, app_name, self.store.email(user), back_to))
            return HTMLResponse(login_html(None, request_id, app_name, back_to=back_to,
                                           unknown=back_to not in TRUSTED_APP_HOSTS))

        form = await request.form()
        request_id = str(form.get("request", ""))
        entry = self.pending.get(request_id)
        if not entry:
            return HTMLResponse(login_html(expired), status_code=400)
        _, client, params = entry
        app_name, back_to = client.client_name or "An app", _host(params.redirect_uri)
        if form.get("ticket"):
            # Second step: TOTP or recovery code.
            ticket = str(form.get("ticket"))
            user = self.ticket_user(ticket, "app:" + request_id)
            if not user:
                return HTMLResponse(login_html("That took too long. Sign in again.", request_id, app_name,
                                               back_to=back_to, unknown=back_to not in TRUSTED_APP_HOSTS), status_code=401)
            recovery = form.get("mode") == "recovery"
            if form.get("switch"):
                return HTMLResponse(code_html(None, ticket, "/login", request_id, recovery=not recovery))
            problem = await self.check_code(user, str(form.get("code", "")), request)
            if problem:
                return HTMLResponse(code_html(problem, ticket, "/login", request_id, recovery=recovery),
                                    status_code=429 if problem == CODE_LOCKED else 401)
            self.second_step.pop(_hash_token(ticket), None)
            self.store.note_signin(user, f"{app_name} (via {device_name(request.headers.get('user-agent', ''))})",
                                   "password + code, to connect an AI app")
            self.store.save()
        elif form.get("continue") == "1":
            # One-tap approval requires an existing session cookie (SameSite=Lax) and a same-origin POST.
            user = self.store.session_user(request.cookies.get(SESSION_COOKIE))
            if not user or not self._same_site_post(request) or back_to not in TRUSTED_APP_HOSTS:
                return HTMLResponse(login_html("Sign in to continue.", request_id, app_name, back_to=back_to,
                                               unknown=back_to not in TRUSTED_APP_HOSTS), status_code=401)
        else:
            email, password = str(form.get("email", "")), str(form.get("password", ""))
            ip = "ip:" + client_ip(request)
            if self.lockout.locked(email) or self.lockout.locked(ip):
                return HTMLResponse(login_html(LOCKED, request_id, app_name, email, back_to), status_code=429)
            user = self.store.check(email, password)
            if not user:
                await self.lockout.failed(email)
                await self.lockout.failed(ip)
                return HTMLResponse(login_html(WRONG, request_id, app_name, email, back_to), status_code=401)
            if self.store.has_2fa(user):
                return HTMLResponse(code_html(None, self.new_ticket(user, "app:" + request_id), "/login", request_id))

        del self.pending[request_id]
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + CODE_TTL,
            client_id=client.client_id, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            # Tokens are audience-bound (RFC 8707); clients that omit `resource` get this server.
            resource=params.resource or f"{self.base_url}/mcp", subject=user,
        )
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
                                status_code=302)

    # ---- tokens ----
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        code = self.codes.get(authorization_code)
        if not code or code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    def _issue(self, user: str, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        self.store.data["access"][_hash_token(access)] = {
            "user": user, "client_id": client_id, "scopes": scopes, "expires_at": now + ACCESS_TTL,
            "resource": resource, "refresh": _hash_token(refresh)}
        self.store.data["refresh"][_hash_token(refresh)] = {
            "user": user, "client_id": client_id, "scopes": scopes, "expires_at": now + REFRESH_TTL,
            "resource": resource}
        self._prune()
        self.store.save()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes) or None)

    def _revoke_app(self, user: str, client_id: str) -> None:
        for kind in ("access", "refresh"):
            self.store.data[kind] = {k: v for k, v in self.store.data[kind].items()
                                     if not (v.get("user") == user and v.get("client_id") == client_id)}
        self.store.save()

    def _prune(self) -> None:
        now = time.time()
        for kind in ("access", "refresh", "used_refresh"):
            self.store.data[kind] = {k: v for k, v in self.store.data[kind].items() if v["expires_at"] > now}

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:   # single use
            raise TokenError("invalid_grant", "Authorization code already used")
        return self._issue(authorization_code.subject, client.client_id, authorization_code.scopes,
                           authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        h = _hash_token(refresh_token)
        used = self.store.data["used_refresh"].get(h)
        if used:
            # Refresh-token reuse: revoke the whole family (OAuth 2.1 §4.3.1).
            self._revoke_app(used["user"], used["client_id"])
            return None
        raw = self.store.data["refresh"].get(h)
        if (not raw or raw["client_id"] != client.client_id or raw["expires_at"] < time.time()
                or raw.get("user") not in self.store.data["users"]):
            return None
        return RefreshToken(token=refresh_token, client_id=raw["client_id"], scopes=raw["scopes"],
                            expires_at=raw["expires_at"], resource=raw.get("resource"), subject=raw["user"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        # Rotate on every refresh.
        old = _hash_token(refresh_token.token)
        self.store.data["refresh"].pop(old, None)
        self.store.data["used_refresh"][old] = {"user": refresh_token.subject, "client_id": client.client_id,
                                                "expires_at": int(time.time()) + REFRESH_TTL}
        self.store.data["access"] = {k: v for k, v in self.store.data["access"].items() if v.get("refresh") != old}
        return self._issue(refresh_token.subject, client.client_id, scopes or refresh_token.scopes,
                           refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        raw = self.store.data["access"].get(_hash_token(token))
        if not raw or raw["expires_at"] < time.time() or raw.get("user") not in self.store.data["users"]:
            return None
        return AccessToken(token=token, client_id=raw["client_id"], scopes=raw["scopes"],
                           expires_at=raw["expires_at"], resource=raw.get("resource"), subject=raw["user"])

    async def revoke_token(self, token) -> None:
        h = _hash_token(token.token)
        refresh = self.store.data["access"].pop(h, {}).get("refresh") or h
        self.store.data["refresh"].pop(refresh, None)
        self.store.data["access"] = {k: v for k, v in self.store.data["access"].items() if v.get("refresh") != refresh}
        self.store.save()


PAGE_STYLE = """
  @font-face { font-family:"Instrument Serif"; src:url("/font/instrument-serif.woff2") format("woff2"); font-display:swap; }
  :root { --bg:#F3EDE2; --ink:#2B2622; --muted:#685E54; --hair:#E2D8C8; --field:#FBF7F0; --danger:#9f2f2d;
          --primary:#34507E; --on-primary:#fff; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#1E1A17; --ink:#EFE7DA; --muted:#B3A796; --hair:#3B342E; --field:#2A2521; --danger:#e07a76;
            --primary:#9DB4DC; --on-primary:#1E1A17; }
  }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink);
         font:17px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", system-ui, sans-serif; padding:24px 16px; }
  main { width:100%; max-width:380px; }
  h1 { font:400 30px/1 "Instrument Serif", "New York", Georgia, serif; letter-spacing:-.01em; margin:0 0 8px; color:var(--muted); }
  .who { color:var(--muted); margin:0 0 24px; } .who b { color:var(--ink); font-weight:600; }
  .who .back { display:block; margin-top:6px; font-size:13px; }
  label { display:block; font-size:12px; color:var(--muted); margin:18px 0 6px; }
  label:first-of-type { margin-top:0; }
  input:not([type=hidden]) { width:100%; height:44px; font:inherit; padding:0 12px; background:var(--field);
         color:var(--ink); border:1px solid var(--hair); border-radius:6px; outline:none; }
  input:focus, select:focus { border-color:var(--muted); }
  select { width:100%; height:44px; font:inherit; padding:0 32px 0 12px; background:var(--field); color:var(--ink);
           border:1px solid var(--hair); border-radius:6px; outline:none; -webkit-appearance:none; appearance:none;
           background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='%23787774' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4.5 6.5 8 10l3.5-3.5'/%3E%3C/svg%3E");
           background-repeat:no-repeat; background-position:right 12px center; background-size:14px; }
  #tz-search { margin-bottom:8px; }
  @media (max-width: 700px) { input, select { font-size:16px; } }   /* stops iPhone zooming in */
  .hint { font-size:12px; color:var(--muted); margin:6px 0 0; }
  button { margin-top:24px; width:100%; height:48px; font:600 16px inherit; color:var(--on-primary); background:var(--primary);
         border:0; border-radius:6px; cursor:pointer; }
  .error { color:var(--danger); margin:0 0 18px; font-size:14px; }
  .switch { margin:28px 0 0; font-size:14px; color:var(--muted); text-align:center; }
  .switch a { color:var(--ink); }
  a { color:var(--ink); }
  button.link { font:inherit; font-weight:600; }
  .tz-row { display:flex; justify-content:space-between; align-items:center; gap:12px; min-height:44px; margin:0;
            padding:0 12px; background:var(--field); border:1px solid var(--hair); border-radius:6px; }
  .tz-row[hidden], #tz-pick[hidden] { display:none; }
  button.link { margin:0; width:auto; height:auto; padding:8px 0; background:none; color:var(--ink); font-weight:600;
                text-decoration:underline; }
  .sr { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }
  .q h2 { font:700 34px/1.12 -apple-system, BlinkMacSystemFont, "SF Pro Display", system-ui, sans-serif; margin:28px 0 8px; letter-spacing:-.02em; }
  .q .line { color:var(--muted); margin:0 0 20px; }
  .q .error { margin:12px 0 0; }
  .q[hidden] { display:none; }
  .qcount { position:fixed; top:calc(16px + env(safe-area-inset-top)); right:20px; margin:0; font-size:13px;
            color:var(--muted); letter-spacing:.01em; }
  .qnav { display:flex; justify-content:space-between; gap:12px; margin-top:28px; }
  .qnav button { width:auto; min-width:120px; margin:0; padding:0 22px; height:48px; border-radius:10px; font-size:16px; }
  .qnav .back { background:none; color:var(--ink); border:1px solid var(--hair); }
  .qnav a.back { display:inline-flex; align-items:center; justify-content:center; min-width:120px; height:48px;
                 padding:0 22px; border-radius:10px; text-decoration:none; font-size:16px; }
  #code { letter-spacing:.12em; font-variant-numeric:tabular-nums; }
  body:not(.stepped) .qnav .back { display:none; }
  body:not(.stepped) .qnav { justify-content:stretch; } body:not(.stepped) .qnav .next { width:100%; }
  @media (min-width: 701px) {    /* on a laptop, keep "N of M" by the form, not in the window's far corner */
    main { position:relative; }
    .qcount { position:absolute; top:8px; right:0; }
  }
  @media (max-width: 700px) {
    /* 44pt touch targets without moving the text: padding grows the hit area, the negative margin cancels it */
    .switch a, button.link { display:inline-block; padding:14px 6px; margin:-14px -6px; }
    body.stepped { place-items:start center; padding-bottom:120px; }
    body.stepped .qnav { position:fixed; left:0; right:0; bottom:0; padding:14px 20px calc(14px + env(safe-area-inset-bottom));
                         background:var(--bg); border-top:1px solid var(--hair); margin:0; }
  }
"""


def page_html(body: str, title: str = "Sign in") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} · Thread</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>{PAGE_STYLE}</style></head><body><main>
  <h1>Thread</h1>
  {body}
  <p class="hint" style="margin-top:28px;text-align:center"><a href="/privacy">Privacy</a> · <a href="/terms">Terms</a> · <a href="/support">Support</a></p>
</main></body></html>"""


def _error(error: str | None) -> str:
    return f'<p class="error" role="alert">{escape(error)}</p>' if error else ""


def _email_password(email: str, new: bool = False) -> str:
    return f"""
        <label for="email">Email</label>
        <input id="email" name="email" type="email" autocomplete="email" value="{escape(email)}" required {'' if email else 'autofocus'}>
        <label for="password">Password</label>
        <input id="password" name="password" type="password" minlength="{MIN_PASSWORD if new else 1}"
               autocomplete="{'new-password' if new else 'current-password'}" required {'autofocus' if email else ''}
               {f'passwordrules="minlength: {MIN_PASSWORD};"' if new else ''}>
        {f'<p class="hint">At least {MIN_PASSWORD} characters.</p>' if new else ''}"""


def _host(uri) -> str:
    """Redirect host shown on the consent screen."""
    from urllib.parse import urlparse
    return urlparse(str(uri or "")).hostname or ""


def _app_line(app_name: str, back_to: str, unknown: bool = False) -> str:
    where = f' <span class="back">It will send you back to <b>{escape(back_to)}</b>.</span>' if back_to else ""
    warn = ('<p class="error" role="alert">Thread doesn\'t recognise this app. Only continue if you started this '
            'yourself from an app you trust. The name above is chosen by the app, so it can say anything.</p>') if unknown else ""
    return f'<p class="who"><b>{escape(app_name)}</b> wants to use your Thread memory.{where}</p>{warn}'


def login_html(error: str | None, request_id: str = "", app_name: str = "", email: str = "", back_to: str = "",
               unknown: bool = False) -> str:
    """The page an AI app sends you to: sign in to let it use your memory."""
    form = "" if not request_id else f"""
      {_app_line(app_name, back_to, unknown)}
      <form method="post" action="/login">
        <input type="hidden" name="request" value="{escape(request_id)}">
        {_email_password(email)}
        <button type="submit">Allow access</button>
      </form>"""
    return page_html(_error(error) + form)


def continue_html(request_id: str, app_name: str, email: str, back_to: str = "") -> str:
    """Already signed in to Thread in this browser: approve the app in one tap, no password."""
    return page_html(f"""
      {_app_line(app_name, back_to)}
      <form method="post" action="/login">
        <input type="hidden" name="request" value="{escape(request_id)}">
        <input type="hidden" name="continue" value="1">
        <button type="submit" autofocus>Continue as {escape(email)}</button>
      </form>
      <p class="switch"><a href="/login?request={escape(request_id)}&amp;other=1">Use a different account</a></p>""",
                     "Allow access")


def code_html(error: str | None, ticket: str, action: str, request_id: str = "", recovery: bool = False) -> str:
    """Second-factor screen: TOTP or recovery code."""
    hidden = f'<input type="hidden" name="ticket" value="{escape(ticket)}">' + (
        f'<input type="hidden" name="request" value="{escape(request_id)}">' if request_id else "")
    back = f"/login?request={escape(request_id)}&amp;other=1" if request_id else "/signin"
    if recovery:
        title, line = "Use a recovery code", "One of the codes you saved when you turned on two-step sign-in. Each works once."
        field = ('<input id="code" name="code" autocomplete="off" autocapitalize="off" spellcheck="false" '
                 'placeholder="abcde-fghjk" enterkeyhint="go" required autofocus>')
        other = "Use my authenticator app instead"
    else:
        title, line = "Enter your code", "Open your authenticator app and type the 6-digit code for Thread."
        field = ('<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]*" '
                 'maxlength="7" enterkeyhint="go" required autofocus>')
        other = "Lost your phone? Use a recovery code"
    mode = f'<input type="hidden" name="mode" value="{"recovery" if recovery else "app"}">'
    # Recovery-code toggle is a separate form so Enter in the code field submits Continue.
    return page_html(f"""
      <form method="post" action="{action}" class="q" novalidate>
        {hidden}{mode}
        <h2>{title}</h2>
        <p class="line">{line}</p>
        <label for="code" class="sr">{'Recovery code' if recovery else '6-digit code'}</label>
        {field}
        {_error(error)}
        <p class="qcount" aria-hidden="true">2 of 2</p>
        <nav class="qnav">
          <a class="back" href="{back}">Back</a>
          <button type="submit" class="next">Continue</button>
        </nav>
      </form>
      <form method="post" action="{action}" class="switch">{hidden}{mode}
        <button class="link" type="submit" name="switch" value="1">{other}</button>
      </form>
      <script>document.body.classList.add("stepped");</script>""", "Two-step sign-in")


def signin_html(error: str | None, email: str = "") -> str:
    return page_html(f"""{_error(error)}
      <p class="who">Sign in to see your memory.</p>
      <form method="post" action="/signin">{_email_password(email)}
        <button type="submit">Sign in</button>
      </form>
      <p class="switch">Got an invite code? <a href="/signup">Create an account</a></p>""")


def _zone_field(zone: str = "") -> str:
    """Time zone picker grouped by region; preselects the device's zone."""
    from thread import timezones
    return f"""
        <label for="tz-search" class="sr">Your time zone</label>
        <p class="tz-row" id="tz-row" hidden><span id="tz-name"></span>
          <button type="button" class="link" id="tz-change">Change</button></p>
        <div id="tz-pick">
          <input id="tz-search" type="search" placeholder="Search a city or country" autocomplete="off"
                 aria-controls="timezone" hidden>
          <select id="timezone" name="timezone" required>{timezones.options_html(zone)}</select>
          <input type="hidden" name="device_zone" id="device-zone">
        </div>
        <p class="hint" id="tz-hint">You can change it later.</p>
        <script>
        (() => {{
          const sel = document.getElementById("timezone"), search = document.getElementById("tz-search");
          const old = {json.dumps(timezones.OLD_NAMES)};
          search.hidden = false;
          try {{ document.getElementById("device-zone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; }} catch (e) {{}}
          if (!sel.value) {{
            let tz = ""; try {{ tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; }} catch (e) {{}}
            tz = old[tz] || tz;
            if ([...sel.options].some(o => o.value === tz)) {{
              sel.value = tz;
              // Already right for almost everyone: show it as one line with "Change", not a search box + list.
              const row = document.getElementById("tz-row"), pick = document.getElementById("tz-pick");
              document.getElementById("tz-name").textContent = sel.selectedOptions[0].textContent;
              row.hidden = false; pick.hidden = true;
              document.getElementById("tz-hint").textContent = "From your device. You can change it later.";
              document.getElementById("tz-change").onclick = () => {{ row.hidden = true; pick.hidden = false; search.focus(); }};
            }}
          }}
          const all = [...sel.querySelectorAll("option")].filter(o => o.value);
          search.addEventListener("input", () => {{
            const q = search.value.trim().toLowerCase();
            let first = null;
            for (const o of all) {{
              const hit = !q || o.textContent.toLowerCase().includes(q) || o.value.toLowerCase().includes(q);
              o.hidden = !hit; if (hit && !first) first = o;
            }}
            for (const g of sel.querySelectorAll("optgroup")) g.hidden = ![...g.children].some(o => !o.hidden);
            if (q && first) sel.value = first.value;
          }});
        }})();
        </script>"""


def signup_html(error: str | None, email: str = "", code: str = "", zone: str = "",
                invited_by: str | None = None, error_at: str = "") -> str:
    """Stepped sign-up form: invite code, email, password, time zone.

    Steps are validated via /signup/check; only the final POST creates the account and consumes the invite.
    Without JS it renders as a single form. A non-None `invited_by` means the user came from /join/<code>
    and the code is carried in a hidden field.
    """
    from_link = invited_by is not None
    who = (f"<b>{escape(invited_by)}</b> invited you." if invited_by else "You're invited.") if from_link else ""
    code_q = f'<input type="hidden" name="code" value="{escape(code)}"><input type="hidden" name="via" value="link">' \
        if from_link else f"""
      <section class="q" data-q="code">
        <h2>Your invite code</h2>
        <p class="line">Thread is invite-only for now. The code is in the message you got.</p>
        <label for="code" class="sr">Invite code</label>
        <input id="code" name="code" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="next"
               placeholder="abcd-efgh-jkmn" value="{escape(code)}" required>
      </section>"""
    return page_html(f"""
      <form method="post" action="/signup" id="signup" novalidate>
        {code_q}
        <section class="q" data-q="email">
          <h2>{"Welcome to Thread" if from_link else "Your email"}</h2>
          <p class="line">{who + " " if who else ""}What's your email? You'll sign in with it.</p>
          <label for="email" class="sr">Email</label>
          <input id="email" name="email" type="email" autocomplete="email" inputmode="email" enterkeyhint="next"
                 value="{escape(email)}" required>
        </section>
        <section class="q" data-q="password">
          <h2>Choose a password</h2>
          <p class="line">At least {MIN_PASSWORD} characters. Your browser can suggest a strong one and save it.</p>
          <label for="password" class="sr">Password</label>
          <input id="password" name="password" type="password" minlength="{MIN_PASSWORD}" autocomplete="new-password"
                 passwordrules="minlength: {MIN_PASSWORD};" enterkeyhint="next" required>
        </section>
        <section class="q" data-q="zone">
          <h2>Your time zone</h2>
          <p class="line">So deadlines show in your own time.</p>
          {_zone_field(zone)}
        </section>
        {_error(error) if error else ""}
        <p class="qcount" id="q-count" aria-live="polite"></p>
        <nav class="qnav">
          <button type="button" class="back" id="q-back">Back</button>
          <button type="submit" class="next" id="q-next">Create account</button>
        </nav>
      </form>
      <p class="switch">Already have an account? <a href="/signin">Sign in</a></p>
      <script>{_STEPS_JS % json.dumps(error_at)}</script>""", "Create account")


# Stepped UI over a plain form that works without JS. Each step is validated server-side; the last submits.
_STEPS_JS = """
(() => {
  const form = document.getElementById("signup"), qs = [...form.querySelectorAll(".q")];
  const back = document.getElementById("q-back"), next = document.getElementById("q-next");
  const errorAt = %s;
  document.body.classList.add("stepped");
  // A refresh mid-sign-up keeps the email typed so far (this tab only; the password is never kept).
  const keep = (() => { try { return window.sessionStorage; } catch (e) { return null; } })();
  try { if (keep && !form.email.value) form.email.value = keep.getItem("thread-signup-email") || ""; } catch (e) {}
  form.email.addEventListener("input", () => { try { keep && keep.setItem("thread-signup-email", form.email.value); } catch (e) {} });
  let at = Math.max(0, qs.findIndex(q => q.dataset.q === errorAt));
  if (!errorAt) at = Math.max(0, qs.findIndex(q => { const i = q.querySelector("input[required]"); return i && !i.value; }));
  const say = (msg) => {
    let p = qs[at].querySelector(".error");
    if (!p) { p = document.createElement("p"); p.className = "error"; p.setAttribute("role", "alert"); qs[at].appendChild(p); }
    p.textContent = msg || ""; p.hidden = !msg;
  };
  const show = () => {
    qs.forEach((q, i) => q.hidden = i !== at);
    back.style.visibility = at === 0 ? "hidden" : "visible";
    next.textContent = at === qs.length - 1 ? "Create account" : "Continue";
    // A faint "1 of 6" in the corner: sign-up's screens plus setup's three questions after it.
    document.getElementById("q-count").textContent = (at + 1) + " of " + (qs.length + 3);
    const field = qs[at].querySelector("input:not([type=hidden]):not([hidden]), select");
    if (field && document.activeElement !== field && !qs[at].querySelector("#tz-row:not([hidden])")) field.focus();
  };
  const old = form.querySelector(":scope > .error"); if (old) { const msg = old.textContent; old.remove(); show(); say(msg); }
  back.onclick = () => { if (at > 0) { at--; show(); } };
  form.addEventListener("submit", async e => {
    const q = qs[at], key = q.dataset.q, input = q.querySelector("input:not([type=hidden]), select");
    if (at === qs.length - 1) {
      if (!document.getElementById("timezone").value) { e.preventDefault(); say("Choose your time zone."); return; }
      try { keep && keep.removeItem("thread-signup-email"); } catch (err) {}
      return;
    }
    e.preventDefault();
    if (input && !input.checkValidity()) {
      say(key === "email" ? "That doesn't look like an email address." : key === "password"
          ? "Use at least " + input.minLength + " characters." : "Paste the invite code from your message."); return;
    }
    if (key === "code" || key === "email") {
      next.disabled = true;
      try {
        const body = new URLSearchParams({ code: form.code.value, email: key === "email" ? form.email.value : "" });
        const r = await fetch("/signup/check", { method: "POST", body });
        const data = await r.json();
        if (!r.ok) { say(data.error); return; }
      } catch (err) { say("Couldn't reach Thread. Check your connection."); return; }
      finally { next.disabled = false; }
    }
    say(""); at++; show();
  });
  show();
})();
"""


class HomeSessions:
    """Sign-in for the home screen in a browser: email + password, then a 7-day session cookie.
    Shares the per-account lockout with the app sign-in, and stores sessions only as hashes."""

    def __init__(self, store: AuthStore, provider: ThreadAuthProvider, secure: bool, on_signup=None):
        """`on_signup(user_id, timezone, email, device_zone)` seeds the new account's profile (optional)."""
        self.store, self.provider, self.secure = store, provider, secure
        self.on_signup = on_signup

    def user(self, request: Request) -> str | None:
        """The signed-in account for this browser, or None."""
        return self.store.session_user(request.cookies.get(SESSION_COOKIE))

    def valid(self, request: Request) -> bool:
        return self.user(request) is not None

    def _cross_site(self, request: Request) -> bool:
        """True for a cross-origin POST. A missing Origin is allowed; SameSite cookies still apply."""
        origin = request.headers.get("origin")
        return bool(origin) and origin.rstrip("/") != self.provider.base_url

    async def signin_page(self, request: Request) -> Response:
        if request.method == "GET":
            if self.valid(request):
                return RedirectResponse("/", status_code=303)
            return HTMLResponse(signin_html(None))
        if self._cross_site(request):
            return HTMLResponse(signin_html("Sign in from the Thread page."), status_code=403)
        form = await request.form()
        if form.get("ticket"):
            return await self._signin_second_step(request, form)
        email, password = str(form.get("email", "")), str(form.get("password", ""))
        ip = "ip:" + client_ip(request)
        if self.provider.lockout.locked(email) or self.provider.lockout.locked(ip):
            return HTMLResponse(signin_html(LOCKED, email), status_code=429)
        user = self.store.check(email, password)
        if not user:
            await self.provider.lockout.failed(email)
            await self.provider.lockout.failed(ip)
            return HTMLResponse(signin_html(WRONG, email), status_code=401)
        if self.store.has_2fa(user):
            return HTMLResponse(code_html(None, self.provider.new_ticket(user, "signin"), "/signin"))
        return self._start_session(user, RedirectResponse("/", status_code=303), request)

    async def _signin_second_step(self, request: Request, form) -> Response:
        ticket = str(form.get("ticket"))
        user = self.provider.ticket_user(ticket, "signin")
        if not user:
            return HTMLResponse(signin_html("That took too long. Sign in again."), status_code=401)
        recovery = form.get("mode") == "recovery"
        if form.get("switch"):
            return HTMLResponse(code_html(None, ticket, "/signin", recovery=not recovery))
        problem = await self.provider.check_code(user, str(form.get("code", "")), request)
        if problem:
            return HTMLResponse(code_html(problem, ticket, "/signin", recovery=recovery),
                                status_code=429 if problem == CODE_LOCKED else 401)
        self.provider.second_step.pop(_hash_token(ticket), None)
        left = self.store.twofa_info(user)["recovery_left"]
        # Low on recovery codes after using one: open Settings with a prompt to regenerate.
        where = "/?recovery-low=1#me" if recovery and left <= 3 else "/"
        return self._start_session(user, RedirectResponse(where, status_code=303), request,
                                   "password + recovery code" if recovery else "password + code")

    async def join_page(self, request: Request) -> Response:
        """/join/<code>: the link `thread-admin invite` prints. Opens sign-up with the code already in."""
        if self.valid(request):
            return RedirectResponse("/", status_code=303)
        code = request.path_params.get("code", "")
        info = self.store.invite_info(code)
        if not info:
            return HTMLResponse(signup_html("This invite link doesn't work anymore. Ask for a new one, "
                                            "or paste a code below."), status_code=404)
        return HTMLResponse(signup_html(None, code=code, invited_by=info.get("by", "")))

    async def signup_check(self, request: Request) -> Response:
        """Validate one sign-up step (invite code, optional email) without creating anything."""
        if self._cross_site(request):
            return JSONResponse({"error": "Sign up from the Thread page."}, status_code=403)
        form = await request.form()
        code, email = str(form.get("code", "")), str(form.get("email", ""))
        who = "invite:" + client_ip(request)
        if self.provider.lockout.locked(who):
            return JSONResponse({"error": "Too many wrong invite codes. Try again in 15 minutes."}, status_code=429)
        if not self.store.invite_valid(code):
            await self.provider.lockout.failed(who)
            return JSONResponse({"error": "That invite code doesn't work. Check it, or ask for a new one."}, status_code=403)
        if email:
            email = normalize_email(email)
            if not EMAIL_RE.match(email):
                return JSONResponse({"error": "That doesn't look like an email address."}, status_code=400)
            if self.store.user_id(email):
                return JSONResponse({"error": "There's already an account with that email. Sign in instead."},
                                    status_code=400)
        return JSONResponse({"ok": True})

    async def signup_page(self, request: Request) -> Response:
        if request.method == "GET":
            if self.valid(request):
                return RedirectResponse("/", status_code=303)
            return HTMLResponse(signup_html(None, code=request.query_params.get("code", "")))
        from thread import timezones
        if self._cross_site(request):
            return HTMLResponse(signup_html("Sign up from the Thread page."), status_code=403)
        form = await request.form()
        code, email, password, picked = (str(form.get(k, "")) for k in ("code", "email", "password", "timezone"))
        zone = timezones.canonical(picked) or ""
        info = self.store.invite_info(code) if form.get("via") == "link" else None
        invited_by = (info or {}).get("by", "") if form.get("via") == "link" else None
        page = lambda msg, status, at="": HTMLResponse(signup_html(msg, email, code, zone, invited_by, at), status_code=status)
        if self.provider.lockout.locked("invite:" + client_ip(request)):
            return page("Too many wrong invite codes. Try again in 15 minutes.", 429, "code")
        if not self.store.invite_valid(code):
            await self.provider.lockout.failed("invite:" + client_ip(request))
            return page("That invite code doesn't work. Check it, or ask for a new one.", 403, "code")
        if not zone:
            return page("Choose your time zone from the list, so your deadlines show in your own time.", 400, "zone")
        try:
            user = self.store.create_user(email, password)
        except ValueError as e:
            return page(str(e), 400, "password" if "password" in str(e) else "email")
        self.store.use_invite(code)   # consume only after the account is created
        if self.on_signup:
            # A deliberately chosen zone counts as "keep", so the device-zone prompt doesn't fire right after sign-up.
            device = timezones.canonical(str(form.get("device_zone", ""))) or ""
            self.on_signup(user, zone, normalize_email(email), device)
        # ?welcome=<n>: sign-up step count, continued by setup
        screens = 3 if form.get("via") == "link" else 4
        return self._start_session(user, RedirectResponse(f"/?welcome={screens}#home", status_code=303), request,
                                   "new account")

    def _start_session(self, user: str, response: Response, request: Request | None = None,
                       how: str = "password") -> Response:
        device = device_name(request.headers.get("user-agent", "")) if request is not None else ""
        token = self.store.new_session(user, device, how)
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, secure=self.secure,
                            samesite="lax", path="/")
        # Not secret: namespaces client-side storage per account.
        response.set_cookie(ACCOUNT_COOKIE, hashlib.sha256(user.encode()).hexdigest()[:12], max_age=SESSION_TTL,
                            secure=self.secure, samesite="lax", path="/")
        return response

    async def change_password(self, request: Request) -> Response:
        """Change password: requires the current one and revokes every other session and token."""
        user = self.user(request)
        email = self.store.email(user) if user else ""
        if not user:
            return JSONResponse({"error": "Sign in first"}, status_code=401)
        if self.provider.lockout.locked(email):
            return JSONResponse({"error": LOCKED}, status_code=429)
        body = await request.json()
        current, new = str(body.get("current", "")), str(body.get("new", ""))
        if self.store.check(email, current) != user:
            await self.provider.lockout.failed(email)
            return JSONResponse({"error": "Your current password is wrong."}, status_code=403)
        if len(new) < MIN_PASSWORD:
            return JSONResponse({"error": f"Use at least {MIN_PASSWORD} characters."}, status_code=400)
        if new == current:
            return JSONResponse({"error": "That's the same as your current password."}, status_code=400)
        self.store.set_password(user, new)
        return self._start_session(user, JSONResponse({"ok": True}), request, "password changed")

    async def signout(self, request: Request) -> Response:
        # Reject cross-site GET /signout (Sec-Fetch-Site), so embedding the URL can't sign users out.
        if request.method == "GET" and request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return RedirectResponse("/", status_code=303)
        if request.method == "POST" and self._cross_site(request):
            return RedirectResponse("/", status_code=303)
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            self.store.data["sessions"].pop(_hash_token(token), None)
            self.store.save()
        response = RedirectResponse("/signin", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(ACCOUNT_COOKIE, path="/")
        return response
