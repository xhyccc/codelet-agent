# cowork

Enterprise-grade multi-agent collaborative office layer built on top of **codelet-agent**.  
All modules are **stdlib-only** (Python 3.10+) — no new external dependencies.

---

## Quickstart — launching the full system

```bash
# 1. Install (editable)
pip install -e .

# 2. Start the web UI (opens browser automatically)
python -m cowork serve

# 3. Optional flags
python -m cowork serve --port 9000          # custom port
python -m cowork serve --host 0.0.0.0       # listen on all interfaces
python -m cowork serve --no-browser         # skip auto-open

# 4. CLI smoke-test (no browser needed)
python -m cowork demo                       # prints JSON summary of all subsystems
python -m cowork demo --workers 4 --tasks 8 --json
```

The server starts at **http://127.0.0.1:8765** by default and serves a
dark-theme 7-tab single-page application — no build step, no npm, no external JS.

### Tab overview

| Tab | What it does |
|-----|-------------|
| **Chat** | Conversational interface backed by the in-process codelet engine |
| **Tasks** | OODA orchestration dashboard — generate plans, run swarms, monitor kanban status |
| **Artifacts** | Versioned artifact store with sandboxed preview + rollback slider |
| **Connectors** | OAuth-style connector marketplace with health indicators and scope badges |
| **Projects** | Multiplayer project hubs — visibility controls, member roles, chat snapshots |
| **Admin** | SSO / SAML config, RBAC capability matrix, SCIM group sync, telemetry, CSV export |
| **Guardrails** | Payload sensitivity warnings, diff-review approval gates |

---

## Architecture overview

```
cowork/
├── models.py           F1  dataclass domain model (Tenant, User, Workspace, Session, …)
├── store.py            F1  SQLite multi-tenant store (WAL, FK-ON, RLS helpers)
├── events.py           F2  in-process pub/sub event bus + channel helpers
├── engine.py           F3  codelet subprocess bridge (sync + streaming)
├── parser.py           F3  <tool> / <final> stream parser matching codelet wire format
├── collab.py           F4  LWW-CRDT primitives + advisory file-lock manager
├── artifacts.py        F5  artifact stream parser + HTML/SVG sanitizer + persistence
├── orchestrator.py     F6  hierarchical / sequential-DAG / swarm orchestrators
├── memory.py           F7  BM25 + hashed-TF hybrid retrieval memory store
├── security.py         F8  RBAC policy matrix + audit helper
├── office/             F9  pluggable office connectors + registry
│   ├── registry.py         ConnectorRegistry (register / invoke / as_tool_list)
│   ├── microsoft_graph.py  MicrosoftGraphConnector — email, calendar, files, Teams
│   ├── zoom.py             ZoomConnector — OAuth token cache + create_meeting
│   ├── wecom.py            WeComConnector — send_message + signature verification
│   ├── libreoffice.py      LibreOfficeConnector — headless document conversion
│   └── docling.py          DoclingConnector — document ingestion seam
├── cli.py              F10 `demo` + `serve` subcommands
├── web.py              F11 stdlib HTTP server + 7-tab enterprise SPA (P1–P7)
├── artifacts_store.py  F12 versioned artifact store (SQLite, rollback)
├── scheduler.py        F13 natural-language → cron scheduler + TaskScheduler
├── projects.py         F14 multiplayer project store (visibility, members, snapshots)
├── admin.py            F15 SSO / RBAC / SCIM / telemetry / CSV export
├── guardrails.py       F16 payload warnings + diff-review approval engine
└── tests/                  216 tests (all pass with LibreOffice installed)
```

---

## Feature reference

### F1 — Multi-tenant foundation (`models.py`, `store.py`)

Domain dataclasses: `Tenant`, `User`, `Workspace`, `WorkspaceMember`, `Session`,
`AgentInstance`, `Artifact`, `AuditLog`.

