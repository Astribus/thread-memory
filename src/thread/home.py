"""Web UI and JSON API. Local mode: `uv run thread-home`, then open http://127.0.0.1:8765."""

import hashlib
import io
import json
import re
import time
import zipfile
from datetime import date
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from thread import gcal, timezones
from thread import legal
from thread.brain import LINK_RE, PRIVACY_LEVELS, SAVING_MODES, Brain, default_root
from thread.canvas import CanvasAuthError, CanvasClient
from thread.chats import RETENTION_DAYS, as_markdown
from thread.fingerprint import CATEGORIES, Fingerprint
from thread.feedback import Feedback, feedback_file
from thread.workspace import Workspace, Workspaces
from thread.profile import guess_name, form_schema, read_profile, write_profile

WEB = Path(__file__).parent / "web"


def _fingerprint(name: str) -> str:
    """Short hash of a static file's contents; changes whenever the file does."""
    return hashlib.sha256((WEB / name).read_bytes()).hexdigest()[:10]
HOST, PORT = "127.0.0.1", 8765   # local mode binds to loopback only
# CSRF: state-changing requests must be same-origin JSON.
ALLOWED_ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}
MAX_UPLOAD = 5 * 1024 * 1024   # per chat upload
# The extension authenticates with a key, not a cookie. Account endpoints are sent no-store.
NO_STORE = {"cache-control": "no-store"}
NUDGE_AFTER_SIGNINS = 3   # sign-ins before suggesting 2FA
EXTENSION_CORS = {"access-control-allow-origin": "*", "access-control-allow-methods": "POST, OPTIONS",
                  "access-control-allow-headers": "authorization, content-type"}


