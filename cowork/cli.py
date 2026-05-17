"""Cowork demo CLI.

Run as::

    python -m cowork demo

Creates an in-memory tenant + workspace + session, registers all stub
office connectors, runs a tiny swarm with a fake runner, and prints the
resulting kanban state. Intended as a smoke-test of the F1-F9 surface.
"""
from __future__ import annotations

import argparse
import json
import sys
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


def _build_registry() -> ConnectorRegistry:
    reg = ConnectorRegistry()
    reg.register(MicrosoftGraphConnector())
    reg.register(ZoomConnector())
    reg.register(WeComConnector())
    reg.register(LibreOfficeConnector())
    reg.register(DoclingConnector())
    return reg


def cmd_demo(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="cowork demo")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--tasks", type=int, default=4)
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    args = parser.parse_args(argv or [])

    store = Store(db_path=":memory:")
    tenant = store.create_tenant(Tenant(name="Acme"))
    user = store.create_user(User(tenant_id=tenant.id, email="owner@acme.test"))
    ws = store.create_workspace(Workspace(tenant_id=tenant.id, name="Demo WS"))
    actor = Actor(user_id=user.id, tenant_id=tenant.id, role=ROLE_OWNER)
    require(actor, ACTION_WRITE, ws)
    session = store.create_session(
        Session(tenant_id=tenant.id, workspace_id=ws.id, title="demo")
    )
    audit(store, actor, "session.create", target=session.id, metadata={"title": session.title})

    bus = EventBus()
    chan = workspace_channel(ws.id)
    bus.publish(Event(kind="session.started", channel=chan, payload={"session_id": session.id}))

    mem = MemoryStore(dim=64)
    mem.add("Quarterly sales were up 12% YoY.", item_id="d1")
    mem.add("New product launch scheduled for Q3.", item_id="d2")
    mem.add("Hiring freeze lifted in engineering.", item_id="d3")
    hits = mem.search("product launch", k=2)

    registry = _build_registry()

    sw = SwarmOrchestrator(workspace_id=ws.id, lock_manager=FileLockManager(default_ttl=60))
    for i in range(args.tasks):
        sw.add(Task(id=f"t{i}", prompt=f"work item {i}"))

    def runner(tid, prompt, ctx):
        return f"completed:{tid}"

    workers = [f"agent-{i}" for i in range(args.workers)]
    sw.run(runner, workers=workers)

    summary = {
        "tenant": tenant.id,
        "workspace": ws.id,
        "session": session.id,
        "channel": chan,
        "kanban": sw.stats(),
        "connectors": registry.names(),
        "top_memory_hit": (hits[0].item.id if hits else None),
        "audit_count": len(store.list_audit(tenant.id)),
    }

    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    else:
        sys.stdout.write("cowork demo\n")
        sys.stdout.write("===========\n")
        for k, v in summary.items():
            sys.stdout.write(f"  {k}: {v}\n")
    return 0


def cmd_serve(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="cowork serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv or [])
    from .web import CoworkApp, serve  # local import to keep startup fast
    serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        argv = ["demo"]
    cmd, *rest = argv
    if cmd == "demo":
        return cmd_demo(rest)
    if cmd == "serve":
        return cmd_serve(rest)
    if cmd in ("-h", "--help", "help"):
        sys.stdout.write(
            "usage: python -m cowork <command> [options]\n"
            "commands:\n"
            "  demo   [--workers N] [--tasks N] [--json]\n"
            "  serve  [--host HOST] [--port PORT] [--no-browser]\n"
        )
        return 0
    sys.stderr.write(f"unknown command: {cmd}\n")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