```python
from cowork.store import Store
from cowork.models import Tenant, User, Workspace

store = Store(db_path=":memory:")          # or a real path for persistence
tenant = store.create_tenant(Tenant(name="Acme"))
user   = store.create_user(User(tenant_id=tenant.id, email="alice@acme.com"))
ws     = store.create_workspace(Workspace(tenant_id=tenant.id, name="Eng"))
logs   = store.list_audit(tenant.id, limit=50)
```

Cross-tenant queries always return `None` / empty — isolation is enforced at the
store layer, not in application logic.

---

### F2 — Event bus (`events.py`)

In-process pub/sub with per-channel history ring-buffer.

```python
from cowork.events import Event, EventBus, workspace_channel, session_channel

bus = EventBus(history_per_channel=100)
chan = workspace_channel("ws_abc")

sub = bus.subscribe(chan)          # QueueSubscription
bus.publish(Event(kind="artifact.ready", channel=chan, payload={"id": "..."}))
event = sub.get(timeout=1.0)
```

Channel helpers: `workspace_channel(id)`, `session_channel(id)`,
`tenant_budget_channel(id)`.

---

### F3 — Codelet engine + parser (`engine.py`, `parser.py`)

Thin subprocess bridge to `python -m codelet`.

```python
from cowork.engine import CodeletEngine, CodeletInvocation

engine = CodeletEngine()
result = engine.run(CodeletInvocation(prompt="list files", timeout=30))
print(result.final)          # extracted <final>…</final> text
print(result.tool_calls)     # list of parsed <tool name="…">{…}</tool> dicts
```

Streaming variant:

```python
for chunk in engine.stream(CodeletInvocation(prompt="…")):
    print(chunk, end="", flush=True)
```

Parser standalone:

```python
from cowork.parser import parse_codelet_output, extract_final

calls = parse_codelet_output(raw_output)   # list of {name, args}
final = extract_final(raw_output)
```

---

### F4 — CRDT collaboration + file locks (`collab.py`)

**LWW (Last-Write-Wins) CRDT primitives** for conflict-free distributed state:

```python
from cowork.collab import LWWMap, LWWText

m = LWWMap(replica_id="node-1")
m.set("status", "in-progress")
m.merge(remote_snapshot)          # idempotent, commutative

t = LWWText(replica_id="node-1")
t.set("Hello, world!")
```

**Advisory file locks** (in-memory, TTL-based):

```python
from cowork.collab import FileLockManager

mgr = FileLockManager(default_ttl=30.0)
token = mgr.acquire("ws_abc", "task:auth.py", "agent-1")
mgr.refresh(token)
mgr.release(token)
held = mgr.is_held("ws_abc", "task:auth.py")
```

---

### F5 — Artifact parser + sanitizer (`artifacts.py`)

Parse `<artifact …>…</artifact>` blocks from LLM output, sanitize HTML/SVG,
and persist to disk.

```python
from cowork.artifacts import ArtifactEngine

engine = ArtifactEngine(workspace_root="/tmp/ws")
artifacts = engine.ingest(llm_raw_output)
# each artifact is an Artifact dataclass with .kind, .path, .body
```

Sanitizer strips `<script>`, `<iframe>`, `on*` event handlers,
`javascript:` / `vbscript:` / `data:` URIs — idempotent, safe to call twice.

Allowed kinds: `html`, `react`, `markdown`, `code`, `json`, `svg`.

---

### F6 — Multi-agent orchestrators (`orchestrator.py`)

Three topology modes:

**Hierarchical** — lead agent fans out to workers, collects results:

```python
from cowork.orchestrator import HierarchicalOrchestrator, Task

orch = HierarchicalOrchestrator(runner=my_runner, lead_id="lead")
result = orch.run(
    lead_prompt="Summarise findings",
    subtasks=[Task(id="t1", prompt="Analyse Q1"), Task(id="t2", prompt="Analyse Q2")],
)
```

**Sequential DAG** — topological sort, upstream outputs forwarded:

