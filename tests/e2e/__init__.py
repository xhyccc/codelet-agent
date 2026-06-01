"""Deterministic end-to-end regression tests for codelet agent iterations.

These tests script :class:`codelet.FakeModelClient` to exercise full
``MiniAgent.ask`` loops without any network access, locking in the behaviour
added across the tuning iterations (multi-tool batching, file-read dedup,
progress circuit-breaker, argument repair, and skills metadata).
"""
