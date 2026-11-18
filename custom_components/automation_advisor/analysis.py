"""Pattern mining for Automation Advisor.

Pure Python: no Home Assistant imports, so every rule can be unit-tested with synthetic event
logs. The Home Assistant layer (manager.py) feeds it events from the archive, entity metadata,
sunrise/sunset times and the existing automations, and turns the returned suggestions into UI
items.

Vocabulary
- trigger entity: something the user does not control directly (motion, door, person arriving).
- target entity: something the user switches (light, switch, fan, input_boolean, cover).
- manual event: a target change made by a person (wall switch, app, voice), not by an automation.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
import hashlib
import json
import math
from statistics import median

CAUSE_MANUAL = "manual"  # device-reported (wall switch, remote) or not attributable
CAUSE_USER = "user"  # a Home Assistant user (dashboard, app, voice)
CAUSE_AUTOMATION = "automation"  # an automation's action (cause_ref = automation entity id)
CAUSE_OTHER = "other"  # script, scene or another integration acting on someone's behalf
MANUAL_CAUSES = frozenset({CAUSE_MANUAL, CAUSE_USER})

TARGET_DOMAINS = frozenset({"light", "switch", "fan", "input_boolean", "cover"})
TARGET_STATES = {
    "light": ("on", "off"),
    "switch": ("on", "off"),
    "fan": ("on", "off"),
    "input_boolean": ("on", "off"),
    "cover": ("open", "closed"),
}
SERVICES = {
    ("cover", "open"): "cover.open_cover",
    ("cover", "closed"): "cover.close_cover",
}
# Domains whose habits usually follow daylight (wall switches often drive lights).
SUN_DOMAINS = frozenset({"light", "switch"})
MOTION_CLASSES = frozenset({"motion", "occupancy", "presence"})
OPENING_CLASSES = frozenset({"door", "garage_door", "opening", "window"})
PRESENCE_DOMAINS = frozenset({"person", "device_tracker"})

# Home Assistant keeps a service call's context on the entity for this long
# (helpers/entity.py CONTEXT_RECENT_TIME_SECONDS).
CONTEXT_REUSE_S = 5.0

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

KIND_CREATE = "create"
KIND_REVIEW = "review"


# --------------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Event:
    """One state transition (old != new) of a tracked entity."""

    ts: float  # UTC epoch seconds
    entity_id: str
    old: str | None
    new: str
    cause: str = CAUSE_MANUAL
    cause_ref: str | None = None  # automation entity id when cause == automation


@dataclass(slots=True)
class EntityInfo:
    """What the analysis needs to know about an entity."""

    entity_id: str
    name: str
    device_class: str | None = None
    area: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


@dataclass(slots=True)
class ExistingAutomation:
    """An automation that already exists in Home Assistant."""

    entity_id: str  # automation.xyz
    alias: str
    config_id: str | None = None  # id in automations.yaml
    editable: bool = False  # stored in automations.yaml (UI-managed)
    enabled: bool = True
    trigger_entities: frozenset[str] = frozenset()
    trigger_platforms: frozenset[str] = frozenset()
    action_entities: frozenset[str] = frozenset()
    config: dict | None = None


@dataclass(slots=True)
class Settings:
    """Tunable thresholds. Defaults are deliberately conservative: a wrong suggestion costs
    the user's trust more than a missed one."""

    min_occurrences: int = 5  # how many times a pattern must have happened
    min_days: int = 3  # ... on at least this many different days
    min_confidence: float = 0.6
    motion_window_s: float = 60.0  # manual action this soon after motion counts as a reaction
    opening_window_s: float = 90.0
    arrival_window_s: float = 300.0
    override_window_s: float = 90.0  # user undoing an automation this fast = override
    routine_tolerance_min: float = 20.0
    routine_min_day_ratio: float = 0.7
    routine_min_days: int = 5
    window_coverage: float = 0.9  # a time window must contain this share of the user's actions
    # Test mode: pretend no automations exist, so what they do counts as done by hand.
    ignore_existing: bool = False


@dataclass(slots=True)
class Suggestion:
    """A proposed new automation, or a proposed change to an existing one."""

    kind: str  # KIND_CREATE | KIND_REVIEW
    category: str  # trigger_action | auto_off | routine | overridden | broken
    fingerprint: str  # stable identity across runs (dedupe, dismissed memory)
    title: str
    description: str
    confidence: float
    evidence: dict = field(default_factory=dict)
    automation: dict | None = None  # config to create (create) or to replace with (review)
    target_automation: str | None = None  # automation entity id (review)
    options: list[str] = field(default_factory=list)  # accept | modify | delete | disable

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "category": self.category,
            "fingerprint": self.fingerprint,
            "title": self.title,
            "description": self.description,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
            "automation": self.automation,
            "target_automation": self.target_automation,
            "options": list(self.options),
        }


@dataclass(slots=True)
class Context:
    """Everything one analysis run looks at."""

    events: list[Event]
    entities: dict[str, EntityInfo]
    automations: list[ExistingAutomation]
    tz: tzinfo
    # local date -> (sunrise_ts, sunset_ts); missing days fall back to clock windows
    sun: dict[date, tuple[float, float]] = field(default_factory=dict)
    settings: Settings = field(default_factory=Settings)
    now: float | None = None


def fingerprint(*parts: object) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def config_hash(config: dict | None) -> str:
    return fingerprint(config or {})


# --------------------------------------------------------------------------------------------
# Time windows
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Window:
    """When the user's actions happen: a clock window or the dark part of the day."""

    mode: str  # "time" | "sun"
    start: int  # minutes after midnight (time) / minutes relative to sunset (sun)
    end: int  # minutes after midnight (time) / minutes relative to sunrise (sun)
    coverage: float

    def contains(self, ts: float, tz: tzinfo, sun: dict) -> bool:
        local = datetime.fromtimestamp(ts, tz)
        if self.mode == "time":
            m = local.hour * 60 + local.minute
            if self.start <= self.end:
                return self.start <= m < self.end
            return m >= self.start or m < self.end
        times = sun.get(local.date())
        if times is None:
            return False
        sunrise, sunset = times
        return ts >= sunset + self.start * 60 or ts < sunrise + self.end * 60

    def condition(self) -> dict:
        if self.mode == "time":
            return {
                "condition": "time",
                "after": _hhmm(self.start),
                "before": _hhmm(self.end),
            }
        # "after sunset OR before sunrise" needs two sun conditions in an "or".
        return {
            "condition": "or",
            "conditions": [
                {"condition": "sun", "after": "sunset", "after_offset": _offset(self.start)},
                {"condition": "sun", "before": "sunrise", "before_offset": _offset(self.end)},
            ],
        }

    def describe(self) -> str:
        if self.mode == "time":
            return f"between {_hhmm(self.start)[:5]} and {_hhmm(self.end)[:5]}"
        return f"from {_rel('sunset', self.start)} until {_rel('sunrise', self.end)}"

    def as_dict(self) -> dict:
        return {"mode": self.mode, "start": self.start, "end": self.end,
                "coverage": round(self.coverage, 3), "text": self.describe()}


