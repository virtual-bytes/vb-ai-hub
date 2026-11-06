# Changelog

## 1.0.0

This is the first public release of VB AI Hub.

I built it because my AI work was spread across Claude Code sessions, claude.ai chats and Open WebUI, and I kept losing track of what had been done and where. The hub pulls all of that into one place you run yourself. There's a live board of what's happening, and a library that keeps every handoff, document and transcript, so a project can be picked up weeks later.

### What's in it

**The board.** Sessions and chats show up on their own as they happen, in Ideas, Active, Needs you and Done. You can drag cards between columns or switch to a list.

**The library.** Each project has an overview, documents, work, activity and settings. Handoffs and documents are versioned, and search covers everything. You can also ask the library a question and get an answer with sources.

**Handoffs.** Generate a handoff from a project's real history, compare it with the last version, choose which sessions it's built from, then save it. One click turns its next steps into tasks. New Claude Code sessions get the project's latest handoff automatically.

**Usage.** A ticker across the top keeps a running count of tokens. The Usage page shows burn rate, daily charts, and rankings by project, model and source, plus estimated cost at API prices with a monthly budget and daily limit. Costs are estimates, so on a Claude subscription they show what the same usage would cost on the API, not what you pay.

**Tasks.** Quick add, due dates and priorities, with reminders. The Library opens on a page showing which projects need you.

**Claude can use it too.** The hub is an MCP server, so Claude Code can search your library, read handoffs and manage tasks from inside a session. It can add and update tasks, and it can't delete anything.

**The rest.** Accounts or single sign-on, secret scrubbing on everything stored, nightly backups with restore, a trash, webhook notifications for Home Assistant and ntfy, and light and dark themes that work on a phone.

### What it connects to

| Tool | What you get |
|---|---|
| Claude Code | Everything, automatically, through hooks and MCP |
| Open WebUI | Chats sync on their own, and it writes your handoffs and answers. GPT and Grok used through Open WebUI come along too |
| LiteLLM | Counts the work done by your own hardware |
| claude.ai | Import a data export |
| ChatGPT and Grok websites | Add a chat link to the board. The conversation itself isn't captured yet |
| Cursor and Codex | Setup for using the library through MCP. Their sessions don't appear on the board yet |
| Anything else | A simple API for cards, work logs, files and token usage |

### Known limits

- The Cursor and Codex setup follows their documentation, but I haven't tried it myself. If you use either one, I'd love to hear how it goes.
- ChatGPT and Grok data exports can't be imported yet.
- Connectors on claude.ai and ChatGPT need OAuth, which the hub doesn't have yet.
- Work on local models isn't broken down by project.
- Prices in Settings are a snapshot, so check them when prices change.

### Install

You need Docker. The README has the three commands, and Settings shows the rest once it's running.
