"""Recorder backfill (internal schema, so it gets its own test) and the AI refinement step."""

from __future__ import annotations

import json
import pytest
import yaml
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.recorder import get_instance

from custom_components.automation_advisor import analysis as A
from custom_components.automation_advisor.collector import backfill_from_recorder
from custom_components.automation_advisor.const import DOMAIN

from .test_integration import _setup, _shift_to_now
from .test_analysis import kitchen_events


async def test_backfill_from_recorder(recorder_mock, hass: HomeAssistant) -> None:
    hass.states.async_set("light.porch", "off")
    await async_wait_recording_done(hass)
    hass.states.async_set("light.porch", "on", context=Context())  # wall switch
    hass.states.async_set("light.porch", "off", context=Context(user_id="0123456789abcdef0123456789abcdef"))  # app
    ctx = Context()
    hass.bus.async_fire("automation_triggered", {"entity_id": "automation.porch"}, context=ctx)
    hass.states.async_set("light.porch", "on", context=ctx)  # automation
    hass.states.async_set("light.porch", "on", {"brightness": 3}, context=Context())  # attr only
    hass.states.async_set("light.porch", "unavailable")
    hass.states.async_set("light.porch", "on")  # back from unavailable: not a transition
    await async_wait_recording_done(hass)

    events = await get_instance(hass).async_add_executor_job(
        backfill_from_recorder, hass, ["light.porch"], time.time() - 3600
    )
    porch = [(e.old, e.new, e.cause, e.cause_ref) for e in events if e.entity_id == "light.porch"]
    assert porch == [
        ("off", "on", A.CAUSE_MANUAL, None),
        ("on", "off", A.CAUSE_USER, None),
        ("off", "on", A.CAUSE_AUTOMATION, "automation.porch"),
    ]
    assert any(e.entity_id == "automation.porch" and e.new == "triggered" for e in events)


MOTION = [{"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "to": "on"}]
DARK = [{"condition": "or", "conditions": [
    {"condition": "sun", "after": "sunset", "after_offset": "-01:00:00"},
    {"condition": "sun", "before": "sunrise", "before_offset": "02:00:00"}]}]
ON = [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]


def _answer(ideas):
    return SimpleNamespace(data="<think>hmm</think>Sure! ```json\n" + json.dumps({"ideas": ideas}) + "\n```",
                           conversation_id="x")


async def test_only_ai_suggestions_are_shown(hass: HomeAssistant, config_dir) -> None:
    hass.states.async_set("lock.front", "locked", {"friendly_name": "Front door"})
    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))
    seen = {}

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        seen.setdefault("prompt", instructions)
        return _answer([
            {"kind": "create", "title": "Good idea", "why": "motion then light, evenings",
             "automation": {"triggers": MOTION, "conditions": DARK, "actions": ON}},
            {"title": "Every hour", "automation": {
                "triggers": [{"trigger": "time_pattern", "minutes": "0"}], "actions": ON}},
            {"title": "Unknown entity", "automation": {
                "triggers": [{"trigger": "state", "entity_id": "light.nope", "to": "on"}], "actions": ON}},
            {"title": "Unlock", "automation": {"triggers": MOTION,
                "actions": [{"action": "lock.unlock", "target": {"entity_id": "lock.front"}}]}},
            {"title": "Broken syntax", "automation": {
                "triggers": [{"trigger": "no_such_platform"}], "actions": ON}},
        ])

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        await manager.async_run()

    prompt = seen["prompt"]
    assert "LEADS FROM THE STATISTICS ENGINE" in prompt and "L1 [" in prompt
    assert "replay of the draft: Replayed on" in prompt          # leads carry their replay
    assert "DEVICES (entity_id | name | room | kind)" in prompt and "(by hand)" in prompt
    pending = manager.pending()
    assert [r["suggestion"]["title"] for r in pending] == ["Good idea"]   # no statistics cards
    assert pending[0]["suggestion"]["evidence"]["backtest"]["passes"]
    rep = manager.data["last_report"]
    assert rep["leads"] >= 2 and rep["headline"].startswith("The AI proposed 5 item(s); 1 held up")
    reasons = {r["title"]: r["reason"] for r in rep["ai"]["rejected"]}
    assert reasons["Every hour"].startswith("failed the replay")
    assert reasons["Unknown entity"].startswith("unknown entities")
    assert reasons["Unlock"].startswith("not allowed")
    assert reasons["Broken syntax"].startswith("invalid automation")


async def test_no_ai_means_no_suggestions(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass, show_statistical=False)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))
    await manager.async_run()
    assert manager.pending() == []
    assert manager.data["last_report"]["headline"].startswith("Connect an AI")
    assert manager.data["last_report"]["leads"] >= 2


