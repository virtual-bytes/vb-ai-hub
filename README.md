# Virtual Bytes AI Hub

**VB AI Hub** is a self-hosted live board and project library for your AI work across Claude Code, claude.ai, Open WebUI, and any other tool that posts to it. It's plain Python with no dependencies and no build step, and all your data stays on your own server.

- **Board:** Ideas → Active → Needs you → Done, updated live.
- **Library:** every item keeps its handoff docs and documents (versioned), a work log of summaries, the files it created or edited, and the full transcript. You can browse the library by project and search across all of it. Archiving an item takes it off the board and keeps it in the library. **Download all** exports an item as a zip.

## Install

You need Docker. Get the repository, set an access token, and start the hub:

```bash
git clone https://github.com/virtual-bytes/vb-ai-hub.git
cd vb-ai-hub
echo "HUB_TOKEN=$(openssl rand -hex 32)" > .env
docker compose up -d
```

Open `http://your-server:8787` and create the admin account. It asks for the token in `.env`, which also connects tools and hooks to the hub.

Without Docker, you only need Python. It's tested on 3.13 and 3.14:

```bash
HUB_HOST=0.0.0.0 HUB_TOKEN=<token> python3 server.py
```

Data lives in the `vb-ai-hub-data` volume: `hub.db` plus `blobs/`. Transcripts can hold sensitive text, so put the hub behind TLS and keep it off the open internet. [examples/reverse-proxy](examples/reverse-proxy) covers TLS and single sign-on.

### Connect your tools

- **Claude Code:** Settings → Connect tools shows two commands to copy. One tracks that machine's sessions, and the other lets Claude use the library through MCP.
- **Cursor and Codex:** the same page shows the MCP setup, so they can use the library. Their sessions don't appear on the board yet.
- **GPT and Grok:** use them through Open WebUI and their chats sync like any other. A chatgpt.com or grok.com link added to the board gets its own tab, but the conversation itself isn't captured.
- **Open WebUI:** add your address and API key under Settings → Connections. The hub then syncs your chats and can write handoffs and answers with your models.
- **LiteLLM:** add your gateway under Settings → Connections to count the work done by your own hardware.
- **claude.ai:** there's no listing API, so use Settings → Import and capture for a data export.

The hook setup writes `~/.config/vb-ai-hub/config.json` and merges the hooks into `~/.claude/settings.json`, saving `settings.json.bak-<ts>` first. From a copy of the repository, `HUB_URL=<hub> HUB_TOKEN=<token> ./install.sh` does the same.

## Projects, tasks and rules

- **Projects** are real objects, each with a description, goals, links, and a status (active, paused, done, archived). Rename a project under its Settings tab. Renaming it to an existing name, or using **Merge into…**, merges the two: items, tasks, documents, and rules all move. A rename also adds a rule, so new events from the old source name keep landing in the renamed project.
- **Matching rules** decide an item's project automatically. Three kinds are checked in order: *title contains* (or `/regex/`), *folder starts with*, and *source project is* (the git repo, Open WebUI folder, or cwd name). Adding or removing a rule re-files existing items. An item whose project you set by hand keeps it. Choose **use rules** on the item to hand it back to the rules.
- **Tasks** group the sessions and chats behind one piece of work. Each task has notes and done/open status, its own documents, and a **Handoff** button.

## Tasks view

**Tasks** (in the side menu) lists open tasks across projects, grouped as overdue, due this week, later, no due date, and done. Type in the box at the top to add one, tick it off in the list, and click it to edit priority (urgent, high, normal, low) and due date beside the list. Cards show their task, which turns red when it's overdue. With notifications on, a daily reminder lists tasks that are due or overdue (default 08:00).

## Ask the library

**Ask the library**, at the top of the Library page, answers a question from your docs, handoffs, work logs, notes, and project and task descriptions, with numbered, clickable sources. It can be limited to one project.

- **Index:** a background job splits content into ~1,400-character passages and embeds them through Open WebUI's `/api/embeddings` with the `embed` model, about every 90 seconds. **Index now** in Settings runs it immediately.
- **Retrieval:** a question combines keyword hits (FTS5) and embedding similarity, merges duplicates (forks repeat passages), and sends the top 10 to the `chat` model.
- **Without embeddings:** if Open WebUI has no embeddings endpoint, it falls back to keyword retrieval only.

## Deletions and trash

**Move to trash** puts an item in **Trash**, at the top of the Library page. The trash is emptied after 7 days (adjustable; 0 = immediately). Restore or delete forever from there. Items deleted at their source follow the Deletions setting (trash, or keep archived in the library):

