# Local RAG (Ollama + FastAPI + React)

Chat with your own documents using local models. Models, embeddings, vector store and chat history all stay on your machine; no cloud LLM or API keys are needed.

Beyond plain document Q&A it includes the kind of enterprise-assistant features offered by products like Herbie.ai, built locally: **agent personas with tools**, **human handoff**, **multilingual + voice**, **folder/URL connectors**, **analytics**, **roles**, and an **embeddable chat widget**.

```
React (Vite) ──/api──▶ FastAPI ──▶ Ollama   (chat, tool calling, embeddings)  via LangChain
 widget.js  ──/api──▶    │──────▶ ChromaDB  (data/chroma, cosine)             via LangChain
                         │──────▶ SQLite    (data/app.db: docs, chats, agents, handoffs, analytics, users)
                         └──────▶ LlamaIndex readers (PDF / DOCX / Markdown) · faster-whisper (speech-to-text)
```

> **Multi-tenant ready:** turn on `AUTH_ENABLED=true` to get a login page, organizations (each with its own users, documents and chats), role-based permissions and per-customer website chat widgets. See *Accounts, organizations and access control* below.

## Quick start
```powershell
ollama pull llama3.1:8b
ollama pull nomic-embed-text

# terminal 1 - backend (Python 3.12+)
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload      # http://127.0.0.1:8000  (docs at /docs)

# terminal 2 - frontend (Node 20+)
cd frontend
npm.cmd install
npm.cmd run dev                                                   # http://localhost:5173
```
macOS/Linux: activate the venv with `source .venv/bin/activate` and use `uvicorn`/`npm` directly. On Windows, `npm.cmd` and calling the venv's `python.exe` avoid PowerShell's script-execution policy.

Docker alternative (Ollama still runs on the host): `docker compose up --build`, then open http://localhost:8080.

## Features
**Grounded RAG** - upload PDF/TXT/MD/DOCX/CSV, ask questions, get streamed answers with clickable citations and excerpts. If nothing relevant is retrieved the model is *not called*; the app says the documents don't contain the answer.

**Assistants (agent personas + tools)** - pick HR, IT helpdesk, Sales, Management or General per chat, or create your own in *Admin → Assistants* (instructions, allowed tools, knowledge scope = which documents it may search). Assistants call tools through Ollama function calling:

| Tool | What it does |
|---|---|
| `get_leave_balance`, `request_leave`, `list_my_leave_requests` | 20 days/year entitlement; validates dates, weekdays, balance |
| `create_ticket`, `get_ticket_status`, `list_my_tickets`, `request_password_reset` | IT tickets (a password-reset *request* is logged; no password is changed) |
| `schedule_followup`, `list_followups` | Sales follow-ups |
| `business_summary` | Live counts of tickets, leave, handoffs, documents, unanswered questions |
| `request_human_agent` | Escalate to a person (always available to assistants that have tools) |

**Actions need your confirmation.** Tools that change data (`request_leave`, `create_ticket`, `request_password_reset`, `schedule_followup`) never run straight from the model. The assistant validates the request (a dry run), shows an **action card** ("Create IT ticket (title: VPN down)") and nothing happens until the user presses *Confirm*; *Cancel* discards it, and cards expire after 30 minutes. This is the defence against **prompt injection**: even if a hostile sentence inside a document talks the model into calling a tool, the user sees the action and can decline. The embeddable widget shows the same card. Set `CONFIRM_ACTIONS=false` only for fully trusted setups. Read-only tools run immediately.

**Who may use what.** Each assistant has a minimum role (*Admin → Assistants*; Management insights is admin-only) and each tool can require a role (`business_summary` is admin-only), enforced on the server whichever assistant asks. Non-admins see only the assistants they may use and never see prompts, tool lists or document scopes. Note that document access is **not** per-user: an assistant's "knowledge scope" narrows what *that assistant* searches but is not a confidentiality boundary, because every signed-in user can chat with the General assistant, which searches everything.

> **Honest scope:** these tools read/write tables in this app's own SQLite database. They are *not* connected to a real HRMS, ITSM, CRM or Active Directory. To integrate one, replace the handler body in `backend/app/services/agent_tools.py`; the schema the model sees stays the same. Tool results are shown in the chat as expandable chips.

**Human handoff** - "Talk to a human" button (or the model decides to escalate). While a human owns a chat the model stays out of it. Agents/admins work from the **Console**: queue with wait times, claim, canned replies, "Rewrite with AI" in a chosen tone (professional/friendly/concise/empathetic), resolve to return the chat to the bot. Customers see replies within ~2.5 s (polling).

