"""Plain-English description of an automation config, generated from the config itself.

Shown on every card next to the AI's own wording, so what the card says can never differ from
what the automation will do. Pure Python, unit-tested.
"""

from __future__ import annotations

from typing import Any

WEEKDAYS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
            "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
STATE_WORDS = {"on": "on", "off": "off", "home": "home", "not_home": "away", "open": "open",
               "closed": "closed", "locked": "locked", "unlocked": "unlocked",
               "below_horizon": "below the horizon", "above_horizon": "above the horizon"}
SERVICE_WORDS = {"turn_on": "turn on", "turn_off": "turn off", "toggle": "toggle",
                 "open_cover": "open", "close_cover": "close", "lock": "lock", "unlock": "unlock",
                 "media_play": "play", "media_pause": "pause", "media_stop": "stop"}


def _list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _names(v: Any, names: dict[str, str]) -> str:
    ents = []
    for x in _list(v):
        ents += [p.strip() for p in str(x).split(",") if p.strip()]
    labels = [names.get(e, e) for e in ents]
    if len(labels) <= 1:
        return labels[0] if labels else "?"
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _dur(v: Any) -> str:
    if isinstance(v, dict):
        parts = [f"{int(v[k])} {k[:-1] if int(v[k]) == 1 else k}"
                 for k in ("hours", "minutes", "seconds") if v.get(k)]
        return " ".join(parts) or "0 seconds"
    if isinstance(v, (int, float)):
        return _dur({"seconds": v})
    if isinstance(v, str) and v.count(":") >= 1:
        p = [int(x) for x in v.lstrip("-").split(":")] + [0, 0]
        return _dur({"hours": p[0], "minutes": p[1], "seconds": p[2]})
    return str(v)


def _offset(event: str, off: Any) -> str:
    if not off:
        return event
    s = str(off)
    before = s.startswith("-")
    return f"{_dur(s)} {'before' if before else 'after'} {event}"


def _state(v: Any) -> str:
    vals = [STATE_WORDS.get(str(x), str(x)) for x in _list(v)]
    return " or ".join(vals)


def _hhmm(v: Any) -> str:
    return str(v)[:5] if isinstance(v, str) else str(v)


def trigger_text(t: dict, names: dict[str, str]) -> str:
    kind = t.get("trigger", t.get("platform"))
    if kind == "state":
        who = _names(t.get("entity_id"), names)
        text = f"{who} changes"
        if "to" in t:
            text = f"{who} becomes {_state(t['to'])}"
            if "from" in t:
                text += f" (from {_state(t['from'])})"
        if t.get("for"):
            verb = "have" if len(_list(t.get("entity_id"))) > 1 else "has"
            text = (f"{who} {verb} been {_state(t['to'])} for {_dur(t['for'])}" if "to" in t
                    else f"{text} and stays so for {_dur(t['for'])}")
        return text
    if kind == "numeric_state":
        who = _names(t.get("entity_id"), names)
        if "below" in t and "above" in t:
            text = f"{who} goes between {t['above']} and {t['below']}"
        elif "below" in t:
            text = f"{who} drops below {t['below']}"
        else:
            text = f"{who} rises above {t.get('above')}"
        return text + (f" for {_dur(t['for'])}" if t.get("for") else "")
    if kind == "time":
        return "it is " + " or ".join(_hhmm(a) for a in _list(t.get("at")))
    if kind == "sun":
        return "it is " + _offset(str(t.get("event", "sunset")), t.get("offset"))
    if kind == "device":
        what = " ".join(str(t.get(k)).replace("_", " ") for k in ("type", "subtype") if t.get(k))
        return f"{names.get(t.get('device_id'), 'the device')} reports {what or 'an event'}"
    if kind == "event":
        return f"the event {t.get('event_type')} happens"
    if kind == "time_pattern":
        if str(t.get("minutes", "")).startswith("/"):
            return f"every {str(t['minutes'])[1:]} minutes (a regular check)"
        if str(t.get("hours", "")).startswith("/"):
            return f"every {str(t['hours'])[1:]} hours"
        return "on a time pattern"
    return f"a {kind} trigger fires"


