const $ = id => document.getElementById(id);
function ago(ms) { const s = (Date.now() - ms) / 1000; return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; }
async function show() {
  const { thread = "", key = "", status } = await chrome.storage.local.get(["thread", "key", "status"]);
  $("thread").value = thread; $("key").value = key;
  const el = $("status");
  if (!status) { el.textContent = thread && key ? "Open a chat on chatgpt.com or claude.ai and it will back up." : ""; return; }
  el.className = status.ok ? "ok" : "bad";
  el.textContent = status.ok
    ? `Backed up ${ago(status.at)}: "${status.title}" (${status.messages} messages)`
    : `${status.error} (${ago(status.at)})`;
}
$("save").onclick = async () => {
  let thread = $("thread").value.trim().replace(/\/+$/, "");
  if (thread && !/^https?:\/\//.test(thread)) thread = "https://" + thread;
  const key = $("key").value.trim();
  // Self-hosted servers need a host permission.
  try {
    const u = new URL(thread);
    // The key is sent with every chat: require https (or localhost).
    if (u.protocol !== "https:" && !["127.0.0.1", "localhost"].includes(u.hostname)) throw new Error("not https");
    const origin = u.origin + "/*";
    if (!/\.fly\.dev\/\*$|^http:\/\/(127\.0\.0\.1|localhost)/.test(origin)) await chrome.permissions.request({ origins: [origin] });
  } catch { $("status").className = "bad"; $("status").textContent = "That address doesn't look right."; return; }
  await chrome.storage.local.set({ thread, key, status: null });
  $("status").className = "ok"; $("status").textContent = "Saved. Open a chat on chatgpt.com or claude.ai.";
};
show();