**Multilingual & voice** - *Settings → Answer language*: `auto` replies in the language of the question, or force one. Retrieval quality for non-English documents needs a multilingual embedding model (`ollama pull bge-m3`, then set it in Settings and re-upload). Voice input records in the browser and is transcribed **locally** by faster-whisper (first use downloads the `base` model, ~140 MB). Read-aloud uses your OS/browser voices. The built-in "no information" message is English.

**Connectors** - *Admin → Connectors*: sync a local folder (must be inside `CONNECTOR_ROOTS`, default `data/sources`) or a web page URL; optional refresh interval. New/changed files are re-indexed, deleted files are removed. URLs to localhost/private networks are blocked unless `ALLOW_PRIVATE_URLS=true` (SSRF protection).

**Analytics** - *Admin → Analytics*: questions/day, answer rate, response time, handoffs, 👍/👎 feedback, **unanswered questions (knowledge gaps)**, most-cited documents, per-assistant usage, tool usage. Question text is stored (truncated to 300 chars) in the local database.

### Accounts, organizations and access control

By default the app is a **single local user** with full rights: no login, one organization called "Default". To run it for several teams or customers set `AUTH_ENABLED=true` and restart.

**Sign-in** - email + password (a password is hashed with scrypt, never stored), 12-hour sessions (`SESSION_HOURS`), "sign out", change password (other devices are signed out). Wrong passwords are throttled per email and per IP, and a wrong email looks exactly like a wrong password. API keys (`rag_...`) remain available for scripts and integrations. On first start with auth on, a **platform admin** is created: set `ADMIN_EMAIL` / `ADMIN_PASSWORD`, or leave them empty and the generated credentials are written to `data/admin_credentials.txt` (and an API key to `data/admin_token.txt`). Existing single-user databases are migrated automatically: everything moves into the "Default" organization, old users and API keys keep working, and chats from single-user mode are handed to the platform admin so the first sign-in is not an empty app.

**Organizations (tenants)** - *Platform* page (platform admins only). Each organization has its **own** users, roles, documents and search index scope, chats, assistants, canned replies, connectors (private folder `CONNECTOR_ROOTS/<slug>`), embed keys, analytics, action records and tuning settings. Nothing is shared: other organizations' ids always answer `404`, search only ever sees the caller's own documents, and identical files in two organizations are stored separately. Only the Ollama server address and the default models are platform-wide; organizations may override the models and tuning but never the server. Disabling an organization signs everyone out at once and turns its widgets off. Platform admins can "open" an organization for support (the header shows which one).

**Roles & permissions (RBAC)** - every endpoint requires one permission; a role is a named set of permissions.