async def test_ai_fixes_existing_automation(hass: HomeAssistant, config_dir) -> None:
    from .test_integration import HALL_CFG, _hall_override_events

    (config_dir / "automations.yaml").write_text(yaml.safe_dump([HALL_CFG]))
    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _hall_override_events(True))
    target = "automation.hall_light_on_motion"
    fixed = {"triggers": HALL_CFG["triggers"], "conditions": [
        {"condition": "time", "after": "17:00:00", "before": "23:59:00"}], "actions": HALL_CFG["actions"]}

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        assert f"existing automation: {target}" in instructions  # the overridden lead
        return _answer([
            {"kind": "modify", "target_automation": target, "title": "Hall light only in the evening",
             "why": "You switch it off straight away in the daytime.", "automation": fixed},
            {"kind": "delete", "target_automation": target, "title": "Delete it", "why": "meh"},
        ])

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        await manager.async_run()
    assert len(manager.pending()) == 1  # fix and delete for one automation = one card
    rec = manager.pending()[0]
    sg = rec["suggestion"]
    assert sg["kind"] == "review" and sg["target_automation"] == target
    assert sg["options"] == ["modify", "delete", "disable"], manager.data["last_report"]["ai"]["rejected"]
    assert "Alternatively (delete it)" in sg["description"]
    ev = sg["evidence"]
    assert ev["backtest"]["unwanted"] < ev["backtest_before"]["unwanted"]
    await manager.async_decide(rec["id"], "modify")
    saved = yaml.safe_load(open(config_dir / "automations.yaml"))
    assert saved[0]["id"] == HALL_CFG["id"] and saved[0]["conditions"][0]["condition"] == "time"


async def test_ai_failure_keeps_statistics(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass, ai_task_entity="ai_task.fake")
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))
    with patch("custom_components.automation_advisor.llm._generate",
               AsyncMock(side_effect=RuntimeError("model offline"))):
        await manager.async_run()
    rep = manager.data["last_report"]
    assert rep["ai"]["error"].endswith("model offline") and rep["retry_at"] > time.time() + 3000
    assert len(manager.pending()) >= 2


async def test_failed_idea_is_revised_with_replay_feedback(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))
    calls = []

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        calls.append(instructions)
        if len(calls) == 1:  # first try: no time window, lights on all day
            return _answer([{"title": "Kitchen motion light", "automation": {"triggers": MOTION, "actions": ON}}])
        assert "did not hold up when they were replayed" in instructions
        assert "you probably would not have wanted" in instructions
        return _answer([{"title": "Kitchen motion light", "why": "only when dark",
                         "automation": {"triggers": MOTION, "conditions": DARK, "actions": ON}}])

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        await manager.async_run()
    ai = manager.data["last_report"]["ai"]
    assert len(calls) == 2 and ai["revised"] == 1, ai
    assert ai["accepted"] == ["Kitchen motion light (revised)"]
    sg = manager.pending()[0]["suggestion"]
    assert sg["evidence"]["revised"] and sg["evidence"]["backtest"]["passes"]


def test_lenient_json_parsing():
    from custom_components.automation_advisor.llm import _parse

    assert _parse('Here you go:\n{"ideas": [{"title": "a", "mode": "single",},], // note\n "x": True}') == {
        "ideas": [{"title": "a", "mode": "single"}], "x": True}


async def test_ollama_entity_streams_thinking_and_answer(hass: HomeAssistant, aioclient_mock, caplog) -> None:
    from homeassistant.config_entries import ConfigSubentryData
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.automation_advisor import llm

    entry = MockConfigEntry(domain="ollama", data={"url": "http://ollama.test:11434"},
                            subentries_data=[ConfigSubentryData(
                                data={"model": "gemma4:12b-it-q4_K_M", "num_ctx": 16384,
                                      "keep_alive": 600, "think": True},
                                subentry_type="ai_task_data", title="Ollama AI Task", unique_id=None)])
    entry.add_to_hass(hass)
    sub_id = next(iter(entry.subentries))
    er.async_get(hass).async_get_or_create("ai_task", "ollama", "x", config_entry=entry,
                                           config_subentry_id=sub_id, suggested_object_id="ollama_ai_task")
    lines = [
        {"message": {"role": "assistant", "thinking": "The basement "}},
        {"message": {"role": "assistant", "thinking": "stays on."}},
        {"message": {"role": "assistant", "content": '{"ideas": '}},
        {"message": {"role": "assistant", "content": "[]}"}, "done": True, "eval_count": 5},
    ]
    aioclient_mock.post("http://ollama.test:11434/api/chat",
                        text="\n".join(json.dumps(x) for x in lines) + "\n")
    got = []
    res = await llm._generate(hass, task_name="t", entity_id="ai_task.ollama_ai_task",
                              instructions="hi", on_delta=lambda k, t: got.append((k, t)))
    assert res.data == '{"ideas": []}'
    assert [t for k, t in got if k == "thinking"] == ["The basement ", "stays on."]
    assert "AI thinking › The basement stays on." in caplog.text   # mirrored to the terminal
    assert 'AI › {"ideas": []}' in caplog.text
    sent = aioclient_mock.mock_calls[0][2]
    assert sent["model"] == "gemma4:12b-it-q4_K_M" and sent["think"] is True
    assert sent["options"]["num_ctx"] == 16384 and sent["keep_alive"] == "600s"
    assert sent["format"] == "json"  # Ollama's JSON mode: always valid JSON
    # a non-Ollama entity still goes through AI Task
    assert llm._ollama_settings(hass, "ai_task.other") is None


