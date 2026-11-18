"""Unit tests for the pure pattern-mining module (no Home Assistant needed)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import random
from zoneinfo import ZoneInfo

from custom_components.automation_advisor import analysis as A

TZ = ZoneInfo("America/Los_Angeles")
START = date(2026, 10, 20)  # a Tuesday
DAYS = 14
SUNRISE = (7, 15)
SUNSET = (18, 10)


def ts(d: date, h: int, m: int = 0, s: int = 0) -> float:
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=TZ).timestamp()


def sun_table(days=DAYS):
    """Late-autumn sun: sunrise ~1.2 min later and sunset ~1.5 min earlier every day."""
    return {
        START + timedelta(days=i): (
            ts(START + timedelta(days=i), *SUNRISE) + i * 72,
            ts(START + timedelta(days=i), *SUNSET) - i * 90,
        )
        for i in range(days)
    }


def entities(*items):
    out = {}
    for eid, dc, area in items:
        out[eid] = A.EntityInfo(eid, eid.split(".")[1].replace("_", " ").title(), dc, area)
    return out


def ctx(events, ents, automations=(), days=DAYS, **settings):
    return A.Context(
        events=sorted(events, key=lambda e: e.ts),
        entities=ents,
        automations=list(automations),
        tz=TZ,
        sun=sun_table(days),
        settings=A.Settings(**settings),
        now=ts(START + timedelta(days=days - 1), 23, 59),
    )


def motion_pulse(events, sensor, t, length=120):
    events.append(A.Event(t, sensor, "off", "on"))
    events.append(A.Event(t + length, sensor, "on", "off"))


def kitchen_events(rng=None):
    """Motion every 90 min from 07:00 to 23:00; the user switches the light on 20 s after
    motion only when it is dark (after 17:30 or before 08:30) and off 3 min after leaving."""
    rng = rng or random.Random(1)
    ev = []
    for i in range(DAYS):
        d = START + timedelta(days=i)
        for minute in range(7 * 60, 23 * 60, 90):
            t = ts(d, minute // 60, minute % 60) + rng.randint(-300, 300)
            motion_pulse(ev, "binary_sensor.kitchen_motion", t, 240)
            local = datetime.fromtimestamp(t, TZ)
            mod = local.hour * 60 + local.minute
            if mod >= 17 * 60 + 30 or mod < 8 * 60 + 30:
                on = t + 20
                ev.append(A.Event(on, "light.kitchen", "off", "on", A.CAUSE_MANUAL))
                ev.append(A.Event(t + 240 + 180, "light.kitchen", "on", "off", A.CAUSE_MANUAL))
    return ev


KITCHEN = entities(
    ("binary_sensor.kitchen_motion", "motion", "kitchen"),
    ("binary_sensor.hall_motion", "motion", "hall"),
    ("light.kitchen", None, "kitchen"),
)


def by_cat(results, cat):
    return [s for s in results if s.category == cat]


def test_kitchen_motion_light_with_dark_window():
    res = A.analyze(ctx(kitchen_events(), KITCHEN))
    ta = by_cat(res, "trigger_action")
    assert len(ta) == 1
    sg = ta[0]
    assert sg.evidence["trigger"] == "binary_sensor.kitchen_motion"
    assert sg.evidence["target"] == "light.kitchen"
    assert sg.evidence["ratio"] == 1.0
    win = sg.evidence["window"]
    assert win is not None and win["mode"] == "sun"
    # user starts ~40 min before sunset (17:30 vs 18:10) and stops 45 min after sunrise
    assert -90 <= win["start"] <= -30
    assert 30 <= win["end"] <= 105
    cfg = sg.automation
    assert cfg["triggers"] == [
        {"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "from": "off", "to": "on"}
    ]
    assert cfg["conditions"][0]["condition"] == "or"
    assert cfg["actions"] == [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]
    assert sg.options == ["accept"]


def test_kitchen_auto_off():
    res = A.analyze(ctx(kitchen_events(), KITCHEN))
    off = by_cat(res, "auto_off")
    assert len(off) == 1
    assert off[0].evidence["trigger"] == "binary_sensor.kitchen_motion"
    assert off[0].evidence["delay_min"] == 5
    trig = off[0].automation["triggers"][0]
    assert trig == {"trigger": "state", "entity_id": "binary_sensor.kitchen_motion",
                    "to": "off", "for": {"minutes": 5}}


def test_no_routine_for_explained_actions():
    res = A.analyze(ctx(kitchen_events(), KITCHEN))
    assert by_cat(res, "routine") == []


def test_reactions_to_covered_sensor_are_not_routines():
    """The kitchen already has motion automations; the user's reactions to that sensor must not
    reappear as 'turn on at 19:30' routines."""
    existing = A.ExistingAutomation(
        entity_id="automation.kitchen", alias="Kitchen",
        trigger_entities=frozenset({"binary_sensor.kitchen_motion"}),
        action_entities=frozenset({"light.kitchen"}),
        trigger_platforms=frozenset({"state"}),
        config={"actions": [{"action": "light.turn_on"}, {"action": "light.turn_off"}]},
    )
    ents = {k: A.EntityInfo(v.entity_id, v.name, v.device_class, None) for k, v in KITCHEN.items()}
    assert A.analyze(ctx(kitchen_events(), ents, [existing])) == []


def test_existing_automation_suppresses_suggestion():
    existing = A.ExistingAutomation(
        entity_id="automation.kitchen",
        alias="Kitchen",
        trigger_entities=frozenset({"binary_sensor.kitchen_motion"}),
        action_entities=frozenset({"light.kitchen"}),
        trigger_platforms=frozenset({"state"}),
        config={"actions": [{"action": "light.turn_on"}, {"action": "light.turn_off"}]},
    )
    res = A.analyze(ctx(kitchen_events(), KITCHEN, [existing]))
    assert by_cat(res, "trigger_action") == []
    assert by_cat(res, "auto_off") == []


def test_no_condition_when_user_reacts_all_day():
    ev = []
    for i in range(DAYS):
        d = START + timedelta(days=i)
        for h in (8, 12, 16, 20):
            t = ts(d, h)
            motion_pulse(ev, "binary_sensor.kitchen_motion", t)
            ev.append(A.Event(t + 15, "light.kitchen", "off", "on", A.CAUSE_USER))
            ev.append(A.Event(t + 1800, "light.kitchen", "on", "off", A.CAUSE_USER))
    sg = by_cat(A.analyze(ctx(ev, KITCHEN)), "trigger_action")[0]
    assert sg.evidence["window"] is None
    assert sg.automation["conditions"] == []


def test_random_noise_gives_nothing():
    rng = random.Random(7)
    ev = []
    for i in range(DAYS):
        d = START + timedelta(days=i)
        for _ in range(6):
            motion_pulse(ev, "binary_sensor.kitchen_motion", ts(d, rng.randint(0, 23), rng.randint(0, 59)))
        for _ in range(2):
            t = ts(d, rng.randint(0, 23), rng.randint(0, 59))
            ev.append(A.Event(t, "light.kitchen", "off", "on"))
            ev.append(A.Event(t + rng.randint(60, 7200), "light.kitchen", "on", "off"))
    res = A.analyze(ctx(ev, KITCHEN))
    assert by_cat(res, "trigger_action") == []
    assert by_cat(res, "routine") == []


def hall_automation_events(pattern: str):
    """automation.hall_light turns the hall light on with motion 4x a day. The user turns it
    straight off: during the day ('daytime') or at random ('random')."""
    rng = random.Random(3)
    ev = []
    for i in range(DAYS):
        d = START + timedelta(days=i)
        for h in (9, 11, 13, 15, 19, 22):
            t = ts(d, h, 5)
            motion_pulse(ev, "binary_sensor.hall_motion", t)
            ev.append(A.Event(t + 1, "light.hall", "off", "on", A.CAUSE_AUTOMATION,
                              "automation.hall_light"))
            undo = (h < 17) if pattern == "daytime" else rng.random() < 0.7
            if undo:
                ev.append(A.Event(t + 12, "light.hall", "on", "off", A.CAUSE_MANUAL))
            else:
                ev.append(A.Event(t + 900, "light.hall", "on", "off", A.CAUSE_AUTOMATION,
                                  "automation.hall_light_off"))
    return ev


HALL_AUTO = A.ExistingAutomation(
    entity_id="automation.hall_light",
    alias="Hall light on motion",
    config_id="123",
    editable=True,
    trigger_entities=frozenset({"binary_sensor.hall_motion"}),
    action_entities=frozenset({"light.hall"}),
    trigger_platforms=frozenset({"state"}),
    config={
        "id": "123",
        "alias": "Hall light on motion",
        "triggers": [{"trigger": "state", "entity_id": "binary_sensor.hall_motion", "to": "on"}],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.hall"}}],
    },
)
HALL = entities(("binary_sensor.hall_motion", "motion", "hall"), ("light.hall", None, "hall"))


def test_overridden_daytime_suggests_window():
    res = A.analyze(ctx(hall_automation_events("daytime"), HALL, [HALL_AUTO]))
    rv = by_cat(res, "overridden")
    assert len(rv) == 1
    sg = rv[0]
    assert sg.kind == A.KIND_REVIEW
    assert sg.target_automation == "automation.hall_light"
    assert sg.evidence["ratio"] == 0.667
    assert sg.options == ["modify", "delete", "disable"]
    cond = sg.automation["conditions"][-1]
    assert cond["condition"] in ("or", "time")
    assert sg.automation["id"] == "123"
    # reviews come first
    assert res[0] is sg


def test_overridden_random_suggests_delete():
    res = A.analyze(ctx(hall_automation_events("random"), HALL, [HALL_AUTO]))
    sg = by_cat(res, "overridden")[0]
    assert "modify" not in sg.options
    assert sg.options == ["delete", "disable"]
    assert sg.automation is None


def test_yaml_automation_only_disable():
    auto = A.ExistingAutomation(**{**{f: getattr(HALL_AUTO, f) for f in HALL_AUTO.__slots__},
                                   "editable": False})
    sg = by_cat(A.analyze(ctx(hall_automation_events("random"), HALL, [auto])), "overridden")[0]
    assert sg.options == ["disable"]


def test_undo_presses_are_not_habits():
    res = A.analyze(ctx(hall_automation_events("daytime"), HALL, [HALL_AUTO]))
    assert by_cat(res, "routine") == []
    assert by_cat(res, "auto_off") == []
    assert by_cat(res, "trigger_action") == []


def test_sunset_routine():
    ev = []
    rng = random.Random(5)
    for i in range(DAYS):
        d = START + timedelta(days=i)
        if i == 4:
            continue  # one day off
        t = sun_table()[d][1] + 10 * 60 + rng.randint(-480, 480)
        ev.append(A.Event(t, "switch.porch", "off", "on", A.CAUSE_MANUAL))
        ev.append(A.Event(ts(d, 23, 30) + rng.randint(-3000, 3000), "switch.porch", "on", "off",
                          A.CAUSE_MANUAL))
    ents = entities(("switch.porch", None, "outside"))
    res = by_cat(A.analyze(ctx(ev, ents)), "routine")
    on = [s for s in res if s.evidence["state"] == "on"]
    assert len(on) == 1
    trig = on[0].automation["triggers"][0]
    assert trig["trigger"] == "sun" and trig["event"] == "sunset"
    assert trig["offset"] in ("00:05:00", "00:10:00", "00:15:00")
    assert on[0].automation["actions"][0]["action"] == "switch.turn_on"


def test_weekday_routine():
    ev = []
    for i in range(21):
        d = START + timedelta(days=i)
        if d.weekday() < 5:
            ev.append(A.Event(ts(d, 6, 28), "switch.coffee", "off", "on", A.CAUSE_MANUAL))
            ev.append(A.Event(ts(d, 7, 40), "switch.coffee", "on", "off", A.CAUSE_MANUAL))
        else:
            ev.append(A.Event(ts(d, 9, 30), "switch.coffee", "off", "on", A.CAUSE_MANUAL))
            ev.append(A.Event(ts(d, 11, 0), "switch.coffee", "on", "off", A.CAUSE_MANUAL))
    ents = entities(("switch.coffee", None, "kitchen"))
    res = by_cat(A.analyze(ctx(ev, ents, days=21)), "routine")
    on = [s for s in res if s.evidence["state"] == "on"][0]
    assert on.evidence["weekdays_only"] is True
    assert on.automation["triggers"][0] == {"trigger": "time", "at": "06:30:00"}
    assert on.automation["conditions"] == [
        {"condition": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]}
    ]


def test_fingerprint_stable_across_runs():
    a = by_cat(A.analyze(ctx(kitchen_events(random.Random(1)), KITCHEN)), "trigger_action")[0]
    b = by_cat(A.analyze(ctx(kitchen_events(random.Random(2)), KITCHEN)), "trigger_action")[0]
    assert a.fingerprint == b.fingerprint


def test_clock_window_wraps_midnight():
    d = START
    times = [ts(d, 22, 30), ts(d, 23, 45), ts(d + timedelta(days=1), 0, 30),
             ts(d + timedelta(days=1), 1, 0)]
    w = A.fit_clock_window(times, TZ, 1.0)
    assert w is not None and w.start > w.end  # wraps
    assert all(w.contains(t, TZ, {}) for t in times)
    assert not w.contains(ts(d, 12, 0), TZ, {})


def test_wall_switch_undo_within_context_window_counts_as_undo():
    """HA stamps the entity's own state writes with the automation's context for 5 s, so a
    fast wall-switch undo arrives labelled as the automation."""
    ev = []
    for i in range(DAYS):
        d = START + timedelta(days=i)
        for h in (9, 13, 19, 22):
            t = ts(d, h, 5)
            motion_pulse(ev, "binary_sensor.hall_motion", t)
            ev.append(A.Event(t + 1, "light.hall", "off", "on", A.CAUSE_AUTOMATION, "automation.hall_light"))
            ev.append(A.Event(t + 3, "light.hall", "on", "off", A.CAUSE_AUTOMATION, "automation.hall_light"))
    sg = by_cat(A.analyze(ctx(ev, HALL, [HALL_AUTO])), "overridden")[0]
    assert sg.evidence["ratio"] == 1.0


def test_switch_gets_sun_window():
    ev = [A.Event(e.ts, "switch.kitchen" if e.entity_id == "light.kitchen" else e.entity_id,
                  e.old, e.new, e.cause, e.cause_ref) for e in kitchen_events()]
    ents = entities(("binary_sensor.kitchen_motion", "motion", "kitchen"), ("switch.kitchen", None, "kitchen"))
    sg = by_cat(A.analyze(ctx(ev, ents)), "trigger_action")[0]
    assert sg.evidence["window"]["mode"] == "sun"
    assert sg.automation["actions"][0]["action"] == "switch.turn_on"


def test_report_explains_empty_runs():
    rep = A.diagnose(ctx([], KITCHEN), [])
    assert rep["headline"] == "No activity recorded yet."
    ev = hall_automation_events("daytime")
    c = ctx(ev, HALL, [HALL_AUTO])
    found = A.analyze(c)
    rep = A.diagnose(c, found)
    assert rep["events"] == len(ev) and rep["days"] >= 13
    assert rep["automations"][0]["automation"] == "automation.hall_light"
    assert rep["automations"][0]["kept_pct"] == 33
    assert "suggestion" in rep["headline"]
    # well-covered house: automations kept, nothing by hand
    kept = A.without_undone_runs(ctx(ev, HALL, [HALL_AUTO])).events
    quiet = [e for e in kept if e.cause == A.CAUSE_AUTOMATION or e.entity_id.startswith("binary_sensor")]
    c = ctx(quiet, HALL, [HALL_AUTO])
    rep = A.diagnose(c, A.analyze(c))
    assert rep["headline"].startswith("Nothing to suggest")


BASEMENT = entities(
    ("binary_sensor.stairs_motion", "motion", "basement"),
    ("binary_sensor.back_door", "door", "basement"),
    ("light.basement", None, "basement"),
)
OFF_AUTO = A.ExistingAutomation(
    entity_id="automation.basement_off", alias="Basement off", config_id="9", editable=True,
    trigger_entities=frozenset({"binary_sensor.stairs_motion"}), trigger_platforms=frozenset({"state"}),
    action_entities=frozenset({"light.basement"}),
    config={"id": "9", "alias": "Basement off",
            "triggers": [{"trigger": "state", "entity_id": "binary_sensor.stairs_motion", "to": "off", "for": {"minutes": 5}}],
            "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.basement"}}]},
)
ON_DOOR = A.ExistingAutomation(
    entity_id="automation.basement_door", alias="Basement on from back door",
    trigger_entities=frozenset({"binary_sensor.back_door"}), trigger_platforms=frozenset({"state"}),
    action_entities=frozenset({"light.basement"}),
)


def basement_events():
    """Like the owner's basement: walking in past the motion sensor works (off 5 min after it
    goes quiet). Coming in through the back door switches the light on while the motion
    sensor is already quiet, and it stays on until the sensor next sees someone."""
    ev = []
    for i in range(DAYS):
        d = START + timedelta(days=i)
        t = ts(d, 9)
        motion_pulse(ev, "binary_sensor.stairs_motion", t, 120)
        ev.append(A.Event(t + 1, "light.basement", "off", "on", A.CAUSE_AUTOMATION, "automation.basement_on"))
        ev.append(A.Event(t + 120 + 300, "light.basement", "on", "off", A.CAUSE_AUTOMATION, "automation.basement_off"))
        if i % 2 == 0:  # back-door entry, nobody passes the sensor for hours
            t = ts(d, 17)
            ev.append(A.Event(t, "binary_sensor.back_door", "off", "on"))
            ev.append(A.Event(t + 1, "light.basement", "off", "on", A.CAUSE_AUTOMATION, "automation.basement_door"))
            ev.append(A.Event(t + 30, "binary_sensor.back_door", "on", "off"))
            back = ts(d, 22)
            motion_pulse(ev, "binary_sensor.stairs_motion", back, 60)
            ev.append(A.Event(back + 60 + 300, "light.basement", "on", "off", A.CAUSE_AUTOMATION, "automation.basement_off"))
    return ev


def test_stuck_on_light_gets_robust_off_automation():
    res = A.analyze(ctx(basement_events(), BASEMENT, [OFF_AUTO, ON_DOOR]))
    sg = by_cat(res, "stuck_on")[0]
    assert sg.kind == A.KIND_REVIEW and sg.options == ["modify"]
    assert sg.target_automation == "automation.basement_off"
    assert sg.evidence["stuck"] == 7 and sg.evidence["started_by"] == "automation.basement_door"
    assert "Basement on from back door" in sg.description
    cfg = sg.automation
    assert cfg["id"] == "9"
    assert {"trigger": "state", "entity_id": "light.basement", "to": "on", "for": {"minutes": 5}} in cfg["triggers"]
    assert {"trigger": "time_pattern", "minutes": "/10"} in cfg["triggers"]
    assert cfg["conditions"][0] == {"condition": "state", "entity_id": ["binary_sensor.stairs_motion"],
                                    "state": "off", "for": {"minutes": 5}}
    assert cfg["actions"] == OFF_AUTO.config["actions"]


def test_stuck_on_yaml_automation_offers_safety_net():
    auto = A.ExistingAutomation(**{**{f: getattr(OFF_AUTO, f) for f in OFF_AUTO.__slots__}, "editable": False})
    sg = by_cat(A.analyze(ctx(basement_events(), BASEMENT, [auto, ON_DOOR])), "stuck_on")[0]
    assert sg.kind == A.KIND_CREATE and sg.options == ["accept"]
    assert "id" not in sg.automation and "safety net" in sg.automation["alias"]


def test_working_off_automation_is_not_flagged():
    ev = [e for e in basement_events() if e.entity_id != "binary_sensor.back_door"
          and not (e.cause_ref == "automation.basement_door")]
    ev = [e for e in ev if not (e.entity_id == "light.basement" and datetime.fromtimestamp(e.ts, TZ).hour >= 17)]
    assert by_cat(A.analyze(ctx(ev, BASEMENT, [OFF_AUTO])), "stuck_on") == []


def test_test_mode_rediscovers_automated_habits():
    """With existing automations ignored, what the hall automation does is treated as a habit."""
    ev = []
    for i in range(DAYS):
        d = START + timedelta(days=i)
        for h in (8, 13, 19):
            t = ts(d, h)
            motion_pulse(ev, "binary_sensor.hall_motion", t)
            ev.append(A.Event(t + 2, "light.hall", "off", "on", A.CAUSE_AUTOMATION, "automation.hall_light"))
            ev.append(A.Event(t + 600, "light.hall", "on", "off", A.CAUSE_AUTOMATION, "automation.hall_off"))
    assert by_cat(A.analyze(ctx(ev, HALL, [HALL_AUTO])), "trigger_action") == []
    res = A.analyze(ctx(ev, HALL, [HALL_AUTO], ignore_existing=True))
    sg = by_cat(res, "trigger_action")[0]
    assert sg.evidence["test_mode"] and sg.description.startswith("Test mode")


def test_mirrored_entities_get_one_suggestion():
    ev = kitchen_events()
    ev += [A.Event(e.ts + 0.2, "light.kitchen_driver", e.old, e.new, e.cause, e.cause_ref)
           for e in ev if e.entity_id == "light.kitchen"]
    ents = {**KITCHEN, **entities(("light.kitchen_driver", None, "kitchen"))}
    res = A.analyze(ctx(ev, ents))
    targets = [sg.evidence["target"] for sg in res if sg.category == "trigger_action"]
    assert targets == ["light.kitchen"]
