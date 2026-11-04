// Reads the open conversation on chatgpt.com / claude.ai and hands it to the background worker.
// Reads message content only (never the input box), and sends only after a reply finishes and the chat
// changed. If the page structure changes, backup stops rather than saving the wrong thing.

const SITE = location.hostname.includes("claude.ai") ? "claude" : "chatgpt";
const clean = text => (text || "").replace(/​/g, "").trim();

function readChatGPT() {
  const section = document.querySelector("section[data-conversation-id]");
  const fromUrl = (location.pathname.match(/\/(?:c|uc)\/([0-9a-f-]{20,})/) || [])[1];
  const chatId = section?.dataset.conversationId || fromUrl;
  if (!chatId) return null;
  let rows = [...document.querySelectorAll("ol[data-conversation-transcript] > li[data-message-role]")].map(li => ({
    id: li.id, role: li.dataset.messageRole,
    text: clean((li.querySelector(li.dataset.messageRole === "user" ? "[data-user-message-copy]" : "[data-assistant-markdown]") || li).innerText),
  }));
  if (!rows.length) {   // older ChatGPT layout
    rows = [...document.querySelectorAll("[data-message-author-role]")].map(el => ({
      id: el.dataset.messageId || "", role: el.dataset.messageAuthorRole, text: clean(el.innerText) }));
  }
  const generic = /^ChatGPT(:|$)/.test(document.title);
  const firstUser = rows.find(r => r.role === "user")?.text || "";
  return { source: "chatgpt", chat_id: chatId, url: location.href,
           title: generic ? firstUser.slice(0, 80) : document.title.replace(/\s*[-|]\s*ChatGPT$/, ""),
           messages: rows, busy: !!document.querySelector('[data-testid="stop-button"], button[aria-label*="Stop"]') };
}

function readClaude() {
  const chatId = (location.pathname.match(/\/chat\/([0-9a-f-]{20,})/) || [])[1];
  if (!chatId) return null;
  const rows = [...document.querySelectorAll('[data-testid="transcript-row"]')];
  const busy = rows.some(r => r.dataset.perfRowStreaming === "true") || !!document.querySelector('[data-is-streaming="true"]');
  const messages = rows.map(r => {
    const user = r.dataset.perfRow === "human";
    const body = r.querySelector(user ? '[data-testid="user-message"]' : ".font-claude-response");
    return { id: "row-" + (r.dataset.index ?? ""), role: user ? "user" : "assistant", text: clean(body?.innerText) };
  }).filter(m => m.id !== "row-");
  return { source: "claude", chat_id: chatId, url: location.href,
           title: document.title.replace(/\s*-\s*Claude$/, ""), messages, busy };
}

let lastSent = "";
let timer = null;
function check() {
  const chat = SITE === "claude" ? readClaude() : readChatGPT();
  if (!chat || chat.busy) return;
  chat.messages = chat.messages.filter(m => m.id && m.text && (m.role === "user" || m.role === "assistant"));
  if (!chat.messages.length) return;
  delete chat.busy;
  const signature = JSON.stringify([chat.chat_id, chat.title, chat.messages]);
  if (signature === lastSent) return;
  lastSent = signature;
  chrome.runtime.sendMessage({ type: "backup", chat }).catch(() => {});
}
// Debounce: wait for 3 s of no DOM changes.
new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(check, 3000); })
  .observe(document.body, { childList: true, subtree: true, characterData: true });
setInterval(check, 15000);
check();