async def test_live_progress_is_published(hass: HomeAssistant, config_dir, hass_ws_client) -> None:
    entry = await _setup(hass)
    manager = entry.runtime_data
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/live"})
    assert (await client.receive_json())["success"]
    first = await client.receive_json()
    assert first["event"]["live"] is None  # idle
    await manager.async_run()
    steps = []
    while True:
        msg = await client.receive_json()
        live = msg["event"]["live"]
        if live is None:
            break
        steps.append(live["step"])
    assert steps[0].startswith("Reading the activity log")
    assert any(s.startswith("Finding patterns") for s in steps)


async def test_delete_without_evidence_is_refused(hass: HomeAssistant, config_dir) -> None:
    from .test_integration import HALL_CFG

    (config_dir / "automations.yaml").write_text(yaml.safe_dump([HALL_CFG]))
    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        return _answer([{"kind": "delete", "target_automation": "automation.hall_light_on_motion",
                         "title": "Delete hall", "why": "no reason"}])

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        await manager.async_run()
    assert manager.pending() == []
    reasons = {r["title"]: r["reason"] for r in manager.data["last_report"]["ai"]["rejected"]}
    assert reasons["Delete hall"].startswith("no evidence")


async def test_identical_fixes_are_not_duplicated(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))
    same = {"triggers": MOTION, "conditions": DARK, "actions": ON}

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        return _answer([{"title": "A", "automation": same}, {"title": "B", "automation": same}])

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        await manager.async_run()
    assert [r["suggestion"]["title"] for r in manager.pending()] == ["A"]


async def test_test_mode_gives_the_ai_a_blank_slate(hass: HomeAssistant, config_dir) -> None:
    """Test mode: no automations in the AI's context, their actions look manual, only new
    automations are accepted."""
    from .test_analysis import hall_automation_events
    from .test_integration import HALL_CFG

    (config_dir / "automations.yaml").write_text(yaml.safe_dump([HALL_CFG]))
    hass.states.async_set("binary_sensor.hall_motion", "off", {"device_class": "motion"})
    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False,
                         test_ignore_existing=True)
    manager = entry.runtime_data
    evs = _shift_to_now([A.Event(e.ts, e.entity_id, e.old, e.new, e.cause,
                                 "automation.hall_light_on_motion" if e.cause_ref else None)
                         for e in hall_automation_events("daytime")])
    await hass.async_add_executor_job(manager.archive.add, evs)
    seen = {}

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        seen.setdefault("prompt", instructions)
        return _answer([
            {"kind": "modify", "target_automation": "automation.hall_light_on_motion",
             "title": "Fix hall", "automation": {"triggers": MOTION, "actions": ON}},
            {"kind": "adopt", "lead": "L1", "title": "Adopted in test mode"},
            {"kind": "create", "title": "Hall light with motion in the evening", "automation": {
                "triggers": [{"trigger": "state", "entity_id": "binary_sensor.hall_motion", "to": "on"}],
                "conditions": [{"condition": "time", "after": "17:00:00"}],
                "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.hall"}}]}},
        ])

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        await manager.async_run()
    prompt = seen["prompt"]
    assert prompt.startswith("TEST MODE")
    assert "EXISTING AUTOMATIONS: none." in prompt
    assert "hall_light_on_motion" not in prompt and "Hall light on motion" not in prompt
    assert "(auto:" not in prompt and "light.hall on (by hand)" in prompt
    assert "existing automation" not in prompt.split("LEADS FROM THE STATISTICS ENGINE:")[1].split("HOME:")[0]
    titles = [r["suggestion"]["title"] for r in manager.pending()]
    assert "Hall light with motion in the evening" in titles
    rejected = {r["title"] for r in manager.data["last_report"]["ai"]["rejected"]}
    assert "Adopted in test mode" in titles or "Adopted in test mode" not in str(
        [r for r in manager.data["last_report"]["ai"]["rejected"] if "only new" in r["reason"]])
    rep = manager.data["last_report"]
    assert rep["test_mode"] and rep["automations"] == []
    assert {"title": "Fix hall", "reason": "test mode: only new automations"} in rep["ai"]["rejected"]


async def test_ai_can_adopt_a_lead_exactly(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))
    seen = {}

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        seen["p"] = instructions
        n = re.search(r"L(\d+) \[reacts to a sensor\]", instructions).group(1)
        return _answer([{"kind": "adopt", "lead": f"L{n}", "title": "Kitchen light after dark",
                         "why": "You always switch it on when you walk in after dark."},
                        {"kind": "adopt", "lead": "L99", "title": "Nope"}])

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        await manager.async_run()
    sg = manager.pending()[0]["suggestion"]
    assert sg["title"] == "Kitchen light after dark" and sg["evidence"]["adopted_lead"].startswith("L")
    assert sg["automation"]["triggers"][0]["entity_id"] == "binary_sensor.kitchen_motion"
    assert sg["automation"]["conditions"]  # the lead's dark window came along exactly
    reasons = {r["title"]: r["reason"] for r in manager.data["last_report"]["ai"]["rejected"]}
    assert reasons["Nope"].startswith("unknown lead")


