"""A compact, readable description of the home for the AI (pure Python, unit-tested).

Sized for a local model's context window (about 4-5k tokens): the device list with rooms, the
existing automations in one line each, and a few typical days as timelines where every change is
marked with who did it. Motion is collapsed to "motion" once per 5 minutes per sensor.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, tzinfo
import json

from .analysis import (
    CAUSE_AUTOMATION, CAUSE_USER, MANUAL_CAUSES, MOTION_CLASSES, Context, ExistingAutomation, Event,
)

MAX_TIMELINE_LINES = 90  # per day
MAX_DAYS = 3
MAX_AUTOMATION_CHARS = 260
MOTION_COLLAPSE_S = 300


def _kind(ctx: Context, eid: str) -> str:
    info = ctx.entities.get(eid)
    domain = eid.split(".", 1)[0]
    if domain in ("binary_sensor", "sensor") and info and info.device_class:
        return f"{domain}/{info.device_class}"
    return domain


def _name(ctx: Context, eid: str) -> str:
    info = ctx.entities.get(eid)
    return info.name if info else eid


def _auto_line(a: ExistingAutomation) -> str:
    cfg = a.config or {}
    body = {k: cfg[k] for k in ("triggers", "trigger", "conditions", "condition", "actions", "action")
            if cfg.get(k)}
    text = json.dumps(body, separators=(",", ":"), default=str)
    if len(text) > MAX_AUTOMATION_CHARS:
        text = text[:MAX_AUTOMATION_CHARS] + "…"
    state = "" if a.enabled else " [off]"
    return f'- {a.entity_id} "{a.alias}"{state}: {text}'


def _pick_days(ctx: Context) -> list[date]:
    days: dict[date, int] = defaultdict(int)
    for e in ctx.events:
        days[datetime.fromtimestamp(e.ts, ctx.tz).date()] += 1
    if not days:
        return []
    complete = sorted(days)[1:-1] or sorted(days)  # skip the partial first/last day
    picked = complete[-2:]  # two most recent full days
    weekend = [d for d in complete if d.weekday() >= 5 and d not in picked]
    if weekend:
        picked.append(weekend[-1])
    busiest = max((d for d in complete if d not in picked), key=lambda d: days[d], default=None)
    if busiest:
        picked.append(busiest)
    return sorted(picked)[:MAX_DAYS]


def _who(e: Event, ctx: Context) -> str:
    if e.cause == CAUSE_AUTOMATION:
        autos = {a.entity_id: a.alias for a in ctx.automations}
        name = autos.get(e.cause_ref, e.cause_ref or "?")
        return f" (auto: {name[:32]})"
    if e.cause == CAUSE_USER:
        return " (by you, in the app)"
    if e.cause in MANUAL_CAUSES and e.entity_id.split(".", 1)[0] in (
            "light", "switch", "fan", "cover", "lock", "media_player", "climate", "input_boolean"):
        return " (by hand)"
    return ""


def timeline(ctx: Context, day: date, skip: set[str] | None = None) -> list[str]:
    start = datetime.combine(day, datetime.min.time(), ctx.tz).timestamp()
    end = start + 86400
    last_motion: dict[str, float] = {}
    lines = []
    for e in ctx.events:
        if not start <= e.ts < end or e.new == "triggered" or e.entity_id in (skip or ()):
            continue
        if e.entity_id.startswith("automation."):
            continue
        info = ctx.entities.get(e.entity_id)
        if e.entity_id.startswith("binary_sensor."):
            # one line per sensor per 5 minutes of activity; "off" adds nothing for the AI
            if e.new != "on" or e.ts - last_motion.get(e.entity_id, 0) < MOTION_COLLAPSE_S:
                if e.new == "on":
                    last_motion[e.entity_id] = e.ts
                continue
            last_motion[e.entity_id] = e.ts
            what = "motion" if info and info.device_class in MOTION_CLASSES else "on (opened/active)"
        elif e.entity_id.startswith("sensor."):
            what = f"light level {e.new} lx"
        else:
            what = e.new
        hhmm = datetime.fromtimestamp(e.ts, ctx.tz).strftime("%H:%M")
        lines.append(f"{hhmm} {e.entity_id} {what}{_who(e, ctx)}")
    if len(lines) > MAX_TIMELINE_LINES:
        keep = MAX_TIMELINE_LINES // 2
        lines = lines[:keep] + [f"… {len(lines) - 2 * keep} more lines …"] + lines[-keep:]
    return lines


def build(ctx: Context, findings: list[dict]) -> str:
    from .analysis import mirrored_targets

    mirrors = mirrored_targets(ctx)
    used = sorted({e.entity_id for e in ctx.events if e.entity_id in ctx.entities
                   and not e.entity_id.startswith("automation.") and e.entity_id not in mirrors})
    rise = [datetime.fromtimestamp(v[0], ctx.tz).strftime("%H:%M") for v in ctx.sun.values()]
    sset = [datetime.fromtimestamp(v[1], ctx.tz).strftime("%H:%M") for v in ctx.sun.values()]
    days = max(1, round((ctx.events[-1].ts - ctx.events[0].ts) / 86400)) if ctx.events else 0
    out = [f"HOME: {days} days of history. Sunrise about {min(rise, default='?')}–{max(rise, default='?')}, "
           f"sunset about {min(sset, default='?')}–{max(sset, default='?')}."]
    out.append("\nDEVICES (entity_id | name | room | kind). These are the ONLY entities that exist:")
    for eid in used:
        info = ctx.entities[eid]
        twins = [m for m, p in mirrors.items() if p == eid]
        note = f" (switches together with {', '.join(twins)})" if twins else ""
        out.append(f"{eid} | {info.name} | {info.area or '-'} | {_kind(ctx, eid)}{note}")
    if not any(e.split(".", 1)[0] in ("person", "device_tracker") for e in used):
        out.append("(There are no person or device_tracker entities, so presence/home/away "
                   "cannot be used. Motion sensors are the only sign that someone is around.)")
    if ctx.automations:
        out.append("\nEXISTING AUTOMATIONS:")
        out += [_auto_line(a) for a in ctx.automations]
    else:
        out.append("\nEXISTING AUTOMATIONS: none.")
    if findings:
        out.append("\nFOUND BY STATISTICS (already suggested, do not repeat):")
        out += [f"- {f}" for f in findings]
    for day in _pick_days(ctx):
        out.append(f"\nTIMELINE {day.strftime('%A %d %b')} (local time):")
        out += timeline(ctx, day, set(mirrors)) or ["(nothing)"]
    return "\n".join(out)
