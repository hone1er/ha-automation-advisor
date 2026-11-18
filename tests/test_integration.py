"""End-to-end tests inside a test Home Assistant instance."""

from __future__ import annotations

import time
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
import yaml

from homeassistant.components.automation import DOMAIN as AUTOMATION_DOMAIN
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.automation_advisor import analysis as A
from custom_components.automation_advisor.const import DOMAIN, STATUS_ACCEPTED, STATUS_DISMISSED, STATUS_PENDING

from .test_analysis import KITCHEN, kitchen_events


async def _setup(hass: HomeAssistant, **options):
    """Most tests exercise the statistics engine, so its suggestions are shown unless a test
    says otherwise (the product shows only AI suggestions)."""
    options = {"show_statistical": True, **options}
    """Load automations from the config dir like a real start, then the advisor."""
    from homeassistant import config as conf_util

    config = await conf_util.async_hass_config_yaml(hass)
    assert await async_setup_component(hass, AUTOMATION_DOMAIN, config)
    hass.states.async_set("binary_sensor.kitchen_motion", "off", {"device_class": "motion", "friendly_name": "Kitchen motion"})
    hass.states.async_set("binary_sensor.hall_motion", "off", {"device_class": "motion", "friendly_name": "Hall motion"})
    hass.states.async_set("light.kitchen", "off", {"friendly_name": "Kitchen light"})
    hass.states.async_set("light.hall", "off", {"friendly_name": "Hall light"})
    entry = MockConfigEntry(domain=DOMAIN, title="Automation Advisor", options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _shift_to_now(events: list[A.Event]) -> list[A.Event]:
    """The synthetic log starts 2026-10-20; move it so it ends just before now."""
    days = int((time.time() - 3600 - max(e.ts for e in events)) // 86400)
    offset = days * 86400  # whole days, so local clock times stay the same
    return [A.Event(e.ts + offset, e.entity_id, e.old, e.new, e.cause, e.cause_ref) for e in events]


async def test_cause_attribution(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass)
    manager = entry.runtime_data

    # 1. wall switch: plain context
    hass.states.async_set("light.kitchen", "on", context=Context())
    # 2. app user
    hass.states.async_set("light.kitchen", "off", context=Context(user_id="abc"))
    # 3. automation: automation_triggered then a state change with the same context
    ctx = Context()
    hass.bus.async_fire("automation_triggered", {"entity_id": "automation.x", "name": "x"}, context=ctx)
    hass.states.async_set("light.kitchen", "on", context=ctx)
    # 4. a script started by that automation
    hass.states.async_set("light.kitchen", "off", context=Context(parent_id=ctx.id))
    # 5. unrelated chained call
    hass.states.async_set("light.kitchen", "on", context=Context(parent_id="other"))
    # attribute-only update: ignored
    hass.states.async_set("light.kitchen", "on", {"brightness": 10}, context=Context())
    # untracked sensor: ignored
    hass.states.async_set("sensor.temp", "21")
    await hass.async_block_till_done()
    await manager.collector.async_flush()

    events = await hass.async_add_executor_job(manager.archive.since, 0)
    light = [(e.new, e.cause, e.cause_ref) for e in events if e.entity_id == "light.kitchen"]
    assert light == [
        ("on", A.CAUSE_MANUAL, None),
        ("off", A.CAUSE_USER, None),
        ("on", A.CAUSE_AUTOMATION, "automation.x"),
        ("off", A.CAUSE_AUTOMATION, "automation.x"),
        ("on", A.CAUSE_OTHER, None),
    ]
    assert any(e.entity_id == "automation.x" and e.new == "triggered" for e in events)
    assert not any(e.entity_id == "sensor.temp" for e in events)


async def test_suggest_accept_creates_running_automation(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))

    stats = await manager.async_run()
    assert len(stats["new"]) >= 2
    pending = manager.pending()
    ta = next(r for r in pending if r["suggestion"]["category"] == "trigger_action")
    assert ta["suggestion"]["evidence"]["target"] == "light.kitchen"
    assert hass.states.get("sensor.automation_advisor_pending_suggestions").state == str(len(pending))

    rec = await manager.async_decide(ta["id"], "accept")
    assert rec["status"] == STATUS_ACCEPTED
    entity_id = rec["result"]["entity_id"]
    assert hass.states.get(entity_id).state == "on"

    saved = yaml.safe_load(open(config_dir / "automations.yaml"))
    assert len(saved) == 1 and saved[0]["alias"] == ta["suggestion"]["title"]
    assert saved[0]["id"]

    # The new automation really works: drive the trigger and watch the light.
    # (Remove the time window so the test does not depend on the clock.)
    hass.states.async_set("light.kitchen", "off")
    calls = []
    hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    saved[0]["conditions"] = []
    (config_dir / "automations.yaml").write_text(yaml.safe_dump(saved))
    await hass.services.async_call(AUTOMATION_DOMAIN, "reload", blocking=True)
    hass.states.async_set("binary_sensor.kitchen_motion", "on", {"device_class": "motion"})
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in calls] == [["light.kitchen"]]

    # A re-run does not propose it again
    await manager.async_run()
    assert ta["id"] not in [r["id"] for r in manager.pending()]