| Permission | Allows |
|---|---|
| `chat.use` | chat with assistants, give feedback, confirm actions |
| `documents.read` / `documents.write` | see the document list / upload and delete |
| `settings.read` / `settings.write` | view / change retrieval & model tuning |
| `agents.manage` | create and edit assistants |
| `connectors.manage` | folder and web-page connectors |
| `analytics.view` | analytics, and assistants/tools that expose business numbers |
| `handoffs.handle` | the human-agent console and canned replies (also lets you read customers' chats in your organization) |
| `users.manage` / `roles.manage` | users, passwords, API keys / custom roles |
| `embed.manage` | website embed keys |
| `platform.manage` | organizations and platform settings (super admin only) |

Built-in roles: **admin** (everything in the organization), **support agent** (chat, documents list, settings view, console), **member** (chat, documents list). Create **custom roles** in *Admin → Roles*. Safety rails: nobody can grant a permission they don't hold themselves, edit someone more powerful than themselves, change or delete their own account, or leave an organization without a user manager; changes to a role or a user take effect on the next request. An assistant can require a permission (for example *Management insights* needs `analytics.view`), and tools have their own requirements enforced on the server.

### Embedding the chat on your website

In *Admin → Website embed* create a key (name, **the websites allowed to use it**, assistant, title, welcome text, colour) and paste the one-line snippet it shows into your page:

```html
<script src="https://YOUR-SERVER/widget.js" data-key="pk_..." async></script>
```
Optional attributes: `data-container="#chat"` (render inline in that element instead of a floating bubble), `data-position="left"`, `data-open="true"`. Try it before publishing from `https://YOUR-SERVER/widget-demo`.

How it stays safe: the key is **publishable** (it is meant to be visible in your page source) and can *only chat*; it works only from the origins you list (`https://www.example.com`, `https://*.example.com`, or `*` for anywhere, which the UI warns about), answers with one fixed assistant, and cannot see documents, settings or users. Each visitor gets an anonymous id stored in their browser; visitors can't read each other's conversations; chat is rate-limited per visitor and per key. The widget lives in a **Shadow DOM** (your CSS can't break it, it can't break your site) and renders all text with `textContent`, never as HTML. Visitors can still use *Talk to a human* (your support agents answer in the Console) and confirm assistant actions. Set `PUBLIC_URL` when the server is reached through a different address than the one admins use, so the snippet is correct. Never put a user API key or a login token in a web page; use an embed key. Note that origin checks stop other *websites'* browsers from using your key; they cannot stop someone copying the key into a script of their own, which is why the rate limits exist.

## API
`POST /api/auth/login` · `POST /api/auth/logout` · `POST /api/auth/password` · `GET /api/auth/me` · `GET|POST /api/orgs`, `PUT /api/orgs/{id}` · `GET|PUT /api/platform/settings` · `GET /api/permissions` · `GET|POST /api/roles`, `PUT|DELETE /api/roles/{id}` · `PUT /api/users/{id}`, `POST /api/users/{id}/api-key` · `GET|POST /api/embed-keys`, `PUT|DELETE /api/embed-keys/{id}`, `GET /api/embed/config` · `GET /api/health` · `GET /api/models` · `GET|PUT /api/settings` · `POST /api/documents/upload` · `GET /api/documents[/{id}]` · `DELETE /api/documents/{id}` · `GET|POST /api/sessions` · `GET|DELETE /api/sessions/{id}/messages` · `DELETE /api/sessions/{id}` · `POST /api/chat` · `POST /api/chat/stream` · `POST /api/actions/{id}/confirm|reject` · `POST /api/messages/{id}/feedback` ·
`GET|POST /api/agents`, `PUT|DELETE /api/agents/{id}` · `POST|DELETE /api/sessions/{id}/handoff` · `GET /api/handoffs` · `POST /api/handoffs/{id}/claim|reply|resolve` · `POST /api/handoffs/paraphrase` · `GET|POST /api/canned`, `DELETE /api/canned/{id}` · `GET|POST /api/connectors`, `POST /api/connectors/{id}/sync`, `DELETE /api/connectors/{id}` · `GET /api/analytics` · `GET|POST /api/users`, `DELETE /api/users/{id}` · `GET /api/voice/status` · `POST /api/voice/transcribe`.

Credentials: `Authorization: Bearer <session token or API key>`, `X-API-Key`, or - for embedded widgets - `X-Embed-Key` + `X-Visitor-Id`. Platform admins may add `X-Org-Id` to act inside an organization. Interactive docs at `/docs` (switched off when auth is on). Errors are always `{"error": {"code", "message"}}`. Stream events (`text/event-stream`): `session`, `handoff`, `sources`, `token`, `tool`, `done`, `error`. Over-limit requests get `429 rate_limited` or `413 payload_too_large`.

## Configuration
Env vars (see [.env.example](.env.example)) give initial defaults; values saved in the Settings page (stored in `data/app.db`) win. Notes:
- Changing chunk size/overlap affects only newly indexed documents. Changing the **embedding model** makes existing documents unsearchable until re-uploaded (vectors from different models aren't comparable); the app warns when this happens.
- `SIMILARITY_THRESHOLD` (cosine, default 0.35) is the main knob for "answer vs. admit ignorance". Tuned for `nomic-embed-text`.
- With several organizations: `OLLAMA_URL` and the default models are *platform* settings; everything else is per organization (an organization that never saved its own settings inherits the platform defaults).
- Tool calling needs a model that supports it (`llama3.1:8b` does). Small models can pick the wrong tool or arguments; destructive-sounding actions here only write to the local database.

## Tests
```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest                 # 169 tests incl. a cross-tenant attack sweep and QA regressions; Ollama mocked, no network
.\.venv\Scripts\python.exe ..\qa\e2e_tenancy.py      # real server + real model: login, two tenants, embed key, CORS, streaming (from backend\)
.\.venv\Scripts\python.exe -m pytest -m integration  # real Ollama + models (+ Windows speech for the voice test); several minutes on CPU
.\.venv\Scripts\python.exe -m ruff check .

cd frontend
npm.cmd test ; npm.cmd run typecheck ; npm.cmd run build
```

## Security & privacy
- Documents, embeddings, chats, speech and analytics never leave the machine. Chroma telemetry is disabled. Browser read-aloud uses OS voices; voice *input* is transcribed by the local server, not the browser vendor's cloud.
- Uploads: extension allow-list, magic-number check, size cap, sanitized names, generated storage names, DOCX zip-bomb guard.
- Connectors: folders restricted to allowed roots (symlink escapes ignored); URL fetching blocks private addresses (re-checked on every redirect), caps size/redirects.
- Retrieved text is wrapped as untrusted context and the system prompt forbids following instructions inside it. A small local model can still be talked into things (we reproduced a document making an assistant call a tool), so the real protection is structural: state-changing tools only *propose* an action that the user must confirm, tool arguments are treated as untrusted and validated, and tools/assistants are role-checked server-side.
- The prompt is budgeted against the model's context window (lowest-ranked passages and oldest history are dropped, with a warning), and Settings rejects combinations of Top-k / chunk size / context that cannot fit.
- Throttling: wrong-token attempts (20/min per IP, then a 1-minute lockout), chat/upload/voice requests (60/min per user), handoff requests (5 open per user), and a 256 KB cap on JSON bodies. These are in-memory and per-process; put a reverse proxy in front for anything internet-facing.
- Tenant isolation is enforced in the data layer and re-checked in every route; an automated sweep calls every endpoint with another organization's ids as an admin, a member and an embedded visitor, and checks that nothing succeeds and nothing changes (deliberately breaking a check makes it fail). Folder connectors are confined to the organization's own directory.
- With auth on, chats are private to their owner (others get 404); only people with `handoffs.handle` can read other users' chats in their organization. Passwords use scrypt; API keys, session tokens and embed lookups are stored hashed; sessions are revoked on password change, disable and organization disable. The platform admin password lives in `data/admin_credentials.txt` if you didn't set `ADMIN_PASSWORD`: protect or delete that file.
- No TLS is built in. Don't expose ports 8000/8080/11434 to a network without a reverse proxy providing HTTPS. Browsers also block an `https://` website from calling an `http://` server (mixed content), so an embedded widget on a real site needs your server behind HTTPS.

## Limitations
- No OCR for scanned PDFs. One document is indexed at a time; a restart during indexing marks that document failed.
- Organizations are fully separated, but *within* an organization every user who can chat searches all of that organization's documents: an assistant's knowledge scope narrows what the assistant searches, it is not a confidentiality boundary between departments. If departments need that, give them separate organizations.
- Tenants share one Ollama server and one Chroma store (separated by document ids and verified by the isolation sweep); a very busy tenant can slow the others. There is no per-tenant quota, billing or self-service sign-up; the platform admin creates organizations. Deleting an organization is not offered (disable it instead). The URL connector checks the target address before connecting but does not pin the resolved IP, so a hostile DNS server could in theory rebind it (mitigated by the private-address check on every redirect; keep ALLOW_PRIVATE_URLS=false).
- Handoff uses polling, not websockets; no email/SMS/WhatsApp/Alexa channels (they would route data through third parties).
- Agent tools are local stand-ins (see above). Analytics is a local table, not a data warehouse.
- Answer quality depends on the local model; always check citations.

## Troubleshooting
| Symptom | Fix |
|---|---|
| "Ollama offline" / "Model missing" | Start Ollama; run the `ollama pull` command shown in the banner |
| `npm`/`Activate.ps1` blocked on Windows | Use `npm.cmd` and the venv's `python.exe` directly, or `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `DLL load failed ... chromadb_rust_bindings` | Install the Microsoft Visual C++ Redistributable (x64) |
| Everything answers "not enough information" | Lower the similarity threshold; check the document is *ready* and was indexed with the current embedding model |
| Mic button missing | `pip install faster-whisper "av>=14,<17"`, restart the backend; the browser needs microphone permission (localhost or HTTPS) |
| Assistant says it did something but no tool chip appears | The model answered without calling the tool; rephrase or use a larger model |
| Widget says "This website is not allowed to use this chat widget" | Add the page's exact origin (scheme + host + port, e.g. `https://www.example.com`) to the key's allowed websites in *Admin → Website embed* |
| Widget says "The chat is unavailable" | The key was disabled/deleted, its organization is disabled, or the server is unreachable / not on HTTPS while your site is |
| "Incorrect email or password" for the right password | Emails are case-insensitive but must match the account; after 8 wrong tries the account waits 15 minutes. Ask an admin to reset the password in *Admin → Users* |
| "Too many failed sign-in attempts" | Your IP hit the limit (20 wrong attempts per minute). Wait a minute |
| Lost the platform admin password | On the login page choose "Sign in with an API key instead" and use the key in `data/admin_token.txt`, then set a new password for that account in *Admin → Users → Password…* |
| The snippet shows the wrong address | Set `PUBLIC_URL=https://your-server` so snippets use the address visitors reach |