- **Open WebUI:** each sync compares the hub against your chat list *and* your archived list. A chat in neither list that returns 404 is treated as deleted. A chat archived in Open WebUI is archived in the hub. The sync refuses to remove more than a third of your chats at once.
- **claude.ai:** there's no API, so re-import a fresh export with **Also remove chats/projects that are no longer in this export**.
- **Claude Code:** on session start, the hook (at most hourly) sends the list of sessions still on disk (`python3 hook.py --reconcile` does it now). Cards for sessions deleted on that machine are removed. Sessions older than Claude Code's `cleanupPeriodDays` (default 30) are kept, because Claude Code deletes those transcripts itself and the library is where they live on. The reconcile refuses to remove more than half at once.

## Secret redaction

Everything stored is scrubbed first: work logs, prompts, titles, text documents, and the inside of compressed transcripts. The scrubber catches:

- private key blocks
- `sk-…`, `sk-ant-…`, GitHub, GitLab, Slack, AWS, and Google keys
- JWTs and bearer tokens
- credentials inside URLs
- `password=`/`token:`-style values on the same line
- the hub's own token, the Open WebUI key, and the webhook URL

Placeholders (`$VAR`, `<x>`, `` `code` ``) and plain words are left alone. `POST /api/admin/redact` scrubs existing content once. It rewrites affected files, rebuilds search, vacuums the database, and replaces the backup set so older unredacted copies don't survive.

## Open WebUI tool

`owui-tool/vb_ai_hub.py` is an Open WebUI tool with four functions:

- `search_library`
- `ask_library`
- `project_brief`: description, open tasks, and the latest handoff
- `file_this_chat`: files the current chat under a project and task, creating them if needed