async def test_dismiss_restore_and_purge(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))
    await manager.async_run()
    rec = manager.pending()[0]
    sid = rec["id"]

    await manager.async_decide(sid, "dismiss")
    assert manager.data["suggestions"][sid]["status"] == STATUS_DISMISSED
    # dismissed ideas are not re-proposed by the next run
    await manager.async_run()
    assert manager.data["suggestions"][sid]["status"] == STATUS_DISMISSED

    await manager.async_decide(sid, "restore")
    assert manager.data["suggestions"][sid]["status"] == STATUS_PENDING

    # still kept after 29 days, gone after 31
    await manager.async_decide(sid, "dismiss")
    manager.data["suggestions"][sid]["dismissed_at"] = time.time() - 29 * 86400
    manager._purge_dismissed(time.time())
    assert sid in manager.data["suggestions"]
    manager.data["suggestions"][sid]["dismissed_at"] = time.time() - 31 * 86400
    manager._purge_dismissed(time.time())
    assert sid not in manager.data["suggestions"]


async def test_accept_dismissed_suggestion(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))
    await manager.async_run()
    sid = manager.pending()[0]["id"]
    await manager.async_decide(sid, "dismiss")
    rec = await manager.async_decide(sid, "accept")
    assert rec["status"] == STATUS_ACCEPTED


HALL_CFG = {
    "id": "1700000000000",
    "alias": "Hall light on motion",
    "triggers": [{"trigger": "state", "entity_id": "binary_sensor.hall_motion", "to": "on"}],
    "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.hall"}}],
}


def _hall_override_events(daytime_only: bool) -> list[A.Event]:
    from .test_analysis import hall_automation_events

    evs = hall_automation_events("daytime" if daytime_only else "random")
    return _shift_to_now(
        [A.Event(e.ts, e.entity_id, e.old, e.new, e.cause,
                 "automation.hall_light_on_motion" if e.cause_ref == "automation.hall_light" else e.cause_ref)
         for e in evs]
    )


async def test_review_modify_and_delete(hass: HomeAssistant, config_dir) -> None:
    (config_dir / "automations.yaml").write_text(yaml.safe_dump([HALL_CFG]))
    entry = await _setup(hass)
    manager = entry.runtime_data
    assert hass.states.get("automation.hall_light_on_motion") is not None

    await hass.async_add_executor_job(manager.archive.add, _hall_override_events(True))
    await manager.async_run()
    review = next(r for r in manager.pending() if r["suggestion"]["category"] == "overridden")
    assert review["suggestion"]["options"][:2] == ["modify", "delete"]

    rec = await manager.async_decide(review["id"], "modify")
    assert rec["previous_config"]["alias"] == "Hall light on motion"
    saved = yaml.safe_load(open(config_dir / "automations.yaml"))
    assert saved[0]["id"] == HALL_CFG["id"]
    assert saved[0]["conditions"], "a time window condition was added"
    assert hass.states.get("automation.hall_light_on_motion").state == "on"


