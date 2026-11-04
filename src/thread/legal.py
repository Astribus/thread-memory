"""Public pages: /privacy, /terms, /support and /.well-known/security.txt.

The policy text is a draft, not legal advice. Operators should review it and set THREAD_OPERATOR and
THREAD_SUPPORT_EMAIL.
"""

import os
import os
from html import escape

DRAFT = "DRAFT, not legal advice. Last updated October 21, 2026."
OPERATOR = os.environ.get("THREAD_OPERATOR", "the person who runs this Thread server")
SUPPORT_EMAIL = os.environ.get("THREAD_SUPPORT_EMAIL", "support@<domain>")

DOC_STYLE = """
  @font-face { font-family:"Instrument Serif"; src:url("/font/instrument-serif.woff2") format("woff2"); font-display:swap; }
  :root { --bg:#F3EDE2; --ink:#2B2622; --muted:#685E54; --hair:#E2D8C8; --card:#FBF7F0; --primary:#34507E; }
  @media (prefers-color-scheme: dark) { :root { --bg:#1E1A17; --ink:#EFE7DA; --muted:#B3A796; --hair:#3B342E;
    --card:#2A2521; --primary:#9DB4DC; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:17px/1.6 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", system-ui, sans-serif; }
  main { max-width:680px; margin:0 auto; padding:48px 20px 80px; }
  .mark { font:400 30px/1 "Instrument Serif", "New York", Georgia, serif; color:var(--muted); margin:0 0 28px; display:block;
          text-decoration:none; }
  h1 { font:400 40px/1.1 "Instrument Serif", "New York", Georgia, serif; letter-spacing:-.01em; margin:0 0 10px; }
  h2 { font:600 19px/1.35 inherit; margin:34px 0 8px; }
  p, li { color:var(--ink); } ul { padding-left:22px; } li { margin:6px 0; }
  .draft { background:var(--card); border:1px solid var(--hair); border-radius:12px; padding:12px 16px; color:var(--muted);
           font-size:14px; margin:0 0 26px; }
  .muted { color:var(--muted); }
  a { color:var(--primary); }
  nav.foot { margin-top:48px; padding-top:18px; border-top:1px solid var(--hair); font-size:14px; color:var(--muted); }
  nav.foot a { margin-right:16px; }
"""


def doc_page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)} · Thread</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>{DOC_STYLE}</style></head><body><main>
<a class="mark" href="/">Thread</a>
<h1>{escape(title)}</h1>
<p class="draft">{escape(DRAFT)}</p>
{body}
<nav class="foot"><a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="/support">Support</a></nav>
</main></body></html>"""


PRIVACY = f"""
<p>Thread is a personal memory for your AI. When you connect Thread to an AI app like Claude or ChatGPT, that AI
can save what matters about you into your Thread and read it back in later chats. This page explains what Thread
stores, where, who can see it, and how to get it back or delete it. Thread is run by {escape(OPERATOR)}, as an
individual.</p>