async def _one_ai_suggestion(hass, config_dir):
    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _shift_to_now(kitchen_events()))

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        return _answer([{"kind": "create", "title": "Kitchen light with motion after dark",
                         "does": "When the kitchen motion sensor sees someone after dark, it turns on the kitchen light.",
                         "why": "You do this every evening.",
                         "automation": {"triggers": MOTION, "conditions": DARK, "actions": ON}}])

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        await manager.async_run()
    return manager, manager.pending()[0]


async def test_suggestions_explain_what_they_do(hass: HomeAssistant, config_dir) -> None:
    manager, rec = await _one_ai_suggestion(hass, config_dir)
    ev = rec["suggestion"]["evidence"]
    assert ev["does"].startswith("When the kitchen motion sensor sees someone")
    assert ev["exactly"].startswith("When Kitchen motion becomes on, but only if (it is after 1 hour before sunset")
    assert ev["exactly"].endswith("it will turn on Kitchen light.")
    snap = manager.snapshot()["suggestions"][0]
    assert "triggers:" in snap["yaml"] and not snap["yaml"].startswith("id:") and "\nid:" not in snap["yaml"]


async def test_edit_yaml_revalidates_and_replays(hass: HomeAssistant, config_dir, hass_ws_client) -> None:
    manager, rec = await _one_ai_suggestion(hass, config_dir)
    before = rec["suggestion"]["evidence"]["backtest"]
    client = await hass_ws_client(hass)
    no_window = rec["yaml"] if "yaml" in rec else manager.snapshot()["suggestions"][0]["yaml"]
    no_window = no_window.split("conditions:")[0] + "actions:" + no_window.split("actions:")[1]
    await client.send_json_auto_id({"type": f"{DOMAIN}/edit", "suggestion_id": rec["id"], "yaml": no_window})
    msg = await client.receive_json()
    assert msg["success"], msg
    sg = manager.data["suggestions"][rec["id"]]["suggestion"]
    assert sg["automation"].get("conditions") in (None, [])
    assert sg["evidence"]["backtest"]["unwanted"] > before["unwanted"]  # replay was redone
    assert "but only if" not in sg["evidence"]["exactly"]
    assert sg["evidence"]["edits"][-1]["how"] == "yaml"
    assert "does" not in sg["evidence"]  # the AI's wording described the old version
    # bad edits are refused with a clear message and change nothing
    for bad, words in (("triggers: [", "not valid YAML"),
                       (no_window.replace("light.kitchen", "light.nope"), "unknown entities")):
        await client.send_json_auto_id({"type": f"{DOMAIN}/edit", "suggestion_id": rec["id"], "yaml": bad})
        msg = await client.receive_json()
        assert not msg["success"] and words in msg["error"]["message"]
    # accepting creates the edited version
    created = await manager.async_decide(rec["id"], "accept")
    saved = yaml.safe_load(open(config_dir / "automations.yaml"))
    assert saved[-1].get("conditions") in (None, []) and created["status"] == "accepted"


async def test_edit_with_prompt(hass: HomeAssistant, config_dir) -> None:
    manager, rec = await _one_ai_suggestion(hass, config_dir)
    seen = {}

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        seen["p"] = instructions
        return _answer_one({"title": "Kitchen light with motion after 22:00",
                            "does": "After 22:00, motion in the kitchen turns on the kitchen light.",
                            "why": "Only late at night.",
                            "automation": {"triggers": MOTION, "conditions": [
                                {"condition": "time", "after": "22:00:00", "before": "06:00:00"}], "actions": ON}})

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        await manager.async_edit(rec["id"], instruction="only after 10 pm")
    assert 'The person asks: "only after 10 pm"' in seen["p"] and "light.kitchen | Kitchen" in seen["p"]
    sg = manager.data["suggestions"][rec["id"]]["suggestion"]
    assert sg["title"] == "Kitchen light with motion after 22:00"
    assert sg["evidence"]["does"].startswith("After 22:00")
    assert "it is between 22:00 and 06:00" in sg["evidence"]["exactly"]
    assert sg["evidence"]["edits"][-1] == {"at": sg["evidence"]["edits"][-1]["at"], "how": "prompt",
                                           "instruction": "only after 10 pm"}


def _answer_one(obj):
    return SimpleNamespace(data=json.dumps(obj), conversation_id="x")


async def test_manual_only_never_runs_or_retries_by_itself(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass, ai_task_entity="ai_task.fake", interval_hours=0)
    manager = entry.runtime_data
    assert manager.manual_only and manager.next_run is None
    with patch("custom_components.automation_advisor.llm._generate",
               AsyncMock(side_effect=RuntimeError("Ollama is off"))):
        await manager.async_run()
    assert "retry_at" not in manager.data["last_report"] and manager.next_run is None
    assert manager.snapshot()["manual_only"]


