"""Lightweight stdlib-only web UI for the cowork demo.

Run::

    python -m cowork serve [--port 8765] [--no-browser]

API endpoints::

    GET  /                   HTML shell
    GET  /api/status         overview JSON
    GET  /api/connectors     connector names
    GET  /api/audit          recent audit entries
    POST /api/memory/search  {"query": "…", "k": 5}
    POST /api/tasks/run      {"tasks": 4, "workers": 2}
"""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from .collab import FileLockManager
from .events import Event, EventBus, workspace_channel
from .memory import MemoryStore
from .models import ROLE_OWNER, Session, Tenant, User, Workspace
from .office import (
    ConnectorRegistry,
    DoclingConnector,
    LibreOfficeConnector,
    MicrosoftGraphConnector,
    WeComConnector,
    ZoomConnector,
)
from .orchestrator import SwarmOrchestrator, Task
from .security import ACTION_WRITE, Actor, audit, require
from .store import Store

# ---------------------------------------------------------------------------
# Embedded single-page UI
# ---------------------------------------------------------------------------

_UI_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>cowork — Enterprise Agent Dashboard</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}
a{color:inherit;text-decoration:none}
header{background:#13151f;border-bottom:1px solid #1e2235;padding:0 2rem;display:flex;align-items:center;height:56px;gap:.75rem;position:sticky;top:0;z-index:10}
header h1{font-size:1.1rem;font-weight:800;letter-spacing:-.03em;background:linear-gradient(90deg,#7c3aed,#3b82f6);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.dot{width:8px;height:8px;border-radius:50%;background:#10b981;transition:background .4s}
.dot.offline{background:#ef4444}
#status-label{font-size:.72rem;color:#6b7280;margin-left:-.25rem}
main{padding:2rem;max-width:1280px;margin:0 auto;display:flex;flex-direction:column;gap:2rem}
h2{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#4b5563;margin-bottom:.9rem}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:.875rem}
.card{background:#13151f;border:1px solid #1e2235;border-radius:12px;padding:1.1rem 1.25rem}
.card .lbl{font-size:.65rem;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:#6b7280;margin-bottom:.35rem}
.card .val{font-size:1.65rem;font-weight:800;color:#e2e8f0;line-height:1}
.card .sub{font-size:.68rem;color:#4b5563;margin-top:.3rem;font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card.violet{border-color:#7c3aed30;background:linear-gradient(140deg,#13151f,#1a1030)}
.c-green{color:#10b981}.c-blue{color:#3b82f6}.c-amber{color:#f59e0b}.c-red{color:#ef4444}.c-violet{color:#a78bfa}
.pills{display:flex;flex-wrap:wrap;gap:.6rem}
.pill{background:#13151f;border:1px solid #1e2235;border-radius:8px;padding:.45rem .9rem;display:flex;align-items:center;gap:.45rem;font-size:.82rem;color:#d1d5db}
.pill .pdot{width:6px;height:6px;border-radius:50%;background:#10b981}
.search-row{display:flex;gap:.6rem;margin-bottom:.9rem}
.search-row input{flex:1;background:#13151f;border:1px solid #1e2235;border-radius:8px;padding:.55rem 1rem;color:#e2e8f0;font-size:.88rem;outline:none;transition:border-color .2s}
.search-row input:focus{border-color:#7c3aed}
btn,button{display:inline-flex;align-items:center;justify-content:center;border:none;border-radius:8px;padding:.55rem 1.1rem;font-size:.88rem;font-weight:600;cursor:pointer;transition:opacity .15s}
.btn-violet{background:#7c3aed;color:#fff}.btn-violet:hover{opacity:.85}
.btn-green{background:#059669;color:#fff}.btn-green:hover{opacity:.85}
.hits{display:flex;flex-direction:column;gap:.45rem}
.hit{background:#13151f;border:1px solid #1e2235;border-radius:8px;padding:.65rem 1rem;font-size:.83rem;display:flex;justify-content:space-between;align-items:center;gap:1rem}
.hit .txt{color:#e2e8f0;flex:1}
.hit .score{color:#a78bfa;font-family:monospace;font-size:.72rem;white-space:nowrap}
.run-row{display:flex;align-items:center;gap:.75rem;flex-wrap:wrap}
.run-row label{font-size:.78rem;color:#6b7280}
.run-row input[type=number]{width:68px;background:#13151f;border:1px solid #1e2235;border-radius:8px;padding:.5rem .7rem;color:#e2e8f0;font-size:.88rem;outline:none}
#run-msg{font-size:.8rem;font-family:monospace;color:#10b981}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{text-align:left;padding:.5rem .75rem;color:#4b5563;font-size:.65rem;text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid #1e2235}
td{padding:.55rem .75rem;border-bottom:1px solid #13151f;color:#c4c9d4}
tr:hover td{background:#13151f80}
.tag-ok{color:#10b981}.tag-err{color:#ef4444}
</style>
</head>
<body>
<header>
  <div class="dot" id="dot"></div>
  <h1>cowork</h1>
  <span id="status-label" style="font-size:.72rem;color:#6b7280">connecting…</span>
</header>
<main>

<section>
  <h2>Overview</h2>
  <div class="cards">
    <div class="card violet"><div class="lbl">Tenant</div><div class="val c-violet" id="ov-tenant">—</div><div class="sub" id="ov-tenant-id"></div></div>
    <div class="card"><div class="lbl">Workspace</div><div class="val" id="ov-ws">—</div><div class="sub" id="ov-ws-id"></div></div>
    <div class="card"><div class="lbl">Session</div><div class="val" id="ov-ses">—</div><div class="sub" id="ov-ses-id"></div></div>
    <div class="card"><div class="lbl">Audit Events</div><div class="val c-blue" id="ov-audit">—</div></div>
  </div>
</section>

<section>
  <h2>Kanban</h2>
  <div class="cards">
    <div class="card"><div class="lbl">Pending</div><div class="val c-amber" id="kb-pending">—</div></div>
    <div class="card"><div class="lbl">Claimed</div><div class="val c-blue" id="kb-claimed">—</div></div>
    <div class="card"><div class="lbl">Done</div><div class="val c-green" id="kb-done">—</div></div>
    <div class="card"><div class="lbl">Failed</div><div class="val c-red" id="kb-failed">—</div></div>
  </div>
</section>

<section>
  <h2>Run Tasks</h2>
  <div class="run-row">
    <label>Tasks</label>
    <input type="number" id="run-n" value="4" min="1" max="20">
    <label>Workers</label>
    <input type="number" id="run-w" value="2" min="1" max="8">
    <button class="btn-green" onclick="runTasks()">&#9654; Run</button>
    <span id="run-msg"></span>
  </div>
</section>

<section>
  <h2>Office Connectors</h2>
  <div class="pills" id="connectors"></div>
</section>

<section>
  <h2>Memory Search</h2>
  <div class="search-row">
    <input type="text" id="q" placeholder="e.g. product launch, budget, hiring…" onkeydown="if(event.key==='Enter')search()">
    <button class="btn-violet" onclick="search()">Search</button>
  </div>
  <div class="hits" id="hits"></div>
</section>

<section>
  <h2>Audit Log</h2>
  <table>
    <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Target</th><th>Status</th></tr></thead>
    <tbody id="audit-body"></tbody>
  </table>
</section>

</main>
<script>
function esc(s){const d=document.createElement('div');d.textContent=String(s);return d.innerHTML}

async function fetchStatus(){
  try{
    const d=await(await fetch('/api/status')).json();
    document.getElementById('dot').className='dot';
    document.getElementById('status-label').textContent='live';
    document.getElementById('ov-tenant').textContent=d.tenant_name||'—';
    document.getElementById('ov-tenant-id').textContent=d.tenant_id;
    document.getElementById('ov-ws').textContent='Demo WS';
    document.getElementById('ov-ws-id').textContent=d.workspace_id;
    document.getElementById('ov-ses').textContent=d.session_title||'demo';
    document.getElementById('ov-ses-id').textContent=d.session_id;
    document.getElementById('ov-audit').textContent=d.audit_count;
    document.getElementById('kb-pending').textContent=d.kanban.pending;
    document.getElementById('kb-claimed').textContent=d.kanban.claimed;
    document.getElementById('kb-done').textContent=d.kanban.done;
    document.getElementById('kb-failed').textContent=d.kanban.failed;
  }catch{
    document.getElementById('dot').className='dot offline';
    document.getElementById('status-label').textContent='offline';
  }
}

async function fetchConnectors(){
  const d=await(await fetch('/api/connectors')).json();
  document.getElementById('connectors').innerHTML=
    d.connectors.map(c=>`<div class="pill"><span class="pdot"></span>${esc(c)}</div>`).join('');
}

async function fetchAudit(){
  const d=await(await fetch('/api/audit')).json();
  document.getElementById('audit-body').innerHTML=d.entries.map(e=>{
    const t=new Date(e.at*1000).toLocaleTimeString();
    let st='—';try{st=(JSON.parse(e.metadata||'{}')).status||'—';}catch{}
    const cls=st==='ok'?'tag-ok':'tag-err';
    return`<tr><td>${esc(t)}</td><td style="font-family:monospace;font-size:.7rem">${esc((e.actor_id||'').slice(0,14))}</td><td>${esc(e.action)}</td><td style="font-family:monospace;font-size:.7rem">${esc(e.target||'—')}</td><td class="${cls}">${esc(st)}</td></tr>`;
  }).join('');
}

async function search(){
  const q=document.getElementById('q').value.trim();
  if(!q)return;
  const d=await(await fetch('/api/memory/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q,k:5})})).json();
  const el=document.getElementById('hits');
  if(!d.hits.length){el.innerHTML='<div class="hit"><span class="txt" style="color:#4b5563">No results.</span></div>';return;}
  el.innerHTML=d.hits.map(h=>`<div class="hit"><span class="txt">${esc(h.text)}</span><span class="score">score ${h.score.toFixed(3)}</span></div>`).join('');
}

async function runTasks(){
  const n=parseInt(document.getElementById('run-n').value);
  const w=parseInt(document.getElementById('run-w').value);
  document.getElementById('run-msg').textContent='running…';
  const d=await(await fetch('/api/tasks/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tasks:n,workers:w})})).json();
  document.getElementById('run-msg').textContent=`✓ done=${d.done}  failed=${d.failed}`;
  fetchStatus();fetchAudit();
}

async function refresh(){await Promise.all([fetchStatus(),fetchConnectors(),fetchAudit()]);}
refresh();
setInterval(fetchStatus,5000);
setInterval(fetchAudit,10000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

class CoworkApp:
    """In-memory cowork application state shared across HTTP requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.store = Store(db_path=":memory:")
        self.tenant = self.store.create_tenant(Tenant(name="Acme"))
        self.user = self.store.create_user(
            User(tenant_id=self.tenant.id, email="owner@acme.test")
        )
        self.workspace = self.store.create_workspace(
            Workspace(tenant_id=self.tenant.id, name="Demo WS")
        )
        self.actor = Actor(user_id=self.user.id, tenant_id=self.tenant.id, role=ROLE_OWNER)
        require(self.actor, ACTION_WRITE, self.workspace)
        self.session = self.store.create_session(
            Session(tenant_id=self.tenant.id, workspace_id=self.workspace.id, title="demo")
        )
        audit(
            self.store, self.actor, "session.create",
            target=self.session.id, metadata={"title": self.session.title},
        )

        self.bus = EventBus()
        self.chan = workspace_channel(self.workspace.id)
        self.bus.publish(
            Event(kind="session.started", channel=self.chan,
                  payload={"session_id": self.session.id})
        )

        self.mem = MemoryStore(dim=64)
        self.mem.add("Quarterly sales were up 12% YoY.", item_id="d1")
        self.mem.add("New product launch scheduled for Q3.", item_id="d2")
        self.mem.add("Hiring freeze lifted in engineering.", item_id="d3")
        self.mem.add("Customer satisfaction improved 8 points.", item_id="d4")
        self.mem.add("Budget approved for cloud infrastructure upgrade.", item_id="d5")

        self.registry = ConnectorRegistry()
        for c in [
            MicrosoftGraphConnector(), ZoomConnector(), WeComConnector(),
            LibreOfficeConnector(), DoclingConnector(),
        ]:
            self.registry.register(c)

        self.lock_mgr = FileLockManager(default_ttl=60)
        self.swarm = SwarmOrchestrator(
            workspace_id=self.workspace.id, lock_manager=self.lock_mgr
        )
        # seed the swarm with initial tasks so the kanban shows something
        for i in range(4):
            self.swarm.add(Task(id=f"seed{i}", prompt=f"seed task {i}"))
        workers = ["agent-0", "agent-1"]
        self.swarm.run(lambda tid, prompt, ctx: f"done:{tid}", workers=workers)

    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        with self._lock:
            return {
                "tenant_id": self.tenant.id,
                "tenant_name": self.tenant.name,
                "workspace_id": self.workspace.id,
                "session_id": self.session.id,
                "session_title": self.session.title,
                "kanban": self.swarm.stats(),
                "audit_count": len(self.store.list_audit(self.tenant.id)),
            }

    def get_connectors(self) -> dict:
        return {"connectors": self.registry.names()}

    def get_audit(self) -> dict:
        logs = self.store.list_audit(self.tenant.id, limit=50)
        return {"entries": [
            {
                "id": lg.id, "at": lg.at, "actor_id": lg.actor_id,
                "action": lg.action, "target": lg.target, "metadata": lg.metadata,
            }
            for lg in reversed(logs)
        ]}

    def memory_search(self, query: str, k: int = 5) -> dict:
        hits = self.mem.search(query, k=k)
        return {"hits": [
            {"text": h.item.text, "id": h.item.id, "score": round(h.score, 4)}
            for h in hits
        ]}

    def run_tasks(self, n_tasks: int = 4, n_workers: int = 2) -> dict:
        with self._lock:
            # Use a fresh FileLockManager so prior runs' held locks don't block re-claim.
            sw = SwarmOrchestrator(workspace_id=self.workspace.id)
            for i in range(max(1, n_tasks)):
                sw.add(Task(id=f"rt{i}", prompt=f"task {i}"))
            workers = [f"agent-{i}" for i in range(max(1, n_workers))]
            sw.run(lambda tid, prompt, ctx: f"done:{tid}", workers=workers)
            stats = sw.stats()
            self.swarm = sw
            audit(
                self.store, self.actor, "tasks.run",
                metadata={"n": n_tasks, "done": stats["done"]},
            )
        return stats


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    app: CoworkApp  # set via _make_server

    def log_message(self, fmt: str, *args) -> None:  # suppress access log
        pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            body = _UI_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            self._send_json(self.app.get_status())
        elif path == "/api/connectors":
            self._send_json(self.app.get_connectors())
        elif path == "/api/audit":
            self._send_json(self.app.get_audit())
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        body = self._read_json()
        if path == "/api/memory/search":
            q = str(body.get("query", ""))
            k = int(body.get("k", 5))
            self._send_json(self.app.memory_search(q, k))
        elif path == "/api/tasks/run":
            n = int(body.get("tasks", 4))
            w = int(body.get("workers", 2))
            self._send_json(self.app.run_tasks(n, w))
        else:
            self._send_json({"error": "not found"}, 404)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _make_server(app: CoworkApp, host: str, port: int) -> HTTPServer:
    """Create (but do not start) an HTTPServer bound to *app*."""

    class BoundHandler(_Handler):
        pass

    BoundHandler.app = app
    return HTTPServer((host, port), BoundHandler)


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    open_browser: bool = True,
    app: Optional[CoworkApp] = None,
) -> None:
    """Start the cowork web UI and block until Ctrl-C."""
    if app is None:
        app = CoworkApp()
    server = _make_server(app, host, port)
    url = f"http://{host}:{port}"
    print(f"cowork web UI  →  {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=[url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