To install it, go to Workspace → Tools → +, paste the file, save, and set the valves `hub_url` (your hub's address) and `hub_token`. Enable the tool on a model or per chat.

## Project export

**Export (.zip)** in a project's Settings tab bundles the project README (description, goals, links, tasks, items, usage), each item's README, work log, file list, commits and documents with versions, plus task and project documents. Full transcripts are included only if you ask (`?transcripts=1`).

## Generated handoffs and digests

**Generate handoff** works on an item, a task, or a project. The hub sends that scope's notes, work log, files, commits, and latest handoff to Open WebUI's `/api/chat/completions` with the configured model (default `chat`). The reply is saved as a versioned `HANDOFF-….md`. A project handoff opens as a draft first: compare it with an earlier version, choose which sessions it's built from, then save or discard it. **Add next steps as tasks** on a project's Overview turns the steps in its latest handoff into tasks. A **weekly digest** is generated for every project with activity in the last 7 days, by default on Monday at 07:00 server time, as `DIGEST-<project>.md` on the project. Generations run one at a time.

## Notifications and health

- When a Claude Code card has waited in *Needs you* longer than *After (minutes)* (default 10), the hub POSTs to the webhook. Only waits that began in the last 24 hours count.
  - **Home Assistant:** the body is JSON `{title, message, url}`.
  - **ntfy:** the body is plain text, with the `Title` and `Click` headers set.
- Set **Hub address** so the link opens the card (`…/#card=<id>`).
- `GET /healthz` is a plain liveness check. `GET /healthz?deep=1` returns 503 if the last backup is older than 36 hours or failed, or if Open WebUI sync is failing. Point Uptime Kuma at it.

Home Assistant automation for the webhook (replace the notify target):

```yaml
alias: VB AI Hub
trigger:
  - platform: webhook
    webhook_id: vb-ai-hub-<random>
    allowed_methods: [POST]
    local_only: true
action:
  - service: notify.mobile_app_<your_phone>
    data:
      title: "{{ trigger.json.title }}"
      message: "{{ trigger.json.message }}"
      data:
        url: "{{ trigger.json.url }}"
        clickAction: "{{ trigger.json.url }}"
```

The webhook URL is then `http://<home-assistant>:8123/api/webhook/vb-ai-hub-<random>`.

## Token ticker

A strip across the top shows all tokens in and out, Claude usage today and over 7 and 30 days, and usage on your own hardware. It counts up live when usage arrives. Click it, or Usage in the side menu, for the Usage page: totals, burn rate and daily charts for 7 days, 30 days or all time, ranked by project, model, source and local workload, with exact figures underneath.

- **In** is everything the models read (new input, cache writes, cache reads); **out** is what they wrote.
- **Claude:** `hook.py` reports usage per local day at the end of each turn, including subagent and workflow transcripts. Daily rows are kept when a card is deleted, because those tokens were still spent. Sessions no longer on disk are dated by their last activity.
- **Local models:** work served by your own hardware, from the LiteLLM connector below or anything posting to `POST /api/usage/local`.
- `GET /api/usage/stats?today=YYYY-MM-DD` returns everything the ticker shows.
- `GET /api/usage/report?today=YYYY-MM-DD` returns the Usage page: totals and breakdowns for 7 days, 30 days and all time, plus every day since records began.
- `GET /api/projects/<id>/usage?today=YYYY-MM-DD` returns the same Claude figures for one project, for its Status tab. Local model work isn't recorded per project.

## Usage and links

- **Token usage:** for Claude Code, from each transcript's per-message usage (deduped across split records). For Open WebUI chats, from per-message usage where Open WebUI records it. Shown per item, per project, and per model.
- **Commit links:** commits in the session's repos (its folder and the repos of files it edited) made during the session, linked only if their hash appears in the transcript. GitLab merge request and GitHub pull request URLs that appear in the transcript are linked too.
- Recompute everything for existing sessions with `python3 hook.py --reindex`, which is safe to repeat.

## LiteLLM

Settings → Connections → LiteLLM shows how much work your gateway served from your own hardware instead of a paid API. Enter the gateway's URL and a proxy admin or view-only key; other keys only see their own requests. The hub reads `GET /spend/logs/v2` every few minutes, 30 days back on first connect and 2 days after that. That endpoint leaves out prompts and responses, and the hub keeps only daily totals per key alias and model.

**Count as local** decides what counts, one rule per line. A plain rule matches part of the backend address or provider (`10.0.0.5`, `ollama`, `hosted_vllm`), and `model:name` matches a model exactly, which helps with calls LiteLLM logs without a backend address, such as embeddings on some versions. With no rules, everything counts. **Leave out these key aliases** drops health checks and test traffic. Set **Time zone** so days match yours, and rename the ticker with **Label**.

If the hub can't reach the gateway, send totals instead. Each post replaces those day/client/model rows, so repeating one is safe:

```bash
curl -X POST $HUB/api/usage/local -H "Authorization: Bearer $T" -d '{"rows":[{"day":"2026-11-04","client":"my-app","model":"chat","requests":12,"input":3400,"output":900}]}'
```

Use one or the other for the same gateway. The connector needs LiteLLM with `/spend/logs/v2`.

## Backups

With `HUB_BACKUP_DIR` set (the compose file mounts the `vb-ai-hub-backup` volume at `/backup`), the hub takes a nightly online SQLite snapshot (`db/hub-<ts>.db.gz`) and copies new files into `blobs/`. Snapshots older than *Keep snapshots (days)* are pruned, but the newest three are always kept. Use **Back up now** in Settings for an immediate run. To keep backups somewhere else, point that volume at a NAS or other storage. To restore, stop the hub and run:

```bash
docker compose stop
```

```bash
docker compose run --rm vb-ai-hub python3 server.py --restore
```

```bash
docker compose start
```

`--restore <file>` picks a specific snapshot. The replaced database is kept as `hub.db.before-restore-<ts>`.

## Other

- **Auto-archive:** Done items are archived after 30 days (adjustable; 0 turns it off).
- **Select** on the board turns on multi-select. The bar that appears can move cards, set a project or task, archive, or delete.
- **Show as list** on the board swaps the columns for a list grouped as Needs you, Working now, Ideas and Done.
- **Project picker:** the button next to the page title filters the board and tasks, and opens a project in the Library. A dot marks projects that need attention.
- **Phone layout:** a bottom menu, and one board column at a time with Needs you first.

## Sources

| Source | How | What gets archived |
|---|---|---|
| Claude Code (CLI and desktop) | Hooks run `hook.py` on SessionStart, UserPromptSubmit, Stop, and SessionEnd. Archiving runs in a detached background process, so sessions never wait. | Docs the session wrote or edited (`.md`, `.txt`, `.rst`, `.adoc`), handoff docs in the working folder that the session names and that changed while it ran, every file path written or edited, Claude's final reply each turn, and the compressed transcript (re-sent after 1 MB of growth or 20 minutes, and always on SessionEnd). |
| Open WebUI | The server polls the API with the key set in **Settings**. Folders become projects. | Each assistant reply goes to the work log, and the full chat JSON is stored as the transcript. |
| claude.ai | **Import** your data export (`conversations.json`, `projects.json`), the bookmarklet, or **Add card**. claude.ai has no listing API. | Full conversation, project knowledge files, and project instructions. |
| Anything else | `POST /api/ingest` and `PUT …/attachments` (below). | Whatever you send. |

Claude Code's own memory files (`~/.claude/projects/*/memory`) are excluded.

## API

All `/api/*` calls need `Authorization: Bearer <HUB_TOKEN>`. For SSE only, `?token=` also works.

Generic ingest, for any tool:

```bash
curl -X POST $HUB/api/ingest -H "Authorization: Bearer $T" -d '{"source":"my-tool","id":"run-42","title":"Nightly benchmark","project":"My project","status":"running","worklog":"Started: 3 prompts x 5 runs"}'
```

Attach a file. Identical content is stored once, and changed content becomes a new version:

```bash
curl -X PUT --data-binary @HANDOFF.md "$HUB/api/cards/my-tool:run-42/attachments?name=HANDOFF.md&kind=handoff" -H "Authorization: Bearer $T"
```

The rest of the API:

| Method and path | Purpose |
|---|---|
| `GET /api/cards` (`?archived=1`), `GET /api/cards/{id}`, `PATCH /api/cards/{id}`, `DELETE /api/cards/{id}` | Read and edit cards. PATCH takes `title`, `project`, `notes`, `url`, `column`, `position`, `archived`. |
| `GET /api/cards/{id}/archive`, `GET /api/cards/{id}/bundle` (zip), `GET /api/blobs/{sha}` | Read an item's library. |
| `POST /api/cards/{id}/files` `[{path, action, at}]`, `POST /api/cards/{id}/worklog` `[{ref, at, text}]` | Add to the files log and work log. |
| `GET /api/library`, `GET /api/library/project?name=`, `GET /api/search?q=` | Browse and search the library. |
| `POST /api/events` | Claude Code hook payload. |
| `POST /api/import` | claude.ai export. |
| `POST /api/settings`, `POST /api/integrations/owui/sync`, `GET /api/stream` | Settings, Open WebUI sync, and live events. |
| `GET/POST /api/projects`, `GET/PATCH /api/projects/{id}`, `POST /api/projects/{id}/merge` | Projects. |
| `POST /api/tasks`, `GET/PATCH/DELETE /api/tasks/{id}` | Tasks. |
| `GET/POST /api/rules`, `DELETE /api/rules/{id}` | Matching rules. |
| `POST /api/cards/bulk` `{ids, set:{column, archived, project, task_id}, delete}` | Bulk changes. |
| `POST /api/cards/{id}/usage`, `POST /api/cards/{id}/links` | Token usage and commit/MR links. |
| `GET /api/owners/{owner}/archive`, `PUT /api/owners/{owner}/attachments` | Library content for any owner: a card id, `task:<id>`, or `project:<id>`. |
| `POST /api/generate` `{kind: handoff\|digest, scope: card\|task\|project, id, review?, exclude?}`, `GET /api/jobs/{id}` | Generated handoffs and digests. With `review`, a project handoff becomes a draft instead of a saved version; `exclude` lists sources to leave out. |
| `GET`/`DELETE /api/projects/{id}/draft`, `POST /api/projects/{id}/draft/save` | Read or discard the draft with its sources and saved versions, or save it as the next version. |
| `POST /api/notify/test`, `POST /api/backup` | Test the webhook and run a backup now. |
| `POST /api/ask` `{question, project_id?}`, `POST /api/index` | Ask the library; build the index now. |
| `GET /api/cards?trash=1`, `POST /api/cards/{id}/restore`, `DELETE /api/cards/{id}` (trash) / `?purge=1` (forever) | Trash. |
| `POST /api/reconcile/code` `{host, sessions, cleanup_days}`, `POST /api/import?prune=1` | Deletion sync. |
| `GET /api/projects/{id}/bundle?transcripts=0\|1` | Project export. |
| `POST /api/tools/file-chat`, `GET /api/tools/brief?project=` | Used by the Open WebUI tool. |
| `POST /api/admin/redact` | One-time redaction of existing content. |

## Sign-in

Three ways in, and any one is enough:

- **Accounts:** on a fresh hub the first visitor creates the admin account, which requires the access token (`HUB_TOKEN`) so nobody else can claim the install. Admins add people under Settings → Accounts and access, as admin or member. Members use the board and library; settings, users, backups and maintenance are admin-only. Passwords are PBKDF2-hashed, sessions last 30 days in an HttpOnly, SameSite cookie, and an account locks for 5 minutes after 5 wrong passwords.
- **Access token:** `Authorization: Bearer <HUB_TOKEN>`, for hooks and tools. It acts as admin.
- **Single sign-on:** a trusted proxy sends `X-Authentik-Username` with the shared `HUB_PROXY_SECRET`. It acts as admin.

With no token and no accounts the hub is open, which is only meant for a local install.

## Project context

When a new Claude Code session starts, the hook asks `GET /api/context/<card id>` and hands Claude the project's description, goals, open tasks, the last three work summaries from other sessions, and the latest handoff, capped at about 6,000 characters. It's labelled as reference material, because it comes from past sessions. Resumed sessions don't get it. Turn it off per project in its Settings tab, or everywhere in Settings. Each use is logged on the card as "Context given".

## Costs and budgets

The Usage page estimates cost from the prices in Settings → Costs and budgets, one model per line in dollars per million tokens: `model = input, output, cache read, cache write`. A name also covers models that start with it, and the longest match wins. The defaults are Anthropic's API prices at the time of writing, so check them when prices change. Models without a price count as $0, and the page lists them.

These are estimates at API prices. On a Claude subscription they show what the same usage would cost on the API, not what you pay.

Set a monthly budget or daily limit to get a meter on the Usage page. The hub warns at 80% and 100% of the monthly budget, and once a day past the daily limit, through the notification webhook.

## Use the library from your tools (MCP)

The hub is an MCP server at `/mcp`, so Claude Code can use the library from inside a session. Settings → Connect tools has this command with your address filled in, plus the setup for Cursor and Codex:

```bash
claude mcp add --transport http --scope user vb-ai-hub https://your-hub/mcp --header "Authorization: Bearer $HUB_TOKEN"
```

| Tool | What it does |
|---|---|
| `list_projects` | Every project with its open tasks, overdue count, last handoff and next step |
| `get_project` | One project's description, goals, tasks, recent sessions, latest handoff and next steps |
| `search_library` | Keyword search across handoffs, documents, work logs, chats and tasks |
| `read_document` | A project's document, or its latest handoff by default |
| `list_tasks`, `add_task`, `update_task` | Read, add and change tasks |

No tool deletes anything. Library text comes from past sessions, so the server tells clients to treat it as information rather than instructions. Connectors on claude.ai and ChatGPT need OAuth, which the hub doesn't offer yet.

## Code layout

`server.py` starts the background loops and the HTTP server. Everything else is standard-library Python in `hub/` and plain files in `web/`, with no build step.

| Path | Holds |
|---|---|
| `hub/config.py`, `util.py` | Environment settings, constants, stateless helpers |
| `hub/db.py` | Schema, the shared connection, stored settings |
| `hub/redaction.py` | Secret removal |
| `hub/live.py`, `records.py` | Live events to browsers; reading cards |
| `hub/projects.py`, `archive.py`, `cards.py` | Projects and rules; blobs and attachments; card changes and trash |
| `hub/usage.py` | Token usage, costs and budgets |
| `hub/sources.py`, `owui.py`, `litellm.py` | Hooks, ingest, exports; Open WebUI sync; LiteLLM usage |
| `hub/notifications.py`, `generate.py`, `retrieval.py` | Notifications; handoffs, digests and jobs; Ask the library |
| `hub/backup.py`, `library.py`, `maintenance.py`, `accounts.py` | Backups; library and session context; the maintenance loop; sign-in |
| `hub/mcp.py` | The MCP server |
| `hub/routes.py`, `web.py` | The JSON API; the HTTP handler, authentication and dispatch |
| `web/` | The page, styles and scripts |
| `hook.py` | The Claude Code hook that runs on each workstation |

Modules import only from the ones above them. Scripts in `web/js/` are classic scripts that share globals, loaded in the order listed in `web/index.html`; `boot.js` is last and starts the app, so top-level code in other files should only define things and bind handlers. `web/` is served before sign-in, so keep anything private out of it.

All threads share one SQLite connection through `db.conn`. Always use `db.conn` rather than importing `conn`, which is set at startup. Each `execute()` finishes under `db_lock`; hold `db_lock` yourself only to group statements, and never across a network call.

## Tests

`python3 -m unittest tests.test_hub` starts a real server on a temporary database and drives it over HTTP. No dependencies, and it never touches a live hub.

## Notes

- **Transcripts can hold secrets.** They are complete records, so anything pasted into a chat is stored too. The hub is token-protected and should stay on your own network. Put it behind TLS, with `proxy_buffering off` for `/api/stream`.
- **Open WebUI scope:** the sync sees the chats of the account that owns the API key.

## License

MIT. See [LICENSE](LICENSE).