async def test_ask_ai_to_fix_an_existing_automation(hass: HomeAssistant, config_dir, hass_ws_client) -> None:
    from .test_integration import HALL_CFG, _hall_override_events

    (config_dir / "automations.yaml").write_text(yaml.safe_dump([HALL_CFG]))
    hass.states.async_set("binary_sensor.hall_motion", "off", {"device_class": "motion", "friendly_name": "Hall motion"})
    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data
    await hass.async_add_executor_job(manager.archive.add, _hall_override_events(True))
    # one real run, so Home Assistant keeps a trace
    hass.services.async_register("light", "turn_on", lambda call: None)
    hass.states.async_set("binary_sensor.hall_motion", "on", {"device_class": "motion"})
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/automations"})
    autos = (await client.receive_json())["result"]
    assert {"entity_id": "automation.hall_light_on_motion", "alias": "Hall light on motion",
            "editable": True, "enabled": True} in autos

    seen = {}
    fixed = {"triggers": HALL_CFG["triggers"],
             "conditions": [{"condition": "time", "after": "17:00:00", "before": "23:59:00"}],
             "actions": HALL_CFG["actions"]}

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        seen["p"] = instructions
        return _answer_one({"title": "Hall light with motion in the evening only",
                            "does": "From 17:00 to midnight, motion in the hall turns on the hall light.",
                            "why": "It also ran in the daytime, when you switch it straight off.",
                            "automation": fixed})

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        await client.send_json_auto_id({"type": f"{DOMAIN}/fix", "automation": "automation.hall_light_on_motion",
                                        "problem": "it turns the light on during the day"})
        msg = await client.receive_json()
    assert msg["success"], msg
    p = seen["p"]
    assert 'The person says: "it turns the light on during the day"' in p
    assert "Its last runs (Home Assistant traces" in p and "result: finished" in p
    assert "light.hall" in p and "(by hand)" in p                     # device activity
    assert "Replay of the current automation" in p and "you switched it back" in p
    rec = msg["result"]
    sg = rec["suggestion"]
    assert sg["kind"] == "review" and sg["target_automation"] == "automation.hall_light_on_motion"
    assert sg["evidence"]["asked"] == "it turns the light on during the day"
    assert sg["evidence"]["backtest"]["unwanted"] < sg["evidence"]["backtest_before"]["unwanted"]
    assert "it is between 17:00 and 23:59" in sg["evidence"]["exactly"]
    # it is a normal pending suggestion: apply it
    await manager.async_decide(rec["id"], "modify")
    saved = yaml.safe_load(open(config_dir / "automations.yaml"))
    assert saved[0]["id"] == HALL_CFG["id"] and saved[0]["conditions"][0]["after"] == "17:00:00"
    # a later analysis does not throw the asked-for fix away
    assert manager.data["suggestions"][rec["id"]]["status"] == "accepted"


async def test_fix_that_changes_nothing_is_reported_not_shown(hass: HomeAssistant, config_dir) -> None:
    from homeassistant.exceptions import HomeAssistantError
    from .test_integration import HALL_CFG

    (config_dir / "automations.yaml").write_text(yaml.safe_dump([HALL_CFG]))
    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        return _answer_one({"title": "Same", "why": "No traces show a problem.",
                            "automation": {k: HALL_CFG[k] for k in ("triggers", "actions")}})

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate), \
            pytest.raises(HomeAssistantError, match="did not change anything.*No traces show a problem"):
        await manager.async_fix("automation.hall_light_on_motion", "it is too slow")
    assert manager.pending() == [] and not manager.running


async def test_applied_fix_updates_the_original_card(hass: HomeAssistant, config_dir) -> None:
    manager, rec = await _one_ai_suggestion(hass, config_dir)
    created = await manager.async_decide(rec["id"], "accept")
    entity_id = created["result"]["entity_id"]
    applied = [r for r in manager.data["suggestions"].values() if r["status"] == "accepted"]
    assert len(applied) == 1
    for n, problem in enumerate(["turns on too early", "turns on too late"]):
        async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
            return _answer_one({"title": f"Kitchen light v{n + 2}", "why": "fixed",
                                "automation": {"triggers": MOTION, "conditions": [
                                    {"condition": "time", "after": f"1{8 + n}:00:00"}], "actions": ON}})
        with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
            fix = await manager.async_fix(entity_id, problem)
        assert fix["suggestion"]["kind"] == "review"  # modifies the created automation in place
        merged = await manager.async_decide(fix["id"], "modify")
        assert merged["id"] == created["id"]
    applied = [r for r in manager.data["suggestions"].values() if r["status"] == "accepted"]
    assert len(applied) == 1                           # still one card
    card = applied[0]
    assert card["suggestion"]["title"] == "Kitchen light v3"
    assert [h["asked"] for h in card["history"]] == ["turns on too early", "turns on too late"]
    saved = yaml.safe_load(open(config_dir / "automations.yaml"))
    assert len(saved) == 1 and saved[0]["conditions"][0]["after"] == "19:00:00"  # one automation