```python
from cowork.orchestrator import SequentialOrchestrator

orch = SequentialOrchestrator(runner=my_runner)
results = orch.run([
    Task(id="fetch",  prompt="Fetch data"),
    Task(id="clean",  prompt="Clean data",   depends_on=["fetch"]),
    Task(id="report", prompt="Write report", depends_on=["clean"]),
])
```

**Swarm / Kanban** — peers compete for tasks via file locks:

```python
from cowork.orchestrator import SwarmOrchestrator

sw = SwarmOrchestrator(workspace_id="ws_abc")
sw.add(Task(id="t0", prompt="…"))
sw.add(Task(id="t1", prompt="…"))
sw.run(runner=my_runner, workers=["agent-0", "agent-1"])
print(sw.stats())   # {"pending": 0, "claimed": 0, "done": 2, "failed": 0}
```

---

### F7 — Hybrid retrieval memory (`memory.py`)

BM25 + hashed-TF cosine hybrid retrieval — no external vector DB required.

```python
from cowork.memory import MemoryStore

mem = MemoryStore(dim=256, alpha=0.5)   # alpha: BM25 vs vector blend
mem.add("Quarterly revenue up 12% YoY.", item_id="d1")
mem.add_document(long_text, max_chars=512, overlap=64)   # auto-chunk

hits = mem.search("revenue growth", k=5)
for h in hits:
    print(h.score, h.item.text)
```

---

### F8 — RBAC + audit (`security.py`)

Role constants: `ROLE_OWNER`, `ROLE_ADMIN`, `ROLE_EDITOR`, `ROLE_VIEWER`.  
Action constants: `ACTION_READ/WRITE/DELETE/INVITE/MANAGE_BILLING/MANAGE_MEMBERS/EXECUTE_TOOL/EXPORT`.

```python
from cowork.security import Actor, require, audit, DEFAULT_POLICY, ACTION_WRITE

actor = Actor(user_id="usr_1", tenant_id="ten_1", role="owner")
require(actor, ACTION_WRITE, resource=workspace)   # raises PermissionDenied on fail

audit(store, actor, "artifact.create", target=artifact.id,
      metadata={"kind": "html"})                   # no-op if store is None
```

Default permission matrix:

| Role    | Read | Write | Delete | Invite | Billing | Members | Execute | Export |
|---------|------|-------|--------|--------|---------|---------|---------|--------|
| owner   | ✓    | ✓     | ✓      | ✓      | ✓       | ✓       | ✓       | ✓      |
| admin   | ✓    | ✓     | ✓      | ✓      | —       | ✓       | ✓       | ✓      |
| editor  | ✓    | ✓     | —      | —      | —       | —       | ✓       | ✓      |
| viewer  | ✓    | —     | —      | —      | —       | —       | —       | —      |

---

### F9 — Office connectors (`office/`)

All connectors share the `ConnectorRegistry` interface and use injectable `_fetch_token`
/ `_convert` seams for testing without live credentials.

```python
from cowork.office import ConnectorRegistry, MicrosoftGraphConnector, ZoomConnector

reg = ConnectorRegistry()
reg.register(MicrosoftGraphConnector())
reg.register(ZoomConnector(account_id="…", client_id="…", client_secret="…"))

result = reg.invoke("ms_graph", {"query": "list my emails"})
result = reg.invoke("zoom", {"action": "create_meeting",
                             "topic": "Sprint review",
                             "start_time": "2026-06-01T10:00:00Z"})
```

| Connector           | Class                    | Key capabilities                              |
|---------------------|--------------------------|-----------------------------------------------|
| `ms_graph`          | `MicrosoftGraphConnector`| Email, calendar, OneDrive, Teams, contacts    |
| `zoom`              | `ZoomConnector`          | OAuth token cache, create meeting             |
| `wecom`             | `WeComConnector`         | Send message, signature verification          |
| `libreoffice`       | `LibreOfficeConnector`   | Headless `soffice` document conversion        |
| `docling`           | `DoclingConnector`       | Document ingestion (injectable `_convert`)    |

