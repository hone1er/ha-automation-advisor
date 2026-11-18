"""Replay an automation over the recorded history ("what would it have done?").

Pure Python, no Home Assistant imports. Every suggestion, from the statistics or from an AI, is
checked here before it is shown, and the numbers go on its card:

- fires:     times the triggers fired and the conditions passed
- actions:   times it would have changed something (the target was not already in that state)
- agreed:    ...and the target reached that state within 5 minutes anyway (you or an
             existing automation did the same thing), so it would have done what you wanted
- helpful:   it would have switched something off while nobody was in the room, earlier than
             it really went off (saved_min = minutes of light/power saved)
- unwanted:  it would have switched something on that you did not want on, or off while the
             room's motion sensors show someone there
- covered / manual: of your manual actions on the targets, how many it would have done for you

Supported: state / numeric_state / time / sun / time_pattern triggers; state / numeric_state /
time / sun / and / or / not conditions; turn_on / turn_off, covers, locks, climate hvac mode,
media play/pause. Anything else (templates, devices, choose, ...) makes the result "partial" or
"unsupported" and is reported as such, never guessed.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, tzinfo
import math
from typing import Any

from .analysis import MANUAL_CAUSES, MOTION_CLASSES, EntityInfo, Event

AGREE_S = 300.0
COVER_BEFORE_S = 120.0
LAG_S = 2.0
UNDO_S = 90.0  # reversed by hand this fast = you did not want it
PRESENCE_S = 120.0  # motion this soon after = someone was still there
OFF_STATES = {"off", "closed", "locked", "paused", "idle", "not_home"}
MAX_FIRES = 20000


class Unsupported(Exception):
    """The automation uses something the replay cannot evaluate."""


@dataclass
class BacktestResult:
    supported: bool = True
    partial: list[str] = field(default_factory=list)
    reason: str | None = None
    days: float = 0.0
    fires: int = 0
    actions: int = 0
    agreed: int = 0
    helpful: int = 0
    unwanted: int = 0
    unclear: int = 0
    saved_min: float = 0.0
    covered: int = 0
    manual: int = 0
    examples: list[str] = field(default_factory=list)

    @property
    def good_rate(self) -> float:
        return (self.agreed + self.helpful) / self.actions if self.actions else 0.0

    @property
    def unwanted_rate(self) -> float:
        return self.unwanted / self.actions if self.actions else 0.0

    def passes(self, min_actions: int = 3) -> bool:
        """Good enough to show an AI idea."""
        return (self.supported and self.actions >= min_actions
                and self.good_rate >= 0.6 and self.unwanted_rate <= 0.25)

    def summary(self) -> str:
        if not self.supported:
            return f"Could not replay this on your history ({self.reason})."
        if self.fires == 0:
            return f"Replayed on {self.days:.0f} days: it would never have run."
        if self.actions == 0:
            return (f"Replayed on {self.days:.0f} days: it would have run {self.fires} times "
                    "but never changed anything (things were already that way).")
        parts = [f"Replayed on {self.days:.0f} days: it would have acted {self.actions} times"]
        details = []
        if self.agreed:
            details.append(f"{self.agreed} matched what really happened")
        if self.helpful:
            details.append(f"{self.helpful} switched off an empty room earlier "
                           f"(≈{self.saved_min / 60:.1f} h less on)")
        if self.unwanted:
            details.append(f"{self.unwanted} you probably would not have wanted")
        if self.unclear:
            details.append(f"{self.unclear} unclear")
        text = parts[0] + (": " + ", ".join(details) if details else "") + "."
        if self.manual:
            text += (f" It would have done {self.covered} of your {self.manual} matching "
                     "manual actions for you.")
        if self.partial:
            text += " Not replayed: " + "; ".join(sorted(set(self.partial))) + "."
        return text

    def as_dict(self) -> dict:
        return {
            "supported": self.supported, "reason": self.reason, "partial": sorted(set(self.partial)),
            "days": round(self.days, 1), "fires": self.fires, "actions": self.actions,
            "agreed": self.agreed, "helpful": self.helpful, "unwanted": self.unwanted,
            "unclear": self.unclear, "saved_min": round(self.saved_min), "covered": self.covered,
            "manual": self.manual, "good_pct": round(100 * self.good_rate),
            "unwanted_pct": round(100 * self.unwanted_rate), "passes": self.passes(),
            "summary": self.summary(), "examples": self.examples[:5],
        }


# --------------------------------------------------------------------------------------------
# History access
# --------------------------------------------------------------------------------------------


class History:
    def __init__(self, events: list[Event], tz: tzinfo, sun: dict[date, tuple[float, float]],
                 entities: dict[str, EntityInfo]) -> None:
        self.tz, self.sun, self.entities = tz, sun, entities
        self.by: dict[str, list[Event]] = defaultdict(list)
        for e in sorted(events, key=lambda e: e.ts):
            if e.new != "triggered":
                self.by[e.entity_id].append(e)
        self.ts = {k: [e.ts for e in v] for k, v in self.by.items()}
        # What the replayed automation itself did: entity -> (state, until the next real change)
        self.overlay: dict[str, tuple[str, float]] = {}
        # Devices the replayed automation switches are read LAG_S before each moment: when an
        # existing automation did the same thing at that very instant, the replay must see the
        # state it was in just before, not the result.
        self.lagged: set[str] = set()
        self.start = min((e.ts for e in events), default=0.0)
        self.end = max((e.ts for e in events), default=0.0)

    def state(self, entity: str, t: float) -> str | None:
        if entity in self.lagged:
            t -= LAG_S
        if (ov := self.overlay.get(entity)) is not None and t < ov[1]:
            return ov[0]
        if entity == "sun.sun":
            times = self.sun.get(datetime.fromtimestamp(t, self.tz).date())
            if times is None:
                return None
            return "above_horizon" if times[0] <= t < times[1] else "below_horizon"
        evs = self.by.get(entity)
        if not evs:
            return None
        i = bisect.bisect_right(self.ts[entity], t)
        return evs[i - 1].new if i else evs[0].old

    def since(self, entity: str, t: float) -> float:
        """Time of the last change at or before t (start of history if none)."""
        i = bisect.bisect_right(self.ts.get(entity, []), t)
        return self.ts[entity][i - 1] if i else self.start

    def next_change(self, entity: str, t: float, raw: bool = False) -> Event | None:
        if entity in self.lagged and not raw:
            t -= LAG_S
        evs = self.by.get(entity, [])
        i = bisect.bisect_right(self.ts.get(entity, []), t)
        return evs[i] if i < len(evs) else None

    def days(self) -> list[date]:
        d0 = datetime.fromtimestamp(self.start, self.tz).date()
        d1 = datetime.fromtimestamp(self.end, self.tz).date()
        return [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]

    def at(self, d: date, hh: int, mm: int, ss: int = 0) -> float:
        return datetime.combine(d, dtime(hh, mm, ss), self.tz).timestamp()


# --------------------------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------------------------


def _list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _entities(v: Any) -> list[str]:
    out: list[str] = []
    for x in _list(v):
        if isinstance(x, str):
            out += [p.strip() for p in x.split(",") if p.strip()]
    if any("{" in e for e in out):
        raise Unsupported("templates")
    return out


def _seconds(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        return (float(v.get("days", 0)) * 86400 + float(v.get("hours", 0)) * 3600
                + float(v.get("minutes", 0)) * 60 + float(v.get("seconds", 0)))
    if isinstance(v, str):
        if "{" in v:
            raise Unsupported("templates")
        neg = v.startswith("-")
        parts = [float(p) for p in v.lstrip("-").split(":")]
        while len(parts) < 3:
            parts.append(0.0)
        s = parts[0] * 3600 + parts[1] * 60 + parts[2]
        return -s if neg else s
    raise Unsupported("unknown duration")


def _clock(v: str) -> tuple[int, int, int]:
    if not isinstance(v, str) or "." in v.split(":")[0] or "{" in v:
        raise Unsupported("times from helpers or templates")
    parts = [int(p) for p in v.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _section(cfg: dict, plural: str, singular: str) -> list:
    return _list(cfg.get(plural, cfg.get(singular)))


def _float(v: str | None) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------------
# Triggers
# --------------------------------------------------------------------------------------------


def _trigger_times(trig: dict, h: History) -> list[float]:
    kind = trig.get("trigger", trig.get("platform"))
    if kind == "state":
        if "attribute" in trig:
            raise Unsupported("attribute triggers")
        to = [str(x) for x in _list(trig.get("to"))] if "to" in trig else None
        frm = [str(x) for x in _list(trig.get("from"))] if "from" in trig else None
        hold = _seconds(trig.get("for"))
        out = []
        for ent in _entities(trig.get("entity_id")):
            evs = h.by.get(ent, [])
            for i, e in enumerate(evs):
                if to is not None and e.new not in to:
                    continue
                if frm is not None and (e.old or "") not in frm:
                    continue
                t = e.ts + hold
                if hold and i + 1 < len(evs) and evs[i + 1].ts <= t:
                    continue  # state did not last long enough
                if t <= h.end:
                    out.append(t)
        return out
    if kind == "numeric_state":
        out = []
        below, above = trig.get("below"), trig.get("above")
        if isinstance(below, str) or isinstance(above, str):
            raise Unsupported("numeric thresholds from helpers")
        hold = _seconds(trig.get("for"))
        for ent in _entities(trig.get("entity_id")):
            prev_ok = None
            evs = h.by.get(ent, [])
            for i, e in enumerate(evs):
                v = _float(e.new)
                ok = v is not None and (below is None or v < below) and (above is None or v > above)
                if ok and prev_ok is False:
                    t = e.ts + hold
                    if not (hold and i + 1 < len(evs) and evs[i + 1].ts <= t):
                        out.append(t)
                prev_ok = ok if v is not None else prev_ok
        return out
    if kind == "time":
        out = []
        for at in _list(trig.get("at")):
            hh, mm, ss = _clock(at)
            out += [h.at(d, hh, mm, ss) for d in h.days()]
        return [t for t in out if h.start <= t <= h.end]
    if kind == "sun":
        idx = {"sunrise": 0, "sunset": 1}.get(trig.get("event"))
        if idx is None:
            raise Unsupported("sun event")
        off = _seconds(trig.get("offset"))
        out = [times[idx] + off for d, times in h.sun.items()]
        return [t for t in out if h.start <= t <= h.end]
    if kind == "time_pattern":
        def match(spec: Any, value: int) -> bool:
            if spec in (None, "*"):
                return True
            spec = str(spec)
            if spec.startswith("/"):
                return value % int(spec[1:]) == 0
            return value == int(spec)
        out = []
        step = 60
        t = math.ceil(h.start / step) * step
        if "seconds" in trig and str(trig["seconds"]) not in ("0", "00"):
            raise Unsupported("second-level time patterns")
        hours, minutes = trig.get("hours"), trig.get("minutes")
        if hours is not None and minutes is None:
            minutes = 0
        while t <= h.end and len(out) < MAX_FIRES:
            local = datetime.fromtimestamp(t, h.tz)
            if match(hours, local.hour) and match(minutes, local.minute):
                out.append(float(t))
            t += step
        return out
    raise Unsupported(f"'{kind}' triggers")


# --------------------------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------------------------


def _condition(c: Any, t: float, h: History) -> bool:
    if isinstance(c, str):
        raise Unsupported("template conditions")
    kind = c.get("condition")
    if kind == "trigger":
        return str(getattr(h, "trigger_id", None)) in {str(x) for x in _list(c.get("id"))}
    if kind in ("and", "or", "not"):
        subs = [_condition(x, t, h) for x in _list(c.get("conditions"))]
        return all(subs) if kind == "and" else any(subs) if kind == "or" else not any(subs)
    if kind == "state":
        if "attribute" in c:
            raise Unsupported("attribute conditions")
        wanted = [str(x) for x in _list(c.get("state"))]
        hold = _seconds(c.get("for"))
        res = []
        for ent in _entities(c.get("entity_id")):
            cur = h.state(ent, t)
            ok = cur in wanted and (not hold or t - h.since(ent, t) >= hold)
            res.append(ok)
        return any(res) if c.get("match") == "any" else all(res)
    if kind == "numeric_state":
        below, above = c.get("below"), c.get("above")
        if isinstance(below, str) or isinstance(above, str):
            raise Unsupported("numeric thresholds from helpers")
        res = []
        for ent in _entities(c.get("entity_id")):
            v = _float(h.state(ent, t))
            res.append(v is not None and (below is None or v < below) and (above is None or v > above))
        return all(res)
    if kind == "time":
        local = datetime.fromtimestamp(t, h.tz)
        if c.get("weekday"):
            names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            if names[local.weekday()] not in _list(c["weekday"]):
                return False
        m = local.hour * 3600 + local.minute * 60 + local.second
        after = c.get("after")
        before = c.get("before")
        a = sum(x * y for x, y in zip(_clock(after), (3600, 60, 1))) if after else None
        b = sum(x * y for x, y in zip(_clock(before), (3600, 60, 1))) if before else None
        if a is not None and b is not None and a > b:  # wraps midnight
            return m >= a or m < b
        return (a is None or m >= a) and (b is None or m < b)
    if kind == "sun":
        d = datetime.fromtimestamp(t, h.tz).date()
        times = h.sun.get(d)
        if times is None:
            return False
        ok = True
        if after := c.get("after"):
            ok &= t >= times[0 if after == "sunrise" else 1] + _seconds(c.get("after_offset"))
        if before := c.get("before"):
            ok &= t < times[0 if before == "sunrise" else 1] + _seconds(c.get("before_offset"))
        return ok
    raise Unsupported(f"'{kind}' conditions")


# --------------------------------------------------------------------------------------------
# Actions -> expected states
# --------------------------------------------------------------------------------------------

SERVICE_STATE = {
    "turn_on": "on", "turn_off": "off", "open_cover": "open", "close_cover": "closed",
    "lock": "locked", "unlock": "unlocked", "media_play": "playing", "media_pause": "paused",
}


def _effects_at(actions: list, t: float, h: "History", partial: list[str]) -> list[tuple[str, str]]:
    """Effects of the actions for one run at time t: 'choose' picks its first option whose
    conditions hold (including trigger-id conditions); other steps as in _effects."""
    out: list[tuple[str, str]] = []
    for step in actions:
        if isinstance(step, dict) and "choose" in step:
            chosen = None
            for opt in _list(step["choose"]):
                if all(_condition(c, t, h) for c in _list(opt.get("conditions"))):
                    chosen = _list(opt.get("sequence"))
                    break
            if chosen is None:
                chosen = _list(step.get("default"))
            out += _effects_at(chosen, t, h, partial)
        else:
            out += _effects([step], partial)
    return out


def _all_effects(actions: list, partial: list[str]) -> list[tuple[str, str]]:
    """Every effect any branch could have (to know which devices to watch)."""
    out: list[tuple[str, str]] = []
    for step in actions:
        if isinstance(step, dict) and "choose" in step:
            for opt in _list(step["choose"]):
                out += _all_effects(_list(opt.get("sequence")), partial)
            out += _all_effects(_list(step.get("default")), partial)
        else:
            out += _effects([step], partial)
    return out


def _effects(actions: list, partial: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for step in actions:
        if not isinstance(step, dict):
            continue
        service = step.get("action", step.get("service"))
        if service is None:
            if "delay" in step:
                partial.append("delays (treated as instant)")
            elif "sequence" in step or "parallel" in step:
                for sub in _list(step.get("sequence")) + _list(step.get("parallel")):
                    out += _effects(_list(sub.get("sequence", sub)) if isinstance(sub, dict) else [], partial)
            else:
                partial.append("choose/if/wait steps")
            continue
        if "{" in str(service):
            partial.append("templated actions")
            continue
        domain, _, name = str(service).partition(".")
        target = step.get("target") or {}
        ents = _entities(target.get("entity_id", step.get("entity_id",
                                                           (step.get("data") or {}).get("entity_id"))))
        if (target.get("area_id") or target.get("device_id")) and not ents:
            partial.append("area/device targets")
            continue
        state = SERVICE_STATE.get(name)
        if domain == "climate" and name == "set_hvac_mode":
            state = (step.get("data") or {}).get("hvac_mode")
        if domain == "media_player" and name == "turn_on":
            state = None  # becomes idle or playing; cannot tell
        if domain in ("scene", "script", "notify", "persistent_notification", "tts"):
            continue  # no state we can check
        if name == "toggle":
            partial.append("toggles")
            continue
        if state is None:
            if name not in ("turn_on",):
                partial.append(f"{service}")
            continue
        out += [(e, state) for e in ents if e.split(".", 1)[0] == domain or domain == "homeassistant"]
    return out


# --------------------------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------------------------


def _presence_sensors(target: str, h: History, extra: set[str]) -> set[str]:
    info = h.entities.get(target)
    area = info.area if info else None
    room = {e for e, i in h.entities.items()
            if i.domain == "binary_sensor" and i.device_class in MOTION_CLASSES
            and area is not None and i.area == area}
    return room | {e for e in extra if (i := h.entities.get(e)) and i.device_class in MOTION_CLASSES}


def _someone_there(sensors: set[str], t: float, h: History) -> bool | None:
    if not sensors:
        return None
    for s in sensors:
        if h.state(s, t) == "on":
            return True
        nxt = h.next_change(s, t)
        if nxt is not None and nxt.new == "on" and nxt.ts - t <= PRESENCE_S:
            return True
    return False


def backtest(config: dict, events: list[Event], tz: tzinfo,
             sun: dict[date, tuple[float, float]], entities: dict[str, EntityInfo]) -> BacktestResult:
    h = History(events, tz, sun, entities)
    res = BacktestResult(days=max(0.0, (h.end - h.start) / 86400))
    try:
        triggers = _section(config, "triggers", "trigger")
        conditions = _section(config, "conditions", "condition")
        actions = _section(config, "actions", "action")
        effects = _all_effects(actions, res.partial)
        fires_at: dict[float, str] = {}
        for i, trig in enumerate(triggers):
            tid = str(trig.get("id", i))
            for t in _trigger_times(trig, h):
                fires_at.setdefault(round(t, 3), tid)
        times = sorted(fires_at)[:MAX_FIRES]
        referenced: set[str] = set()
        for trig in triggers:
            referenced |= set(_entities(trig.get("entity_id")))
        if not effects:
            raise Unsupported("no action whose result can be checked")
        h.lagged = {ent for ent, _ in effects}
        mode_single = config.get("mode", "single") == "single"
        last_fire = -math.inf
        fired: dict[tuple[str, str], list[float]] = defaultdict(list)
        for t in times:
            h.trigger_id = fires_at[t]
            if not all(_condition(c, t, h) for c in conditions):
                continue
            if mode_single and t - last_fire < 1:
                continue
            last_fire = t
            res.fires += 1
            for ent, state in _effects_at(actions, t, h, []):
                fired[(ent, state)].append(t)
                cur = h.state(ent, t)
                if cur == state:
                    continue
                res.actions += 1
                nxt = h.next_change(ent, t)
                # reached that state soon anyway (you, or something else, did the same)?
                if nxt is not None and nxt.new == state and nxt.ts - t <= AGREE_S:
                    after = h.next_change(ent, nxt.ts, raw=True)
                    if (after is not None and after.cause in MANUAL_CAUSES
                            and after.new != state and after.ts - nxt.ts <= UNDO_S):
                        res.unwanted += 1  # it happened, but you reversed it straight away
                        _example(res, h, t, f"{ent} {state}: you switched it back within "
                                            f"{int(after.ts - nxt.ts)} s")
                    else:
                        res.agreed += 1
                    continue
                if state in OFF_STATES:
                    there = _someone_there(_presence_sensors(ent, h, referenced), t, h)
                    if there is None:
                        res.unclear += 1
                    elif there:
                        res.unwanted += 1
                        _example(res, h, t, f"would switch {ent} {state} while someone was there")
                    else:
                        res.helpful += 1
                        end = nxt.ts if nxt is not None else h.end
                        res.saved_min += (end - t) / 60
                        # it stays off until something real happens to it next; an unwanted
                        # action, by contrast, is assumed undone at once (so repeats count)
                        h.overlay[ent] = (state, end)
                else:
                    res.unwanted += 1
                    _example(res, h, t, f"would switch {ent} {state} when you did not")
        # your manual actions it would have done for you
        for ent, state in set(effects):
            fires = sorted(fired.get((ent, state), []))
            for e in h.by.get(ent, []):
                if e.cause in MANUAL_CAUSES and e.new == state:
                    res.manual += 1
                    i = bisect.bisect_left(fires, e.ts - COVER_BEFORE_S)
                    if i < len(fires) and fires[i] <= e.ts + 5:
                        res.covered += 1
    except Unsupported as err:
        res.supported = False
        res.reason = str(err)
    return res


def _example(res: BacktestResult, h: History, t: float, what: str) -> None:
    if len(res.examples) < 5:
        when = datetime.fromtimestamp(t, h.tz).strftime("%a %d %b %H:%M")
        res.examples.append(f"{when}: {what}")


# --------------------------------------------------------------------------------------------
# Fitting a lead's time window to the history
# --------------------------------------------------------------------------------------------


def _score(r: BacktestResult) -> float:
    """Higher is better: what it gets right, minus twice what it gets wrong."""
    return r.agreed + r.helpful + 0.5 * r.covered - 2 * r.unwanted


def tune_window(config: dict, window: dict, events: list[Event], tz: tzinfo,
                sun: dict, entities: dict) -> tuple[dict, dict, BacktestResult] | None:
    """Try tighter versions of a lead's time window (later start, earlier end, 15-minute steps)
    and return (config, window, result) for the one that replays best, or None if the original
    is already best. The window condition is the last condition in the config."""
    from .analysis import Window

    if not window or window.get("mode") not in ("sun", "time"):
        return None
    base = backtest(config, events, tz, sun, entities)
    best = (_score(base), None, None, base)
    for d_start in (0, 15, 30, 45, 60):
        for d_end in (0, -15, -30, -45, -60, -90):
            if d_start == 0 and d_end == 0:
                continue
            w = Window(window["mode"], (window["start"] + d_start) % (1440 if window["mode"] == "time" else 10**6),
                       window["end"] + d_end if window["mode"] == "sun" else (window["end"] + d_end) % 1440, 0.0)
            cfg = {**config, "conditions": [*config.get("conditions", [])[:-1], w.condition()]}
            r = backtest(cfg, events, tz, sun, entities)
            if r.supported and _score(r) > best[0]:
                best = (_score(r), cfg, {"mode": w.mode, "start": w.start, "end": w.end,
                                         "text": w.describe()}, r)
    return None if best[1] is None else (best[1], best[2], best[3])


def tune_delay(config: dict, events: list[Event], tz: tzinfo, sun: dict,
               entities: dict) -> tuple[dict, int, BacktestResult] | None:
    """For an 'off after N minutes without motion' lead: try other N against the history and
    return (config, minutes, result) for the best, or None if the original is already best."""
    trig = (config.get("triggers") or [{}])[0]
    if not isinstance(trig.get("for"), dict) or "minutes" not in trig["for"]:
        return None
    base = backtest(config, events, tz, sun, entities)
    best = (_score(base), None, None, base)
    for minutes in (2, 3, 4, 5, 7, 10, 15, 20, 30):
        if minutes == trig["for"]["minutes"]:
            continue
        cfg = {**config, "triggers": [{**trig, "for": {"minutes": minutes}},
                                      *config["triggers"][1:]]}
        r = backtest(cfg, events, tz, sun, entities)
        if r.supported and _score(r) > best[0]:
            best = (_score(r), cfg, minutes, r)
    return None if best[1] is None else (best[1], best[2], best[3])
