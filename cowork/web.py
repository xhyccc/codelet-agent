"""Lightweight stdlib-only web UI for the cowork enterprise platform.

Phases implemented
------------------
P1  Multi-pane UI architecture  (sidebar + 7 tab panels)
P2  OODA task orchestration dashboard  (plan generator + kanban + scheduler)
P3  Stateful artifact versioning + sandboxed preview rendering
P4  Connector marketplace with health indicators
P5  Multiplayer project hubs with privacy controls + chat snapshots
P6  Admin governance: SSO / RBAC capability matrix / SCIM / telemetry / compliance
P7  Guardrails: payload warnings + diff-review approval gates

Run::

    python -m cowork serve [--port 8765] [--no-browser]
"""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

from .admin import (
    AdminStore, ALL_CAPABILITIES, RBACRole, SCIMGroup, SSOConfig, TelemetryEvent,
)
from .artifacts_store import ArtifactVersion, ArtifactVersionStore
from .collab import FileLockManager
from .engine import CodeletEngine, CodeletInvocation
from .events import Event, EventBus, workspace_channel
from .guardrails import DiffReview, GuardrailEngine, PayloadWarning
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
from .projects import (
    ChatSnapshot,
    Project,
    ProjectMember,
    ProjectStore,
    ROLE_EDITOR,
    ROLE_VIEWER,
    VISIBILITY_INVITED,
    VISIBILITY_ORG,
    VISIBILITY_PRIVATE,
)
from .scheduler import nl_to_cron, ScheduledTask, TaskScheduler
from .security import ACTION_WRITE, Actor, audit, require
from .store import Store

_CONNECTOR_META: dict[str, dict] = {
    "microsoftgraph": {
        "scopes": ["mail.read", "calendar.readwrite", "files.readwrite"],
        "healthy": True,
    },
    "zoom": {"scopes": ["meeting.read", "recording.read"], "healthy": True},
    "wecom": {"scopes": ["message.send", "contacts.read"], "healthy": True},
    "libreoffice": {"scopes": ["document.convert", "document.edit"], "healthy": True},
    "docling": {"scopes": ["document.parse", "table.extract"], "healthy": True},
}

# ---------------------------------------------------------------------------
# Embedded single-page UI (raw string – JS template literals are preserved)
# ---------------------------------------------------------------------------