---

### F10 — Demo CLI (`cli.py`, `__main__.py`)

```
python -m cowork demo [--workers N] [--tasks N] [--json]
```

Wires every subsystem together in memory: creates a tenant + workspace + session,
runs a swarm, ingests memory documents, queries them, and prints a summary.

```
cowork demo
===========
  tenant: ten_d5cf68eeb88a
  workspace: ws_45d214af9035
  session: ses_27f8474f5ec8
  kanban: {'pending': 0, 'claimed': 0, 'done': 4, 'failed': 0}
  connectors: ['docling', 'libreoffice', 'ms_graph', 'wecom', 'zoom']
  top_memory_hit: d2
  audit_count: 1
```

---

### F11 — Web dashboard (`web.py`) — enterprise 7-tab SPA (P1–P7)

```bash
python -m cowork serve [--host 127.0.0.1] [--port 8765] [--no-browser]
```

Stdlib-only HTTP server (`http.server`) serving a dark-theme single-page application.
No npm, no build step — the entire UI is embedded in `web.py` as a raw string.

**REST API — full reference:**

| Method   | Path                                        | Description                                             |
|----------|---------------------------------------------|---------------------------------------------------------|
| `GET`    | `/`                                         | HTML SPA shell (7-tab dashboard)                        |
| `GET`    | `/api/status`                               | Tenant / workspace / kanban / scheduler / audit count   |
| `GET`    | `/api/connectors`                           | Registered connector names                              |
| `GET`    | `/api/connectors/details`                   | Connector health + OAuth scopes                         |
| `GET`    | `/api/audit`                                | Recent audit log entries (last 50)                      |
| `GET`    | `/api/scheduler`                            | List all scheduled tasks                                |
| `GET`    | `/api/artifacts`                            | List all artifacts                                      |
| `GET`    | `/api/artifacts/{id}`                       | Get artifact by ID                                      |
| `GET`    | `/api/artifacts/{id}/versions`              | List all versions of an artifact                        |
| `GET`    | `/api/artifacts/{id}/versions/{v}`          | Get a specific artifact version (rollback)              |
| `GET`    | `/api/projects`                             | List all projects                                       |
| `GET`    | `/api/projects/{id}/snapshots`              | List chat snapshots for a project                       |
| `GET`    | `/api/admin/sso`                            | Current SSO / SAML config                               |
| `GET`    | `/api/admin/roles`                          | All RBAC roles + capability matrix                      |
| `GET`    | `/api/admin/groups`                         | SCIM-synced groups                                      |
| `GET`    | `/api/admin/telemetry`                      | Telemetry event log                                     |
| `GET`    | `/api/admin/audit/export?days=180`          | Download audit log as CSV                               |
| `GET`    | `/api/guardrails/warnings`                  | Active payload sensitivity warnings                     |
| `GET`    | `/api/guardrails/diffs`                     | Pending diff-review requests                            |
| `POST`   | `/api/chat`                                 | `{"message": "…"}` → chat response                     |
| `POST`   | `/api/memory/search`                        | `{"query": "…", "k": 5}` → ranked hits                 |
| `POST`   | `/api/tasks/run`                            | `{"tasks": 4, "workers": 2}` → kanban stats             |
| `POST`   | `/api/tasks/plan`                           | `{"prompt": "…"}` → OODA plan checklist                 |
| `POST`   | `/api/scheduler`                            | `{"name", "prompt", "cron_nl"}` → create scheduled task |
| `POST`   | `/api/artifacts`                            | `{"title", "kind", "body"}` → create artifact + v1      |
| `POST`   | `/api/projects`                             | `{"name", "visibility"}` → create project               |
| `POST`   | `/api/projects/{id}/members`               | `{"user_id", "role"}` → add member                      |
| `POST`   | `/api/projects/{id}/snapshots`             | `{"title", "content"}` → create chat snapshot           |
| `POST`   | `/api/admin/sso`                            | Save SSO config                                         |
| `POST`   | `/api/admin/roles`                          | Upsert RBAC role                                        |
| `POST`   | `/api/admin/groups`                         | Sync SCIM group                                         |
| `POST`   | `/api/guardrails/check`                     | Check payload sensitivity                               |
| `POST`   | `/api/guardrails/diffs/{id}/resolve`        | Approve / reject a diff review                          |
| `PATCH`  | `/api/scheduler/{id}`                       | Toggle scheduled task enabled/disabled                  |
| `PATCH`  | `/api/projects/{id}`                        | Update project visibility or instructions               |
| `DELETE` | `/api/scheduler/{id}`                       | Remove a scheduled task                                 |
| `DELETE` | `/api/snapshots/{id}`                       | Revoke / unshare a chat snapshot                        |
| `DELETE` | `/api/guardrails/warnings/{id}`             | Dismiss a payload warning                               |

