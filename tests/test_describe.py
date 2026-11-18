"""Generated plain-English descriptions."""

from custom_components.automation_advisor.describe import describe

NAMES = {"binary_sensor.kitchen_motion": "Kitchen motion", "light.kitchen": "Kitchen light",
         "light.strip": "Strip", "binary_sensor.hall": "Hall sensor"}


def test_motion_light_after_dark():
    cfg = {"triggers": [{"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "from": "off", "to": "on"}],
           "conditions": [{"condition": "or", "conditions": [
               {"condition": "sun", "after": "sunset", "after_offset": "-00:45:00"},
               {"condition": "sun", "before": "sunrise", "before_offset": "01:00:00"}]}],
           "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]}
    assert describe(cfg, NAMES) == (
        "When Kitchen motion becomes on (from off), but only if (it is after 45 minutes before sunset "
        "or it is before 1 hour after sunrise), it will turn on Kitchen light.")


def test_off_after_quiet_with_safety_net():
    cfg = {"triggers": [
        {"trigger": "state", "entity_id": ["binary_sensor.kitchen_motion", "binary_sensor.hall"], "to": "off", "for": {"minutes": 5}},
        {"trigger": "time_pattern", "minutes": "/10"}],
        "conditions": [{"condition": "state", "entity_id": ["binary_sensor.kitchen_motion", "binary_sensor.hall"],
                        "state": "off", "for": {"minutes": 5}},
                       {"condition": "state", "entity_id": "light.kitchen", "state": "on"}],
        "actions": [{"action": "light.turn_off", "target": {"entity_id": ["light.kitchen", "light.strip"]}}]}
    assert describe(cfg, NAMES) == (
        "When Kitchen motion and Hall sensor have been off for 5 minutes or when every 10 minutes "
        "(a regular check), but only if Kitchen motion and Hall sensor are off (for at least 5 minutes) "
        "and Kitchen light is on, it will turn off Kitchen light and Strip.")


def test_time_weekdays_and_unknown_entity():
    cfg = {"triggers": [{"trigger": "time", "at": "06:30:00"}],
           "conditions": [{"condition": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]}],
           "actions": [{"action": "switch.turn_on", "target": {"entity_id": "switch.coffee"}},
                       {"delay": {"minutes": 40}},
                       {"action": "switch.turn_off", "target": {"entity_id": "switch.coffee"}}]}
    assert describe(cfg, NAMES) == (
        "When it is 06:30, but only if it is on Monday, Tuesday, Wednesday, Thursday and Friday, "
        "it will turn on switch.coffee, then wait 40 minutes, then turn off switch.coffee.")


def test_four_button_remote():
    names = {"dev1": "Bedroom remote", "bedroom": "Bedroom"}
    trig = lambda i, sub: {"trigger": "device", "domain": "zha", "device_id": "dev1",  # noqa: E731
                           "type": "remote_button_short_press", "subtype": sub, "id": i}
    cfg = {"triggers": [trig("up", "turn_on"), trig("down", "turn_off"), trig("left", "dim_down"), trig("right", "dim_up")],
           "actions": [{"choose": [
               {"conditions": [{"condition": "trigger", "id": "up"}],
                "sequence": [{"action": "light.turn_on", "target": {"area_id": "bedroom"}}]},
               {"conditions": [{"condition": "trigger", "id": "down"}],
                "sequence": [{"action": "light.turn_off", "target": {"area_id": "bedroom"}}]},
               {"conditions": [{"condition": "trigger", "id": "left"}],
                "sequence": [{"action": "light.turn_on", "target": {"area_id": "bedroom"}, "data": {"brightness_step_pct": -10}}]},
               {"conditions": [{"condition": "trigger", "id": "right"}],
                "sequence": [{"action": "light.turn_on", "target": {"area_id": "bedroom"}, "data": {"brightness_step_pct": 10}}]},
           ]}]}
    text = describe(cfg, names)
    assert text.startswith("When Bedroom remote reports remote button short press turn on or when")
    assert "if it was “up”: turn on all lights in Bedroom" in text
    assert "if it was “left”: make all lights in Bedroom dimmer by 10 %" in text
    assert text.endswith("if it was “right”: make all lights in Bedroom brighter by 10 %.")
