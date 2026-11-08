# Changelog

## 1.0.1

This release is all security fixes. Before more people installed the hub, I had its code reviewed for security, and this release fixes everything the review found. If you run 1.0.0, please update.

### Fixed

- **Download headers.** A file's type, as given when it was uploaded, was copied into the headers of every later download. Someone with the access token or a member account could use that to add their own headers. File types are now checked when they're stored and again when they're sent, and types stored before this release are cleaned up when the hub starts.
- **The token in the address.** `?token=` worked on every address, not just the live stream, and a wrong token fell back to a signed-in cookie. With the reverse proxy example, that let someone with a local account get around single sign-on. The token in the address now only works for the live stream, a wrong token is always refused, and the proxy example no longer lets `?token=` skip Authentik.
- **What members can do.** Members could delete forever, merge projects, change rules and start generations. Members can still read everything, work with cards, tasks and projects, upload files and ask the library. The rest is now for admins, and any new route is admin only until it's marked as safe for members.
- **Links.** Links that weren't web addresses, such as `javascript:`, could be saved on cards, projects and commit links and then clicked. Only `http` and `https` links are kept now, and the page checks again before it shows one.
- **Card status.** A status sent to the ingest API could be shown as HTML in the Details tab. It's plain text now.
- **Open WebUI chat ids.** A chat id containing `../` could make the hub fetch other Open WebUI pages using your key. Chat ids are now checked.
- **Secret scrubbing.** Notes, card links, folders, task and project text, file names and paths are now scrubbed too. It also catches Slack and Discord webhooks, Stripe, SendGrid, Twilio, npm, PyPI, Hugging Face, DigitalOcean, Google OAuth, Telegram, Azure keys and Basic auth. To clean what's already stored, send `POST /api/admin/redact` with the access token once.
- **Names with accents.** Downloading or exporting a file or card with letters outside Latin-1, like Polish ones, failed. Downloads now send the real name the standard way.
- **Login lockout.** Failing to sign in with thousands of different names reset every lock. Locks now stay until they expire, and an address is locked after 30 wrong passwords across any accounts.
- **Regex rules.** A title rule like `/^(a+)+$/` could freeze the whole hub. Rules that repeat a group, use back references or repeat more than three times are refused.
- **Smaller fixes.** A negative `Content-Length` is refused, JSON requests are limited to 16 MB, and exports are written to a temporary file instead of memory. `--restore` only takes a snapshot name. The page has a Content Security Policy and can't be framed. Writes from other sites are refused, and the session cookie is `SameSite=Strict`. The detailed health check needs the token. The database and files are readable only by the hub's own user. Errors no longer show internal details. Hook fields are checked for the right type. The installer checks the hook it downloads against a checksum and warns when the hub isn't on https. Text quoted from your library is clearly marked as material, not instructions, everywhere it reaches a model. Task changes made through MCP show in the task's activity.

The test suite has a test for each of these.

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