Auto-refreshes status + scheduler every 10 s.

---

### F12 — Versioned artifact store (`artifacts_store.py`)

SQLite-backed store that saves every iteration of a generated artifact and supports
instant rollback via the version slider in the Artifacts tab.

```python
from cowork.artifacts_store import ArtifactVersionStore, ArtifactVersion

store = ArtifactVersionStore(":memory:")
av = ArtifactVersion(artifact_id="art_1", version=1, body="<h1>v1</h1>", attrs={"kind": "html"})
store.save(av)
latest = store.get_latest("art_1")
v1     = store.get_version("art_1", 1)
all_v  = store.list_versions("art_1")
```

---

### F13 — Natural-language scheduler (`scheduler.py`)

Converts plain-English recurrence descriptions to cron expressions and manages an
in-memory task schedule displayed in the Tasks tab.

```python
from cowork.scheduler import nl_to_cron, TaskScheduler, ScheduledTask

print(nl_to_cron("every day at 9am"))   # "0 9 * * *"
print(nl_to_cron("every monday"))        # "0 9 * * 1"

sched = TaskScheduler()
task  = ScheduledTask(id="t1", name="Daily report", prompt="summarise today", cron="0 9 * * *")
sched.add(task)
sched.toggle("t1")    # enable / disable
sched.mark_run("t1")  # record last-run timestamp
```

---

### F14 — Multiplayer project store (`projects.py`)

Persistent project workspaces with visibility controls, member roles, and shareable
chat snapshots — backing the Projects tab.

```python
from cowork.projects import ProjectStore, Project, VISIBILITY_ORG, ROLE_EDITOR

ps = ProjectStore(":memory:")
proj = ps.create(Project(name="Q3 Analysis", visibility=VISIBILITY_ORG))
ps.add_member(proj.id, "usr_alice", ROLE_EDITOR)
snap = ps.create_snapshot(proj.id, title="Initial findings", content="…")
ps.revoke_snapshot(snap.id)
```

Visibility constants: `VISIBILITY_PRIVATE`, `VISIBILITY_INVITED`, `VISIBILITY_ORG`.  
Role constants: `ROLE_VIEWER`, `ROLE_EDITOR`.

---

### F15 — Admin governance (`admin.py`)

SSO / SAML config, RBAC capability matrix, SCIM group sync, telemetry event log, and
CSV audit export — all surfaced in the Admin tab.

```python
from cowork.admin import AdminStore, SSOConfig, RBACRole, ALL_CAPABILITIES

adm = AdminStore(":memory:")
adm.save_sso(SSOConfig(provider="okta", metadata_url="https://…", require_sso=True))
adm.upsert_role(RBACRole(name="analyst", capabilities=["read", "export"]))
adm.sync_group({"name": "data-team", "members": ["alice", "bob"]})

csv_bytes = adm.export_audit_csv(days=90)
```

