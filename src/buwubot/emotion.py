"""Pure feeling/retention curves; storage and explicit recall are coordinated elsewhere."""

import math
from dataclasses import replace

from .memory_dynamics import DAY, advance, half_lives


def feeling_view(anchor,policy,*,now):
    """V2: one emotional residue, followed by significance-dependent retention."""
    meaning=.65*anchor.initial_significance+.35*anchor.initial_emotion
    retention_days=policy.low_days+(policy.high_days-policy.low_days)/(1+math.exp(-12*(meaning-.55)))
    curve=replace(policy.curve(),base_half_life=retention_days*DAY,
                  minimum_half_life=policy.low_days*DAY,maximum_half_life=policy.high_days*DAY,
                  emotion_half_life=(policy.half_life_minutes or 36500*1440)*60)
    current=advance(anchor,curve,now=now)
    lives=half_lives(anchor,curve)
    active=max(0,math.log2(anchor.emotion/policy.perceptibility)*lives['emotion']) if anchor.emotion>policy.perceptibility else 0
    elapsed=max(0,now-anchor.since)
    if policy.half_life_minutes is None:
        current.update(emotion=anchor.emotion,remaining=anchor.remaining)
    else:
        current['emotion']=current['emotion'] if elapsed<active else 0.
        immortal=policy.permanent_threshold is not None and anchor.initial_emotion>=policy.permanent_threshold
        current['remaining']=anchor.remaining if elapsed<=active or immortal else (
            anchor.remaining*2**-min(1075,(elapsed-active)/lives['meaning']))
        if current['remaining']<policy.forget_threshold:
            current['remaining']=0.
    return current


def reinforce(strength: float, gain: float = .15) -> float:
    if not all(math.isfinite(x) and 0 <= x <= 1 for x in (strength, gain)):
        raise ValueError("strength and recall gain must be finite fractions")
    return strength + (1 - strength) * gain


def emotion_levels(*, strength: float, since: float, now: float,
                   half_life_minutes: float = 30, low_days: float = 2,
                   high_days: float = 365, permanent_threshold: float | None = None) -> tuple[float, float]:
    """Return current feeling and remaining memory, both fractions. No writes or deletion.

    Feeling reaches zero at the 1% perceptibility threshold. Only then does
    retention fade via smoothstep; a logistic curve maps strength to lifetime.
    """
    if (not all(math.isfinite(x) for x in (strength, since, now, half_life_minutes, low_days, high_days))
            or not 0 <= strength <= 1 or half_life_minutes <= 0 or not 0 < low_days <= high_days
            or (permanent_threshold is not None and
                (not math.isfinite(permanent_threshold) or not 0 < permanent_threshold <= 1))):
        raise ValueError("invalid emotion curve parameters")
    elapsed = max(0, now - since)
    half_life = half_life_minutes * 60
    active_seconds = max(0, math.log2(strength / .01) * half_life) if strength > .01 else 0
    feeling = strength * 2 ** -min(1024, elapsed / half_life) if elapsed < active_seconds else 0.0
    if feeling or (permanent_threshold is not None and strength >= permanent_threshold):
        return feeling, 1.0
    lifetime_days = low_days + (high_days - low_days) / (1 + math.exp(-14 * (strength - .65)))
    progress = min(1.0, max(0.0, elapsed - active_seconds) / (lifetime_days * 86400))
    remaining = (1 - progress) ** 2 * (1 + 2 * progress)
    return feeling, remaining