<h2>What Thread stores</h2>
<ul>
  <li><b>Your account:</b> your email address and your password, stored only as a scrypt hash (Thread can't read
    your password). Sign-in sessions, AI-app access tokens, invite codes and backup keys are stored only as SHA-256
    hashes. If you turn on two-step sign-in, your authenticator key is stored encrypted and your recovery codes
    only as hashes. Thread keeps your last 20 sign-ins for up to 30 days (the time, the browser and its system,
    like "Safari on iPhone") so you can spot one that wasn't you; it doesn't keep your IP address or location
    with them.</li>
  <li><b>Your memory:</b> the notes your AI saves or you write: your profile (name, school, goals, rules), notes in
    folders, "What my AI learned" observations about how you think and work, a few of your own chat messages copied
    word for word as examples of how you talk ("My voice"), your saving and privacy choices, your time zone, and
    your class colors.</li>
  <li><b>Chat backups</b> (only if you install Thread's browser extension and turn it on): copies of the ChatGPT
    and Claude chats you open in that browser, so a chat you delete there can still be found.</li>
  <li><b>Connections you choose to add:</b> a Canvas calendar feed link or a Canvas access token, and a Google
    sign-in token plus which calendars to read, so Thread can read your deadlines and add calendar events you ask
    for.</li>
  <li><b>Bug reports and ideas</b> you send (from Settings or through your AI): the message, your account email,
    and your screen size, window size and browser name. Never your notes.</li>
</ul>
<p>Thread does not use analytics, advertising trackers or third-party cookies. It sets two cookies: one that
keeps you signed in (7 days) and one that tells your browser which account's display settings to use.</p>

<h2>How Thread uses it</h2>
<ul>
  <li>To show your memory to you and to the AI apps you connect, when they ask for it.</li>
  <li>To read your own Canvas deadlines and syllabus, and your Google Calendar, when you or your AI ask.</li>
  <li>To fix problems you report.</li>
</ul>
<p><b>Thread never sells your data, never shows ads, and never uses your data to train AI models.</b></p>

<h2>Where it's stored</h2>
<p>On servers run by Fly.io in the United States (the Newark, New Jersey region), on an encrypted disk. Fly.io
keeps daily snapshots of that disk for 5 days as a backup. Each account's data sits in its own folder, and one
account can't read another's.</p>

<h2>Who can see it</h2>
<ul>
  <li><b>You</b>, signed in to your account.</li>
  <li><b>The AI apps you connect</b>, such as Claude (Anthropic) or ChatGPT (OpenAI). When they call Thread's
    tools, the part of your memory they ask for is sent to them, and what they do with it is covered by their own
    privacy policies. You can disconnect an app at any time in that app's settings, and connections expire if
    unused for 14 days.</li>
  <li><b>The person who runs Thread</b> ({escape(OPERATOR)}) can access the server to keep it running, and will only look at
    your data to fix a problem you report, with your permission, or when the law requires it.</li>
  <li><b>Services Thread calls for you:</b> Canvas (Instructure) and Google, only with the link or sign-in you
    gave, and only to read your deadlines and calendar or add an event you asked for.</li>
</ul>
<p>Thread doesn't share your data with anyone else.</p>

<h2>How long it's kept, and deleting it</h2>
<ul>
  <li><b>Notes</b> stay until you delete them. Deleted notes go to a trash folder in your account first, so you can
    undo a mistake.</li>
  <li><b>Chat backups</b> delete themselves 10 days after they're first saved.</li>
  <li><b>Sign-ins</b> expire: browser sessions after 7 days, AI-app access after 1 hour (renewed automatically for
    up to 14 days of use).</li>
  <li><b>Download everything</b> anytime: Settings → Your data → Download my memory (.zip). It's plain Markdown
    files you can open anywhere.</li>
  <li><b>Delete your account:</b> email {escape(SUPPORT_EMAIL)} from your account's email address, and your account,
    memory, chat backups and connections are deleted within 30 days (disk snapshots age out 5 days later).</li>
</ul>

<h2>Security</h2>
<p>Thread uses HTTPS everywhere, stores passwords and sign-in tokens only as hashes, encrypts your connected
accounts' keys, offers two-step sign-in with an authenticator app, asks you to confirm it's you before sensitive
changes, locks an account for 15 minutes after 5 wrong passwords or codes, and keeps each account's files private to that account. No system is perfectly
secure; if you find a problem, please report it (see <a href="/support">Support</a>).</p>

<h2>Changes</h2>
<p>If this policy changes, the new version will be posted here with a new date, and you'll get an email for any
change that affects how your data is used.</p>

<h2>Contact</h2>
<p>{escape(SUPPORT_EMAIL)}, or Settings → Report a bug or idea.</p>
"""

TERMS = f"""
<p>These terms are between you and {escape(OPERATOR)}, the individual who runs this Thread server. By creating an account you agree to
them.</p>

<h2>What Thread is</h2>
<p>Thread is an early beta, open by invite only. It's free right now. Features may change, break or be removed, and
there is no guarantee it will be available at any particular time.</p>

<h2>Your account</h2>
<ul>
  <li>You need an invite to sign up, and you must give a working email address.</li>
  <li>Keep your password private; you're responsible for what happens in your account.</li>
  <li>You must be at least 18 years old to use Thread.</li>
</ul>

<h2>Your content is yours</h2>
<p>Everything in your memory belongs to you. You give Thread permission to store it, process it and send it to the
AI apps you connect, only so the service works for you. You can download it or ask for it to be deleted at any
time (see the <a href="/privacy">Privacy Policy</a>).</p>

<h2>Using Thread responsibly</h2>
<ul>
  <li>Don't use Thread to break the law, to store other people's private information without the right to, or to
    harass anyone.</li>
  <li>Don't try to get into other accounts, overload the service, or get around its security.</li>
  <li>When you connect Canvas or Google, you're responsible for following your school's and those services' rules,
    including any rules about creating Canvas access tokens.</li>
  <li>The chat backup extension saves the chats you open in your own browser. Use it only with your own
    accounts.</li>
</ul>

<h2>AI and accuracy</h2>
<p>Your memory is written mostly by AI apps, and AI can be wrong. Thread shows you everything it keeps so you can
fix it, but don't rely on it for anything critical (for example, always check due dates in Canvas).</p>

<h2>Ending</h2>
<p>You can stop using Thread and ask for your account to be deleted at any time. Thread may suspend an account that
breaks these terms or puts the service or other people at risk, and may shut the beta down; if that happens you'll
get notice and a chance to download your memory when possible.</p>

<h2>No warranty, limited liability</h2>
<p>Thread is provided "as is", without warranties of any kind. To the extent the law allows, {escape(OPERATOR)} isn't liable
for indirect or consequential damages, or for lost data, arising from using Thread.</p>

<h2>Changes and contact</h2>
<p>If these terms change, the new version will be posted here with a new date. Questions: {escape(SUPPORT_EMAIL)}.</p>
"""

SUPPORT = f"""
<p>Thread is in an invite-only beta, run by one person. Here's how to get help.</p>

<h2>Something's broken, confusing, or you have an idea</h2>
<ul>
  <li>In Thread: <b>Settings → Report a bug or idea</b>.</li>
  <li>Or tell your AI "report a Thread bug: …" and it will send it after you agree.</li>
  <li>Or email {escape(SUPPORT_EMAIL)}.</li>
</ul>

<h2>Common questions</h2>
<ul>
  <li><b>Connect Claude:</b> in a browser, go to claude.ai → Settings → Connectors → Add custom connector, and paste
    your Thread address followed by <code>/mcp</code>. It then works in the Claude apps on your phone too.</li>
  <li><b>Forgot your password:</b> email {escape(SUPPORT_EMAIL)} from your account's email address for a temporary
    one.</li>
  <li><b>Lost your phone and your recovery codes:</b> email {escape(SUPPORT_EMAIL)} from your account's email
    address. Thread checks it's really you before turning two-step sign-in off, and the change shows in your
    Recent sign-ins.</li>
  <li><b>Get your data or delete your account:</b> see the <a href="/privacy">Privacy Policy</a>.</li>
</ul>

<h2>Security problems</h2>
<p>If you've found a security issue, please email {escape(SUPPORT_EMAIL)} with "Security" in the subject and don't
share it publicly until it's fixed. You'll get a reply as soon as possible.</p>
"""


def security_txt(public_url: str) -> str:
    """RFC 9116. Points at the support page until the support email exists."""
    base = public_url.rstrip("/")
    return (f"Contact: {base}/support\n"
            "Expires: 2027-10-21T00:00:00.000Z\n"
            "Preferred-Languages: en\n"
            f"Policy: {base}/support\n")
