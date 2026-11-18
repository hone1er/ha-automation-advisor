"""Backtester: replay automations over synthetic households."""

from __future__ import annotations

from custom_components.automation_advisor import analysis as A
from custom_components.automation_advisor.backtest import backtest

from .test_analysis import (
    BASEMENT, KITCHEN, OFF_AUTO, ON_DOOR, TZ, basement_events, by_cat, ctx, kitchen_events,
    sun_table,
)


def run(cfg, events, ents):
    return backtest(cfg, events, TZ, sun_table(), ents)


def test_kitchen_suggestion_replays_well():
    ev = kitchen_events()
    sg = by_cat(A.analyze(ctx(ev, KITCHEN)), "trigger_action")[0]
    r = run(sg.automation, ev, KITCHEN)
    assert r.supported and not r.partial
    assert r.actions > 50
    assert r.good_rate >= 0.8 and r.unwanted_rate <= 0.2  # a few early fires at the window edge
    assert r.covered / r.manual >= 0.9
    assert r.passes()
    assert "matched what really happened" in r.summary()


def test_kitchen_auto_off_saves_light_without_darkness():
    ev = kitchen_events()
    sg = by_cat(A.analyze(ctx(ev, KITCHEN)), "auto_off")[0]
    r = run(sg.automation, ev, KITCHEN)
    assert r.supported and r.unwanted == 0
    assert r.agreed + r.helpful == r.actions


def test_basement_fix_turns_off_stuck_lights():
    ev = basement_events()
    sg = by_cat(A.analyze(ctx(ev, BASEMENT, [OFF_AUTO, ON_DOOR])), "stuck_on")[0]
    r = run(sg.automation, ev, BASEMENT)
    assert r.supported
    assert r.helpful >= 6           # the back-door evenings
    assert r.saved_min > 6 * 4 * 60  # hours of light saved
    assert r.unwanted == 0
    # the original off-automation never catches those evenings
    r0 = run(OFF_AUTO.config, ev, BASEMENT)
    assert r0.helpful == 0


