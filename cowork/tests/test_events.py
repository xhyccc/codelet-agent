"""Event bus tests."""
from __future__ import annotations

import threading

from cowork.events import (
    Event,
    EventBus,
    QueueSubscription,
    session_channel,
    tenant_budget_channel,
    workspace_channel,
)


def test_channel_helpers():
    assert workspace_channel("ws_1") == "workspace:ws_1"
    assert session_channel("ses_1") == "session:ses_1"
    assert tenant_budget_channel("ten_1") == "tenant:ten_1:budget"


def test_subscribe_publish_fanout():
    bus = EventBus()
    received_a: list[Event] = []
    received_b: list[Event] = []
    bus.subscribe("workspace:w1", received_a.append)
    bus.subscribe("workspace:w1", received_b.append)
    bus.subscribe("workspace:w2", lambda e: received_a.append(e))  # other channel
    n = bus.publish(Event(kind="agent.output", channel="workspace:w1", payload={"x": 1}))
    assert n == 2
    assert len(received_a) == 1 and received_a[0].payload == {"x": 1}
    assert len(received_b) == 1


def test_unsubscribe():
    bus = EventBus()
    hits: list[Event] = []
    sub = bus.subscribe("c", hits.append)
    bus.publish(Event(kind="k", channel="c"))
    sub.cancel()
    bus.publish(Event(kind="k", channel="c"))
    assert len(hits) == 1


def test_history_bounded():
    bus = EventBus(history_per_channel=3)
    for i in range(5):
        bus.publish(Event(kind="t", channel="c", payload={"i": i}))
    hist = bus.history("c")
    assert [e.payload["i"] for e in hist] == [2, 3, 4]


def test_history_since():
    bus = EventBus()
    bus.publish(Event(kind="t", channel="c", payload={"i": 0}))
    mid_ev = Event(kind="t", channel="c", payload={"i": 1})
    bus.publish(mid_ev)
    bus.publish(Event(kind="t", channel="c", payload={"i": 2}))
    later = bus.history("c", since=mid_ev.at)
    assert {e.payload["i"] for e in later} >= {1, 2}


def test_handler_exception_isolation():
    bus = EventBus()

    def boom(_e):
        raise RuntimeError("nope")

    good: list[Event] = []
    bus.subscribe("c", boom)
    bus.subscribe("c", good.append)
    bus.publish(Event(kind="k", channel="c"))
    assert len(good) == 1


def test_queue_subscription_blocking():
    bus = EventBus()
    q = QueueSubscription(bus, "c")

    def producer():
        bus.publish(Event(kind="t", channel="c", payload={"n": 1}))

    threading.Thread(target=producer).start()
    ev = q.get(timeout=2.0)
    assert ev.payload == {"n": 1}
    q.cancel()
