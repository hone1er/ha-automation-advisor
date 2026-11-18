"""Remote source: logbook conversion and read-only behaviour."""

from __future__ import annotations

from custom_components.automation_advisor import analysis as A
from custom_components.automation_advisor.sources import logbook_to_events


def test_logbook_entries_keep_their_cause():
    entries = [
        # automation run: the logbook's own "triggered" entry, then the light it switched
        {"when": 100.0, "entity_id": "automation.hall", "name": "Hall", "message": "triggered by state of binary_sensor.hall", "source": "state of binary_sensor.hall", "context_id": "c1"},
        {"when": 100.2, "entity_id": "light.hall", "state": "on", "context_id": "c1",
         "context_event_type": "automation_triggered", "context_domain": "automation",
         "context_entity_id": "automation.hall", "context_name": "Hall"},
        # wall switch: no context fields
        {"when": 110.0, "entity_id": "light.hall", "state": "off", "context_id": "c2"},
        # app user
        {"when": 120.0, "entity_id": "light.hall", "state": "on", "context_id": "c3",
         "context_user_id": "abc", "context_event_type": "call_service", "context_domain": "light"},
        # script / another integration without a user
        {"when": 130.0, "entity_id": "light.hall", "state": "off", "context_id": "c4",
         "context_event_type": "call_service", "context_domain": "script"},
        # user upstream, origin not described (logbook right after a restart): not a habit
        {"when": 135.0, "entity_id": "light.hall", "state": "on", "context_user_id": "abc"},
        # drop-out and recovery are not presses
        {"when": 140.0, "entity_id": "light.hall", "state": "unavailable"},
        {"when": 150.0, "entity_id": "light.hall", "state": "on"},
        # motion sensor (tracked through its device class), temperature sensor (ignored)
        {"when": 160.0, "entity_id": "binary_sensor.hall", "state": "off"},
        {"when": 161.0, "entity_id": "binary_sensor.hall", "state": "on"},
        {"when": 162.0, "entity_id": "sensor.temp", "state": "21"},
        # excluded entity
        {"when": 170.0, "entity_id": "switch.ignored", "state": "off"},
        {"when": 171.0, "entity_id": "switch.ignored", "state": "on"},
    ]
    events = logbook_to_events(entries, {"binary_sensor.hall": "motion"}, {"switch.ignored"})
    assert [(e.ts, e.entity_id, e.old, e.new, e.cause, e.cause_ref) for e in events] == [
        (100.0, "automation.hall", None, "triggered", A.CAUSE_OTHER, None),
        (110.0, "light.hall", "on", "off", A.CAUSE_MANUAL, None),
        (120.0, "light.hall", "off", "on", A.CAUSE_USER, None),
        (130.0, "light.hall", "on", "off", A.CAUSE_OTHER, None),
        (135.0, "light.hall", "off", "on", A.CAUSE_OTHER, None),
        (161.0, "binary_sensor.hall", "off", "on", A.CAUSE_MANUAL, None),
    ]
