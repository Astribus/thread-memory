// Uploads chats to Thread with the backup key and records the last result for the popup.
async function settings() {
  const { thread = "", key = "" } = await chrome.storage.local.get(["thread", "key"]);
  return { thread: thread.replace(/\/+$/, ""), key };
}

const CHAT_SITES = ["https://chatgpt.com", "https://claude.ai"];
chrome.runtime.onMessage.addListener((message, sender) => {
  // Accept messages only from this extension's content script.
  if (message?.type !== "backup" || sender.id !== chrome.runtime.id) return;
  if (!sender.url || !CHAT_SITES.some(site => sender.url.startsWith(site + "/"))) return;
  (async () => {
    const { thread, key } = await settings();
    if (!thread || !key) return chrome.storage.local.set({ status: { ok: false, at: Date.now(), error: "Not set up yet." } });
    if (!/^https:\/\/|^http:\/\/(127\.0\.0\.1|localhost)(:|\/|$)/.test(thread))
      return chrome.storage.local.set({ status: { ok: false, at: Date.now(), error: "Thread's address must start with https://." } });
    try {
      const res = await fetch(thread + "/backup/chat", {
        method: "POST", headers: { "content-type": "application/json", authorization: "Bearer " + key },
        body: JSON.stringify(message.chat),
      });
      const body = await res.json().catch(() => ({}));
      await chrome.storage.local.set({ status: res.ok
        ? { ok: true, at: Date.now(), title: message.chat.title, app: message.chat.source, messages: body.messages }
        : { ok: false, at: Date.now(), error: body.error || `Thread said ${res.status}.` } });
    } catch {
      await chrome.storage.local.set({ status: { ok: false, at: Date.now(), error: "Couldn't reach your Thread." } });
    }
  })();
});