def build_app(brain: Brain | None = None, sessions=None, allowed_origins: set[str] | None = None,
              public_url: str | None = None, workspaces: Workspaces | None = None,
              feedback: Feedback | None = None) -> Starlette:
    """Build the Starlette app. With `sessions` and `workspaces` set (online), every route requires
    a signed-in session scoped to that user's workspace."""
    origins = allowed_origins or ALLOWED_ORIGINS
    online = sessions is not None
    feedback = feedback or Feedback(feedback_file(online))
    local = None if online else Workspace.local(brain)

    def here(request: Request) -> Workspace:
        """The signed-in person's workspace (the guard below already refused anyone signed out)."""
        return local if local is not None else workspaces.get(request.scope["thread.user"])

    def base(request: Request) -> str:
        return (public_url or str(request.base_url)).rstrip("/")

    def from_thread_page(request: Request) -> bool:
        return (request.headers.get("origin") in origins
                and request.headers.get("content-type", "").startswith("application/json"))

    async def index(request: Request):
        # Static URLs carry a content hash (?v=…) so stale assets are never mixed with new markup.
        html = (WEB / "index.html").read_text()
        for name in ("style.css", "app.js", "d3.min.js"):
            html = html.replace(f'/static/{name}"', f'/static/{name}?v={_fingerprint(name)}"')
        return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})

    async def graph(request: Request):
        # Polled every 4 s: build off the event loop and answer 304 when the ETag matches.
        brain = here(request).brain
        data = await run_in_threadpool(brain.graph)
        tag = f'W/"{brain.graph_version()}"'
        headers = {"ETag": tag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(data, headers=headers)

    async def note(request: Request):
        brain = here(request).brain
        note_id = request.query_params.get("id", "")
        try:
            text = brain.read(note_id + ".md")
        except (ValueError, FileNotFoundError, IsADirectoryError):
            return JSONResponse({"error": "No such note"}, status_code=404)
        # Resolve [[links]] so the client can render them.
        links = {}
        for match in LINK_RE.finditer(text):
            target = brain.resolve(match.group(1))
            links[match.group(1).strip()] = target.with_suffix("").as_posix() if target else None
        return JSONResponse({"id": note_id, "text": text, "links": links})

    async def tree(request: Request):
        brain = here(request).brain
        return JSONResponse(await run_in_threadpool(brain.tree_data))

    async def change(request: Request, action):
        if not from_thread_page(request):
            return JSONResponse({"error": "Changes are only accepted from the Thread page"}, status_code=403)
        body = await request.json()
        try:
            return JSONResponse(action(body))
        except FileExistsError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except (ValueError, FileNotFoundError, KeyError) as e:
            return JSONResponse({"error": str(e) or "Bad request"}, status_code=400)

    async def new_folder(request: Request):
        brain = here(request).brain
        return await change(request, lambda b: {"path": brain.create_folder(b.get("parent", ""), b["name"])})

    async def note_write(request: Request):
        brain = here(request).brain
        if request.method == "POST":
            return await change(request, lambda b: {"id": brain.create_note(b.get("folder", ""), b["title"])})
        return await change(request, lambda b: (brain.save(b["id"], b["text"]), {"id": b["id"]})[1])

    async def rename(request: Request):
        brain = here(request).brain
        def act(b):
            if b["kind"] == "folder":
                return {"path": brain.rename_folder(b["path"], b["name"])}
            return {"id": brain.rename_note(b["id"], b["name"])}
        return await change(request, act)

    async def delete(request: Request):
        brain = here(request).brain
        return await change(request, lambda b: brain.delete(b["target"]))

    async def restore(request: Request):
        brain = here(request).brain
        return await change(request, lambda b: {"restored": brain.restore(b["trash"])})

    # ---------- Me: profile, settings, help ----------
    async def profile(request: Request):
        w = here(request)
        brain = w.brain
        if request.method == "PUT":
            return await change(request, lambda b: write_profile(brain, b))
        learned = [{"category": CATEGORIES[o["category"]][0], "text": o["text"], "times": len(o["seen"]),
                    "last": o["seen"][-1]} for o in w.fingerprint.observations()]
        return JSONResponse({
            "profile": read_profile(brain), "schema": form_schema(), "privacy": brain.privacy(), "saving": brain.saving() or "everything",
            "learned": sorted(learned, key=lambda o: o["last"], reverse=True),
            "online": online, "mcp_url": base(request) + "/mcp" if online else None,
            "canvas": bool(w.canvas_token()), "canvas_feed": bool(w.canvas_feed()),
            "email": sessions.store.email(request.scope["thread.user"]) if online else None,
            # Suggested name for setup; not saved on its own.
            "name_guess": guess_name(sessions.store.email(request.scope["thread.user"])) if online else "",
        })

    SETUP_STEPS = ("about", "ai", "canvas", "backup")

    def setup_status(request: Request) -> dict:
        """Setup progress derived from actual state (connected clients, Canvas credentials, ...)."""
        w = here(request)
        user = request.scope.get("thread.user")
        p = read_profile(w.brain)
        return {
            **w.setup_state(),
            "online": online,
            "ai_apps": sessions.store.connected_apps(user) if online else [],
            "done": {
                "about": bool(p["about"].get("name") or p["goals"] or p["rules"]),
                "ai": bool(online and sessions.store.connected_apps(user)),
                "canvas": bool(w.canvas_token() or w.canvas_feed()),
                "backup": bool(online and sessions.store.has_backup_key(user)),
            },
        }

    async def setup(request: Request):
        if request.method == "GET":
            return JSONResponse(setup_status(request))
        w = here(request)
        def act(b):
            for key in ("skipped", "unskip"):
                if b.get(key) is not None and b[key] not in SETUP_STEPS:
                    raise ValueError("Unknown setup step")
            w.update_setup(seen=b.get("seen"), skipped=b.get("skipped"), unskip=b.get("unskip"))
            return setup_status(request)
        return await change(request, act)

    async def privacy(request: Request):
        brain = here(request).brain
        def act(b):
            if b.get("level") not in PRIVACY_LEVELS:
                raise ValueError("Privacy must be strict or light")
            brain.set_privacy(b["level"])
            return {"privacy": brain.privacy()}
        return await change(request, act)

    async def saving(request: Request):
        brain = here(request).brain
        def act(b):
            if b.get("mode") not in SAVING_MODES:
                raise ValueError("Saving must be everything, important or ask")
            return {"saving": brain.set_saving(b["mode"])}
        return await change(request, act)

    async def forget_learned(request: Request):
        brain = here(request).brain
        def act(b):
            gone = Fingerprint(brain).forget_one(str(b["text"]))
            if gone is None:
                raise FileNotFoundError("That's already gone")
            return gone
        return await change(request, act)

    async def restore_learned(request: Request):
        brain = here(request).brain
        return await change(request, lambda b: {"text": Fingerprint(brain).restore(str(b["trash"]))})

    # ---------- account security (online only) ----------
    def cookie(request: Request) -> str:
        from thread.accounts import SESSION_COOKIE
        return request.cookies.get(SESSION_COOKIE, "")

    def stale(request: Request) -> bool:
        """Sensitive actions (change password, 2FA, export, a new backup key) need a sign-in from the last
        STEP_UP_SECONDS. Off on a laptop, which has no accounts."""
        return online and not sessions.store.session_fresh(cookie(request))

    def step_up() -> JSONResponse:
        return JSONResponse({"error": "Confirm it's you first.", "reauth": True}, status_code=403,
                            headers=NO_STORE)

    async def security_call(request: Request, action, fresh: bool = True):
        """Wrapper for account endpoints: online only, same-origin, no-store, and step-up when `fresh`."""
        if not online:
            return JSONResponse({"error": "Accounts are only on the online version."}, status_code=400)
        if not from_thread_page(request):
            return JSONResponse({"error": "Changes are only accepted from the Thread page"}, status_code=403)
        if fresh and stale(request):
            return step_up()
        body = await request.json()
        try:
            result = action(body)
            if hasattr(result, "__await__"):
                result = await result
        except (ValueError, KeyError) as e:
            return JSONResponse({"error": str(e) or "Bad request"}, status_code=400, headers=NO_STORE)
        if isinstance(result, Response):
            return result
        return JSONResponse(result, headers=NO_STORE)

    def account_info(request: Request) -> dict:
        user = request.scope["thread.user"]
        store = sessions.store
        info = store.twofa_info(user)
        count = store.data["users"].get(user, {}).get("signin_count", 0)
        return {
            "email": store.email(user),
            "twofa": info,
            "fresh": not stale(request),
            "signins": store.signins(user),
            "other_browsers": max(0, store.active_sessions(user) - 1),
            "ai_apps": store.connected_apps(user),
            # Show the 2FA card: off, signed in a few times, not dismissed.
            "nudge": (not info["on"] and count >= NUDGE_AFTER_SIGNINS
                      and not store.data["users"].get(user, {}).get("nudge_2fa_dismissed")),
        }

    async def account(request: Request):
        if not online:
            return JSONResponse({"online": False})
        return JSONResponse({"online": True, **account_info(request)}, headers=NO_STORE)

    async def reauth(request: Request):
        """Re-enter the password (and a code if two-step is on) to unlock sensitive actions for a while."""
        user = request.scope.get("thread.user")
        async def act(b):
            store, lock = sessions.store, sessions.provider.lockout
            email = store.email(user)
            if lock.locked(email):
                raise ValueError("Too many wrong passwords. Try again in 15 minutes.")
            if store.check(email, str(b.get("password", ""))) != user:
                await lock.failed(email)
                return JSONResponse({"error": "That password isn't right.", "field": "password"}, status_code=403)
            if store.has_2fa(user):
                if not b.get("code"):
                    return {"need_code": True}
                problem = await sessions.provider.check_code(user, str(b["code"]), request)
                if problem:
                    return JSONResponse({"error": problem, "field": "code"}, status_code=403)
            store.mark_fresh(cookie(request))
            return {"ok": True}
        return await security_call(request, act, fresh=False)

    async def twofa_start(request: Request):
        from thread import twofactor
        user = request.scope.get("thread.user")
        def act(b):
            secret = sessions.store.start_2fa(user)
            uri = twofactor.otpauth_uri(sessions.store.email(user), secret)
            return {"qr": twofactor.qr_data_uri(uri), "key": twofactor.spaced(secret), "uri": uri}
        return await security_call(request, act)

    async def twofa_confirm(request: Request):
        user = request.scope.get("thread.user")
        async def act(b):
            lock = sessions.provider.lockout
            if lock.locked("2fa:" + user):
                raise ValueError("Too many wrong codes. Try again in 15 minutes.")
            codes = sessions.store.confirm_2fa(user, str(b.get("code", "")))
            if codes is None:
                await lock.failed("2fa:" + user)
                raise ValueError("That code didn't work. Type the newest one from your authenticator app.")
            # Other sessions only had the password; revoke them.
            sessions.store.sign_out_others(user, cookie(request), apps=False)
            sessions.store.mark_fresh(cookie(request))
            return {"codes": codes, **account_info(request)}
        return await security_call(request, act)

    async def twofa_off(request: Request):
        """Needs the password AND a current code (or a recovery code) every time, however recent the sign-in."""
        user = request.scope.get("thread.user")
        async def act(b):
            store, lock = sessions.store, sessions.provider.lockout
            email = store.email(user)
            if lock.locked(email):
                raise ValueError("Too many wrong passwords. Try again in 15 minutes.")
            if store.check(email, str(b.get("password", ""))) != user:
                await lock.failed(email)
                return JSONResponse({"error": "That password isn't right.", "field": "password"}, status_code=403)
            problem = await sessions.provider.check_code(user, str(b.get("code", "")), request)
            if problem:
                return JSONResponse({"error": problem, "field": "code"}, status_code=403)
            store.disable_2fa(user)
            store.note_signin(user, "", "two-step sign-in turned off", count=False)
            store.save()
            return account_info(request)
        return await security_call(request, act, fresh=False)

    async def twofa_recovery(request: Request):
        user = request.scope.get("thread.user")
        return await security_call(request, lambda b: {"codes": sessions.store.new_recovery_codes(user),
                                                        **account_info(request)})

    async def signout_others(request: Request):
        user = request.scope.get("thread.user")
        def act(b):
            sessions.store.sign_out_others(user, cookie(request), apps=bool(b.get("apps", True)))
            return account_info(request)
        return await security_call(request, act, fresh=False)

    async def nudge_dismiss(request: Request):
        user = request.scope.get("thread.user")
        def act(b):
            sessions.store.data["users"][user]["nudge_2fa_dismissed"] = int(time.time())
            sessions.store.save()
            return {"ok": True}
        return await security_call(request, act, fresh=False)

    async def password(request: Request):
        if not online:
            return JSONResponse({"error": "Passwords are only used by the online version."}, status_code=400)
        if not from_thread_page(request):
            return JSONResponse({"error": "Changes are only accepted from the Thread page"}, status_code=403)
        # Current password is on the form; with 2FA a stale session also needs a code.
        if stale(request) and sessions.store.has_2fa(request.scope["thread.user"]):
            return step_up()
        return await sessions.change_password(request)

    async def export(request: Request):
        """Download every note as a .zip: the user owns their memory and can take it anywhere."""
        if stale(request):
            return step_up()
        brain = here(request).brain
        def build() -> bytes:   # off the event loop
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
                for rel in brain._note_rels():   # skips hidden folders
                    z.write(brain.root / rel, Path("thread-memory") / rel)
            return buffer.getvalue()
        data = await run_in_threadpool(build)
        name = f"thread-memory-{date.today().isoformat()}.zip"
        return Response(data, media_type="application/zip",
                        headers={"content-disposition": f'attachment; filename="{name}"'})

    # Google OAuth: connect, switch, disconnect.
    async def google_status(request: Request):
        w = here(request)
        return JSONResponse(await run_in_threadpool(gcal.status, w.google_token, w.sealer))

    async def google_connect(request: Request):
        try:
            url = gcal.connect_url(base(request) + "/google/callback", request.scope.get("thread.user"))
            return RedirectResponse(url, status_code=303)
        except gcal.CalendarAuthError:
            return RedirectResponse("/?google=unavailable#me", status_code=303)

    async def google_callback(request: Request):
        q = request.query_params
        if q.get("error") or not q.get("code"):
            return RedirectResponse("/?google=cancelled#me", status_code=303)   # user cancelled at Google
        try:
            await run_in_threadpool(gcal.finish_connect, base(request) + "/google/callback",
                                    q.get("state", ""), q["code"], here(request).google_token,
                                    request.scope.get("thread.user"), here(request).sealer)
        except Exception:
            return RedirectResponse("/?google=failed#me", status_code=303)
        return RedirectResponse("/?google=connected#me", status_code=303)

    async def google_disconnect(request: Request):
        token = here(request).google_token
        return await change(request, lambda b: (gcal.disconnect(token), {"state": "disconnected"})[1])

    # Canvas access token (Account → Settings → New access token).
    async def canvas_connect(request: Request):
        if not online:
            return JSONResponse({"error": "On a laptop, Canvas uses the Keychain (see the README)."}, status_code=400)
        if not from_thread_page(request):
            return JSONResponse({"error": "Changes are only accepted from the Thread page"}, status_code=403)
        token = str((await request.json()).get("token", "")).strip()
        if not token:
            return JSONResponse({"error": "Paste your Canvas access token first."}, status_code=400)
        try:
            await run_in_threadpool(CanvasClient(token).courses)   # validate before saving
        except CanvasAuthError:
            return JSONResponse({"error": "Canvas didn't accept that token. Make a new one and paste it again."},
                                status_code=400)
        except Exception:
            return JSONResponse({"error": "Couldn't reach Canvas. Try again in a minute."}, status_code=502)
        here(request).set_canvas_token(token)
        return JSONResponse({"canvas": True})

    async def due(request: Request):
        """Unsubmitted work due in the next 14 days. `class_key` matches the map's colour groups."""
        from datetime import datetime
        from thread.brain import slugify
        from thread.canvas_feed import FeedError
        w = here(request)
        canvas = w.canvas()
        if canvas is None:
            return JSONResponse({"connected": False, "items": []})
        try:
            items = await run_in_threadpool(canvas.upcoming, 14)
        except (CanvasAuthError, FeedError) as e:
            return JSONResponse({"connected": True, "error": str(e), "items": []})
        tz = w.timezone()
        out = []
        for i in items:
            if i.get("submitted") or not i.get("due"):
                continue
            moment = datetime.fromisoformat(i["due"].replace("Z", "+00:00")) if "T" in i["due"] else None
            local = moment.astimezone(tz) if moment else datetime.fromisoformat(i["due"])
            out.append({"title": i["title"], "course": i["course"], "class_key": w.brain.class_slug(i["course"]),
                        "type": i["type"], "url": i.get("url", ""), "date": local.date().isoformat(),
                        "time": local.strftime("%-I:%M %p") if moment else ""})
        return JSONResponse({"connected": True, "source": getattr(canvas, "source", "api"), "items": out})

    async def class_colors(request: Request):
        w = here(request)
        if request.method == "GET":
            return JSONResponse(w.class_colors())
        return await change(request, lambda b: w.set_class_color(str(b.get("key", "")), int(b.get("index", -1))))

    async def canvas_feed(request: Request):
        """Paste the Canvas Calendar Feed link. Thread reads it once before saving, so a wrong link is caught here."""
        from thread.canvas_feed import FeedError, fetch_feed, parse_events
        if not online:
            return JSONResponse({"error": "The calendar feed works on the online version of Thread."}, status_code=400)
        if not from_thread_page(request):
            return JSONResponse({"error": "Changes are only accepted from the Thread page"}, status_code=403)
        url = str((await request.json()).get("url", "")).strip()
        try:
            events = parse_events(await run_in_threadpool(fetch_feed, url))
        except FeedError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        here(request).set_canvas_feed(url)
        return JSONResponse({"canvas_feed": True, "events": len(events)})

    async def canvas_feed_disconnect(request: Request):
        w = here(request)
        return await change(request, lambda b: (w.clear_canvas_feed(), {"canvas_feed": False})[1])

    async def canvas_disconnect(request: Request):
        w = here(request)
        return await change(request, lambda b: (w.clear_canvas_token(), {"canvas": False})[1])

    async def send_feedback(request: Request):
        # Bug reports go to the operator's file, never into a user's notes.
        account = request.scope["thread.user"] if online else "laptop"
        email = sessions.store.email(account) if online else ""
        return await change(request, lambda b: {"id": feedback.add(
            b.get("message", ""), account, "page", email, b.get("context") if isinstance(b.get("context"), dict) else None)["id"]})

    async def time_zone(request: Request):
        # GET: the saved zone and whether ?browser= differs. POST: switch zones.
        w = here(request)
        if request.method == "POST":
            def act(b):
                if b.get("keep"):   # remember the choice for this zone pair
                    w.keep_zone_over(timezones.canonical(str(b["keep"])) or "")
                    return {"timezone": w.timezone_name()}
                return {"timezone": w.set_timezone(str(b.get("timezone", "")))}
            return await change(request, act)
        saved = w.timezone_name()
        browser = timezones.canonical(request.query_params.get("browser", ""))
        differs = bool(browser and saved and browser != saved and not timezones.same_clock(browser, saved)
                       and browser not in w.kept_over())
        return JSONResponse({
            "timezone": saved, "label": timezones.label(saved) if saved else None,
            "browser": browser, "browser_label": timezones.label(browser) if browser else None,
            "browser_city": timezones.city(browser) if browser else None,
            "city": timezones.city(saved) if saved else None,
            "differs": differs, "missing": bool(browser and not saved and online),
        })

    # ---------- chat backup (online only) ----------
    def backups(request: Request):
        w = here(request)
        if w.chats is None:
            raise FileNotFoundError("Chat backup works on the online version of Thread.")
        return w.chats

    async def chats_list(request: Request):
        try:
            box = backups(request)
        except FileNotFoundError as e:
            return JSONResponse({"available": False, "chats": [], "error": str(e)})
        return JSONResponse({"available": True, "days": RETENTION_DAYS, "chats": box.overview(),
                             "has_key": sessions.store.has_backup_key(request.scope["thread.user"])})

    async def chat_one(request: Request):
        try:
            chat = backups(request).get(request.path_params["key"])
        except (FileNotFoundError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        if request.query_params.get("format") == "md":
            name = re.sub(r"[^A-Za-z0-9 -]+", "", chat.get("title", "chat"))[:60].strip() or "chat"
            return Response(as_markdown(chat), media_type="text/markdown",
                            headers={"content-disposition": f'attachment; filename="{name}.md"'})
        return JSONResponse(chat)

    async def backup_key(request: Request):
        if not online:
            return JSONResponse({"error": "Chat backup works on the online version of Thread."}, status_code=400)
        if stale(request):
            return step_up()
        return await change(request, lambda b: {"key": sessions.store.new_backup_key(request.scope["thread.user"])})

    routes = [
        Route("/api/chats", chats_list),
        Route("/api/chats/{key}", chat_one),
        Route("/api/backup-key", backup_key, methods=["POST"]),
        Route("/api/profile", profile, methods=["GET", "PUT"]),
        Route("/api/privacy", privacy, methods=["POST"]),
        Route("/api/saving", saving, methods=["POST"]),
        Route("/api/forget-learned", forget_learned, methods=["POST"]),
        Route("/api/restore-learned", restore_learned, methods=["POST"]),
        Route("/api/password", password, methods=["POST"]),
        Route("/api/account", account),
        Route("/api/reauth", reauth, methods=["POST"]),
        Route("/api/2fa/start", twofa_start, methods=["POST"]),
        Route("/api/2fa/confirm", twofa_confirm, methods=["POST"]),
        Route("/api/2fa/off", twofa_off, methods=["POST"]),
        Route("/api/2fa/recovery", twofa_recovery, methods=["POST"]),
        Route("/api/signout-others", signout_others, methods=["POST"]),
        Route("/api/nudge-2fa", nudge_dismiss, methods=["POST"]),
        Route("/api/export", export),
        Route("/api/google", google_status),
        Route("/api/google/disconnect", google_disconnect, methods=["POST"]),
        Route("/google/connect", google_connect),
        Route("/api/canvas", canvas_connect, methods=["POST"]),
        Route("/api/canvas/disconnect", canvas_disconnect, methods=["POST"]),
        Route("/api/canvas-feed", canvas_feed, methods=["POST"]),
        Route("/api/due", due),
        Route("/api/class-colors", class_colors, methods=["GET", "POST"]),
        Route("/api/canvas-feed/disconnect", canvas_feed_disconnect, methods=["POST"]),
        Route("/api/timezone", time_zone, methods=["GET", "POST"]),
        Route("/api/setup", setup, methods=["GET", "POST"]),
        Route("/api/feedback", send_feedback, methods=["POST"]),
        Route("/google/callback", google_callback),
        Route("/api/rename", rename, methods=["POST"]),
        Route("/api/delete", delete, methods=["POST"]),
        Route("/api/restore", restore, methods=["POST"]),
        Route("/", index),
        Route("/api/graph", graph),
        Route("/api/tree", tree),
        Route("/api/folder", new_folder, methods=["POST"]),
        Route("/api/note", note_write, methods=["POST", "PUT"]),
        Route("/api/note", note),
        Mount("/static", StaticFiles(directory=WEB), name="static"),
    ]
    # The icon is public so the sign-in page can load it.
    icons = [Route("/favicon.svg", lambda r: FileResponse(WEB / "icon.svg", media_type="image/svg+xml")),
             Route("/favicon.ico", lambda r: FileResponse(WEB / "icon.svg", media_type="image/svg+xml")),
             Route("/apple-touch-icon.png", lambda r: FileResponse(WEB / "apple-touch-icon.png")),
             # Wordmark font for the sign-in pages.
             Route("/font/instrument-serif.woff2", lambda r: FileResponse(WEB / "instrument-serif.woff2")),
             # Legal and support pages are public.
             Route("/privacy", lambda r: HTMLResponse(legal.doc_page("Privacy Policy", legal.PRIVACY))),
             Route("/terms", lambda r: HTMLResponse(legal.doc_page("Terms of Use", legal.TERMS))),
             Route("/support", lambda r: HTMLResponse(legal.doc_page("Support", legal.SUPPORT))),
             Route("/.well-known/security.txt",
                   lambda r: PlainTextResponse(legal.security_txt(public_url or str(r.base_url))))]
    routes = icons + routes
    app = Starlette(routes=routes)
    if sessions is None:
        from thread.security import SecurityHeaders
        return SecurityHeaders(app, https=False)   # same headers and CSP as online

    # Online: everything else requires a session.
    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope)
            user = sessions.user(request)
            scope["thread.user"] = user
            if user is None:
                if request.url.path.startswith("/api/"):
                    response = JSONResponse({"error": "Sign in first"}, status_code=401)
                else:
                    response = RedirectResponse("/signin", status_code=303)
                return await response(scope, receive, send)
        await app(scope, receive, send)

    async def backup_upload(request: Request):
        """POST /backup/chat from the extension, signed with the account's backup key."""
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=EXTENSION_CORS)
        auth = request.headers.get("authorization", "")
        user = sessions.store.backup_user(auth[7:]) if auth.lower().startswith("bearer ") else None
        if not user:
            return JSONResponse({"error": "This backup key doesn't work. Make a new one in Thread → Me → Chats."},
                                status_code=401, headers=EXTENSION_CORS)
        too_big = JSONResponse({"error": "That chat is too big to back up."}, status_code=413, headers=EXTENSION_CORS)
        try:
            if int(request.headers.get("content-length") or 0) > MAX_UPLOAD:
                return too_big
        except ValueError:
            return JSONResponse({"error": "Bad request"}, status_code=400, headers=EXTENSION_CORS)
        # Stream with a size cap so chunked bodies without Content-Length can't exhaust memory.
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD:
                return too_big
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            saved = workspaces.get(user).chats.save(json.loads(body))
        except (ValueError, json.JSONDecodeError, AttributeError) as e:
            return JSONResponse({"error": str(e) or "Bad request"}, status_code=400, headers=EXTENSION_CORS)
        return JSONResponse(saved, headers=EXTENSION_CORS)

    return Starlette(routes=[
        *icons,
        Route("/backup/chat", backup_upload, methods=["POST", "OPTIONS"]),
        Route("/signin", sessions.signin_page, methods=["GET", "POST"]),
        Route("/signup", sessions.signup_page, methods=["GET", "POST"]),
        Route("/join/{code}", sessions.join_page),
        Route("/signup/check", sessions.signup_check, methods=["POST"]),
        Route("/signout", sessions.signout, methods=["GET", "POST"]),
        Mount("/", app=guarded),
    ])


def main() -> None:
    print(f"Thread home screen: http://{HOST}:{PORT}")
    uvicorn.run(build_app(Brain(default_root())), host=HOST, port=PORT, log_level="warning")