def condition_text(c: Any, names: dict[str, str]) -> str:
    if isinstance(c, str):
        return "a template is true"
    kind = c.get("condition")
    if kind in ("and", "or"):
        subs = [condition_text(x, names) for x in _list(c.get("conditions"))]
        return "(" + f" {kind} ".join(subs) + ")" if len(subs) > 1 else (subs[0] if subs else "")
    if kind == "not":
        return "not (" + " or ".join(condition_text(x, names) for x in _list(c.get("conditions"))) + ")"
    if kind == "state":
        who = _names(c.get("entity_id"), names)
        text = f"{who} {'is' if len(_list(c.get('entity_id'))) == 1 else 'are'} {_state(c.get('state'))}"
        return text + (f" (for at least {_dur(c['for'])})" if c.get("for") else "")
    if kind == "trigger":
        ids = _list(c.get("id"))
        return "it was " + " or ".join(f"“{i}”" for i in ids)
    if kind == "numeric_state":
        who = _names(c.get("entity_id"), names)
        parts = []
        if "above" in c:
            parts.append(f"above {c['above']}")
        if "below" in c:
            parts.append(f"below {c['below']}")
        return f"{who} is " + " and ".join(parts)
    if kind == "time":
        parts = []
        if c.get("after") and c.get("before"):
            parts.append(f"between {_hhmm(c['after'])} and {_hhmm(c['before'])}")
        elif c.get("after"):
            parts.append(f"after {_hhmm(c['after'])}")
        elif c.get("before"):
            parts.append(f"before {_hhmm(c['before'])}")
        if c.get("weekday"):
            days = [WEEKDAYS.get(d, d) for d in _list(c["weekday"])]
            parts.append("on " + (", ".join(days[:-1]) + " and " + days[-1] if len(days) > 1 else days[0]))
        return "it is " + " ".join(parts)
    if kind == "sun":
        parts = []
        if c.get("after"):
            parts.append("after " + _offset(c["after"], c.get("after_offset")))
        if c.get("before"):
            parts.append("before " + _offset(c["before"], c.get("before_offset")))
        return "it is " + " and ".join(parts)
    return f"a {kind} condition holds"


def action_text(a: dict, names: dict[str, str]) -> str:
    if "delay" in a:
        d = a["delay"]
        if isinstance(d, dict) and set(d) == {"milliseconds"}:
            return f"wait {int(d['milliseconds'])} ms"
        return f"wait {_dur(d)}"
    if "stop" in a:
        return "stop"
    if "repeat" in a:
        r = a["repeat"] or {}
        seq = ", then ".join(action_text(x, names) for x in _list(r.get("sequence")) if isinstance(x, dict))
        if "count" in r:
            return f"repeat up to {r['count']} times ({seq}) until the button is released"
        return f"repeat ({seq})"
    if "choose" in a:
        parts = []
        for opt in _list(a["choose"]):
            cond = " and ".join(condition_text(c, names) for c in _list(opt.get("conditions")))
            seq = ", then ".join(action_text(x, names) for x in _list(opt.get("sequence")) if isinstance(x, dict))
            parts.append(f"if {cond}: {seq}")
        if a.get("default"):
            parts.append("otherwise: " + ", then ".join(
                action_text(x, names) for x in _list(a["default"]) if isinstance(x, dict)))
        return "; ".join(parts)
    service = str(a.get("action", a.get("service", "?")))
    domain, _, name = service.partition(".")
    tgt = a.get("target") or {}
    target = tgt.get("entity_id", a.get("entity_id", (a.get("data") or {}).get("entity_id")))
    if target:
        who = _names(target, names)
    elif tgt.get("area_id"):
        kind = {"light": "lights", "switch": "switches", "cover": "covers", "fan": "fans"}.get(domain, domain)
        who = f"all {kind} in {_names(tgt['area_id'], names)}"
    elif tgt.get("device_id"):
        who = _names(tgt["device_id"], names)
    else:
        who = ""
    verb = SERVICE_WORDS.get(name, f"run {service} on")
    data = {k: v for k, v in (a.get("data") or {}).items() if k != "entity_id"}
    extra = ""
    if "brightness_step_pct" in data:
        step = data["brightness_step_pct"]
        try:
            step = float(step)
            return f"make {who} {'brighter' if step > 0 else 'dimmer'} by {abs(step):g} %"
        except (TypeError, ValueError):
            extra = f" (brightness step {step} %)"
    elif "brightness_pct" in data:
        extra = f" at {data['brightness_pct']} % brightness"
    elif domain == "climate" and "hvac_mode" in data:
        extra = f" to {data['hvac_mode']}"
    elif domain == "climate" and "temperature" in data:
        extra = f" to {data['temperature']}°"
    return f"{verb} {who}{extra}".strip()


def describe(config: dict | None, names: dict[str, str] | None = None) -> str:
    """'When X, only if Y, it will Z.'"""
    if not config:
        return ""
    names = names or {}
    triggers = _list(config.get("triggers", config.get("trigger")))
    conditions = _list(config.get("conditions", config.get("condition")))
    actions = _list(config.get("actions", config.get("action")))
    text = "When " + " or when ".join(trigger_text(t, names) for t in triggers if isinstance(t, dict))
    if conditions:
        text += ", but only if " + " and ".join(condition_text(c, names) for c in conditions)
    text += ", it will " + ", then ".join(action_text(a, names) for a in actions if isinstance(a, dict)) + "."
    return text