async def test_ai_sets_up_a_new_remote(hass: HomeAssistant, config_dir, hass_ws_client) -> None:
    from homeassistant.helpers import area_registry as ar, device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data
    bedroom = ar.async_get(hass).async_create("Bedroom")
    zha = MockConfigEntry(domain="zha")
    zha.add_to_hass(hass)
    remote = dr.async_get(hass).async_get_or_create(
        config_entry_id=zha.entry_id, identifiers={("zha", "remote1")}, name="IKEA 4-button remote",
        manufacturer="IKEA of Sweden", model="RODRET")
    dr.async_get(hass).async_update_device(remote.id, area_id=bedroom.id)
    hass.states.async_set("light.bedroom_ceiling", "off", {"friendly_name": "Bedroom ceiling"})
    triggers = [{"platform": "device", "domain": "zha", "device_id": remote.id,
                 "type": "remote_button_short_press", "subtype": s}
                for s in ("turn_on", "turn_off", "dim_down", "dim_up")]

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/devices"})
    devices = (await client.receive_json())["result"]
    assert devices[0]["name"] == "IKEA 4-button remote" and devices[0]["area"] == "Bedroom"  # newest first

    seen = {}

    calls = []

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        calls.append(instructions)
        seen.setdefault("p", instructions)
        if len(calls) == 1:  # event 9 does not exist: must be repaired
            return _answer_one({"title": "x", "buttons": [{"event": 9, "button": "up", "do": "on",
                                                            "target": {"area_id": bedroom.id}}]})
        assert "event 9 does not exist (the device has 4)" in instructions
        return _answer_one({
            "title": "Bedroom remote controls the bedroom lights",
            "does": "Up turns the bedroom lights on, down off, left brighter, right dimmer.",
            "buttons": [
                {"event": 1, "button": "up", "do": "on", "target": {"area_id": bedroom.id}},
                {"event": 2, "button": "down", "do": "off", "target": {"area_id": bedroom.id}},
                {"event": 4, "button": "left", "do": "brighter", "target": {"entity_id": ["light.bedroom_ceiling"]}, "step_pct": 10},
                {"event": 3, "button": "right", "do": "dimmer", "target": {"entity_id": ["light.bedroom_ceiling"]}, "step_pct": 10}],
            "notes": "Pair the remote with ZHA first if you have not."})

    seen = {}
    with patch("custom_components.automation_advisor.manager.AdvisorManager._context",
               autospec=True) as ctx_patch, \
            patch.object(manager.source, "async_device_triggers", AsyncMock(return_value=triggers)), \
            patch("custom_components.automation_advisor.automations.validate", AsyncMock()), \
            patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        from custom_components.automation_advisor import analysis as AA
        from zoneinfo import ZoneInfo
        ctx_patch.return_value = AA.Context(events=[], entities={}, automations=[], tz=ZoneInfo("UTC"))
        await client.send_json_auto_id({"type": f"{DOMAIN}/build", "device_id": remote.id,
                                        "request": "up/down turn my bedroom on/off, left brighter, right dimmer"})
        msg = await client.receive_json()
    assert msg["success"], msg
    p = seen["p"]
    assert 'They ask: "up/down turn my bedroom on/off, left brighter, right dimmer"' in p
    assert "THE DEVICE: IKEA 4-button remote" in p and "1. remote button short press / turn_on" in p
    assert f"{bedroom.id} | Bedroom" in p and "light.bedroom_ceiling | Bedroom ceiling" in p
    res = msg["result"]
    assert len(calls) == 2 and res["problems"] == [] and res["notes"].startswith("Pair the remote")
    sg = res["suggestions"][0]["suggestion"]
    cfg = sg["automation"]
    assert [t["id"] for t in cfg["triggers"]] == ["up", "down", "left", "right"]
    assert cfg["triggers"][2]["subtype"] == "dim_up" and cfg["triggers"][0]["device_id"] == remote.id
    opts = cfg["actions"][0]["choose"]
    assert [o["conditions"][0]["id"] for o in opts] == ["up", "down", "left", "right"]
    assert opts[2]["sequence"][0] == {"action": "light.turn_on", "target": {"entity_id": ["light.bedroom_ceiling"]},
                                      "data": {"brightness_step_pct": 10, "transition": 0}}
    assert opts[3]["sequence"][0]["data"] == {"brightness_step_pct": -10, "transition": 0}
    assert cfg["mode"] == "restart"  # a new press takes over at once
    ex = sg["evidence"]["exactly"]
    assert "if it was “left”: make Bedroom ceiling brighter by 10 %" in ex
    assert "if it was “up”: turn on all lights in Bedroom" in ex
    assert sg["evidence"]["backtest"]["summary"].startswith("New set-up")


