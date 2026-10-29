"""Pure wall-clock policy, independent of process restarts and network reconnects."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import Settings

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    morning_eligible: bool
    evening_due: bool
    morning_missed: bool
    wakeup_at: datetime
    reason: str


class Schedule:
    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(self, now: datetime, boot_at: datetime, *, previously_awake: bool = False) -> Decision:
        if now.tzinfo is None or boot_at.tzinfo is None:
            raise ValueError("schedule requires timezone-aware timestamps")
        now, boot_at = now.astimezone(SHANGHAI), boot_at.astimezone(SHANGHAI)
        cfg = self.settings
        bedtime = datetime.combine(now.date(), time.fromisoformat(cfg.bedtime), SHANGHAI)
        previous_bedtime = bedtime - timedelta(days=1)
        rebooted_overnight = boot_at >= previous_bedtime
        wake = cfg.reboot_wakeup if rebooted_overnight else cfg.continuous_wakeup
        wakeup = datetime.combine(now.date(), time.fromisoformat(wake), SHANGHAI)
        cutoff = datetime.combine(now.date(), time.fromisoformat(cfg.morning_cutoff), SHANGHAI)
        allowed = not cfg.routine_enabled or wakeup <= now < bedtime
        morning = False
        # Legacy decision fields remain false: greetings are ordinary DS conversation now.
        evening = False
        return Decision(allowed, morning, evening, now >= cutoff, wakeup,
                        "awake" if allowed else "night_pause")