async def test_review_delete(hass: HomeAssistant, config_dir) -> None:
    (config_dir / "automations.yaml").write_text(yaml.safe_dump([HALL_CFG]))
    entry = await _setup(hass)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _hall_override_events(False))
    await manager.async_run()
    review = next(r for r in manager.pending() if r["suggestion"]["category"] == "overridden")
    assert "modify" not in review["suggestion"]["options"]
    await manager.async_decide(review["id"], "delete")
    await hass.async_block_till_done()
    assert yaml.safe_load(open(config_dir / "automations.yaml")) == []
    assert hass.states.get("automation.hall_light_on_motion") is None


async def test_websocket_and_options(hass: HomeAssistant, config_dir, hass_ws_client) -> None:
    entry = await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["retention_days"] == 30

    await client.send_json_auto_id({"type": f"{DOMAIN}/run"})
    msg = await client.receive_json()
    assert msg["success"]
    report = msg["result"]["report"]
    assert report["headline"] == "No activity recorded yet." and report["new"] == 0

    await client.send_json_auto_id({"type": f"{DOMAIN}/decide", "suggestion_id": "nope", "action": "accept"})
    msg = await client.receive_json()
    assert not msg["success"] and "Unknown suggestion" in msg["error"]["message"]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"interval_hours": 6, "lookback_days": 21, "min_occurrences": 4, "min_confidence": 0.7,
         "override_window_s": 60, "dismissed_retention_days": 30,
         "excluded_entities": ["light.hall"], "notify": False, "test_ignore_existing": True},
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["interval_hours"] == 6
    assert entry.runtime_data.collector.excluded == {"light.hall"}


async def test_config_flow_single_instance(hass: HomeAssistant, config_dir) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "menu" and result["menu_options"] == ["local", "remote"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "local"})
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["data"] == {"mode": "local"}
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "abort"


async def test_unavailable_recovery_and_cover_transitions(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass)
    manager = entry.runtime_data
    hass.states.async_set("light.kitchen", "unavailable")
    hass.states.async_set("light.kitchen", "on")  # device came back on: not a press
    hass.states.async_set("light.kitchen", "off")  # real press
    hass.states.async_set("cover.garage", "open")
    hass.states.async_set("cover.garage", "closing")
    hass.states.async_set("cover.garage", "closed")
    await hass.async_block_till_done()
    await manager.collector.async_flush()
    evs = await hass.async_add_executor_job(manager.archive.since, 0)
    assert [(e.entity_id, e.old, e.new) for e in evs] == [
        ("light.kitchen", "on", "off"),
        ("cover.garage", "open", "closed"),
    ]


async def test_stale_modify_is_refused(hass: HomeAssistant, config_dir) -> None:
    (config_dir / "automations.yaml").write_text(yaml.safe_dump([HALL_CFG]))
    entry = await _setup(hass)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _hall_override_events(True))
    await manager.async_run()
    review = next(r for r in manager.pending() if r["suggestion"]["category"] == "overridden")
    # the user edits the automation in the editor afterwards
    edited = {**HALL_CFG, "alias": "Hall light (edited)"}
    (config_dir / "automations.yaml").write_text(yaml.safe_dump([edited]))
    await hass.services.async_call(AUTOMATION_DOMAIN, "reload", blocking=True)
    import pytest
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError, match="edited after"):
        await manager.async_decide(review["id"], "modify")
    assert yaml.safe_load(open(config_dir / "automations.yaml"))[0]["alias"] == "Hall light (edited)"
    # disable still works and uses the service
    rec = await manager.async_decide(review["id"], "disable")
    assert rec["result"]["disabled"]
    assert hass.states.get("automation.hall_light_on_motion").state == "off"


async def test_broken_automation_and_decide_service(hass: HomeAssistant, config_dir) -> None:
    broken = {**HALL_CFG, "id": "999", "alias": "Old garage",
              "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.gone"}}]}
    (config_dir / "automations.yaml").write_text(yaml.safe_dump([broken]))
    entry = await _setup(hass)
    manager = entry.runtime_data
    await manager.async_run()
    rec = next(r for r in manager.pending() if r["suggestion"]["category"] == "broken")
    assert rec["suggestion"]["evidence"]["missing"] == ["light.gone"]
    result = await hass.services.async_call(
        DOMAIN, "decide", {"suggestion_id": rec["id"], "action": "delete"},
        blocking=True, return_response=True,
    )
    assert result["status"] == STATUS_ACCEPTED
    assert yaml.safe_load(open(config_dir / "automations.yaml")) == []