_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>cowork — Enterprise Dashboard</title>
<style>
:root{--bg:#0f1117;--s:#1a1d27;--c:#21242f;--b:#2d3142;--a:#5c7cfa;--ok:#40c057;--w:#fd7e14;--d:#f03e3e;--t:#e9ecef;--m:#868e96}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--t);display:flex;min-height:100vh}
#sidebar{width:220px;min-height:100vh;background:var(--s);border-right:1px solid var(--b);display:flex;flex-direction:column;position:fixed;top:0;left:0;bottom:0;z-index:10}
.logo{padding:1.1rem 1rem;font-size:1.1rem;font-weight:800;letter-spacing:-.03em;color:var(--a);border-bottom:1px solid var(--b)}
.nav-items{flex:1;padding:.4rem 0}
.nav-item{display:flex;align-items:center;gap:.55rem;padding:.55rem .9rem;cursor:pointer;border-radius:6px;margin:.1rem .35rem;font-size:.83rem;color:var(--m);transition:all .12s;user-select:none}
.nav-item:hover{background:var(--c);color:var(--t)}
.nav-item.active{background:color-mix(in srgb,var(--a) 15%,transparent);color:var(--a);font-weight:600}
.sessions-panel{padding:.65rem .9rem;border-top:1px solid var(--b);font-size:.7rem;color:var(--m)}
.sessions-panel .sh{font-weight:600;color:var(--t);margin-bottom:.3rem}
.sessions-panel .sv{font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:.15rem}
#main{margin-left:220px;flex:1;display:flex;flex-direction:column;min-height:100vh}
#topbar{background:var(--s);border-bottom:1px solid var(--b);padding:.55rem 1.4rem;display:flex;align-items:center;gap:.55rem;position:sticky;top:0;z-index:5}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ok);flex-shrink:0}
.dot.off{background:var(--d)}
#status-label{font-size:.7rem;color:var(--m)}
.spacer{flex:1}
#tenant-label{font-size:.73rem;color:var(--m);font-family:monospace}
#panels{flex:1;overflow:auto}
.panel{display:none;padding:1.4rem;max-width:1200px}
.panel.active{display:block}
h2{font-size:.66rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--m);margin-bottom:.85rem}
h3{font-size:.86rem;font-weight:700;color:var(--t);margin-bottom:.65rem}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:.7rem;margin-bottom:1.2rem}
.card{background:var(--c);border:1px solid var(--b);border-radius:10px;padding:.9rem 1rem}
.card .lbl{font-size:.6rem;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--m);margin-bottom:.25rem}
.card .val{font-size:1.4rem;font-weight:800;color:var(--t);line-height:1}
.section{background:var(--c);border:1px solid var(--b);border-radius:10px;padding:1rem;margin-bottom:.9rem}
input,textarea,select{background:var(--s);border:1px solid var(--b);border-radius:6px;padding:.4rem .7rem;color:var(--t);font-size:.83rem;outline:none;font-family:inherit;transition:border-color .18s}
input:focus,textarea:focus,select:focus{border-color:var(--a)}
textarea{resize:vertical;min-height:56px}
.btn{display:inline-flex;align-items:center;justify-content:center;border:none;border-radius:6px;padding:.42rem .95rem;font-size:.8rem;font-weight:600;cursor:pointer;transition:opacity .12s;gap:.3rem;white-space:nowrap}
.btn:hover{opacity:.83}
.btn-primary{background:var(--a);color:#fff}
.btn-success{background:#2f9e44;color:#fff}
.btn-warn{background:#e67700;color:#fff}
.btn-danger{background:var(--d);color:#fff}
.btn-ghost{background:transparent;border:1px solid var(--b);color:var(--t)}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{text-align:left;padding:.45rem .7rem;font-size:.63rem;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--m);border-bottom:1px solid var(--b)}
td{padding:.5rem .7rem;border-bottom:1px solid var(--b);color:var(--t)}
tr:hover td{background:#ffffff04}
.badge{display:inline-flex;align-items:center;padding:.18rem .5rem;border-radius:4px;font-size:.68rem;font-weight:600}
.badge-low{background:#1e2f10;color:#8ce99a}
.badge-med{background:#2f2410;color:var(--w)}
.badge-high{background:#2f1212;color:#ff8787}
.badge-crit{background:#4a0a0a;color:#ff6b6b}
.badge-pending{background:#1a2540;color:#74c0fc}
.badge-ok{background:#0f2820;color:var(--ok)}
.badge-rej{background:#2f1212;color:#ff8787}
.badge-html{background:#1a2540;color:#74c0fc}
.badge-code{background:#231a3a;color:#cc5de8}
.badge-json{background:#2f2414;color:#ffa94d}
.badge-md{background:#0f2820;color:#63e6be}
.badge-svg{background:#2a1a2f;color:#e599f7}
#chat-msgs{height:390px;overflow-y:auto;display:flex;flex-direction:column;gap:.55rem;padding:.4rem 0;margin-bottom:.65rem}
.msg{padding:.6rem .85rem;border-radius:8px;max-width:76%;font-size:.83rem;line-height:1.5}
.msg-user{background:color-mix(in srgb,var(--a) 18%,transparent);border:1px solid color-mix(in srgb,var(--a) 28%,transparent);align-self:flex-end}
.msg-bot{background:var(--c);border:1px solid var(--b);align-self:flex-start}
.msg-meta{font-size:.63rem;color:var(--m);margin-bottom:.22rem}
.chat-input-row{display:flex;gap:.45rem}
.chat-input-row textarea{flex:1;height:46px;min-height:46px}
.kanban{display:grid;grid-template-columns:repeat(3,1fr);gap:.7rem;margin-bottom:.9rem}
.kb-col{background:var(--s);border:1px solid var(--b);border-radius:8px;padding:.65rem}
.kb-col-h{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--m);margin-bottom:.5rem;display:flex;align-items:center;gap:.35rem}
.kb-task{background:var(--c);border:1px solid var(--b);border-radius:5px;padding:.45rem .65rem;margin-bottom:.35rem;font-size:.78rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.plan-list{display:flex;flex-direction:column;gap:.3rem;margin-top:.55rem}
.plan-item{display:flex;align-items:center;gap:.55rem;padding:.4rem .65rem;border-radius:6px;font-size:.8rem;background:var(--s);border:1px solid var(--b)}
.plan-item.done{opacity:.55;text-decoration:line-through}
.artifact-layout{display:grid;grid-template-columns:250px 1fr;gap:.9rem;min-height:540px}
.artifact-list{background:var(--c);border:1px solid var(--b);border-radius:10px;overflow-y:auto;padding:.45rem}
.artifact-item{padding:.45rem .7rem;border-radius:5px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:.45rem;font-size:.8rem}
.artifact-item:hover{background:var(--s)}
.artifact-item.selected{background:color-mix(in srgb,var(--a) 14%,transparent);color:var(--a)}
.artifact-preview{background:var(--c);border:1px solid var(--b);border-radius:10px;display:flex;flex-direction:column;overflow:hidden}
.artifact-hdr{padding:.65rem .9rem;border-bottom:1px solid var(--b);display:flex;align-items:center;justify-content:space-between;gap:.5rem}
.ver-row{padding:.45rem .9rem;border-bottom:1px solid var(--b);display:flex;align-items:center;gap:.65rem;font-size:.8rem}
.ver-row input[type=range]{flex:1;accent-color:var(--a)}
.ptabs{display:flex;gap:.2rem;padding:.35rem .7rem;border-bottom:1px solid var(--b)}
.ptab{padding:.28rem .7rem;border-radius:4px;font-size:.76rem;cursor:pointer;color:var(--m)}
.ptab.active{background:color-mix(in srgb,var(--a) 14%,transparent);color:var(--a);font-weight:600}
.preview-body{flex:1;overflow:auto;padding:.7rem}
.preview-body iframe{width:100%;height:340px;border:none;border-radius:4px;background:#fff}
.preview-body pre{font-family:'JetBrains Mono',monospace;font-size:.76rem;color:#c5d0e0;white-space:pre-wrap;line-height:1.6}
.connector-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:.75rem}
.con-card{background:var(--c);border:1px solid var(--b);border-radius:10px;padding:.9rem}
.con-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:.45rem}
.con-name{font-weight:700;font-size:.88rem}
.hdot{width:8px;height:8px;border-radius:50%}
.hdot-ok{background:var(--ok)}
.hdot-err{background:var(--d)}
.con-scopes{font-size:.7rem;color:var(--m);margin-bottom:.65rem;line-height:1.45}
.con-acts{display:flex;gap:.35rem}
.project-layout{display:grid;grid-template-columns:270px 1fr;gap:.9rem}
.proj-list{background:var(--c);border:1px solid var(--b);border-radius:10px;padding:.65rem;min-height:480px}
.proj-item{padding:.45rem .7rem;border-radius:5px;cursor:pointer;font-size:.8rem;display:flex;justify-content:space-between;align-items:center}
.proj-item:hover{background:var(--s)}
.proj-item.selected{background:color-mix(in srgb,var(--a) 14%,transparent);color:var(--a)}
.proj-detail{background:var(--c);border:1px solid var(--b);border-radius:10px;padding:.9rem}
.vis-btn{padding:.22rem .55rem;border-radius:4px;font-size:.7rem;border:1px solid var(--b);cursor:pointer;background:var(--s);color:var(--t)}
.vis-btn.ap{background:#2a1a3a;color:#cc5de8;border-color:#cc5de8}
.vis-btn.ai{background:#1a2540;color:var(--a);border-color:var(--a)}
.vis-btn.ao{background:#0f2820;color:var(--ok);border-color:var(--ok)}
.admin-tabs{display:flex;gap:.2rem;margin-bottom:.9rem;border-bottom:1px solid var(--b);padding-bottom:.4rem}
.atab{padding:.38rem .85rem;border-radius:4px;font-size:.8rem;cursor:pointer;color:var(--m)}
.atab.active{background:color-mix(in srgb,var(--a) 14%,transparent);color:var(--a);font-weight:600}
.rbac-wrap{overflow-x:auto}
.rbac-wrap th,.rbac-wrap td{min-width:88px;text-align:center}
.rbac-wrap th:first-child,.rbac-wrap td:first-child{text-align:left;min-width:155px}
input[type=checkbox]{accent-color:var(--a);width:13px;height:13px;cursor:default}
.diff-pre{background:#090b12;border:1px solid var(--b);border-radius:6px;padding:.65rem;font-family:monospace;font-size:.73rem;white-space:pre;overflow-x:auto;max-height:180px;overflow-y:auto;color:#c5d0e0;margin:.45rem 0}
.diff-pre .dadd{color:#63e6be}
.diff-pre .drem{color:#ff6b6b}
.diff-pre .dhunk{color:var(--m)}
.res-row{display:flex;gap:.35rem;align-items:center;margin-top:.45rem}
.res-row input{flex:1;height:30px}
.row{display:flex;gap:.45rem;align-items:center;flex-wrap:wrap;margin-bottom:.45rem}
.row label{font-size:.76rem;color:var(--m);white-space:nowrap}
.fw{width:100%}
.mt05{margin-top:.5rem}
.mt1{margin-top:.9rem}
.c-ok{color:var(--ok)}
.c-w{color:var(--w)}
.c-d{color:var(--d)}
.c-a{color:var(--a)}
</style>
</head>
<body>
<div id="sidebar">
  <div class="logo">&#x2B21; cowork</div>
  <div class="nav-items">
    <div class="nav-item active" data-tab="chat" onclick="App.switchTab('chat')">&#128172; Chat</div>
    <div class="nav-item" data-tab="tasks" onclick="App.switchTab('tasks')">&#9989; Tasks</div>
    <div class="nav-item" data-tab="artifacts" onclick="App.switchTab('artifacts')">&#128230; Artifacts</div>
    <div class="nav-item" data-tab="connectors" onclick="App.switchTab('connectors')">&#128268; Connectors</div>
    <div class="nav-item" data-tab="projects" onclick="App.switchTab('projects')">&#128193; Projects</div>
    <div class="nav-item" data-tab="admin" onclick="App.switchTab('admin')">&#9881; Admin</div>
    <div class="nav-item" data-tab="guardrails" onclick="App.switchTab('guardrails')">&#128737; Guardrails</div>
  </div>
  <div class="sessions-panel">
    <div class="sh">Active session</div>
    <div id="sb-session" class="sv">&#8212;</div>
    <div id="sb-ws" class="sv" style="color:var(--m)">&#8212;</div>
  </div>
</div>

<div id="main">
  <div id="topbar">
    <div class="dot" id="dot"></div>
    <span id="status-label">connecting&#8230;</span>
    <div class="spacer"></div>
    <span id="tenant-label"></span>
  </div>
  <div id="panels">

    <!-- CHAT -->
    <div id="panel-chat" class="panel active">
      <h2>Chat</h2>
      <div class="section" style="display:flex;flex-direction:column">
        <div id="chat-msgs"></div>
        <div class="chat-input-row">
          <textarea id="chat-q" placeholder="Message cowork&#8230;" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();App.sendChat()}"></textarea>
          <button class="btn btn-primary" onclick="App.sendChat()">Send</button>
        </div>
      </div>
    </div>

    <!-- TASKS -->
    <div id="panel-tasks" class="panel">
      <h2>Tasks &amp; Orchestration</h2>
      <div class="section">
        <h3>Plan Generator</h3>
        <div class="row">
          <input id="plan-q" style="flex:1" placeholder="Describe the objective&#8230;">
          <button class="btn btn-primary" onclick="App.generatePlan()">Generate Plan</button>
        </div>
        <div id="plan-list" class="plan-list"></div>
      </div>
      <div class="section">
        <h3>Kanban Board</h3>
        <div class="kanban">
          <div class="kb-col"><div class="kb-col-h"><span class="c-w">&#9679;</span> Pending</div><div id="kb-pending"></div></div>
          <div class="kb-col"><div class="kb-col-h"><span class="c-a">&#9679;</span> In Progress</div><div id="kb-progress"></div></div>
          <div class="kb-col"><div class="kb-col-h"><span class="c-ok">&#9679;</span> Done</div><div id="kb-done"></div></div>
        </div>
        <div class="row">
          <label>Tasks</label>
          <input type="number" id="run-n" value="4" min="1" max="20" style="width:58px">
          <label>Workers</label>
          <input type="number" id="run-w" value="2" min="1" max="8" style="width:58px">
          <button class="btn btn-success" onclick="App.runTasks()">&#9654; Run</button>
          <span id="run-msg" style="font-size:.76rem;color:var(--ok)"></span>
        </div>
      </div>
      <div class="section">
        <h3>Scheduled Tasks</h3>
        <div class="row">
          <input id="sched-name" placeholder="Task name" style="width:140px">
          <input id="sched-prompt" placeholder="Prompt" style="flex:1">
          <input id="sched-cron" placeholder="e.g. every morning" style="width:150px">
          <button class="btn btn-primary" onclick="App.addSchedule()">+ Add</button>
        </div>
        <table><thead><tr><th>Name</th><th>Cron</th><th>Status</th><th>Last run</th><th></th></tr></thead>
        <tbody id="sched-body"></tbody></table>
      </div>
    </div>

    <!-- ARTIFACTS -->
    <div id="panel-artifacts" class="panel">
      <h2>Artifacts</h2>
      <div class="row" style="margin-bottom:.75rem">
        <input id="art-name" placeholder="Artifact name" style="width:190px">
        <select id="art-kind" style="width:110px"><option>html</option><option>code</option><option>json</option><option>markdown</option><option>svg</option></select>
        <textarea id="art-body" placeholder="Content&#8230;" style="flex:1;height:34px;min-height:34px"></textarea>
        <button class="btn btn-primary" onclick="App.createArtifact()">+ Save</button>
      </div>
      <div class="artifact-layout">
        <div class="artifact-list" id="artifact-list"><div style="color:var(--m);padding:.7rem;font-size:.8rem">No artifacts.</div></div>
        <div class="artifact-preview" id="artifact-preview">
          <div style="flex:1;display:flex;align-items:center;justify-content:center;color:var(--m);font-size:.83rem">Select an artifact</div>
        </div>
      </div>
    </div>

    <!-- CONNECTORS -->
    <div id="panel-connectors" class="panel">
      <h2>Connector Marketplace</h2>
      <div class="connector-grid" id="connector-grid"></div>
    </div>

    <!-- PROJECTS -->
    <div id="panel-projects" class="panel">
      <h2>Projects</h2>
      <div class="project-layout">
        <div class="proj-list">
          <div class="row" style="margin-bottom:.65rem">
            <input id="proj-name" placeholder="New project&#8230;" style="flex:1">
            <button class="btn btn-primary" onclick="App.createProject()">+</button>
          </div>
          <div id="project-list"></div>
        </div>
        <div class="proj-detail" id="project-detail"><div style="color:var(--m);font-size:.83rem">Select a project</div></div>
      </div>
    </div>

    <!-- ADMIN -->
    <div id="panel-admin" class="panel">
      <h2>Admin</h2>
      <div class="admin-tabs">
        <div class="atab active" onclick="App.adminTab('identity')">Identity</div>
        <div class="atab" onclick="App.adminTab('roles')">Roles</div>
        <div class="atab" onclick="App.adminTab('telemetry')">Telemetry</div>
        <div class="atab" onclick="App.adminTab('compliance')">Compliance</div>
      </div>
      <div id="admin-identity">
        <div class="section">
          <h3>SSO Configuration</h3>
          <div class="row">
            <label>Provider</label>
            <select id="sso-prov" style="width:110px"><option value="saml">SAML</option><option value="oidc">OIDC</option></select>
            <label>Metadata URL</label>
            <input id="sso-url" placeholder="https://idp.example.com/metadata" style="flex:1">
            <label>Client ID</label>
            <input id="sso-cid" placeholder="client id" style="width:135px">
          </div>
          <div class="row mt05">
            <label><input type="checkbox" id="sso-req"> Require SSO</label>
            <label>Allowed domains</label>
            <input id="sso-domains" placeholder="example.com,corp.io" style="flex:1">
            <button class="btn btn-primary" onclick="App.saveSSO()">Save SSO</button>
          </div>
          <div id="sso-status" style="font-size:.76rem;color:var(--ok);margin-top:.35rem"></div>
        </div>
        <div class="section">
          <h3>SCIM Group Sync</h3>
          <div class="row">
            <input id="scim-eid" placeholder="External ID" style="width:125px">
            <input id="scim-name" placeholder="Group name" style="width:150px">
            <select id="scim-role" style="width:210px">
              <option>workspace_user</option>
              <option>workspace_limited_developer</option>
              <option>workspace_developer</option>
              <option>workspace_admin</option>
            </select>
            <button class="btn btn-primary" onclick="App.syncGroup()">Sync</button>
          </div>
          <table><thead><tr><th>External ID</th><th>Name</th><th>Mapped Role</th></tr></thead>
          <tbody id="scim-body"></tbody></table>
        </div>
      </div>
      <div id="admin-roles" style="display:none">
        <div class="section">
          <h3>RBAC Capability Matrix</h3>
          <div class="rbac-wrap" id="rbac-matrix"></div>
        </div>
      </div>
      <div id="admin-telemetry" style="display:none">
        <div class="cards" id="telem-cards"></div>
        <div class="section">
          <h3>Event Type Breakdown</h3>
          <table><thead><tr><th>Event Type</th><th>Count</th></tr></thead>
          <tbody id="telem-body"></tbody></table>
        </div>
      </div>
      <div id="admin-compliance" style="display:none">
        <div class="section">
          <h3>Compliance Export</h3>
          <div class="row">
            <label>Days</label>
            <input type="number" id="cpl-days" value="180" style="width:78px">
            <button class="btn btn-primary" onclick="App.exportCSV()">Export CSV</button>
          </div>
          <p style="font-size:.76rem;color:var(--m);margin-top:.5rem">Exports session metadata only &#8212; prompt/response content is never included.</p>
        </div>
      </div>
    </div>

    <!-- GUARDRAILS -->
    <div id="panel-guardrails" class="panel">
      <h2>Guardrails</h2>
      <div class="section">
        <h3>Payload Check</h3>
        <div class="row">
          <input id="gr-path" placeholder="Resource path" style="flex:1">
          <input type="number" id="gr-size" placeholder="Size (bytes)" style="width:125px">
          <select id="gr-vis" style="width:110px"><option value="private">private</option><option value="invited">invited</option><option value="org">org</option></select>
          <textarea id="gr-content" placeholder="Content sample&#8230;" style="width:180px;height:34px;min-height:34px"></textarea>
          <button class="btn btn-warn" onclick="App.checkPayload()">Check</button>
        </div>
        <div id="gr-result" style="font-size:.8rem;margin-top:.4rem"></div>
      </div>
      <div class="section">
        <h3>Payload Warnings</h3>
        <table>
          <thead><tr><th>Resource</th><th>Size</th><th>Sensitivity</th><th>Visibility</th><th>Message</th><th></th></tr></thead>
          <tbody id="warnings-body"></tbody>
        </table>
      </div>
      <div class="section">
        <h3>Diff Reviews</h3>
        <div id="diffs-list"><div style="color:var(--m);font-size:.8rem">No diff reviews.</div></div>
      </div>
    </div>

  </div><!-- /panels -->
</div><!-- /main -->

<script>
const App = {
  state:{msgs:[],arts:[],selArt:null,selVer:null,vers:[],projs:[],selProj:null,scheds:[]},

  esc(s){const d=document.createElement('div');d.textContent=String(s??'');return d.innerHTML},

  async get(p){try{return(await fetch(p)).json()}catch{return{error:'net'}}},
  async post(p,d){try{return(await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d||{})})).json()}catch{return{error:'net'}}},
  async del(p){try{return(await fetch(p,{method:'DELETE'})).json()}catch{return{error:'net'}}},
  async patch(p,d){try{return(await fetch(p,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(d||{})})).json()}catch{return{error:'net'}}},

  switchTab(tab){
    document.querySelectorAll('.panel').forEach(el=>el.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(el=>el.classList.remove('active'));
    const p=document.getElementById('panel-'+tab);if(p)p.classList.add('active');
    const n=document.querySelector('[data-tab="'+tab+'"]');if(n)n.classList.add('active');
    this.state.activeTab=tab;
    ({chat:()=>this.loadChat(),tasks:()=>this.loadTasks(),artifacts:()=>this.loadArtifacts(),
      connectors:()=>this.loadConnectors(),projects:()=>this.loadProjects(),
      admin:()=>this.loadAdmin(),guardrails:()=>this.loadGuardrails()})[tab]?.();
  },

  adminTab(tab){
    ['identity','roles','telemetry','compliance'].forEach(t=>{
      document.getElementById('admin-'+t).style.display=t===tab?'block':'none';
    });
    document.querySelectorAll('.atab').forEach((el,i)=>{
      el.classList.toggle('active',['identity','roles','telemetry','compliance'][i]===tab);
    });
    if(tab==='roles')this.loadRBACMatrix();
    if(tab==='telemetry')this.loadTelemetry();
  },

  /* ── Chat ── */
  loadChat(){this.renderMsgs()},
  renderMsgs(){
    const el=document.getElementById('chat-msgs');
    if(!this.state.msgs.length){
      el.innerHTML='<div class="msg msg-bot"><div class="msg-meta">cowork</div>Hello! Ask me anything about the workspace.</div>';
      return;
    }
    el.innerHTML=this.state.msgs.map(m=>`<div class="msg msg-${m.r}"><div class="msg-meta">${m.r==='user'?'You':'cowork'}</div>${this.esc(m.c)}</div>`).join('');
    el.scrollTop=el.scrollHeight;
  },
  async sendChat(){
    const q=document.getElementById('chat-q').value.trim();if(!q)return;
    document.getElementById('chat-q').value='';
    this.state.msgs.push({r:'user',c:q});this.renderMsgs();
    const r=await this.post('/api/chat',{message:q});
    this.state.msgs.push({r:'bot',c:r.reply||'&#8230;'});this.renderMsgs();
  },

  /* ── Tasks ── */
  async loadTasks(){
    const[st,sc]=await Promise.all([this.get('/api/status'),this.get('/api/scheduler')]);
    this.renderKanban(st.kanban||{});
    this.state.scheds=sc.tasks||[];this.renderScheduler();
  },
  renderKanban(kb){
    const mk=tasks=>(tasks||[]).map(t=>`<div class="kb-task">${this.esc(t.prompt||t.id)}</div>`).join('');
    document.getElementById('kb-pending').innerHTML=mk(kb.pending_tasks);
    document.getElementById('kb-progress').innerHTML=mk(kb.claimed_tasks);
    document.getElementById('kb-done').innerHTML=mk(kb.done_tasks);
  },
  async generatePlan(){
    const q=document.getElementById('plan-q').value.trim();if(!q)return;
    document.getElementById('plan-list').innerHTML='<div class="plan-item"><span>&#8987;</span> Generating&#8230;</div>';
    const r=await this.post('/api/tasks/plan',{prompt:q});
    const tasks=r.plan||[];
    document.getElementById('plan-list').innerHTML=tasks.map(t=>`<div class="plan-item ${t.status==='done'?'done':''}"><span>${t.status==='done'?'&#9989;':t.status==='failed'?'&#10060;':'&#11036;'}</span>${this.esc(t.title)}<span class="badge badge-pending" style="margin-left:auto">${this.esc(t.status)}</span></div>`).join('');
  },
  async runTasks(){
    const n=parseInt(document.getElementById('run-n').value)||4;
    const w=parseInt(document.getElementById('run-w').value)||2;
    document.getElementById('run-msg').textContent='running&#8230;';
    const r=await this.post('/api/tasks/run',{tasks:n,workers:w});
    document.getElementById('run-msg').textContent='&#10003; done='+r.done+' failed='+r.failed;
    this.loadTasks();
  },
  renderScheduler(){
    const rows=this.state.scheds.map(t=>`<tr>
      <td>${this.esc(t.name)}</td>
      <td style="font-family:monospace;font-size:.73rem">${this.esc(t.cron_expr)}</td>
      <td><span class="badge ${t.enabled?'badge-ok':'badge-high'}">${t.enabled?'enabled':'disabled'}</span></td>
      <td style="font-family:monospace;font-size:.7rem">${t.last_run?new Date(t.last_run*1000).toLocaleString():'never'}</td>
      <td style="display:flex;gap:.28rem">
        <button class="btn btn-ghost" style="padding:.22rem .55rem;font-size:.7rem" onclick="App.toggleSchedule('${t.id}')">${t.enabled?'Disable':'Enable'}</button>
        <button class="btn btn-danger" style="padding:.22rem .55rem;font-size:.7rem" onclick="App.deleteSchedule('${t.id}')">Del</button>
      </td></tr>`).join('');
    document.getElementById('sched-body').innerHTML=rows||'<tr><td colspan="5" style="color:var(--m)">No scheduled tasks.</td></tr>';
  },
  async addSchedule(){
    const name=document.getElementById('sched-name').value.trim();
    const prompt=document.getElementById('sched-prompt').value.trim();
    const nl=document.getElementById('sched-cron').value.trim();
    if(!name||!nl)return;
    await this.post('/api/scheduler',{name,prompt,cron_nl:nl});
    document.getElementById('sched-name').value='';document.getElementById('sched-prompt').value='';document.getElementById('sched-cron').value='';
    const r=await this.get('/api/scheduler');this.state.scheds=r.tasks||[];this.renderScheduler();
  },
  async toggleSchedule(id){await this.patch('/api/scheduler/'+id,{});const r=await this.get('/api/scheduler');this.state.scheds=r.tasks||[];this.renderScheduler()},
  async deleteSchedule(id){await this.del('/api/scheduler/'+id);const r=await this.get('/api/scheduler');this.state.scheds=r.tasks||[];this.renderScheduler()},

  /* ── Artifacts ── */
  async loadArtifacts(){
    const r=await this.get('/api/artifacts');this.state.arts=r.artifacts||[];this.renderArtifactList();
  },
  renderArtifactList(){
    const el=document.getElementById('artifact-list');
    if(!this.state.arts.length){el.innerHTML='<div style="color:var(--m);padding:.65rem;font-size:.78rem">No artifacts yet.</div>';return;}
    el.innerHTML=this.state.arts.map(a=>`<div class="artifact-item ${this.state.selArt===a.artifact_id?'selected':''}" onclick="App.selectArtifact('${a.artifact_id}')"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${this.esc(a.title||a.artifact_id)}</span><span class="badge badge-${a.kind||'code'}">${this.esc(a.kind||'?')}</span></div>`).join('');
  },
  async selectArtifact(id){
    this.state.selArt=id;this.renderArtifactList();
    const r=await this.get('/api/artifacts/'+id+'/versions');
    this.state.vers=r.versions||[];
    if(this.state.vers.length)await this.renderArtifactPreview(id,this.state.vers.length);
  },
  async renderArtifactPreview(id,ver){
    const r=await this.get('/api/artifacts/'+id+'/versions/'+ver);
    const av=r.version;if(!av)return;
    const title=av.attrs?.title||id;const kind=av.attrs?.kind||'code';const total=this.state.vers.length;
    document.getElementById('artifact-preview').innerHTML=`
      <div class="artifact-hdr"><span style="font-weight:700">${this.esc(title)}</span><span class="badge badge-${kind}">${this.esc(kind)}</span></div>
      <div class="ver-row"><span style="color:var(--m)">v${ver}</span><input type="range" min="1" max="${total}" value="${ver}" oninput="App.onVerSlide(this.value,'${id}')"><span style="color:var(--m)">${total} ver${total!==1?'s':''}</span></div>
      <div class="ptabs"><div class="ptab active" id="pt-prev" onclick="App.switchPtab('prev')">Preview</div><div class="ptab" id="pt-code" onclick="App.switchPtab('code')">Code</div></div>
      <div class="preview-body" id="pb-prev">${this._previewHTML(kind,av.body)}</div>
      <div class="preview-body" id="pb-code" style="display:none"><pre>${this.esc(av.body)}</pre></div>`;
  },
  _previewHTML(kind,body){
    if(kind==='html'||kind==='svg'){
      const blob='data:text/html;charset=utf-8,'+encodeURIComponent(body);
      return `<iframe sandbox="allow-scripts allow-same-origin" src="${blob}"></iframe>`;
    }
    return `<pre>${this.esc(body)}</pre>`;
  },
  switchPtab(t){
    document.getElementById('pb-prev').style.display=t==='prev'?'':'none';
    document.getElementById('pb-code').style.display=t==='code'?'':'none';
    document.getElementById('pt-prev').classList.toggle('active',t==='prev');
    document.getElementById('pt-code').classList.toggle('active',t==='code');
  },
  async onVerSlide(v,id){await this.renderArtifactPreview(id,parseInt(v))},
  async createArtifact(){
    const name=document.getElementById('art-name').value.trim();
    const kind=document.getElementById('art-kind').value;
    const body=document.getElementById('art-body').value;
    if(!name||!body)return;
    await this.post('/api/artifacts',{title:name,kind,body});
    document.getElementById('art-name').value='';document.getElementById('art-body').value='';
    await this.loadArtifacts();
  },

  /* ── Connectors ── */
  async loadConnectors(){
    const r=await this.get('/api/connectors/details');
    document.getElementById('connector-grid').innerHTML=(r.connectors||[]).map(c=>`
      <div class="con-card">
        <div class="con-hdr"><span class="con-name">${this.esc(c.name)}</span><div class="hdot ${c.healthy?'hdot-ok':'hdot-err'}"></div></div>
        <div class="con-scopes">${this.esc((c.scopes||[]).join(', ')||'No scopes')}</div>
        <div class="con-acts">
          <button class="btn btn-primary" style="font-size:.73rem;padding:.28rem .65rem">Authorize</button>
          <button class="btn btn-ghost" style="font-size:.73rem;padding:.28rem .65rem">Revoke</button>
        </div>
      </div>`).join('');
  },

  /* ── Projects ── */
  async loadProjects(){
    const r=await this.get('/api/projects');this.state.projs=r.projects||[];this.renderProjList();
  },
  renderProjList(){
    document.getElementById('project-list').innerHTML=this.state.projs.map(p=>`
      <div class="proj-item ${this.state.selProj===p.id?'selected':''}" onclick="App.selectProject('${p.id}')">
        <span>${this.esc(p.name)}</span>
        <span class="badge badge-${p.visibility==='private'?'high':p.visibility==='invited'?'pending':'ok'}">${this.esc(p.visibility)}</span>
      </div>`).join('')||'<div style="color:var(--m);font-size:.78rem;padding:.4rem">No projects.</div>';
  },
  async createProject(){
    const name=document.getElementById('proj-name').value.trim();if(!name)return;
    await this.post('/api/projects',{name,visibility:'private'});
    document.getElementById('proj-name').value='';await this.loadProjects();
  },
  async selectProject(id){
    this.state.selProj=id;this.renderProjList();
    const proj=this.state.projs.find(p=>p.id===id);if(!proj)return;
    const sn=await this.get('/api/projects/'+id+'/snapshots');
    const snaps=sn.snapshots||[];
    document.getElementById('project-detail').innerHTML=`
      <h3>${this.esc(proj.name)}</h3>
      <div class="row mt05">
        <span style="font-size:.76rem;color:var(--m)">Visibility:</span>
        ${['private','invited','org'].map(v=>`<button class="vis-btn ${proj.visibility===v?'a'+v[0]:''}" onclick="App.setVis('${id}','${v}')">${v}</button>`).join('')}
      </div>
      <div class="mt05">
        <label style="font-size:.76rem;color:var(--m)">Instructions</label>
        <textarea id="proj-instr" class="fw mt05" style="height:72px">${this.esc(proj.instructions||'')}</textarea>
        <button class="btn btn-primary mt05" onclick="App.saveInstr('${id}')">Save Instructions</button>
      </div>
      <div class="mt1"><h3>Members</h3>
        <div class="row">
          <input id="mem-uid" placeholder="User ID" style="width:150px">
          <select id="mem-role" style="width:95px"><option value="viewer">viewer</option><option value="editor">editor</option></select>
          <button class="btn btn-primary" onclick="App.addMember('${id}')">+ Add</button>
        </div>
      </div>
      <div class="mt1"><h3>Snapshots</h3>
        <div class="row">
          <input id="snap-title" placeholder="Snapshot title" style="flex:1">
          <textarea id="snap-content" placeholder="Content to share&#8230;" style="width:180px;height:34px;min-height:34px"></textarea>
          <button class="btn btn-primary" onclick="App.createSnap('${id}')">Share</button>
        </div>
        <table style="margin-top:.45rem">
          <thead><tr><th>Title</th><th>Created</th><th></th></tr></thead>
          <tbody>${snaps.map(s=>`<tr><td>${this.esc(s.title)}</td><td style="font-family:monospace;font-size:.7rem">${new Date(s.created_at*1000).toLocaleString()}</td><td><button class="btn btn-danger" style="padding:.18rem .45rem;font-size:.68rem" onclick="App.revokeSnap('${s.id}','${id}')">Revoke</button></td></tr>`).join('')||'<tr><td colspan="3" style="color:var(--m)">No snapshots.</td></tr>'}</tbody>
        </table>
      </div>`;
  },
  async setVis(id,vis){await this.patch('/api/projects/'+id,{visibility:vis});await this.loadProjects();await this.selectProject(id)},
  async saveInstr(id){const instr=document.getElementById('proj-instr')?.value||'';await this.patch('/api/projects/'+id,{instructions:instr})},
  async addMember(pid){
    const uid=document.getElementById('mem-uid').value.trim();
    const role=document.getElementById('mem-role').value;if(!uid)return;
    await this.post('/api/projects/'+pid+'/members',{user_id:uid,role});
    document.getElementById('mem-uid').value='';
  },
  async createSnap(pid){
    const title=document.getElementById('snap-title').value.trim();
    const content=document.getElementById('snap-content').value.trim();if(!title)return;
    await this.post('/api/projects/'+pid+'/snapshots',{title,content});
    document.getElementById('snap-title').value='';document.getElementById('snap-content').value='';
    await this.selectProject(pid);
  },
  async revokeSnap(sid,pid){await this.del('/api/snapshots/'+sid);await this.selectProject(pid)},

  /* ── Admin ── */
  async loadAdmin(){
    const sso=await this.get('/api/admin/sso');
    if(sso.sso){
      document.getElementById('sso-prov').value=sso.sso.provider||'saml';
      document.getElementById('sso-url').value=sso.sso.metadata_url||'';
      document.getElementById('sso-cid').value=sso.sso.client_id||'';
      document.getElementById('sso-req').checked=!!sso.sso.require_sso;
      document.getElementById('sso-domains').value=(sso.sso.allowed_domains||[]).join(',');
    }
    const gr=await this.get('/api/admin/groups');
    document.getElementById('scim-body').innerHTML=(gr.groups||[]).map(g=>`<tr><td style="font-family:monospace;font-size:.73rem">${this.esc(g.external_id)}</td><td>${this.esc(g.name)}</td><td>${this.esc(g.mapped_role)}</td></tr>`).join('')||'<tr><td colspan="3" style="color:var(--m)">No groups synced.</td></tr>';
  },
  async saveSSO(){
    await this.post('/api/admin/sso',{
      provider:document.getElementById('sso-prov').value,
      metadata_url:document.getElementById('sso-url').value,
      client_id:document.getElementById('sso-cid').value,
      require_sso:document.getElementById('sso-req').checked,
      allowed_domains:document.getElementById('sso-domains').value.split(',').map(s=>s.trim()).filter(Boolean)
    });
    const el=document.getElementById('sso-status');el.textContent='&#10003; SSO configuration saved.';
    setTimeout(()=>{el.textContent=''},3000);
  },
  async syncGroup(){
    const d={external_id:document.getElementById('scim-eid').value.trim(),name:document.getElementById('scim-name').value.trim(),mapped_role:document.getElementById('scim-role').value};
    if(!d.external_id)return;
    await this.post('/api/admin/groups',d);await this.loadAdmin();
  },
  async loadRBACMatrix(){
    const r=await this.get('/api/admin/roles');
    const roles=r.roles||[];const caps=r.capabilities||[];
    let h='<table class="rbac-wrap"><thead><tr><th>Capability</th>';
    roles.forEach(ro=>{h+=`<th>${this.esc(ro.name.replace('workspace_',''))}</th>`;});
    h+='</tr></thead><tbody>';
    caps.forEach(cap=>{
      h+=`<tr><td>${this.esc(cap)}</td>`;
      roles.forEach(ro=>{h+=`<td><input type="checkbox" ${ro.capabilities.includes(cap)?'checked':''} disabled></td>`;});
      h+='</tr>';
    });
    h+='</tbody></table>';
    document.getElementById('rbac-matrix').innerHTML=h;
  },
  async loadTelemetry(){
    const r=await this.get('/api/admin/telemetry');
    const agg=r.aggregate||{};const bd=r.breakdown||{};
    document.getElementById('telem-cards').innerHTML=`
      <div class="card"><div class="lbl">Total Tokens</div><div class="val">${agg.total_tokens||0}</div></div>
      <div class="card"><div class="lbl">Event Count</div><div class="val">${agg.event_count||0}</div></div>
      <div class="card"><div class="lbl">Avg Duration</div><div class="val">${Math.round(agg.avg_duration_ms||0)} ms</div></div>`;
    document.getElementById('telem-body').innerHTML=Object.entries(bd).map(([k,v])=>`<tr><td>${this.esc(k)}</td><td>${v}</td></tr>`).join('')||'<tr><td colspan="2" style="color:var(--m)">No events.</td></tr>';
  },
  exportCSV(){
    const days=parseInt(document.getElementById('cpl-days').value)||180;
    window.location.href='/api/admin/audit/export?days='+days;
  },

  /* ── Guardrails ── */
  async loadGuardrails(){
    const[wr,dr]=await Promise.all([this.get('/api/guardrails/warnings'),this.get('/api/guardrails/diffs')]);
    this.renderWarnings(wr.warnings||[]);this.renderDiffs(dr.diffs||[]);
  },
  renderWarnings(warnings){
    document.getElementById('warnings-body').innerHTML=warnings.map(w=>`<tr>
      <td style="font-family:monospace;font-size:.73rem;max-width:190px;overflow:hidden;text-overflow:ellipsis">${this.esc(w.resource_path)}</td>
      <td>${w.size_bytes.toLocaleString()}</td>
      <td><span class="badge badge-${w.sensitivity==='critical'?'crit':w.sensitivity==='high'?'high':w.sensitivity==='medium'?'med':'low'}">${this.esc(w.sensitivity)}</span></td>
      <td>${this.esc(w.workspace_visibility)}</td>
      <td style="font-size:.73rem;max-width:280px">${this.esc(w.message)}</td>
      <td><button class="btn btn-ghost" style="font-size:.7rem;padding:.2rem .45rem" onclick="App.dismissWarn('${w.id}')">Dismiss</button></td>
    </tr>`).join('')||'<tr><td colspan="6" style="color:var(--m)">No active warnings.</td></tr>';
  },
  renderDiffs(diffs){
    const el=document.getElementById('diffs-list');
    if(!diffs.length){el.innerHTML='<div style="color:var(--m);font-size:.8rem">No diff reviews.</div>';return;}
    el.innerHTML=diffs.map(d=>{
      const ip=d.status==='pending';
      const diffLines=(d.diff||'').split('\n').map(line=>{
        if(line.startsWith('---')||line.startsWith('+++'))return `<span class="dadd">${App.esc(line)}</span>`;
        if(line.startsWith('+'))return `<span class="dadd">${App.esc(line)}</span>`;
        if(line.startsWith('-'))return `<span class="drem">${App.esc(line)}</span>`;
        if(line.startsWith('@@'))return `<span class="dhunk">${App.esc(line)}</span>`;
        return App.esc(line);
      }).join('\n');
      return `<div class="section" style="margin-bottom:.7rem">
        <div style="display:flex;align-items:center;gap:.65rem;margin-bottom:.4rem">
          <span style="font-weight:700;font-family:monospace;font-size:.83rem">${this.esc(d.file_path)}</span>
          <span class="badge badge-pending">${this.esc(d.action)}</span>
          <span class="badge ${d.status==='approved'?'badge-ok':d.status==='rejected'?'badge-rej':'badge-pending'}">${this.esc(d.status)}</span>
        </div>
        <div class="diff-pre">${diffLines}</div>
        ${ip?`<div class="res-row"><input id="fb-${d.id}" placeholder="Feedback (optional)" style="flex:1;height:30px"><button class="btn btn-success" style="font-size:.76rem" onclick="App.resolveDiff('${d.id}',true)">&#10003; Approve</button><button class="btn btn-danger" style="font-size:.76rem" onclick="App.resolveDiff('${d.id}',false)">&#10007; Reject</button></div>`:`<div style="font-size:.73rem;color:var(--m);margin-top:.2rem">${this.esc(d.feedback||'')}</div>`}
      </div>`;
    }).join('');
  },
  async checkPayload(){
    const r=await this.post('/api/guardrails/check',{
      resource_path:document.getElementById('gr-path').value.trim(),
      size_bytes:parseInt(document.getElementById('gr-size').value)||0,
      workspace_visibility:document.getElementById('gr-vis').value,
      content_sample:document.getElementById('gr-content').value
    });
    const el=document.getElementById('gr-result');
    if(r.warning){el.innerHTML=`<span class="badge badge-${r.warning.sensitivity==='critical'?'crit':'high'}">&#9888; ${App.esc(r.warning.message)}</span>`;}
    else{el.innerHTML='<span class="badge badge-ok">&#10003; Safe to proceed</span>';}
  },
  async dismissWarn(id){await this.del('/api/guardrails/warnings/'+id);await this.loadGuardrails()},
  async resolveDiff(id,approved){
    const fb=(document.getElementById('fb-'+id)||{value:''}).value||'';
    await this.post('/api/guardrails/diffs/'+id+'/resolve',{approved,feedback:fb});
    await this.loadGuardrails();
  },

  /* ── Status ── */
  async refreshStatus(){
    try{
      const d=await this.get('/api/status');
      document.getElementById('dot').className='dot';
      document.getElementById('status-label').textContent='live';
      document.getElementById('tenant-label').textContent=d.tenant_name||'';
      document.getElementById('sb-session').textContent=d.session_id||'&#8212;';
      document.getElementById('sb-ws').textContent=d.workspace_id||'&#8212;';
    }catch{
      document.getElementById('dot').className='dot off';
      document.getElementById('status-label').textContent='offline';
    }
  },

  async init(){
    await this.refreshStatus();
    await this.loadChat();
    setInterval(()=>this.refreshStatus(),10000);
  }
};

document.addEventListener('DOMContentLoaded',()=>App.init());
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
            Session(
                tenant_id=self.tenant.id,
                workspace_id=self.workspace.id,
                title="demo",
            )
        )
        audit(
            self.store, self.actor, "session.create",
            target=self.session.id, metadata={"title": self.session.title},
        )

        self.bus = EventBus()
        self.chan = workspace_channel(self.workspace.id)
        self.bus.publish(
            Event(
                kind="session.started",
                channel=self.chan,
                payload={"session_id": self.session.id},
            )
        )

        self.mem = MemoryStore(dim=64)

        self.registry = ConnectorRegistry()
        for c in [
            MicrosoftGraphConnector(),
            ZoomConnector(),
            WeComConnector(),
            LibreOfficeConnector(),
            DoclingConnector(),
        ]:
            self.registry.register(c)

        self.engine = CodeletEngine()

        self.lock_mgr = FileLockManager(default_ttl=60)
        self.swarm = SwarmOrchestrator(
            workspace_id=self.workspace.id, lock_manager=self.lock_mgr
        )
        for i in range(4):
            self.swarm.add(Task(id=f"seed{i}", prompt=f"seed task {i}"))
        self.swarm.run(lambda tid, prompt, ctx: f"done:{tid}", workers=["agent-0", "agent-1"])

        # P2-P7: new stores
        self.artifact_store = ArtifactVersionStore(":memory:")
        self.task_scheduler = TaskScheduler()
        self.project_store = ProjectStore(":memory:")
        self.admin_store = AdminStore(":memory:")
        self.guardrails = GuardrailEngine()
        self._seed_new()

    # ------------------------------------------------------------------
    # Demo data for new features
    # ------------------------------------------------------------------

    def _seed_new(self) -> None:
        # Artifacts: two versions of an HTML demo + a code snippet
        aid1 = "art_demo_html"
        self.artifact_store.save(ArtifactVersion(
            artifact_id=aid1, version=0,
            body=(
                "<style>body{font-family:sans-serif;padding:2rem;background:#0f1117;color:#e9ecef}"
                "h1{color:#5c7cfa}</style><h1>cowork Dashboard</h1>"
                "<p>Welcome! This is artifact v1.</p>"
            ),
            attrs={"title": "Dashboard Demo", "kind": "html"},
        ))
        self.artifact_store.save(ArtifactVersion(
            artifact_id=aid1, version=0,
            body=(
                "<style>body{font-family:sans-serif;padding:2rem;background:#0f1117;color:#e9ecef}"
                "h1{color:#40c057}p{color:#868e96}</style>"
                "<h1>cowork Dashboard v2</h1><p>Updated with dark-mode refinements.</p>"
            ),
            attrs={"title": "Dashboard Demo", "kind": "html"},
        ))
        aid2 = "art_demo_code"
        self.artifact_store.save(ArtifactVersion(
            artifact_id=aid2, version=0,
            body='def greet(name: str) -> str:\n    """Return a greeting."""\n    return f"Hello, {name}!"\n',
            attrs={"title": "greet.py", "kind": "code"},
        ))

        # Scheduler: one demo task
        self.task_scheduler.add(ScheduledTask(
            name="Daily Digest",
            prompt="Summarise workspace activity for the day",
            cron_expr="0 9 * * *",
            workspace_id=self.workspace.id,
            enabled=True,
        ))

        # Projects
        proj = Project(
            tenant_id=self.tenant.id,
            name="Alpha Initiative",
            visibility=VISIBILITY_INVITED,
            owner_id=self.user.id,
            instructions="Focus on Q3 deliverables.",
        )
        self.project_store.create_project(proj)
        self.project_store.create_snapshot(ChatSnapshot(
            project_id=proj.id,
            title="Kickoff notes",
            content="[]",
            creator_id=self.user.id,
        ))

        # Admin: seed telemetry events
        for etype, tokens in [
            ("session_start", 50),
            ("tool_use", 200),
            ("tool_use", 175),
            ("session_end", 30),
        ]:
            self.admin_store.record(TelemetryEvent(
                session_id=self.session.id,
                event_type=etype,
                tokens_used=tokens,
                duration_ms=400,
                user_id=self.user.id,
            ))

        # Guardrails: a pre-existing warning + a pending diff review
        self.guardrails.check_payload(
            "HR_salaries_2024.xlsx",
            size_bytes=2048,
            workspace_visibility=VISIBILITY_ORG,
        )
        self.guardrails.request_diff_review(DiffReview(
            file_path="deploy.sh",
            action="execute",
            before="#!/bin/bash\necho 'Start deploy'\n",
            after="#!/bin/bash\nrm -rf /tmp/cache\necho 'Deploy complete'\n",
            description="Production deployment modification",
        ))

    # ------------------------------------------------------------------
    # Existing methods (P1 foundation)
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        with self._lock:
            stats = self.swarm.stats()
            pending_tasks = [
                {"id": c.task.id, "prompt": c.task.prompt}
                for c in self.swarm.cards.values() if c.status == "pending"
            ]
            claimed_tasks = [
                {"id": c.task.id, "prompt": c.task.prompt, "worker": c.worker}
                for c in self.swarm.cards.values() if c.status == "claimed"
            ]
            done_tasks = [
                {"id": c.task.id, "prompt": c.task.prompt}
                for c in self.swarm.cards.values() if c.status == "done"
            ]
            return {
                "tenant_id": self.tenant.id,
                "tenant_name": self.tenant.name,
                "workspace_id": self.workspace.id,
                "session_id": self.session.id,
                "session_title": self.session.title,
                "kanban": {
                    **stats,
                    "pending_tasks": pending_tasks,
                    "claimed_tasks": claimed_tasks,
                    "done_tasks": done_tasks,
                },
                "audit_count": len(self.store.list_audit(self.tenant.id)),
            }

    def get_connectors(self) -> dict:
        return {"connectors": self.registry.names()}

    def get_connector_details(self) -> dict:
        details = []
        for name in self.registry.names():
            key = name.lower().replace(" ", "")
            meta = next(
                (v for k, v in _CONNECTOR_META.items() if k in key),
                {"scopes": ["read", "write"], "healthy": True},
            )
            details.append({"name": name, **meta})
        return {"connectors": details}

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

    # ------------------------------------------------------------------
    # P2 Chat + Plan
    # ------------------------------------------------------------------

    def chat(self, message: str) -> dict:
        # Ground the agent with relevant workspace memory
        hits = self.mem.search(message, k=3)
        ctx_lines = [h.item.text for h in hits[:3]]
        if ctx_lines:
            ctx_block = "Workspace context:\n" + "\n".join(f"- {c}" for c in ctx_lines) + "\n\n"
            prompt = ctx_block + message
        else:
            prompt = message

        inv = CodeletInvocation(
            prompt=prompt,
            cwd=Path.cwd(),
            approval="auto",
            timeout=60.0,
        )
        result = self.engine.run(inv)

        if result.final:
            reply = result.final
            source = "agent"
        elif result.returncode == 0 and result.stdout.strip():
            reply = result.stdout.strip()[:4000]
            source = "agent"
        else:
            # Agent unavailable (not configured / no API key) — surface memory context
            if ctx_lines:
                reply = "[agent unavailable] Workspace context: " + " | ".join(ctx_lines[:2])
            else:
                reply = "Agent unavailable. Check your codelet configuration (API key / model)."
            source = "fallback"

        audit(self.store, self.actor, "chat.message",
              metadata={"len": len(message), "source": source})
        return {"reply": reply, "context_hits": len(hits), "source": source}

    def plan_tasks(self, prompt: str) -> dict:
        steps = [
            f"Analyse requirements: {prompt[:55]}",
            "Gather resources and stakeholders",
            "Design solution architecture",
            "Implement core functionality",
            "Test and validate results",
            "Deploy and monitor",
        ]
        return {"plan": [{"title": s, "status": "pending"} for s in steps]}

    # ------------------------------------------------------------------
    # P2 Scheduler
    # ------------------------------------------------------------------

    def list_scheduler(self) -> dict:
        return {"tasks": [t.to_dict() for t in self.task_scheduler.list()]}

    def add_scheduler(self, name: str, prompt: str, cron_nl: str) -> dict:
        cron = nl_to_cron(cron_nl) if cron_nl else "0 9 * * *"
        task = ScheduledTask(
            name=name or "Unnamed",
            prompt=prompt,
            cron_expr=cron,
            workspace_id=self.workspace.id,
            enabled=True,
        )
        self.task_scheduler.add(task)
        return task.to_dict()

    def toggle_scheduler(self, task_id: str) -> dict:
        self.task_scheduler.toggle(task_id)
        t = self.task_scheduler.get(task_id)
        return t.to_dict() if t else {"error": "not found"}

    def remove_scheduler(self, task_id: str) -> dict:
        return {"ok": self.task_scheduler.remove(task_id)}

    # ------------------------------------------------------------------
    # P3 Artifacts
    # ------------------------------------------------------------------

    def list_artifacts(self) -> dict:
        return {"artifacts": self.artifact_store.list_artifacts()}

    def get_artifact(self, artifact_id: str) -> dict:
        av = self.artifact_store.get_latest(artifact_id)
        return {"version": av.to_dict() if av else None}

    def list_artifact_versions(self, artifact_id: str) -> dict:
        versions = self.artifact_store.list_versions(artifact_id)
        return {
            "versions": [
                {
                    "id": v.id,
                    "artifact_id": v.artifact_id,
                    "version": v.version,
                    "attrs": v.attrs,
                    "created_at": v.created_at,
                }
                for v in versions
            ]
        }

    def get_artifact_version(self, artifact_id: str, version: int) -> dict:
        av = self.artifact_store.get_version(artifact_id, version)
        return {"version": av.to_dict() if av else None}

    def create_artifact(self, title: str, kind: str, body: str) -> dict:
        import uuid
        artifact_id = f"art_{uuid.uuid4().hex[:12]}"
        av = ArtifactVersion(
            artifact_id=artifact_id,
            version=0,
            body=body,
            attrs={"title": title or "Untitled", "kind": kind or "code"},
        )
        saved = self.artifact_store.save(av)
        return saved.to_dict()

    # ------------------------------------------------------------------
    # P5 Projects
    # ------------------------------------------------------------------

    def list_projects(self) -> dict:
        projects = self.project_store.list_projects(self.tenant.id)
        return {"projects": [p.to_dict() for p in projects]}

    def create_project(self, name: str, visibility: str = VISIBILITY_PRIVATE) -> dict:
        proj = Project(
            tenant_id=self.tenant.id,
            name=name or "Untitled",
            visibility=visibility,
            owner_id=self.user.id,
        )
        self.project_store.create_project(proj)
        return proj.to_dict()

    def update_project(
        self,
        project_id: str,
        visibility: Optional[str] = None,
        instructions: Optional[str] = None,
    ) -> dict:
        self.project_store.update_project(
            project_id,
            visibility=visibility,
            instructions=instructions,
        )
        proj = self.project_store.get_project(project_id)
        return proj.to_dict() if proj else {"error": "not found"}

    def add_project_member(self, project_id: str, user_id: str, role: str) -> dict:
        member = ProjectMember(project_id=project_id, user_id=user_id, role=role)
        self.project_store.add_member(member)
        return member.to_dict()

    def list_snapshots(self, project_id: str) -> dict:
        snaps = self.project_store.list_snapshots(project_id)
        return {"snapshots": [s.to_dict() for s in snaps if not s.revoked]}

    def create_snapshot(self, project_id: str, title: str, content: str) -> dict:
        snap = ChatSnapshot(
            project_id=project_id,
            title=title or "Snapshot",
            content=content or "[]",
            creator_id=self.user.id,
        )
        self.project_store.create_snapshot(snap)
        return snap.to_dict()

    def revoke_snapshot(self, snap_id: str) -> dict:
        return {"ok": self.project_store.revoke_snapshot(snap_id)}

    # ------------------------------------------------------------------
    # P6 Admin
    # ------------------------------------------------------------------

    def get_admin_sso(self) -> dict:
        cfg = self.admin_store.get_sso()
        return {"sso": cfg.to_dict() if cfg else None}

    def save_admin_sso(self, data: dict) -> dict:
        cfg = SSOConfig(
            provider=str(data.get("provider", "saml")),
            metadata_url=str(data.get("metadata_url", "")),
            client_id=str(data.get("client_id", "")),
            require_sso=bool(data.get("require_sso", True)),
            allowed_domains=list(data.get("allowed_domains") or []),
        )
        self.admin_store.save_sso(cfg)
        return {"ok": True}

    def list_admin_roles(self) -> dict:
        roles = self.admin_store.list_roles()
        return {
            "roles": [r.to_dict() for r in roles],
            "capabilities": ALL_CAPABILITIES,
        }

    def upsert_admin_role(self, data: dict) -> dict:
        role = RBACRole(
            name=str(data.get("name", "")),
            capabilities=set(data.get("capabilities") or []),
            is_custom=True,
        )
        self.admin_store.upsert_role(role)
        return role.to_dict()

    def list_admin_groups(self) -> dict:
        groups = self.admin_store.list_groups()
        return {"groups": [
            {
                "id": g.id,
                "external_id": g.external_id,
                "name": g.name,
                "mapped_role": g.mapped_role,
            }
            for g in groups
        ]}

    def sync_admin_group(self, data: dict) -> dict:
        g = SCIMGroup(
            external_id=str(data.get("external_id", "")),
            name=str(data.get("name", "")),
            mapped_role=str(data.get("mapped_role", "workspace_user")),
        )
        self.admin_store.sync_group(g)
        return {"ok": True}

    def get_admin_telemetry(self) -> dict:
        agg = self.admin_store.aggregate_usage(days=30)
        bd = self.admin_store.event_type_breakdown(days=30)
        return {"aggregate": agg, "breakdown": bd}

    def export_audit_csv(self, days: int = 180) -> str:
        return self.admin_store.export_audit_csv(days=days)

    # ------------------------------------------------------------------
    # P7 Guardrails
    # ------------------------------------------------------------------

    def list_guardrail_warnings(self) -> dict:
        return {"warnings": [w.to_dict() for w in self.guardrails.list_warnings()]}

    def dismiss_warning(self, warning_id: str) -> dict:
        return {"ok": self.guardrails.dismiss_warning(warning_id)}

    def list_guardrail_diffs(self) -> dict:
        return {"diffs": [d.to_dict() for d in self.guardrails.list_diffs()]}

    def resolve_diff(self, diff_id: str, approved: bool, feedback: str = "") -> dict:
        dr = self.guardrails.resolve_diff(diff_id, approved=approved, feedback=feedback)
        return dr.to_dict() if dr else {"error": "not found"}

    def check_payload(
        self,
        resource_path: str,
        size_bytes: int,
        workspace_visibility: str = "private",
        content_sample: str = "",
    ) -> dict:
        w = self.guardrails.check_payload(
            resource_path,
            size_bytes=size_bytes,
            workspace_visibility=workspace_visibility,
            content_sample=content_sample,
        )
        return {"warning": w.to_dict() if w else None}


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

    def _send_csv(self, text: str, filename: str = "audit_export.csv") -> None:
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self) -> None:
        body = _UI_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length))

    @staticmethod
    def _parts(path: str) -> list[str]:
        return [p for p in path.split("/") if p]

    def do_GET(self) -> None:  # noqa: N802
        path, _, qs = self.path.partition("?")
        p = self._parts(path)

        if not p or p == ["index.html"]:
            return self._serve_html()
        if p == ["api", "status"]:
            return self._send_json(self.app.get_status())
        if p == ["api", "connectors"]:
            return self._send_json(self.app.get_connectors())
        if p == ["api", "connectors", "details"]:
            return self._send_json(self.app.get_connector_details())
        if p == ["api", "audit"]:
            return self._send_json(self.app.get_audit())
        if p == ["api", "scheduler"]:
            return self._send_json(self.app.list_scheduler())
        if p == ["api", "artifacts"]:
            return self._send_json(self.app.list_artifacts())
        if len(p) == 3 and p[:2] == ["api", "artifacts"]:
            return self._send_json(self.app.get_artifact(p[2]))
        if len(p) == 4 and p[:2] == ["api", "artifacts"] and p[3] == "versions":
            return self._send_json(self.app.list_artifact_versions(p[2]))
        if len(p) == 5 and p[:2] == ["api", "artifacts"] and p[3] == "versions":
            try:
                ver = int(p[4])
            except ValueError:
                return self._send_json({"error": "invalid version"}, 400)
            return self._send_json(self.app.get_artifact_version(p[2], ver))
        if p == ["api", "projects"]:
            return self._send_json(self.app.list_projects())
        if len(p) == 4 and p[:2] == ["api", "projects"] and p[3] == "snapshots":
            return self._send_json(self.app.list_snapshots(p[2]))
        if p == ["api", "admin", "sso"]:
            return self._send_json(self.app.get_admin_sso())
        if p == ["api", "admin", "roles"]:
            return self._send_json(self.app.list_admin_roles())
        if p == ["api", "admin", "groups"]:
            return self._send_json(self.app.list_admin_groups())
        if p == ["api", "admin", "telemetry"]:
            return self._send_json(self.app.get_admin_telemetry())
        if p == ["api", "admin", "audit", "export"]:
            params = dict(pair.split("=", 1) for pair in qs.split("&") if "=" in pair)
            try:
                days = int(params.get("days", 180))
            except ValueError:
                days = 180
            return self._send_csv(self.app.export_audit_csv(days))
        if p == ["api", "guardrails", "warnings"]:
            return self._send_json(self.app.list_guardrail_warnings())
        if p == ["api", "guardrails", "diffs"]:
            return self._send_json(self.app.list_guardrail_diffs())
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path, _, _ = self.path.partition("?")
        p = self._parts(path)
        body = self._read_json()

        if p == ["api", "chat"]:
            return self._send_json(self.app.chat(str(body.get("message", ""))))
        if p == ["api", "memory", "search"]:
            return self._send_json(
                self.app.memory_search(str(body.get("query", "")), int(body.get("k", 5)))
            )
        if p == ["api", "tasks", "run"]:
            return self._send_json(
                self.app.run_tasks(int(body.get("tasks", 4)), int(body.get("workers", 2)))
            )
        if p == ["api", "tasks", "plan"]:
            return self._send_json(self.app.plan_tasks(str(body.get("prompt", ""))))
        if p == ["api", "scheduler"]:
            return self._send_json(
                self.app.add_scheduler(
                    str(body.get("name", "")),
                    str(body.get("prompt", "")),
                    str(body.get("cron_nl", "")),
                )
            )
        if p == ["api", "artifacts"]:
            return self._send_json(
                self.app.create_artifact(
                    str(body.get("title", "")),
                    str(body.get("kind", "code")),
                    str(body.get("body", "")),
                )
            )
        if p == ["api", "projects"]:
            return self._send_json(
                self.app.create_project(
                    str(body.get("name", "")),
                    str(body.get("visibility", VISIBILITY_PRIVATE)),
                )
            )
        if len(p) == 4 and p[:2] == ["api", "projects"] and p[3] == "members":
            return self._send_json(
                self.app.add_project_member(
                    p[2],
                    str(body.get("user_id", "")),
                    str(body.get("role", ROLE_VIEWER)),
                )
            )
        if len(p) == 4 and p[:2] == ["api", "projects"] and p[3] == "snapshots":
            return self._send_json(
                self.app.create_snapshot(
                    p[2],
                    str(body.get("title", "")),
                    str(body.get("content", "")),
                )
            )
        if p == ["api", "admin", "sso"]:
            return self._send_json(self.app.save_admin_sso(body))
        if p == ["api", "admin", "roles"]:
            return self._send_json(self.app.upsert_admin_role(body))
        if p == ["api", "admin", "groups"]:
            return self._send_json(self.app.sync_admin_group(body))
        if p == ["api", "guardrails", "check"]:
            return self._send_json(
                self.app.check_payload(
                    str(body.get("resource_path", "")),
                    int(body.get("size_bytes", 0)),
                    str(body.get("workspace_visibility", "private")),
                    str(body.get("content_sample", "")),
                )
            )
        if (
            len(p) == 5
            and p[:2] == ["api", "guardrails"]
            and p[2] == "diffs"
            and p[4] == "resolve"
        ):
            return self._send_json(
                self.app.resolve_diff(
                    p[3],
                    bool(body.get("approved", True)),
                    str(body.get("feedback", "")),
                )
            )
        self._send_json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:  # noqa: N802
        path, _, _ = self.path.partition("?")
        p = self._parts(path)

        if len(p) == 3 and p[:2] == ["api", "scheduler"]:
            return self._send_json(self.app.remove_scheduler(p[2]))
        if len(p) == 3 and p[:2] == ["api", "snapshots"]:
            return self._send_json(self.app.revoke_snapshot(p[2]))
        if len(p) == 4 and p[:2] == ["api", "guardrails"] and p[2] == "warnings":
            return self._send_json(self.app.dismiss_warning(p[3]))
        self._send_json({"error": "not found"}, 404)

    def do_PATCH(self) -> None:  # noqa: N802
        path, _, _ = self.path.partition("?")
        p = self._parts(path)
        body = self._read_json()

        if len(p) == 3 and p[:2] == ["api", "scheduler"]:
            return self._send_json(self.app.toggle_scheduler(p[2]))
        if len(p) == 3 and p[:2] == ["api", "projects"]:
            return self._send_json(
                self.app.update_project(
                    p[2],
                    visibility=body.get("visibility"),
                    instructions=body.get("instructions"),
                )
            )
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
