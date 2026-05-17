"""Tests for cowork.scheduler (F13)."""
import pytest
from cowork.scheduler import (
    nl_to_cron,
    ScheduledTask,
    TaskScheduler,
)


class TestNlToCron:
    def test_every_minute(self):
        assert nl_to_cron("every minute") == "* * * * *"

    def test_every_hour(self):
        assert nl_to_cron("every hour") == "0 * * * *"

    def test_every_n_minutes(self):
        assert nl_to_cron("every 15 minutes") == "*/15 * * * *"

    def test_every_n_hours(self):
        assert nl_to_cron("every 3 hours") == "0 */3 * * *"

    def test_every_day_at_hour_am(self):
        assert nl_to_cron("every day at 9am") == "0 9 * * *"

    def test_every_day_at_hour_pm(self):
        assert nl_to_cron("every day at 3pm") == "0 15 * * *"

    def test_every_morning(self):
        assert nl_to_cron("every morning") == "0 9 * * *"

    def test_every_weekday(self):
        assert nl_to_cron("every weekday") == "0 9 * * 1-5"

    def test_daily_shorthand(self):
        assert nl_to_cron("daily") == "0 9 * * *"

    def test_weekly_shorthand(self):
        assert nl_to_cron("weekly") == "0 9 * * 1"

    def test_monthly_shorthand(self):
        assert nl_to_cron("monthly") == "0 9 1 * *"

    def test_unknown_falls_back_to_daily(self):
        result = nl_to_cron("whenever it feels right")
        assert result == "0 9 * * *"


class TestScheduledTask:
    def test_to_dict_contains_id(self):
        t = ScheduledTask(name="t1", prompt="run report", cron_expr="0 9 * * *")
        d = t.to_dict()
        assert d["id"] == t.id
        assert d["cron_expr"] == "0 9 * * *"
        assert d["enabled"] is True


class TestTaskScheduler:
    def _sched(self) -> TaskScheduler:
        return TaskScheduler()

    def test_add_and_list(self):
        s = self._sched()
        t = s.add(ScheduledTask("t1", "prompt", "* * * * *"))
        assert len(s.list()) == 1
        assert s.get(t.id) is not None

    def test_remove(self):
        s = self._sched()
        t = s.add(ScheduledTask("t1", "p", "* * * * *"))
        assert s.remove(t.id) is True
        assert s.get(t.id) is None

    def test_remove_missing_returns_false(self):
        s = self._sched()
        assert s.remove("missing_id") is False

    def test_enable_disable(self):
        s = self._sched()
        t = s.add(ScheduledTask("t1", "p", "* * * * *"))
        s.disable(t.id)
        assert s.get(t.id).enabled is False
        s.enable(t.id)
        assert s.get(t.id).enabled is True

    def test_toggle(self):
        s = self._sched()
        t = s.add(ScheduledTask("t1", "p", "* * * * *"))
        original = t.enabled
        new_state = s.toggle(t.id)
        assert new_state is not original
        assert s.get(t.id).enabled is not original

    def test_toggle_missing_returns_none(self):
        s = self._sched()
        assert s.toggle("no_such") is None

    def test_mark_run(self):
        s = self._sched()
        t = s.add(ScheduledTask("t1", "p", "* * * * *"))
        s.mark_run(t.id, status="done")
        assert s.get(t.id).last_run is not None
        assert s.get(t.id).last_status == "done"