def test_bad_automation_fails():
    ev = kitchen_events()
    cfg = {"triggers": [{"trigger": "time_pattern", "minutes": "0", "hours": "/1"}],
           "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]}
    r = run(cfg, ev, KITCHEN)
    assert r.supported and r.actions > 50
    assert r.unwanted_rate > 0.5 and not r.passes()
    assert r.examples and "when you did not" in r.examples[0]


def test_conditions_are_applied():
    ev = kitchen_events()
    trig = [{"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "to": "on"}]
    act = [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]
    plain = run({"triggers": trig, "actions": act}, ev, KITCHEN)
    night = run({"triggers": trig, "actions": act, "conditions": [
        {"condition": "or", "conditions": [
            {"condition": "sun", "after": "sunset", "after_offset": "-01:00:00"},
            {"condition": "sun", "before": "sunrise", "before_offset": "01:30:00"}]}]}, ev, KITCHEN)
    assert plain.unwanted > night.unwanted
    assert night.fires < plain.fires


def test_unsupported_and_partial_are_reported():
    ev = kitchen_events()
    r = run({"triggers": [{"trigger": "template", "value_template": "{{ true }}"}],
             "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]}, ev, KITCHEN)
    assert not r.supported and "template" in r.reason and "Could not replay" in r.summary()
    r = run({"triggers": [{"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "to": "on"}],
             "actions": [{"delay": {"seconds": 5}},
                         {"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]}, ev, KITCHEN)
    assert r.supported and r.partial == ["delays (treated as instant)"]


def test_state_for_and_numeric_state():
    ev = kitchen_events()
    # lux sensor: dark in the evening
    from datetime import timedelta
    from .test_analysis import START, ts, DAYS
    for i in range(DAYS):
        d = START + timedelta(days=i)
        ev.append(A.Event(ts(d, 7, 30), "sensor.kitchen_lux", "5.0", "300.0"))
        ev.append(A.Event(ts(d, 17, 20), "sensor.kitchen_lux", "300.0", "5.0"))
    ents = {**KITCHEN, "sensor.kitchen_lux": A.EntityInfo("sensor.kitchen_lux", "Kitchen lux", "illuminance", "kitchen")}
    cfg = {"triggers": [{"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "to": "on"}],
           "conditions": [{"condition": "numeric_state", "entity_id": "sensor.kitchen_lux", "below": 20}],
           "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]}
    r = backtest(cfg, ev, TZ, sun_table(), ents)
    assert r.passes(), r.summary()
    trig = {"triggers": [{"trigger": "numeric_state", "entity_id": "sensor.kitchen_lux", "below": 20}],
            "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]}
    r = backtest(trig, ev, TZ, sun_table(), ents)
    assert r.fires == DAYS  # one crossing into darkness per evening


def test_sun_entity_condition():
    ev = kitchen_events()
    cfg = {"triggers": [{"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "to": "on"}],
           "conditions": [{"condition": "state", "entity_id": "sun.sun", "state": "below_horizon"}],
           "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]}
    r = run(cfg, ev, KITCHEN)
    assert r.supported and r.fires > 0
    assert r.unwanted < run({**cfg, "conditions": []}, ev, KITCHEN).unwanted


def test_same_instant_as_existing_automation_counts_as_agreement():
    """An existing automation switched the light off exactly when the replayed one would have:
    the replay must see the light still on just before, and count it as matching."""
    from datetime import timedelta
    from .test_analysis import START, ts
    ev = []
    for i in range(5):
        d = START + timedelta(days=i)
        t = ts(d, 20)
        ev += [A.Event(t, "binary_sensor.kitchen_motion", "off", "on"),
               A.Event(t + 1, "light.kitchen", "off", "on", A.CAUSE_MANUAL),
               A.Event(t + 60, "binary_sensor.kitchen_motion", "on", "off"),
               A.Event(t + 60 + 600 + 0.3, "light.kitchen", "on", "off", A.CAUSE_MANUAL)]
    cfg = {"triggers": [{"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "to": "off",
                         "for": {"minutes": 10}}],
           "conditions": [{"condition": "state", "entity_id": "light.kitchen", "state": "on"}],
           "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.kitchen"}}]}
    r = run(cfg, ev, KITCHEN)
    assert r.fires == 5 and r.agreed == 5 and r.unwanted == 0


def test_window_is_tuned_to_history():
    """A window that runs too late into the morning gets tightened by the replay."""
    from custom_components.automation_advisor.backtest import tune_window
    ev = kitchen_events()
    sg = by_cat(A.analyze(ctx(ev, KITCHEN)), "trigger_action")[0]
    wide = dict(sg.evidence["window"], end=sg.evidence["window"]["end"] + 90)  # +1.5 h too long
    from custom_components.automation_advisor.analysis import Window
    w = Window(wide["mode"], wide["start"], wide["end"], 0)
    cfg = {**sg.automation, "conditions": [w.condition()]}
    before = run(cfg, ev, KITCHEN)
    tuned = tune_window(cfg, wide, ev, TZ, sun_table(), KITCHEN)
    assert tuned is not None
    _, window, after = tuned
    assert window["end"] < wide["end"]
    assert after.unwanted < before.unwanted and after.passes() and not before.passes()


def test_delay_is_tuned_to_history():
    """Someone who switches off 3 min after leaving: a 10-min auto-off never acts first; the
    tuner finds a delay that matches."""
    from custom_components.automation_advisor.backtest import tune_delay
    ev = kitchen_events()  # user switches off 3 min after motion ends
    cfg = {"triggers": [{"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "to": "off",
                         "for": {"minutes": 10}}],
           "conditions": [{"condition": "state", "entity_id": "light.kitchen", "state": "on"}],
           "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.kitchen"}}]}
    assert run(cfg, ev, KITCHEN).actions == 0
    tuned = tune_delay(cfg, ev, TZ, sun_table(), KITCHEN)
    assert tuned is not None
    _, minutes, r = tuned
    assert minutes <= 3 and r.agreed > 20 and r.unwanted == 0


def test_choose_with_trigger_ids_is_replayed():
    ev = kitchen_events()
    cfg = {"triggers": [{"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "to": "on", "id": "on"},
                        {"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "to": "off",
                         "for": {"minutes": 3}, "id": "off"}],
           "conditions": [{"condition": "or", "conditions": [
               {"condition": "sun", "after": "sunset", "after_offset": "-01:00:00"},
               {"condition": "sun", "before": "sunrise", "before_offset": "01:30:00"}]}],
           "actions": [{"choose": [
               {"conditions": [{"condition": "trigger", "id": "on"}],
                "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]},
               {"conditions": [{"condition": "trigger", "id": "off"}],
                "sequence": [{"action": "light.turn_off", "target": {"entity_id": "light.kitchen"}}]}]}]}
    r = run(cfg, ev, KITCHEN)
    assert r.supported and not r.partial
    assert r.agreed > 50 and r.covered > 50     # both the on and the off branch are replayed