async def test_set_up_repairs_a_rejected_automation(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data
    hass.states.async_set("light.kitchen", "off", {"friendly_name": "Kitchen light"})
    calls = []

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        calls.append(instructions)
        if len(calls) == 1:  # no "trigger" key: Home Assistant rejects it
            trig = [{"entity_id": "binary_sensor.kitchen_motion", "to": "on"}]
        else:
            assert "were rejected" in instructions and "problem:" in instructions
            trig = MOTION
        return _answer_one({"items": [{"title": "Kitchen light at night", "automation": {
            "triggers": trig, "conditions": [{"condition": "time", "after": "23:00:00"}], "actions": ON}}],
            "notes": ""})

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        res = await manager.async_build("kitchen light on with motion after 11 pm")
    assert len(calls) == 2 and len(res["suggestions"]) == 1 and res["problems"] == []
    assert res["suggestions"][0]["suggestion"]["automation"]["triggers"] == MOTION


async def test_invalid_json_is_retried_with_the_error(hass: HomeAssistant, config_dir) -> None:
    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data
    hass.states.async_set("light.kitchen", "off", {"friendly_name": "Kitchen light"})
    calls = []
    good = {"items": [{"title": "Kitchen light at night", "automation": {
        "triggers": MOTION, "conditions": [{"condition": "time", "after": "23:00:00"}], "actions": ON}}],
        "notes": ""}

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        calls.append(instructions)
        if len(calls) == 1:  # a missing comma, like the real failure
            return SimpleNamespace(data='{"items": [{"title": "x" "automation": {}}]}', conversation_id="x")
        assert "was not valid JSON (Expecting ',' delimiter" in instructions
        return _answer_one(good)

    with patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        res = await manager.async_build("kitchen light on with motion after 11 pm")
    assert len(calls) == 2 and len(res["suggestions"]) == 1

    async def always_broken(hass_, **kw):
        return SimpleNamespace(data="{nope", conversation_id="x")

    from homeassistant.exceptions import HomeAssistantError
    with patch("custom_components.automation_advisor.llm._generate", side_effect=always_broken), \
            pytest.raises(HomeAssistantError, match="not valid JSON twice"):
        await manager.async_build("anything")
    assert not manager.running


def test_choose_structure_is_checked():
    from custom_components.automation_advisor.llm import check_build
    catalog = {"areas": [], "devices": [], "entities": {"binary_sensor.m": {}, "light.x": {}}}
    trig = lambda i: {"trigger": "state", "entity_id": "binary_sensor.m", "to": i, "id": i}  # noqa: E731
    opt = lambda i: {"conditions": [{"condition": "trigger", "id": i}],  # noqa: E731
                     "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.x"}}]}
    ok = {"triggers": [trig("on"), trig("off")], "actions": [{"choose": [opt("on"), opt("off")]}]}
    assert check_build(ok, catalog) is None
    unused = {"triggers": [trig("on"), trig("off")], "actions": [{"choose": [opt("on")]}]}
    assert "['off'] have no choose option" in check_build(unused, catalog)
    missing = {"triggers": [trig("on")], "actions": [{"choose": [opt("on"), opt("left")]}]}
    assert "['left'] that no trigger has" in check_build(missing, catalog)


def test_dangerous_actions_are_never_allowed():
    from custom_components.automation_advisor.llm import _unsafe

    def cfg(*actions):
        return {"actions": list(actions)}

    ok = cfg({"action": "light.turn_on", "target": {"entity_id": "light.x"}},
             {"action": "script.goodnight"}, {"action": "notify.mobile_app_phone", "data": {"message": "hi"}},
             {"action": "cover.open_cover", "target": {"entity_id": "cover.living_room_blinds"}})
    assert _unsafe(ok) is None
    for bad in ("lock.unlock", "alarm_control_panel.alarm_disarm", "shell_command.wipe",
                "python_script.x", "hassio.host_reboot", "homeassistant.restart", "homeassistant.stop",
                "recorder.purge", "automation.turn_off", "rest_command.call", "logger.set_level"):
        assert _unsafe(cfg({"action": bad})) == f"uses {bad}", bad
    nested = cfg({"choose": [{"conditions": [], "sequence": [{"service": "Lock.Unlock"}]}]})
    assert _unsafe(nested) == "uses lock.unlock"
    garage = cfg({"action": "cover.open_cover", "target": {"entity_id": "cover.garage_door"}})
    assert "garage" in _unsafe(garage)
    assert "templated" in _unsafe(cfg({"action": "{{ 'lock.unlock' }}"}))


def test_hold_to_dim_loops_until_release():
    from custom_components.automation_advisor.describe import describe
    from custom_components.automation_advisor.llm import buttons_to_automation

    trig = [{"platform": "device", "domain": "zha", "device_id": "r", "type": t, "subtype": st}
            for t, st in (("remote_button_short_press", "turn_on"), ("remote_button_long_press", "left"),
                          ("remote_button_long_release", "left"))]
    target = {"area_id": "bedroom"}
    cfg, err = buttons_to_automation({"buttons": [
        {"event": 1, "button": "up", "do": "on", "target": target},
        {"event": 2, "button": "left hold", "do": "hold_brighter", "target": target, "step_pct": 10},
        {"event": 3, "button": "left release", "do": "stop"}]}, trig)
    assert err is None and cfg["mode"] == "restart"
    hold = cfg["actions"][0]["choose"][1]["sequence"][0]["repeat"]
    assert hold["count"] == 20
    assert hold["sequence"] == [
        {"action": "light.turn_on", "target": target, "data": {"brightness_step_pct": 10, "transition": 0.25}},
        {"delay": {"milliseconds": 250}}]
    assert cfg["actions"][0]["choose"][2]["sequence"] == [{"stop": "Button released"}]
    text = describe(cfg, {"bedroom": "Bedroom", "r": "Remote"})
    assert "if it was “left_hold”: repeat up to 20 times (make all lights in Bedroom brighter by 10 %, then wait 250 ms) until the button is released" in text
    assert "if it was “left_release”: stop" in text
    # a hold without a release would run on after letting go: refused, so the AI fixes it
    _, err = buttons_to_automation({"buttons": [
        {"event": 2, "button": "left hold", "do": "hold_brighter", "target": target}]}, trig)
    assert "no 'stop' on a release event" in err


