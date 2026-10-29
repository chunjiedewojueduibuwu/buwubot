from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from buwubot.config import Settings
from buwubot.schedule import Schedule


def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.mark.parametrize("now,boot,allowed,morning", [
    ("2026-10-21T06:00", "2026-10-21T06:00", False, False),
    ("2026-10-21T06:30", "2026-10-21T06:00", True, True),
    ("2026-10-21T07:00", "2026-10-20T10:00", False, False),
    ("2026-10-21T07:30", "2026-10-20T10:00", True, True),
    ("2026-10-21T11:59", "2026-10-21T11:59", True, True),
    ("2026-10-21T12:00", "2026-10-21T12:00", True, False),
    ("2026-10-21T12:01", "2026-10-21T12:01", True, False),
    ("2026-10-21T22:30", "2026-10-21T06:00", False, False),
    ("2026-10-21T23:00", "2026-10-21T23:00", False, False),
])
def test_wakeup_depends_on_system_boot_not_process_restart(now, boot, allowed, morning):
    decision = Schedule(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'])).evaluate(dt(now), dt(boot))
    assert decision.allowed is allowed
    assert decision.morning_eligible is False


def test_disabling_routine_keeps_chat_running_and_no_late_morning():
    schedule = Schedule(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], routine_enabled=False))
    assert schedule.evaluate(dt("2026-10-21T23:00"), dt("2026-10-20T10:00")).allowed
    assert not schedule.evaluate(dt("2026-10-21T12:01"), dt("2026-10-21T12:00")).morning_eligible


def test_schedule_never_requests_fixed_greetings():
    schedule = Schedule(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']))
    assert not schedule.evaluate(dt("2026-10-21T22:30"), dt("2026-10-21T06:00"),
                             previously_awake=True).evening_due
    assert not schedule.evaluate(dt("2026-10-21T23:00"), dt("2026-10-21T23:00")).evening_due