async def test_accept_without_automations_include_rolls_back(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass)
    manager = entry.runtime_data
    (config_dir / "configuration.yaml").write_text("automation: []\n")  # not included
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))
    await manager.async_run()
    sid = manager.pending()[0]["id"]
    import pytest
    from homeassistant.exceptions import HomeAssistantError

    with patch("custom_components.automation_advisor.automations.RELOAD_WAIT_S", 0.5), \
            pytest.raises(HomeAssistantError, match="automation: !include"):
        await manager.async_decide(sid, "accept")
    assert yaml.safe_load(open(config_dir / "automations.yaml")) == []
    assert manager.data["suggestions"][sid]["status"] == STATUS_PENDING


async def test_records_context_domains(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass)
    manager = entry.runtime_data
    hass.states.async_set("sensor.hall_lux", "300", {"device_class": "illuminance"})
    hass.states.async_set("scene.movie", "2026-11-17T10:00:00+00:00")
    hass.states.async_set("media_player.tv", "off")
    hass.states.async_set("lock.front", "locked")
    hass.states.async_set("sensor.temperature", "20", {"device_class": "temperature"})
    await hass.async_block_till_done()
    hass.states.async_set("sensor.hall_lux", "250", {"device_class": "illuminance"})  # same step
    hass.states.async_set("sensor.hall_lux", "40", {"device_class": "illuminance"})   # crosses 200/100/50
    hass.states.async_set("scene.movie", "2026-11-17T20:00:00+00:00")                 # activated
    hass.states.async_set("media_player.tv", "playing")
    hass.states.async_set("lock.front", "unlocking")
    hass.states.async_set("lock.front", "unlocked")
    hass.states.async_set("sensor.temperature", "21", {"device_class": "temperature"})  # not tracked
    await hass.async_block_till_done()
    await manager.collector.async_flush()
    evs = await hass.async_add_executor_job(manager.archive.since, 0)
    assert [(e.entity_id, e.old, e.new) for e in evs] == [
        ("sensor.hall_lux", "300.0", "40.0"),
        ("scene.movie", "", "activated"),
        ("media_player.tv", "off", "playing"),
        ("lock.front", "locked", "unlocked"),
    ]


async def test_clear_all_suggestions(hass: HomeAssistant, config_dir, hass_ws_client) -> None:
    entry = await _setup(hass)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))
    await manager.async_run()
    sid = manager.pending()[0]["id"]
    await manager.async_decide(sid, "dismiss")
    assert manager.data["suggestions"]
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/clear"})
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["removed"] >= 2
    assert manager.data["suggestions"] == {}
    await manager.async_run()  # a clean run proposes them again
    assert sid in manager.data["suggestions"]


async def test_button_automation_passes_home_assistants_validator(hass: HomeAssistant, config_dir) -> None:
    """The structure the advisor writes for buttons (restart, choose, repeat, stop, transition)
    is accepted by Home Assistant itself (state triggers stand in for the device triggers)."""
    from custom_components.automation_advisor import automations as autos
    from custom_components.automation_advisor.llm import buttons_to_automation

    await _setup(hass)
    trig = [{"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "to": "on"},
            {"trigger": "state", "entity_id": "binary_sensor.hall_motion", "to": "on"},
            {"trigger": "state", "entity_id": "binary_sensor.hall_motion", "to": "off"}]
    cfg, err = buttons_to_automation({"buttons": [
        {"event": 1, "button": "up", "do": "on", "target": {"entity_id": ["light.kitchen"]}},
        {"event": 2, "button": "hold", "do": "hold_dimmer", "target": {"entity_id": ["light.kitchen"]}},
        {"event": 3, "button": "release", "do": "stop"}]}, trig)
    assert err is None
    await autos.validate(hass, {"alias": "Remote", **cfg})