def test_repeated_button_words_get_readable_ids():
    from custom_components.automation_advisor.llm import buttons_to_automation

    trig = [{"platform": "device", "domain": "zha", "device_id": "r", "type": t, "subtype": "left"}
            for t in ("remote_button_short_press", "remote_button_long_press", "remote_button_long_release")]
    t = {"area_id": "bedroom"}
    cfg, err = buttons_to_automation({"buttons": [
        {"event": 1, "button": "left", "do": "brighter", "target": t},
        {"event": 2, "button": "left", "do": "hold_brighter", "target": t},
        {"event": 3, "button": "left", "do": "stop"}]}, trig)
    assert err is None and [x["id"] for x in cfg["triggers"]] == ["left", "left_hold", "left_release"]


async def test_fix_of_a_button_automation_remaps_the_buttons(hass: HomeAssistant, config_dir) -> None:
    """A remote automation (device triggers, area targets) asked to be faster with hold-to-dim:
    the fix goes through the button mapping and passes the device/area-aware checks
    (it used to fail with "refers to no devices")."""
    from homeassistant.helpers import area_registry as ar, device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from zoneinfo import ZoneInfo

    entry = await _setup(hass, ai_task_entity="ai_task.fake", show_statistical=False)
    manager = entry.runtime_data
    kitchen = ar.async_get(hass).async_create("Kitchen")
    zha = MockConfigEntry(domain="zha")
    zha.add_to_hass(hass)
    remote = dr.async_get(hass).async_get_or_create(
        config_entry_id=zha.entry_id, identifiers={("zha", "r2")}, name="Kitchen dimmer", model="STYRBAR")
    dev_trig = [{"platform": "device", "domain": "zha", "device_id": remote.id,
                 "type": t, "subtype": s}
                for t, s in (("remote_button_short_press", "dim_up"), ("remote_button_short_press", "dim_down"),
                             ("remote_button_long_press", "dim_up"), ("remote_button_long_press", "dim_down"),
                             ("remote_button_long_release", "dim_up"), ("remote_button_long_release", "dim_down"))]
    old = {"alias": "Kitchen light dimming and brightening", "mode": "single",
           "triggers": [{**dev_trig[0], "trigger": "device", "id": "up"},
                        {**dev_trig[1], "trigger": "device", "id": "down"}],
           "actions": [{"choose": [
               {"conditions": [{"condition": "trigger", "id": "up"}],
                "sequence": [{"action": "light.turn_on", "target": {"area_id": kitchen.id},
                              "data": {"brightness_step_pct": 10, "transition": 1}}]},
               {"conditions": [{"condition": "trigger", "id": "down"}],
                "sequence": [{"action": "light.turn_on", "target": {"area_id": kitchen.id},
                              "data": {"brightness_step_pct": -10, "transition": 1}}]}]}]}
    auto = A.ExistingAutomation("automation.kitchen_dimming", old["alias"], config_id="k1",
                                editable=True, config=old)
    calls = []

    async def fake_generate(hass_, *, task_name, entity_id, instructions, **kw):
        calls.append(instructions)
        return _answer_one({
            "title": "Kitchen dimmer: instant steps, hold to dim",
            "does": "Up/down step the kitchen lights at once; hold to keep changing, release to stop.",
            "buttons": [
                {"event": 1, "button": "up", "do": "brighter", "target": {"area_id": kitchen.id}, "step_pct": 10},
                {"event": 2, "button": "down", "do": "dimmer", "target": {"area_id": kitchen.id}, "step_pct": 10},
                {"event": 3, "button": "up", "do": "hold_brighter", "target": {"area_id": kitchen.id}},
                {"event": 4, "button": "down", "do": "hold_dimmer", "target": {"area_id": kitchen.id}},
                {"event": 5, "button": "up", "do": "stop"},
                {"event": 6, "button": "down", "do": "stop"}]})

    with patch("custom_components.automation_advisor.manager.AdvisorManager._context",
               autospec=True) as ctx_patch, \
            patch.object(manager.source, "async_device_triggers", AsyncMock(return_value=dev_trig)), \
            patch("custom_components.automation_advisor.automations.validate", AsyncMock()), \
            patch("custom_components.automation_advisor.llm._generate", side_effect=fake_generate):
        ctx_patch.return_value = A.Context(events=[], entities={}, automations=[auto], tz=ZoneInfo("UTC"))
        rec = await manager.async_fix("automation.kitchen_dimming",
                                      "It lags when I press quickly; make it instant and let me hold up/down to dim")
    assert len(calls) == 1
    p = calls[0]
    assert "THE DEVICE: Kitchen dimmer" in p and "make it instant" in p
    assert "currently set up like this" in p and "Kitchen" in p
    sg = rec["suggestion"]
    assert sg["kind"] == "review" and sg["target_automation"] == "automation.kitchen_dimming"
    cfg = sg["automation"]
    assert cfg["id"] == "k1" and cfg["mode"] == "restart"
    assert cfg["alias"] == "Kitchen light dimming and brightening"
    opts = cfg["actions"][0]["choose"]
    assert opts[0]["sequence"][0]["data"] == {"brightness_step_pct": 10, "transition": 0}
    assert any("repeat" in o["sequence"][0] for o in opts)
    assert any(o["sequence"] == [{"stop": "Button released"}] for o in opts)