def _hhmm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def _offset(minutes: int) -> str:
    sign = "-" if minutes < 0 else ""
    m = abs(minutes)
    return f"{sign}{m // 60:02d}:{m % 60:02d}:00"


def _rel(event: str, minutes: int) -> str:
    if minutes == 0:
        return event
    m = abs(minutes)
    amount = f"{m // 60} h {m % 60} min" if m >= 60 and m % 60 else (
        f"{m // 60} h" if m >= 60 else f"{m} min")
    return f"{amount} {'before' if minutes < 0 else 'after'} {event}"


def _floor(x: float, step: int = 15) -> int:
    return int(math.floor(x / step) * step)


def _ceil(x: float, step: int = 15) -> int:
    return int(math.ceil(x / step) * step)


def _quantile(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        raise ValueError("empty")
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _minute_of_day(ts: float, tz: tzinfo) -> int:
    local = datetime.fromtimestamp(ts, tz)
    return local.hour * 60 + local.minute


def fit_clock_window(ts_list: list[float], tz: tzinfo, coverage: float = 0.9) -> Window | None:
    """Smallest clock window (may wrap midnight) holding `coverage` of the times, padded by
    15 min and rounded to quarter hours. None if that window is most of the day anyway."""
    if not ts_list:
        return None
    mins = sorted(_minute_of_day(t, tz) for t in ts_list)
    n = len(mins)
    k = max(1, math.ceil(coverage * n))
    best = None
    for i in range(n):
        j = i + k - 1
        start = mins[i]
        end = mins[j] if j < n else mins[j - n] + 1440
        length = end - start
        if best is None or length < best[0]:
            best = (length, start, end)
    _, start, end = best
    start = _floor(start - 15)
    end = _ceil(end + 15 + 1)
    if end - start >= 20 * 60:
        return None
    win = Window("time", start % 1440, end % 1440, 0.0)
    win.coverage = sum(win.contains(t, tz, {}) for t in ts_list) / n
    return win


def fit_dark_window(
    ts_list: list[float], tz: tzinfo, sun: dict, coverage: float = 0.9
) -> Window | None:
    """Window 'from sunset + a until sunrise + b' that holds `coverage` of the times, or None
    if the times are not concentrated in the dark part of the day."""
    evening: list[float] = []
    morning: list[float] = []
    usable = 0
    for t in ts_list:
        times = sun.get(datetime.fromtimestamp(t, tz).date())
        if times is None:
            continue
        usable += 1
        sunrise, sunset = times
        noon = (sunrise + sunset) / 2
        if t >= noon:
            evening.append((t - sunset) / 60)
        else:
            morning.append((t - sunrise) / 60)
    if usable < max(3, 0.8 * len(ts_list)):
        return None
    tail = (1 - coverage) / 2
    start = _floor(_quantile(evening, tail) - 15) if evening else 0
    end = _ceil(_quantile(morning, 1 - tail) + 15) if morning else 0
    # Only a real "dark" pattern: starting no earlier than 4 h before sunset, ending no later
    # than 4 h after sunrise. Otherwise the clock window describes it better.
    if start < -240 or end > 240:
        return None
    win = Window("sun", start, end, 0.0)
    win.coverage = sum(win.contains(t, tz, sun) for t in ts_list) / len(ts_list)
    return win if win.coverage >= coverage - 0.05 else None


def fit_window(ts_list: list[float], ctx: Context, prefer_sun: bool) -> Window | None:
    cov = ctx.settings.window_coverage
    if prefer_sun:
        win = fit_dark_window(ts_list, ctx.tz, ctx.sun, cov)
        if win is not None:
            return win
    win = fit_clock_window(ts_list, ctx.tz, cov)
    if win is not None and win.coverage >= cov - 0.05:
        return win
    return None


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def trigger_kind(info: EntityInfo) -> tuple[str, str, float] | None:
    """(kind, trigger state, reaction window) for an entity that can trigger, else None."""
    s = Settings()
    if info.domain == "binary_sensor":
        if info.device_class in MOTION_CLASSES:
            return ("motion", "on", s.motion_window_s)
        if info.device_class in OPENING_CLASSES:
            return ("opening", "on", s.opening_window_s)
    if info.domain in PRESENCE_DOMAINS:
        return ("arrival", "home", s.arrival_window_s)
    return None


def _window_for(kind: str, settings: Settings) -> float:
    return {
        "motion": settings.motion_window_s,
        "opening": settings.opening_window_s,
        "arrival": settings.arrival_window_s,
    }[kind]


def service_for(entity_id: str, state: str) -> str:
    domain = entity_id.split(".", 1)[0]
    if (domain, state) in SERVICES:
        return SERVICES[(domain, state)]
    return f"{domain}.turn_{'on' if state == 'on' else 'off'}"


def _name(ctx: Context, entity_id: str) -> str:
    info = ctx.entities.get(entity_id)
    return info.name if info else entity_id


def _verb(entity_id: str, state: str) -> str:
    domain = entity_id.split(".", 1)[0]
    if domain == "cover":
        return "open" if state == "open" else "close"
    return f"turn {state}"


def _local_date(ts: float, tz: tzinfo) -> date:
    return datetime.fromtimestamp(ts, tz).date()


def _covered(ctx: Context, trigger: str | None, target: str, platforms: set[str] | None = None) -> bool:
    """True if an existing automation already links trigger -> target (or, with `platforms`,
    already acts on target from a time/sun trigger)."""
    for auto in ctx.automations:
        if target not in auto.action_entities:
            continue
        if trigger is not None and trigger in auto.trigger_entities:
            return True
        if platforms and auto.trigger_platforms & platforms:
            return True
    return False


def _by_entity(events: Iterable[Event]) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = defaultdict(list)
    for ev in events:
        out[ev.entity_id].append(ev)
    for lst in out.values():
        lst.sort(key=lambda e: e.ts)
    return out


def _times_before(sorted_ts: list[float], t: float, window: float) -> bool:
    """Is there a timestamp in [t - window, t]? (binary search)"""
    import bisect

    i = bisect.bisect_right(sorted_ts, t)
    return i > 0 and t - sorted_ts[i - 1] <= window


def _description_footer(stats: dict) -> str:
    return " ".join(f"{k}: {v}." for k, v in stats.items())


# --------------------------------------------------------------------------------------------
# Detector 1: "when X happens you usually switch Y" (motion / door / arrival -> target)
# --------------------------------------------------------------------------------------------


def detect_trigger_action(ctx: Context) -> list[Suggestion]:
    s = ctx.settings
    by_entity = _by_entity(ctx.events)

    triggers: dict[str, tuple[str, str, list[float]]] = {}
    for eid, evs in by_entity.items():
        info = ctx.entities.get(eid)
        if info is None:
            continue
        tk = trigger_kind(info)
        if tk is None:
            continue
        kind, state, _ = tk
        times = [e.ts for e in evs if e.new == state and e.old != state]
        if times:
            triggers[eid] = (kind, state, times)

    out: list[Suggestion] = []
    for target, evs in by_entity.items():
        domain = target.split(".", 1)[0]
        if domain not in TARGET_DOMAINS:
            continue
        on_state = TARGET_STATES[domain][0]
        manual = [e for e in evs if e.cause in MANUAL_CAUSES and e.new == on_state]
        if len(manual) < s.min_occurrences:
            continue

        best = None
        for trig, (kind, _state, ttimes) in triggers.items():
            window = _window_for(kind, s)
            hits = [e.ts for e in manual if _times_before(ttimes, e.ts, window)]
            if len(hits) < s.min_occurrences:
                continue
            days = {_local_date(t, ctx.tz) for t in hits}
            if len(days) < s.min_days:
                continue
            ratio = len(hits) / len(manual)
            same_area = (
                ctx.entities.get(trig) is not None
                and ctx.entities.get(target) is not None
                and ctx.entities[trig].area is not None
                and ctx.entities[trig].area == ctx.entities[target].area
            )
            key = (ratio + (0.05 if same_area else 0.0), len(hits))
            if best is None or key > best[0]:
                best = (key, trig, kind, hits, ratio, days)
        if best is None:
            continue
        _, trig, kind, hits, ratio, days = best
        if ratio < s.min_confidence or _covered(ctx, trig, target):
            continue

        # Does the user only react at certain times? Look at all trigger events: if many of them
        # fall outside the window in which the user reacts, the automation needs that window.
        ttimes = triggers[trig][2]
        window = fit_window(hits, ctx, prefer_sun=domain in SUN_DOMAINS)
        condition = None
        if window is not None:
            outside = sum(not window.contains(t, ctx.tz, ctx.sun) for t in ttimes)
            if outside / len(ttimes) >= 0.15:
                condition = window

        # How often does the trigger fire *inside the window* without the user reacting?
        # (e.g. lights already on). Reported as evidence, it lowers confidence a little.
        in_window = [t for t in ttimes if condition is None or condition.contains(t, ctx.tz, ctx.sun)]
        tw = _window_for(kind, s)
        hit_set = sorted(hits)
        reacted = sum(1 for t in in_window if _after_within(hit_set, t, tw))
        follow_rate = reacted / len(in_window) if in_window else 0.0
        confidence = min(1.0, 0.7 * ratio + 0.3 * min(1.0, len(hits) / (3 * s.min_occurrences)))

        tname, gname = _name(ctx, trig), _name(ctx, target)
        when = {"motion": "motion is detected by", "opening": "it opens:",
                "arrival": "this person arrives home:"}[kind]
        title = f"{_verb(target, on_state).capitalize()} {gname} when {_short_trigger(kind, tname)}"
        desc = (
            f"{len(hits)} of the {len(manual)} times you turned on {gname} "
            f"({round(100 * ratio)} %), {tname} had triggered within the "
            f"{int(tw)} s before, on {len(days)} different days."
        )
        if condition is not None:
            desc += (
                f" You only do this {condition.describe()}, so the automation is limited to "
                f"that time."
            )
        triggers_cfg = [_state_trigger(trig, triggers[trig][1], kind)]
        conditions = [condition.condition()] if condition else []
        automation = {
            "alias": title,
            "description": f"Suggested by Automation Advisor. {desc}",
            "mode": "single",
            "triggers": triggers_cfg,
            "conditions": conditions,
            "actions": [{"action": service_for(target, on_state), "target": {"entity_id": target}}],
        }
        out.append(
            Suggestion(
                kind=KIND_CREATE,
                category="trigger_action",
                fingerprint=fingerprint("trigger_action", trig, target, on_state),
                title=title,
                description=desc,
                confidence=confidence,
                evidence={
                    "trigger": trig,
                    "target": target,
                    "trigger_kind": kind,
                    "matches": len(hits),
                    "manual_actions": len(manual),
                    "days": len(days),
                    "ratio": round(ratio, 3),
                    "trigger_follow_rate": round(follow_rate, 3),
                    "window": condition.as_dict() if condition else None,
                },
                automation=automation,
                options=["accept"],
            )
        )
    return out


def _short_trigger(kind: str, name: str) -> str:
    if kind == "motion":
        return f"{name} detects motion"
    if kind == "opening":
        return f"{name} opens"
    return f"{name} arrives home"


def _state_trigger(entity_id: str, to_state: str, kind: str) -> dict:
    trig = {"trigger": "state", "entity_id": entity_id, "to": to_state}
    if kind == "arrival":
        trig["from"] = "not_home"
    else:
        trig["from"] = "off"
    return trig


def _after_within(sorted_ts: list[float], t: float, window: float) -> bool:
    """Is there a timestamp in [t, t + window]?"""
    import bisect

    i = bisect.bisect_left(sorted_ts, t)
    return i < len(sorted_ts) and sorted_ts[i] - t <= window


# --------------------------------------------------------------------------------------------
# Detector 2: "turn it off when nobody has been there for N minutes"
# --------------------------------------------------------------------------------------------


def _periods(evs: list[Event], on_state: str) -> list[tuple[float, float, Event]]:
    """(on_ts, off_ts, off_event) for every completed on-period."""
    out = []
    on_ts = None
    for e in evs:
        if e.new == on_state and on_ts is None:
            on_ts = e.ts
        elif e.new != on_state and on_ts is not None:
            out.append((on_ts, e.ts, e))
            on_ts = None
    return out


def _motion_intervals(evs: list[Event]) -> list[tuple[float, float]]:
    """(on, off) intervals of a motion sensor; an open interval ends at +inf."""
    out = []
    start = None
    for e in evs:
        if e.new == "on" and start is None:
            start = e.ts
        elif e.new == "off" and start is not None:
            out.append((start, e.ts))
            start = None
    if start is not None:
        out.append((start, math.inf))
    return out


def detect_auto_off(ctx: Context) -> list[Suggestion]:
    s = ctx.settings
    by_entity = _by_entity(ctx.events)
    motion = {
        eid: _motion_intervals(evs)
        for eid, evs in by_entity.items()
        if (info := ctx.entities.get(eid)) is not None
        and info.domain == "binary_sensor"
        and info.device_class in MOTION_CLASSES
    }
    if not motion:
        return []

    out = []
    for target, evs in by_entity.items():
        domain = target.split(".", 1)[0]
        if domain not in ("light", "switch", "fan"):
            continue
        periods = _periods(evs, "on")
        if len(periods) < s.min_occurrences:
            continue

        # The motion sensor that is most often active just before the target turns on.
        best = None
        for sensor, ivals in motion.items():
            starts = sorted(a for a, _ in ivals)
            n = sum(1 for on_ts, _, _ in periods if _times_before(starts, on_ts, s.motion_window_s)
                    or _active(ivals, on_ts))
            if best is None or n > best[0]:
                best = (n, sensor)
        if best is None or best[0] < s.min_occurrences or best[0] / len(periods) < s.min_confidence:
            continue
        sensor = best[1]
        if _covered_off(ctx, sensor, target):
            continue
        ivals = motion[sensor]

        # For periods the user ended by hand: how long after the room went quiet?
        gaps: list[float] = []
        forgotten = 0
        for on_ts, off_ts, off_ev in periods:
            if off_ev.cause not in MANUAL_CAUSES:
                continue
            quiet_since = _quiet_since(ivals, off_ts)
            if quiet_since is None:
                gaps.append(0.0)  # switched off while motion still active (leaving the room)
                continue
            gap = (off_ts - max(quiet_since, on_ts)) / 60
            gaps.append(gap)
            if gap >= 30:
                forgotten += 1
        if len(gaps) < s.min_occurrences:
            continue

        delay = int(min(30, max(5, _ceil(_quantile(gaps, 0.75), 5))))
        # Risk: times the room was quiet for >= delay while the light stayed on and then motion
        # came back before the user switched off (someone sitting still). Auto-off would have
        # switched the light off on them.
        risky = 0
        for on_ts, off_ts, _ in periods:
            if _quiet_then_back(ivals, on_ts, off_ts, delay * 60):
                risky += 1
        risk = risky / len(periods)
        if risk > 0.25:
            continue
        consistent = median(gaps) <= 15
        if not (consistent or forgotten >= 3):
            continue
        confidence = max(0.0, min(1.0, 0.6 + 0.1 * min(4, len(gaps) / s.min_occurrences) - risk))
        if confidence < s.min_confidence:
            continue

        tname, gname = _name(ctx, target), _name(ctx, sensor)
        title = f"Turn off {tname} after {delay} min without motion"
        if forgotten >= 3:
            why = (f"{tname} was left on 30 min or more after {gname} last saw motion "
                   f"{forgotten} times.")
        else:
            why = (f"You usually switch {tname} off within {round(median(gaps))} min of the "
                   f"room going quiet ({len(gaps)} times).")
        desc = (f"{why} Turning it off after {delay} min without motion would have interrupted "
                f"someone sitting still {risky} times.")
        automation = {
            "alias": title,
            "description": f"Suggested by Automation Advisor. {desc}",
            "mode": "single",
            "triggers": [{"trigger": "state", "entity_id": sensor, "to": "off",
                          "for": {"minutes": delay}}],
            "conditions": [{"condition": "state", "entity_id": target, "state": "on"}],
            "actions": [{"action": service_for(target, "off"), "target": {"entity_id": target}}],
        }
        out.append(Suggestion(
            kind=KIND_CREATE,
            category="auto_off",
            fingerprint=fingerprint("auto_off", sensor, target),
            title=title,
            description=desc,
            confidence=confidence,
            evidence={"trigger": sensor, "target": target, "delay_min": delay,
                      "manual_offs": len(gaps), "median_gap_min": round(median(gaps), 1),
                      "forgotten": forgotten, "would_interrupt": risky},
            automation=automation,
            options=["accept"],
        ))
    return out


def _active(ivals: list[tuple[float, float]], t: float) -> bool:
    return any(a <= t <= b for a, b in ivals)


def _quiet_since(ivals: list[tuple[float, float]], t: float) -> float | None:
    """When motion last ended before t; None if motion is active at t."""
    last_end = None
    for a, b in ivals:
        if a <= t <= b:
            return None
        if b <= t:
            last_end = b if last_end is None else max(last_end, b)
    return last_end


def _quiet_then_back(ivals, on_ts: float, off_ts: float, delay_s: float) -> bool:
    """Inside [on_ts, off_ts], a quiet stretch >= delay_s that ended with new motion."""
    prev_end = None
    for a, b in ivals:
        if b < on_ts or a > off_ts:
            continue
        if prev_end is not None and a - prev_end >= delay_s and a < off_ts:
            return True
        prev_end = b
    return False


def _covered_off(ctx: Context, sensor: str, target: str) -> bool:
    for auto in ctx.automations:
        if target in auto.action_entities and sensor in auto.trigger_entities:
            cfg = json.dumps(auto.config or {})
            if "turn_off" in cfg or '"off"' in cfg:
                return True
    return False


# --------------------------------------------------------------------------------------------
# Detector 3: daily routines ("you switch the porch light on around sunset every day")
# --------------------------------------------------------------------------------------------


def linked_sensors(ctx: Context, extra: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    """Trigger entities that belong to each target: same area, used together in an existing
    automation, or paired by a suggestion in this run."""
    out: dict[str, set[str]] = defaultdict(set)
    triggers = [i for i in ctx.entities.values() if trigger_kind(i) is not None]
    for info in ctx.entities.values():
        if info.domain in TARGET_DOMAINS and info.area:
            out[info.entity_id] |= {t.entity_id for t in triggers if t.area == info.area}
    for auto in ctx.automations:
        for target in auto.action_entities:
            out[target] |= {t for t in auto.trigger_entities if t in ctx.entities
                            and trigger_kind(ctx.entities[t]) is not None}
    for target, sensors in (extra or {}).items():
        out[target] |= sensors
    return out


def _reactions(ctx: Context, target: str, state: str, times: list[float],
               linked: dict[str, set[str]], by_entity: dict[str, list[Event]]) -> set[float]:
    """Manual actions that are reactions to the target's own sensors: switching on right after
    they fire, or off within 30 min of them going quiet. Those are not routines."""
    s = ctx.settings
    out: set[float] = set()
    on_state = TARGET_STATES[target.split(".", 1)[0]][0]
    for sensor in linked.get(target, ()):
        info = ctx.entities.get(sensor)
        tk = trigger_kind(info) if info else None
        if tk is None:
            continue
        kind, trig_state, _ = tk
        evs = by_entity.get(sensor, [])
        if state == on_state:
            starts = [e.ts for e in evs if e.new == trig_state]
            window = _window_for(kind, s)
        else:
            starts = [e.ts for e in evs if e.new != trig_state]
            window = 30 * 60
        out |= {t for t in times if _times_before(starts, t, window)}
    return out


def detect_routines(ctx: Context, exclude: set[tuple[str, str]],
                    linked: dict[str, set[str]] | None = None) -> list[Suggestion]:
    s = ctx.settings
    linked = linked if linked is not None else linked_sensors(ctx)
    if not ctx.events:
        return []
    first = min(e.ts for e in ctx.events)
    last = max(e.ts for e in ctx.events) if ctx.now is None else ctx.now
    all_days = [
        _local_date(first, ctx.tz) + timedelta(days=i)
        for i in range((_local_date(last, ctx.tz) - _local_date(first, ctx.tz)).days + 1)
    ]
    if len(all_days) < max(7, s.routine_min_days):
        return []

    out = []
    by_entity = _by_entity(ctx.events)
    for target, evs in by_entity.items():
        domain = target.split(".", 1)[0]
        if domain not in TARGET_DOMAINS:
            continue
        for state in TARGET_STATES[domain]:
            if (target, state) in exclude:
                continue
            times = [e.ts for e in evs if e.cause in MANUAL_CAUSES and e.new == state]
            reactions = _reactions(ctx, target, state, times, linked, by_entity)
            times = [t for t in times if t not in reactions]
            if len(times) < s.routine_min_days:
                continue
            found = _best_routine(times, ctx, all_days)
            if found is None:
                continue
            mode, anchor, offset_min, days, weekday_only = found
            if _covered(ctx, None, target, {"time", "sun", "time_pattern"}):
                continue
            ratio = len(days) / (len([d for d in all_days if d.weekday() < 5])
                                 if weekday_only else len(all_days))
            name = _name(ctx, target)
            if mode == "time":
                when = f"at {_hhmm(offset_min)[:5]}"
                trigger = {"trigger": "time", "at": _hhmm(offset_min)}
            else:
                when = _rel(anchor, offset_min)
                trigger = {"trigger": "sun", "event": anchor, "offset": _offset(offset_min)}
            if weekday_only:
                when += " on weekdays"
            title = f"{_verb(target, state).capitalize()} {name} {when}"
            desc = (f"You did this by hand on {len(days)} of the last "
                    f"{len(all_days)} days, always within {int(s.routine_tolerance_min)} min "
                    f"of the same time.")
            conditions = []
            if weekday_only:
                conditions.append({"condition": "time", "weekday": WEEKDAYS[:5]})
            automation = {
                "alias": title,
                "description": f"Suggested by Automation Advisor. {desc}",
                "mode": "single",
                "triggers": [trigger],
                "conditions": conditions,
                "actions": [{"action": service_for(target, state),
                             "target": {"entity_id": target}}],
            }
            out.append(Suggestion(
                kind=KIND_CREATE,
                category="routine",
                fingerprint=fingerprint("routine", target, state),
                title=title,
                description=desc,
                confidence=min(1.0, ratio),
                evidence={"target": target, "state": state, "days": len(days),
                          "days_observed": len(all_days), "mode": mode, "anchor": anchor,
                          "offset_min": offset_min, "weekdays_only": weekday_only},
                automation=automation,
                options=["accept"],
            ))
    return out


def _best_routine(times: list[float], ctx: Context, all_days: list[date]):
    """Find the densest daily time (clock or sun-relative) and return
    (mode, anchor, minutes, covered_days, weekday_only) or None."""
    s = ctx.settings
    tol = s.routine_tolerance_min
    candidates: list[tuple[str, str, list[tuple[float, date]]]] = []
    candidates.append(("time", "", [(_minute_of_day(t, ctx.tz), _local_date(t, ctx.tz))
                                    for t in times]))
    for anchor, idx in (("sunrise", 0), ("sunset", 1)):
        pts = []
        for t in times:
            d = _local_date(t, ctx.tz)
            if d in ctx.sun:
                pts.append(((t - ctx.sun[d][idx]) / 60, d))
        if pts:
            candidates.append(("sun", anchor, pts))

    weekdays = [d for d in all_days if d.weekday() < 5]
    best = None
    for mode, anchor, pts in candidates:
        pts.sort()
        vals = [p[0] for p in pts]
        n = len(pts)
        j = 0
        for i in range(n):
            while j < n and vals[j] - vals[i] <= 2 * tol:
                j += 1
            inside = pts[i:j]
            if mode == "time" and vals[i] + 2 * tol >= 1440:  # wrap past midnight
                inside = inside + [p for p in pts if p[0] + 1440 - vals[i] <= 2 * tol]
            days = {d for _, d in inside}
            center = median(v for v, _ in inside)
            if mode == "sun" and abs(center) > 180:
                continue
            all_ratio = len(days) / len(all_days)
            wd_days = {d for d in days if d.weekday() < 5}
            wd_ratio = len(wd_days) / len(weekdays) if weekdays else 0
            weekend_days = len(days) - len(wd_days)
            weekday_only = (
                all_ratio < s.routine_min_day_ratio
                and wd_ratio >= s.routine_min_day_ratio
                and weekend_days <= 1
            )
            score = max(all_ratio, wd_ratio if weekday_only else 0)
            if score < s.routine_min_day_ratio or len(days) < s.routine_min_days:
                continue
            # Prefer more days, then the anchor the user tracks more tightly (sunset-relative
            # times wander on the clock as the seasons change and vice versa), then clock time.
            inside_vals = [v for v, _ in inside]
            spread = _quantile(inside_vals, 0.9) - _quantile(inside_vals, 0.1)
            key = (round(score, 2), -round(spread / 5), 0 if mode == "sun" else 1)
            if best is None or key > best[0]:
                minutes = int(round(center / 5) * 5)
                best = (key, (mode, anchor, minutes % 1440 if mode == "time" else minutes,
                              wd_days if weekday_only else days, weekday_only))
    return None if best is None else best[1]


# --------------------------------------------------------------------------------------------
# Review 1: an automation the user keeps undoing
# --------------------------------------------------------------------------------------------


def automation_runs(ctx: Context) -> dict[tuple[str, str], list[tuple[Event, Event | None]]]:
    """For every (automation, entity): each change the automation made, paired with the
    manual change that undid it within override_window_s (or None if it was kept)."""
    s = ctx.settings
    runs: dict[tuple[str, str], list[tuple[Event, Event | None]]] = defaultdict(list)
    for entity, evs in _by_entity(ctx.events).items():
        consumed: set[Event] = set()
        for i, e in enumerate(evs):
            if e.cause != CAUSE_AUTOMATION or not e.cause_ref or e in consumed:
                continue
            undo = None
            for later in evs[i + 1:]:
                if later.ts - e.ts > s.override_window_s:
                    break
                # For 5 s after a service call Home Assistant stamps the entity's own state
                # writes with that call's context, so a wall-switch undo within 5 s looks like
                # the automation reversing itself. Treat it as the person's undo.
                echo = (later.cause == CAUSE_AUTOMATION and later.cause_ref == e.cause_ref
                        and later.ts - e.ts <= CONTEXT_REUSE_S)
                if later.cause not in MANUAL_CAUSES and not echo:
                    break
                if later.new != e.new:
                    undo = later
                    consumed.add(later)
                    break
            runs[(e.cause_ref, entity)].append((e, undo))
    return runs


def without_undone_runs(ctx: Context) -> Context:
    """The context with every 'automation did it, you immediately undid it' episode removed.
    Those undo presses are reactions to an automation, not habits, and must not turn into
    'you switch the hall light off at 13:05 every day' suggestions."""
    drop: set[Event] = set()
    for items in automation_runs(ctx).values():
        for e, undo in items:
            if undo is not None:
                drop.add(e)
                drop.add(undo)
    if not drop:
        return ctx
    return Context(
        events=[e for e in ctx.events if e not in drop],
        entities=ctx.entities,
        automations=ctx.automations,
        tz=ctx.tz,
        sun=ctx.sun,
        settings=ctx.settings,
        now=ctx.now,
    )


def detect_overridden(ctx: Context) -> list[Suggestion]:
    s = ctx.settings
    autos = {a.entity_id: a for a in ctx.automations}
    out = []
    runs = {
        key: [(e, undo is not None) for e, undo in items]
        for key, items in automation_runs(ctx).items()
    }

    for (auto_id, entity), items in runs.items():
        total = len(items)
        undone = [e for e, u in items if u]
        kept = [e for e, u in items if not u]
        if total < min(4, s.min_occurrences) or len(undone) / total < 0.5:
            continue
        auto = autos.get(auto_id)
        alias = auto.alias if auto else auto_id
        ename = _name(ctx, entity)
        ratio = len(undone) / total
        state = undone[0].new
        desc = (f"\"{alias}\" set {ename} to {state} {total} times; you undid it within "
                f"{int(s.override_window_s)} s {len(undone)} times ({round(100 * ratio)} %).")
        options = []
        new_config = None
        window = None
        if auto and auto.editable and auto.config and len(kept) >= 3:
            window = fit_window([e.ts for e in kept], ctx, prefer_sun=entity.split(".", 1)[0] in SUN_DOMAINS)
            if window is not None:
                excluded = sum(not window.contains(e.ts, ctx.tz, ctx.sun) for e in undone)
                if excluded / len(undone) >= 0.7:
                    new_config = _with_condition(auto.config, window.condition())
                    desc += (f" The times you kept it were all {window.describe()}; limiting the "
                             f"automation to that window would have avoided {excluded} of the "
                             f"{len(undone)} undos.")
                else:
                    window = None
        if new_config is not None:
            options.append("modify")
        if auto and auto.editable:
            options.append("delete")
        if auto is None or auto.enabled:
            options.append("disable")
        if new_config is None:
            desc += (" No time window explains when you keep it, so deleting or disabling it "
                     "is suggested.")
        title = (f"Adjust \"{alias}\": you keep undoing it" if new_config
                 else f"Remove \"{alias}\"? You keep undoing it")
        out.append(Suggestion(
            kind=KIND_REVIEW,
            category="overridden",
            fingerprint=fingerprint("overridden", auto_id, entity,
                                    config_hash(auto.config if auto else None)),
            title=title,
            description=desc,
            confidence=min(1.0, ratio),
            evidence={"automation": auto_id, "entity": entity, "runs": total,
                      "config_hash": config_hash(auto.config if auto else None),
                      "undone": len(undone), "ratio": round(ratio, 3),
                      "window": window.as_dict() if window else None},
            automation=new_config,
            target_automation=auto_id,
            options=options,
        ))
    return out


# --------------------------------------------------------------------------------------------
# Review 2: a light that stays on long after everyone left (the off-automation misses cases)
# --------------------------------------------------------------------------------------------

STUCK_QUIET_S = 30 * 60  # quiet this long while still on = stuck
STUCK_MIN_CASES = 2


def _merge(ivals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(ivals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _longest_quiet(motion: list[tuple[float, float]], start: float, end: float) -> tuple[float, float]:
    """Longest stretch inside [start, end] with no motion: (length_s, quiet_from)."""
    best, best_from, cursor = 0.0, start, start
    for a, b in motion:
        if b < start:
            continue
        if a > end:
            break
        if a > cursor and a - cursor > best:
            best, best_from = a - cursor, cursor
        cursor = max(cursor, b)
    if end > cursor and end - cursor > best:
        best, best_from = end - cursor, cursor
    return best, best_from


def _off_delay_minutes(config: dict | None) -> int | None:
    for trig in (config or {}).get("triggers", (config or {}).get("trigger", [])) or []:
        if isinstance(trig, dict) and str(trig.get("to")) == "off" and trig.get("for"):
            f = trig["for"]
            if isinstance(f, dict):
                return int(f.get("hours", 0)) * 60 + int(f.get("minutes", 0)) or None
            if isinstance(f, str) and f.count(":") == 2:
                h, m, _ = f.split(":")
                return int(h) * 60 + int(m) or None
    return None


def detect_stuck_on(ctx: Context) -> list[Suggestion]:
    autos = {a.entity_id: a for a in ctx.automations}
    by_entity = _by_entity(ctx.events)
    now = ctx.now or (ctx.events[-1].ts if ctx.events else 0)
    motion_ids = {e for e, i in ctx.entities.items()
                  if i.domain == "binary_sensor" and i.device_class in MOTION_CLASSES}
    out: list[Suggestion] = []
    handled: set[str] = set()
    # Lights before plugs/switches, short names first: "Basement" rather than "Smart plug 2".
    order = sorted(by_entity, key=lambda t: (not t.startswith("light."), len(_name(ctx, t)), t))
    for target in order:
        evs = by_entity[target]
        domain = target.split(".", 1)[0]
        if domain not in ("light", "switch", "fan"):
            continue
        # Automations that switch this target off, and how often each ended an on-period.
        offs = [e for e in evs if e.new == "off" and e.cause == CAUSE_AUTOMATION and e.cause_ref]
        if not offs:
            continue
        off_auto = max({e.cause_ref for e in offs}, key=lambda a: sum(e.cause_ref == a for e in offs))
        if off_auto in handled:
            continue  # the same automation also switches the other lights in the room
        # The room's motion sensors: any motion sensor used by an automation acting on target.
        sensors = set()
        for a in ctx.automations:
            if target in a.action_entities:
                sensors |= a.trigger_entities & motion_ids
        sensors |= {i.entity_id for i in ctx.entities.values() if i.entity_id in motion_ids
                    and i.area and ctx.entities.get(target) and i.area == ctx.entities[target].area}
        if not sensors:
            continue
        motion = _merge([iv for sensor in sensors
                         for iv in _motion_intervals(by_entity.get(sensor, []))])
        periods = _periods(evs, "on")
        if evs and evs[-1].new == "on":  # still on now
            periods.append((evs[-1].ts, now, evs[-1]))
        stuck = []
        for on_ts, off_ts, _ in periods:
            quiet, quiet_from = _longest_quiet(motion, on_ts, off_ts)
            if quiet >= STUCK_QUIET_S:
                on_ev = next((e for e in evs if e.ts == on_ts), None)
                stuck.append((on_ts, off_ts, quiet, on_ev.cause_ref if on_ev else None))
        days = {_local_date(s_[0], ctx.tz) for s_ in stuck}
        if len(stuck) < STUCK_MIN_CASES or len(days) < 2:
            continue
        handled.add(off_auto)
        starters = Counter(s_[3] for s_ in stuck if s_[3])
        culprit, culprit_n = starters.most_common(1)[0] if starters else (None, 0)
        hours = sum(s_[2] for s_ in stuck) / 3600
        longest = max(s_[2] for s_ in stuck) / 3600
        auto = autos.get(off_auto)
        alias = auto.alias if auto else off_auto
        tname = _name(ctx, target)
        sensor_names = ", ".join(sorted(_name(ctx, x) for x in sensors))
        desc = (f"{tname} stayed on with no motion for 30 min or more {len(stuck)} times in "
                f"{len(periods)} on-periods (≈{hours:.0f} h in total, longest {longest:.0f} h). ")
        still = periods and periods[-1][1] == now and stuck and stuck[-1][1] == now
        if still:
            desc += f"It is on right now, with no motion for {stuck[-1][2] / 3600:.0f} h. "
        if culprit:
            cname = autos[culprit].alias if culprit in autos else culprit
            desc += (f"In {culprit_n} of these cases it was switched on by \"{cname}\". "
                     f"\"{alias}\" only starts its timer when one of its sensors goes quiet, so "
                     f"if the light comes on while that sensor already is quiet, nothing turns it off. ")
        delay = _off_delay_minutes(auto.config if auto else None) or 5
        desc += (f"The fix: also start the timer when the light turns on, require all of the "
                 f"room's motion sensors ({sensor_names}) to be quiet for {delay} min, and re-check "
                 f"every 10 minutes.")
        # Lights the off-automation switches: keep its actions if we have them.
        off_targets = sorted({e.entity_id for es in by_entity.values() for e in es
                              if e.cause_ref == off_auto and e.new == "off"
                              and e.entity_id.split(".", 1)[0] in ("light", "switch", "fan")})
        actions = (auto.config.get("actions", auto.config.get("action"))
                   if auto and auto.config else None) or [
            {"action": f"{d}.turn_off", "target": {"entity_id": [t for t in off_targets if t.startswith(d + ".")]}}
            for d in sorted({t.split(".", 1)[0] for t in off_targets})
        ]
        sensor_list = sorted(sensors)
        robust = {
            "alias": alias if auto and auto.editable else f"{tname}: off when no motion (safety net)",
            "description": ("Turns the lights off once every motion sensor in the room has been "
                            f"quiet for {delay} min, however the lights were switched on. "
                            "Rewritten by Automation Advisor."),
            "mode": "single",
            "triggers": [
                {"trigger": "state", "entity_id": sensor_list, "to": "off", "for": {"minutes": delay}},
                {"trigger": "state", "entity_id": target, "to": "on", "for": {"minutes": delay}},
                {"trigger": "time_pattern", "minutes": "/10"},
            ],
            "conditions": [
                {"condition": "state", "entity_id": sensor_list, "state": "off", "for": {"minutes": delay}},
                {"condition": "state", "entity_id": target, "state": "on"},
            ],
            "actions": actions,
        }
        editable = bool(auto and auto.editable and auto.config)
        if editable:
            robust = {**{k: v for k, v in auto.config.items() if k == "id"}, **robust}
        out.append(Suggestion(
            kind=KIND_REVIEW if editable else KIND_CREATE,
            category="stuck_on",
            fingerprint=fingerprint("stuck_on", off_auto, target,
                                    config_hash(auto.config if auto else None)),
            title=(f"Fix \"{alias}\": {tname} stays on for hours" if editable
                   else f"Safety net: turn off {tname} when the room is empty"),
            description=desc,
            confidence=min(1.0, 0.5 + 0.1 * len(stuck)),
            evidence={"automation": off_auto, "target": target, "stuck": len(stuck),
                      "periods": len(periods), "hours": round(hours, 1),
                      "longest_h": round(longest, 1), "started_by": culprit,
                      "sensors": sensor_list, "still_on": bool(still),
                      "config_hash": config_hash(auto.config if auto else None)},
            automation=robust,
            target_automation=off_auto if editable else None,
            options=["modify"] if editable else ["accept"],
        ))
    return out


def _with_condition(config: dict, condition: dict) -> dict:
    new = json.loads(json.dumps(config))
    key = "conditions" if "conditions" in new or "condition" not in new else "condition"
    existing = new.get(key) or []
    if isinstance(existing, dict):
        existing = [existing]
    new[key] = [*existing, condition]
    note = "Time window added by Automation Advisor."
    new["description"] = (f"{new.get('description', '')} {note}").strip()
    return new


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------


def blank_slate(ctx: Context) -> Context:
    """The home as if it had no automations (test mode): their actions become manual, their
    'triggered' rows disappear, and the automation list is empty."""
    events = [e if e.cause != CAUSE_AUTOMATION else
              Event(e.ts, e.entity_id, e.old, e.new, CAUSE_MANUAL, None)
              for e in ctx.events
              if e.new != "triggered" and not e.entity_id.startswith("automation.")]
    settings = Settings(**{**{f: getattr(ctx.settings, f) for f in ctx.settings.__dataclass_fields__},
                           "ignore_existing": False})
    return Context(events=events, entities={k: v for k, v in ctx.entities.items()
                                            if not k.startswith("automation.")},
                   automations=[], tz=ctx.tz, sun=ctx.sun, settings=settings, now=ctx.now)


def mirrored_targets(ctx: Context) -> dict[str, str]:
    """Entities that always switch together (a light group and its bulb, a plug exposed as both
    switch and light): {secondary: primary}. Suggestions are made for the primary only."""
    import bisect

    seqs: dict[str, dict[str, list[float]]] = {}
    for eid, evs in _by_entity(ctx.events).items():
        if eid.split(".", 1)[0] in TARGET_DOMAINS and len(evs) >= 4:
            per: dict[str, list[float]] = defaultdict(list)
            for e in evs:
                per[e.new].append(e.ts)
            seqs[eid] = per

    def matches(a: dict[str, list[float]], b: dict[str, list[float]]) -> int:
        n = 0
        for state, times in a.items():
            other = b.get(state, [])
            for t in times:
                i = bisect.bisect_left(other, t - 2)
                if i < len(other) and other[i] <= t + 2:
                    n += 1
        return n

    rank = lambda t: (not t.startswith("light."), len(_name(ctx, t)), t)  # noqa: E731
    ids = sorted(seqs, key=rank)
    size = {e: sum(len(v) for v in seqs[e].values()) for e in ids}
    out: dict[str, str] = {}
    for i, a in enumerate(ids):
        if a in out:
            continue
        for b in ids[i + 1:]:
            if b in out or not 0.8 <= size[a] / size[b] <= 1.25:
                continue
            if matches(seqs[a], seqs[b]) / max(size[a], size[b]) >= 0.9:
                out[b] = a
    return out


def analyze(ctx: Context) -> list[Suggestion]:
    """Run every detector. Order matters only for deduplication: a routine is not proposed for
    something already explained by a trigger."""
    results: list[Suggestion] = []
    habits = without_undone_runs(ctx)
    if ctx.settings.ignore_existing:
        habits = Context(
            events=[e if e.cause != CAUSE_AUTOMATION else
                    Event(e.ts, e.entity_id, e.old, e.new, CAUSE_MANUAL, None)
                    for e in habits.events if e.new != "triggered"],
            entities=ctx.entities, automations=[], tz=ctx.tz, sun=ctx.sun,
            settings=ctx.settings, now=ctx.now,
        )
    ta = detect_trigger_action(habits)
    off = detect_auto_off(habits)
    results += ta + off
    # A routine is not proposed for an action another pattern already explains (switching the
    # light off when leaving can look like "off at 19:35 every day" in a regular household).
    explained = {(sg.evidence["target"], "on") for sg in ta}
    explained |= {(sg.evidence["target"], "off") for sg in off}
    explained |= {
        (sg.evidence["target"], "open") for sg in ta if sg.evidence["target"].startswith("cover.")
    }
    paired: dict[str, set[str]] = defaultdict(set)
    for sg in ta + off:
        paired[sg.evidence["target"]].add(sg.evidence["trigger"])
    results += detect_routines(habits, explained, linked_sensors(habits, paired))
    if ctx.settings.ignore_existing:
        for sg in results:
            sg.evidence["test_mode"] = True
            sg.fingerprint = fingerprint("test", sg.fingerprint)
            sg.description = ("Test mode (your existing automations were ignored): "
                              + sg.description)
    results += detect_overridden(ctx)
    results += detect_stuck_on(ctx)
    mirrors = mirrored_targets(ctx)
    if mirrors:
        kept = []
        for sg in results:
            primary = mirrors.get(sg.evidence.get("target") or sg.evidence.get("entity") or "")
            if primary is None:
                kept.append(sg)
            elif not any((o.evidence.get("target") or o.evidence.get("entity")) == primary
                         and o.category == sg.category for o in results):
                kept.append(sg)  # the primary has no twin suggestion; keep this one
        results = kept
    results.sort(key=lambda sg: (sg.kind != KIND_REVIEW, -sg.confidence))
    return results


def filter_events(events: Iterable[Event], since: float) -> list[Event]:
    return [e for e in events if e.ts >= since]


EntityLookup = Callable[[str], EntityInfo | None]


# --------------------------------------------------------------------------------------------
# Run report: what was looked at, and why nothing (more) was suggested
# --------------------------------------------------------------------------------------------


def diagnose(ctx: Context, found: list[Suggestion]) -> dict:
    """A plain-language summary of one run, so 'no suggestions' is never a silent answer."""
    s = ctx.settings
    by_entity = _by_entity(ctx.events)
    report: dict = {"events": len(ctx.events)}
    if ctx.events:
        first, last = ctx.events[0].ts, ctx.events[-1].ts
        report["days"] = max(1, round((last - first) / 86400))
    else:
        report["days"] = 0

    targets = [e for e in by_entity if e.split(".", 1)[0] in TARGET_DOMAINS]
    sensors = [e for e in by_entity if (i := ctx.entities.get(e)) and trigger_kind(i)]
    manual = {
        t: sum(1 for e in by_entity[t] if e.cause in MANUAL_CAUSES) for t in targets
    }
    automated = {
        t: sum(1 for e in by_entity[t] if e.cause == CAUSE_AUTOMATION) for t in targets
    }
    report["targets"] = len(targets)
    report["sensors"] = len(sensors)
    report["manual_actions"] = sum(manual.values())
    report["automated_actions"] = sum(automated.values())

    # How the existing automations are doing (undo rate), one line per automation.
    per_auto: dict[str, list[int]] = {}
    for (auto_id, _entity), items in automation_runs(ctx).items():
        runs, undone = len(items), sum(1 for _, u in items if u)
        if auto_id not in per_auto or runs > per_auto[auto_id][0]:
            per_auto[auto_id] = [runs, undone]
    aliases = {a.entity_id: a.alias for a in ctx.automations}
    health = [
        {"automation": a, "alias": aliases.get(a, a), "runs": r, "undone": u,
         "kept_pct": round(100 * (r - u) / r) if r else 100}
        for a, (r, u) in per_auto.items()
    ]
    health.sort(key=lambda h: (h["kept_pct"], -h["runs"]))
    report["automations"] = health[:12]

    # Near misses: patterns that exist but are below the thresholds.
    suggested = {sg.evidence.get("target") for sg in found} | {
        sg.target_automation for sg in found
    }
    covered_by: dict[str, list[str]] = defaultdict(list)
    for auto in ctx.automations:
        for t in auto.action_entities:
            covered_by[t].append(auto.alias)
    notes: list[str] = []
    seen_names: set[str] = set()
    for t in sorted(targets, key=lambda t: -manual[t]):
        n = manual[t]
        if n == 0 or t in suggested or _name(ctx, t) in seen_names:
            continue
        seen_names.add(_name(ctx, t))  # a plug exposed as both switch and light: say it once
        days = len({_local_date(e.ts, ctx.tz) for e in by_entity[t] if e.cause in MANUAL_CAUSES})
        if covered_by.get(t):
            names = ", ".join(sorted(set(covered_by[t]))[:2])
            notes.append(
                f"{_name(ctx, t)}: switched by hand {n} time(s), but already automated "
                f"({names}), so similar ideas are not suggested again."
            )
        elif n < s.min_occurrences or days < s.min_days:
            notes.append(
                f"{_name(ctx, t)}: switched by hand {n} time(s) on {days} day(s); a pattern "
                f"needs at least {s.min_occurrences} times on {s.min_days} different days."
            )
        else:
            notes.append(
                f"{_name(ctx, t)}: switched by hand {n} times on {days} days, but not "
                f"consistently enough after one sensor or at one time of day to suggest an "
                f"automation (needs {round(100 * s.min_confidence)} % consistency)."
            )
    for h in health:
        if h["automation"] in suggested or not h["undone"]:
            continue
        pct = 100 - h["kept_pct"]
        if pct >= 10:
            notes.append(
                f"{h['alias']}: you undid it {h['undone']} of {h['runs']} times ({pct} %); "
                f"a review is suggested from 50 %."
            )
    report["near_misses"] = notes[:6]

    if found:
        report["headline"] = f"{len(found)} suggestion(s) from {report['events']} events."
    elif not ctx.events:
        report["headline"] = "No activity recorded yet."
    elif report["manual_actions"] == 0 and report["automated_actions"]:
        report["headline"] = ("Nothing to suggest: your tracked lights and switches were only "
                              "switched by automations in this period.")
    elif all(covered_by.get(t) for t in targets if manual[t]) and report["manual_actions"]:
        report["headline"] = ("Nothing new: the lights and switches you use by hand are already "
                              "covered by your automations.")
    elif health and all(h["kept_pct"] >= 90 for h in health) and report["manual_actions"] < 2 * s.min_occurrences:
        report["headline"] = ("Nothing to suggest: your automations already cover what happens, "
                              "and you rarely switch things by hand.")
    else:
        report["headline"] = "No pattern was strong enough to suggest yet."
    return report
