# Thread Backup (Chrome extension)

Keeps a copy of your ChatGPT and Claude chats in your Thread for 10 days, so a chat you delete in those apps isn't gone forever. Your AI can search the copies too.

## Install
1. Download this `extension` folder. On GitHub: Code → Download ZIP, then unzip it.
2. In Chrome, open `chrome://extensions` and turn on **Developer mode** (top right).
3. Press **Load unpacked** and pick the `extension` folder.
4. In Thread, open **Me → Chats → Make my backup key**, and copy the key and your Thread address.
5. Click the Thread Backup icon in Chrome's toolbar, paste both, and press **Save**.
6. Open any chat on chatgpt.com or claude.ai. A few seconds after a reply finishes, it shows up in Me → Chats.

## What it reads
- Only the conversation on chatgpt.com and claude.ai: your messages and the AI's replies. Never the message box, passwords, or other sites.
- It waits until a reply has finished, and only sends a chat when something changed.
- It works on a computer only. The ChatGPT and Claude phone apps can't run extensions.

## If it stops working
The popup shows the last backup or the last error. If ChatGPT or Claude redesign their page, it says "No chat found" rather than saving the wrong thing. The page selectors it relies on are at the top of `content.js`.