Built-in roles: `admin`, `editor`, `viewer`, `auditor`.  
All 11 capability constants are in `ALL_CAPABILITIES`.

---

### F16 — Guardrails (`guardrails.py`)

Payload sensitivity assessment, warning dismissal, and diff-review approval gates —
surfaced in the Guardrails tab.

```python
from cowork.guardrails import GuardrailEngine

ge = GuardrailEngine()

# Check before ingesting a file
warning = ge.check_payload(
    resource_path="/data/contracts/acme.pdf",
    size_bytes=12_000_000,
    workspace_visibility="org",
    content_sample="CONFIDENTIAL — Master Service Agreement …",
)
if warning:
    ge.dismiss_warning(warning.id)          # user acknowledged

# Request a diff review before applying a patch
review = ge.request_diff_review("patch-xyz", "- old line\n+ new line")
ge.resolve_diff(review.id, approved=True, feedback="LGTM")
```

`GuardrailEngine.MAX_INGEST_BYTES = 52_428_800` (50 MB hard limit).

---

## Running tests

```bash
# Full cowork suite (LibreOffice must be installed for 216/216)
PYTHONPATH=. python -m pytest cowork/tests/ -q
# 216 passed  ← with LibreOffice installed (brew install --cask libreoffice)
# 215 passed, 1 skipped  ← without LibreOffice

# Individual suites
PYTHONPATH=. python -m pytest cowork/tests/test_store.py            # F1
PYTHONPATH=. python -m pytest cowork/tests/test_events.py           # F2
PYTHONPATH=. python -m pytest cowork/tests/test_engine.py           # F3
PYTHONPATH=. python -m pytest cowork/tests/test_collab.py           # F4
PYTHONPATH=. python -m pytest cowork/tests/test_artifacts.py        # F5
PYTHONPATH=. python -m pytest cowork/tests/test_orchestrator.py     # F6
PYTHONPATH=. python -m pytest cowork/tests/test_memory.py           # F7
PYTHONPATH=. python -m pytest cowork/tests/test_security.py         # F8
PYTHONPATH=. python -m pytest cowork/tests/test_office.py           # F9  (1 real LibreOffice test)
PYTHONPATH=. python -m pytest cowork/tests/test_cli.py              # F10
PYTHONPATH=. python -m pytest cowork/tests/test_web.py              # F11
PYTHONPATH=. python -m pytest cowork/tests/test_artifacts_store.py  # F12
PYTHONPATH=. python -m pytest cowork/tests/test_scheduler.py        # F13
PYTHONPATH=. python -m pytest cowork/tests/test_projects.py         # F14
PYTHONPATH=. python -m pytest cowork/tests/test_admin.py            # F15
PYTHONPATH=. python -m pytest cowork/tests/test_guardrails.py       # F16
```

---

## Commit history

| SHA       | Feature |
|-----------|---------|
| `51efcac` | F1  scaffold + dataclass models + SQLite multi-tenant store |
| `89d75bc` | F2  in-process pub/sub event bus with channels |
| `3a6b498` | F3  codelet subprocess engine + streaming output parser |
| `996a0fd` | F4  LWW CRDT primitives + advisory file lock manager |
| `0244066` | F5  artifact stream parser + HTML sanitizer + persistence |
| `1f4859e` | F6  multi-agent orchestrator (hierarchical, sequential DAG, swarm) |
| `eeb459d` | F7  BM25 + hashed-TF hybrid retrieval memory |
| `403052d` | F8  RBAC policy + tenant isolation + audit helper |
| `d89aa7e` | F9  office connectors (MS Graph, Zoom, WeCom, LibreOffice, Docling) + registry |
| `5a25e2b` | F10 demo CLI (`python -m cowork demo`) |
| `fbc7ca3` | F11 web UI (`python -m cowork serve`) |
| `d812ffb` | F12–F16 + P1–P7 enterprise UI: versioned artifacts · scheduler · projects · admin · guardrails · 7-tab SPA |
