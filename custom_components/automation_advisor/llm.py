"""The AI: the only source of suggestions shown to the user.

Through Home Assistant's AI Task integration (Ollama is streamed directly so the panel can show
the model writing). One analysis = one request (+ one revision round):

  in:  a digest of the home (devices with rooms, existing automations, typical days as
       timelines saying who switched what) and LEADS from the statistics engine, each with the
       result of replaying it on the recorded history
  out: the AI's final list: new automations ("create") and changes to existing ones ("modify",
       "delete", "disable")

Nothing the model proposes is trusted as is. Every item must
  1. reference only real entities and contain no unsafe actions (unlocking, opening garage
     doors, disarming alarms),
  2. pass Home Assistant's own automation validator, and
  3. hold up on the recorded history: a new automation must pass the replay; a fix must do
     better than the automation it replaces; a delete/disable needs the automation to be
     broken or to misbehave in the replay.
Items that fail go back to the model once with the reason, to be fixed or dropped. Whatever
still fails is listed in the run report, never shown as a suggestion.

Privacy: the digest contains room-level activity timelines. Use a local model (Ollama) to keep it
on your network.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant

from . import analysis as A, automations as autos
from .backtest import BacktestResult, backtest
from .digest import build as build_digest

_LOGGER = logging.getLogger(__name__)
# The model's streamed output, mirrored to the log so it can be watched in a terminal.
STREAM_LOGGER = logging.getLogger(f"{__name__}.stream")
MAX_IDEAS = 8

INSTRUCTIONS = """You are an expert Home Assistant automation designer. Study how this home is
actually used and decide which automations would genuinely help the people living in it, and
which existing automations should be fixed, deleted or turned off.

You get LEADS from a statistics engine: patterns it found, each already replayed on the real
history (the numbers tell you how well it would have worked). Treat them as evidence, not as
orders. If a lead's draft is good as it is (its replay looks good), ADOPT it with
{{"kind": "adopt", "lead": "L<n>", ...}} instead of rewriting it; you may also improve one by
writing your own version. Skip weak or pointless ones. Then look for what statistics miss:
- several conditions together (motion AND dark AND someone home AND TV off, ...)
- whole-house routines (everyone leaves -> everything off; first one home after dark -> lights)
- devices that are always used together
- things left on, lights on in empty rooms, wasted energy
- existing automations that behave badly (fire at the wrong time, leave things on)

Rules for automations you write:
- Use ONLY entity_ids from the DEVICES list (and sun.sun). Never invent entities.
- Home Assistant syntax: "triggers" (with "trigger": state | numeric_state | time | sun |
  time_pattern), "conditions" (with "condition": state | numeric_state | time | sun | and | or |
  not), "actions" (with "action": "<domain>.<service>", "target": {{"entity_id": ...}}).
  No templates, no device triggers, no "attribute" keys, no choose/if, no scripts.
- A motion sensor only tells you about its own room. Use the sensor in the same room as the
  light (see the room column). Lights turned on by motion need a matching way to turn off.
- Durations as {{"minutes": N}}. Times as "HH:MM:SS". Sun offsets as "-01:00:00".
- Never unlock locks, open garage doors or disarm alarms.
- Do not re-create something an EXISTING AUTOMATION already does; fix that automation instead
  ("modify" with its entity_id as "target_automation" and the complete new automation).
- "title": what the automation does, in at most 60 characters ("Kitchen lights on with motion
  after dark"), not a slogan.
- "does": exactly what it does, in plain words a non-technical person understands, using the
  device NAMES (not entity ids): when it starts, what must be true, what it switches, and any
  times or delays. E.g. "When the kitchen motion sensor sees someone between sunset and an hour
  after sunrise, it turns on the kitchen lights."
- "why": the pattern you saw (from the timelines or a lead) and the benefit.
- At most {max_ideas} items. Fewer good items beat many weak ones. An empty list is fine.

Answer with ONE JSON object and nothing else:
{{
  "ideas": [
    {{"kind": "create", "title": "<max 60 chars>", "does": "<plain words>", "why": "<1-2 sentences>",
      "automation": {{"alias": "...", "mode": "single", "triggers": [...], "conditions": [...],
                     "actions": [...]}}}},
    {{"kind": "modify", "target_automation": "automation.<existing>", "title": "...", "does": "...", "why": "...",
      "automation": {{ ...the complete improved automation... }}}},
    {{"kind": "delete" or "disable", "target_automation": "automation.<existing>", "title": "...",
      "why": "..."}},
    {{"kind": "adopt", "lead": "L2", "title": "...", "does": "...", "why": "..."}}
  ]
}}

LEADS FROM THE STATISTICS ENGINE:
{leads}

{digest}
"""

BLANK_SLATE = """TEST MODE: this home has NO automations at all. Everything in the timelines was
done by the people living here. Design the automations it should have from scratch, including
the obvious ones. Only "create" items.

"""

REVISE = """(Remember: a lead whose draft replays well can be adopted as is with "kind": "adopt".)

These items you proposed did not hold up when they were replayed on the real
recorded history of this home. For each one, either fix it so it would only act when the people
here actually want it (add or change conditions, use the sensor in the right room, change the
trigger or the delay), or drop it. Keep "kind" and "target_automation". Use the same rules and
the same DEVICES as before.

{failures}

Answer with ONE JSON object and nothing else: {{"ideas": [ ... fixed ideas in the same format,
omit the ones you drop ... ]}}
"""

# Actions an AI suggestion may never contain. Every suggestion is also reviewed by a person before
# anything changes, but these could unlock a home, run arbitrary code, or break Home Assistant.
UNSAFE_SERVICES = (
    "lock.unlock", "lock.open", "alarm_control_panel.",          # opening the home
    "shell_command.", "python_script.", "pyscript.", "command_line.",  # arbitrary code
    "hassio.", "homeassistant.restart", "homeassistant.stop", "homeassistant.reload",
    "homeassistant.set_location", "backup.",                       # the system itself
    "recorder.purge", "recorder.disable", "logger.", "system_log.",
    "automation.turn_off", "automation.reload", "script.reload",   # switching other automations off
    "rest_command.", "input_text.set_value",
)


async def _generate(hass: HomeAssistant, *, on_delta=None, **kwargs: Any) -> Any:
    """Ask the AI. For an Ollama AI Task entity, stream directly from Ollama (with that
    entity's own settings) so the panel can show the answer, and any thinking, as it is
    written. Every other provider goes through Home Assistant's AI Task as usual."""
    if on_delta is not None:
        ollama = _ollama_settings(hass, kwargs.get("entity_id"))
        if ollama is not None:
            return await _stream_ollama(hass, ollama, kwargs["instructions"], on_delta)
    from homeassistant.components.ai_task import async_generate_data

    return await async_generate_data(hass, **kwargs)


def _ollama_settings(hass: HomeAssistant, entity_id: str | None) -> dict | None:
    from homeassistant.helpers import entity_registry as er

    if not entity_id or (ent := er.async_get(hass).async_get(entity_id)) is None:
        return None
    entry = hass.config_entries.async_get_entry(ent.config_entry_id) if ent.config_entry_id else None
    if entry is None or entry.domain != "ollama":
        return None
    sub = entry.subentries.get(ent.config_subentry_id) if ent.config_subentry_id else None
    opts = dict(sub.data) if sub else {}
    if not opts.get("model"):
        return None
    return {"url": entry.data.get("url", "http://localhost:11434").rstrip("/"),
            "api_key": entry.data.get("api_key"), "model": opts["model"],
            "num_ctx": int(opts.get("num_ctx", 8192)), "keep_alive": opts.get("keep_alive", -1),
            "think": bool(opts.get("think", False))}


async def _stream_ollama(hass: HomeAssistant, cfg: dict, prompt: str, on_delta) -> Any:
    from types import SimpleNamespace

    import aiohttp

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    keep = cfg["keep_alive"]
    body = {"model": cfg["model"], "stream": True, "think": cfg["think"],
            "format": "json",  # constrained decoding: the answer is always syntactically valid JSON
            "keep_alive": f"{int(keep)}s" if keep not in (None, -1, "-1") else -1,
            "options": {"num_ctx": cfg["num_ctx"]},
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg.get("api_key") else {}
    answer: list[str] = []
    lines = {"thinking": "", "answer": ""}

    def echo(kind: str, text: str, flush: bool = False) -> None:
        """Mirror the stream into Home Assistant's log (the terminal running it), line by line."""
        buf = lines[kind] + text
        *full, rest = buf.split("\n")
        prefix = "AI thinking ›" if kind == "thinking" else "AI ›"
        for line in full + ([rest] if flush and rest else []):
            if line.strip():
                STREAM_LOGGER.info("%s %s", prefix, line)
        lines[kind] = "" if flush else rest

    session = async_get_clientsession(hass)
    async with session.post(f"{cfg['url']}/api/chat", json=body, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=1800, sock_read=600)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Ollama answered {resp.status}: {(await resp.text())[:200]}")
        async for line in resp.content:
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(f"Ollama: {chunk['error']}")
            msg = chunk.get("message") or {}
            if thinking := msg.get("thinking"):
                on_delta("thinking", thinking)
                echo("thinking", thinking)
            if text := msg.get("content"):
                answer.append(text)
                on_delta("answer", text)
                echo("answer", text)
            if chunk.get("done"):
                echo("thinking", "", flush=True)
                echo("answer", "", flush=True)
                STREAM_LOGGER.info("AI done: %s tokens in %.0f s", chunk.get("eval_count"),
                                   (chunk.get("eval_duration") or 0) / 1e9)
                on_delta("done", json.dumps({k: chunk.get(k) for k in ("eval_count", "eval_duration")}))
    return SimpleNamespace(data="".join(answer), conversation_id=None)


async def _ask_json(hass: HomeAssistant, **kwargs: Any) -> dict:
    """Ask the AI and parse its JSON answer. If the answer is not valid JSON, ask once more with
    the exact error (Ollama's JSON mode makes this rare; other providers have no such mode)."""
    result = await _generate(hass, **kwargs)
    try:
        return _parse(result.data)
    except ValueError as err:
        _LOGGER.info("AI answer was not valid JSON (%s); asking again", err)
        retry = dict(kwargs)
        retry["instructions"] = (kwargs["instructions"] + "\n\nIMPORTANT: your previous answer was "
                                 f"not valid JSON ({err}). Answer again with the same content as ONE "
                                 "valid JSON object and nothing else.")
        result = await _generate(hass, **retry)
        try:
            return _parse(result.data)
        except ValueError as err2:
            raise autos.AdvisorError(f"The AI's answer was not valid JSON twice ({err2}). "
                                     "Try again, or shorten the request.") from err2


def _parse(text: Any) -> dict:
    if isinstance(text, dict):
        return text
    text = re.sub(r"<think>.*?</think>", "", str(text), flags=re.S)
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("no JSON object in the AI answer")
    raw = match.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # small models' usual slips: trailing commas, // comments, Python literals
        fixed = re.sub(r"//[^\n\"]*\n", "\n", raw)
        fixed = re.sub(r",(\s*[}\]])", r"\1", fixed)
        fixed = re.sub(r"\bTrue\b", "true", re.sub(r"\bFalse\b", "false", fixed))
        fixed = re.sub(r"\bNone\b", "null", fixed)
        return json.loads(fixed)


def _unsafe(config: dict) -> str | None:
    """Name the first forbidden action in the automation, if any (actions inside choose/if/
    sequence blocks included), or garage/gate covers being opened."""
    services: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("action", "service"):
                if isinstance(node.get(key), str):
                    services.append(node[key].strip().lower())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(config.get("actions", config.get("action", [])))
    for service in services:
        if "{" in service:
            return f"uses a templated action ({service})"
        for bad in UNSAFE_SERVICES:
            if service == bad or (bad.endswith(".") and service.startswith(bad)):
                return f"uses {service}"
        if service in ("cover.open_cover", "cover.toggle"):
            blob = json.dumps(config.get("actions", config.get("action", []))).lower()
            if "garage" in blob or "gate" in blob:
                return f"uses {service} on a garage door or gate"
    return None


CATEGORY_TEXT = {
    "trigger_action": "reacts to a sensor", "auto_off": "auto off", "routine": "daily routine",
    "overridden": "existing automation you keep undoing", "stuck_on": "existing automation leaves things on",
    "broken": "existing automation refers to devices that no longer exist",
}


def leads_text(found: list[A.Suggestion], limit: int = 10) -> str:
    if not found:
        return "(none)"
    lines = []
    for i, sg in enumerate(found[:limit], 1):
        parts = [f"L{i} [{CATEGORY_TEXT.get(sg.category, sg.category)}] {sg.title}"]
        if sg.target_automation:
            parts.append(f"   existing automation: {sg.target_automation}")
        parts.append(f"   finding: {sg.description[:400]}")
        if sg.automation:
            cfg = {k: v for k, v in sg.automation.items() if k in ("triggers", "conditions", "actions")}
            parts.append(f"   engine's draft: {json.dumps(cfg, separators=(',', ':'), default=str)[:450]}")
        if bt := sg.evidence.get("backtest"):
            parts.append(f"   replay of the draft: {bt.get('summary', '')[:300]}")
        lines.append("\n".join(parts))
    return "\n".join(lines)


async def async_ai_suggestions(
    hass: HomeAssistant,
    ai_entity: str,
    leads: list[A.Suggestion],
    ctx: A.Context,
    known_entities: set[str],
    progress=None,
    blank_slate: bool = False,
) -> tuple[list[A.Suggestion], dict]:
    """Returns (the AI's verified suggestions, AI report for the run summary).
    blank_slate (test mode): the AI designs as if the home had no automations; only "create"
    items are accepted.
    `progress(step=..., delta=...)` receives live updates for the panel."""
    progress = progress or (lambda **kw: None)
    known = set(known_entities) | {"sun.sun"}
    digest = await hass.async_add_executor_job(build_digest, ctx, [])
    prompt = INSTRUCTIONS.format(max_ideas=MAX_IDEAS, leads=leads_text(leads), digest=digest)
    if blank_slate:
        prompt = BLANK_SLATE + prompt
    report: dict = {"entity": ai_entity, "prompt_chars": len(prompt), "leads": len(leads),
                    "proposed": 0, "accepted": [], "rejected": []}
    out: list[A.Suggestion] = []

    progress(step=f"Asking {ai_entity} (round 1)")
    answer = await _ask_json(
        hass, task_name="Automation Advisor", entity_id=ai_entity, instructions=prompt,
        on_delta=lambda kind, text: progress(delta=(kind, text)),
    )
    ideas = (answer.get("ideas") or [])[:MAX_IDEAS]
    report["proposed"] = len(ideas)
    progress(step=f"Checking {len(ideas)} AI item(s): validator and replay on your history")
    if blank_slate:
        ideas = [_adopt(i, leads) if isinstance(i, dict) and str(i.get("kind")).lower() == "adopt" else i
                 for i in ideas]
        kept = [i for i in ideas if not isinstance(i, dict)
                or str(i.get("kind") or "create").lower() in ("create", "invalid")]
        for i in ideas:
            if i not in kept:
                report["rejected"].append({"title": str(i.get("title"))[:80],
                                           "reason": "test mode: only new automations"})
        ideas = kept
    failed = await _take_ideas(hass, ideas, known, ctx, out, report, leads=leads)
    if failed:
        text = "\n\n".join(
            f"{i + 1}. {f['title']} (kind: {f['kind']}"
            + (f", target_automation: {f['target']}" if f["target"] else "") + ")\n"
            + (f"automation: {json.dumps(f['automation'], default=str)}\n" if f["automation"] else "")
            + f"result: {f['reason']}"
            + (f"\nexamples: {'; '.join(f['examples'])}" if f["examples"] else "")
            for i, f in enumerate(failed)
        )
        try:
            progress(step=f"Asking {ai_entity} to fix {len(failed)} item(s) that did not hold up")
            revised_answer = await _ask_json(
                hass, task_name="Automation Advisor (revise)", entity_id=ai_entity,
                instructions=prompt + "\n\n" + REVISE.format(failures=text),
                on_delta=lambda kind, text: progress(delta=(kind, text)),
            )
            revised = (revised_answer.get("ideas") or [])[:len(failed)]
            report["revised"] = len(revised)
            progress(step=f"Checking {len(revised)} revised item(s)")
            await _take_ideas(hass, revised, known, ctx, out, report, label="revised", leads=leads)
        except Exception as err:  # the first round still counts
            report["revise_error"] = f"{type(err).__name__}: {err}"
    return out, report


async def _take_ideas(hass, ideas, known, ctx, out, report, label=None, leads=()) -> list[dict]:
    """Check each item; add the good ones to `out`; return the ones worth a revision."""
    failed = []
    for item in ideas:
        if isinstance(item, dict) and str(item.get("kind")).lower() == "adopt":
            item = _adopt(item, leads)
        title = str(item.get("title") or "untitled")[:80] if isinstance(item, dict) else "untitled"
        shown = f"{title} (revised)" if label else title
        sg, reason, examples = await _idea(hass, item, known, ctx)
        if sg is None:
            report["rejected"].append({"title": shown, "reason": reason})
            if not label and (reason.startswith("failed the replay") or reason.startswith("not better")):
                failed.append({"title": title, "kind": item.get("kind", "create"),
                               "target": item.get("target_automation"),
                               "automation": item.get("automation"), "reason": reason,
                               "examples": examples})
        elif any(o.fingerprint == sg.fingerprint for o in out):
            report["rejected"].append({"title": shown, "reason": "duplicate"})
        elif sg.automation and (twin := next((o for o in out if o.automation and _body(o.automation)
                                              == _body(sg.automation)), None)):
            report["rejected"].append({"title": shown, "reason": f"same automation as \"{twin.title}\""})
        elif sg.kind == A.KIND_REVIEW and (same := next(
                (o for o in out if o.kind == A.KIND_REVIEW
                 and o.target_automation == sg.target_automation), None)):
            # one card per existing automation, with every option the AI proposed for it
            order = ["modify", "delete", "disable"]
            same.options = [o for o in order if o in set(same.options) | set(sg.options)]
            if same.automation is None and sg.automation is not None:
                same.automation = sg.automation
                same.evidence.update({k: v for k, v in sg.evidence.items() if k.startswith("backtest")})
            same.description += f" Alternatively ({sg.title.lower()}): {sg.description}"
            report["accepted"].append(f"{shown} (merged)")
        else:
            if label:
                sg.evidence["revised"] = True
            if isinstance(item, dict) and item.get("adopted"):
                sg.evidence["adopted_lead"] = item["adopted"]
            if isinstance(item, dict) and str(item.get("does") or "").strip():
                sg.evidence["does"] = str(item["does"]).strip()
            out.append(sg)
            report["accepted"].append(shown)
    return failed


def _adopt(item: dict, leads) -> dict:
    """Turn {"kind": "adopt", "lead": "L2"} into the lead's verified draft, keeping the AI's
    title and reasoning."""
    m = re.search(r"\d+", str(item.get("lead", "")))
    lead = leads[int(m.group(0)) - 1] if m and 0 < int(m.group(0)) <= len(leads) else None
    if lead is None:
        return {"kind": "invalid", "title": item.get("title"), "why": f"unknown lead {item.get('lead')!r}"}
    out = {"title": item.get("title") or lead.title, "why": item.get("why") or lead.description,
           "does": item.get("does"), "adopted": f"L{m.group(0)}"}
    if lead.kind == A.KIND_REVIEW and lead.target_automation:
        if lead.automation:
            out.update(kind="modify", target_automation=lead.target_automation,
                       automation={k: v for k, v in lead.automation.items() if k != "id"})
        else:
            out.update(kind="delete" if "delete" in lead.options else "disable",
                       target_automation=lead.target_automation)
    else:
        out.update(kind="create", automation=lead.automation)
    return out


def _body(config: dict) -> str:
    return json.dumps({k: config.get(k) for k in ("triggers", "conditions", "actions")},
                      sort_keys=True, default=str)


def _check_config(config: Any, known: set[str]) -> str | None:
    if not isinstance(config, dict):
        return "no automation in the answer"
    refs: set[str] = set()
    autos._collect_entities(config, refs)
    if not refs:
        return "refers to no devices"
    if unknown := sorted(refs - known):
        return "unknown entities: " + ", ".join(unknown[:3])
    if why := _unsafe(config):
        return f"not allowed ({why})"
    return None


async def _replay(hass, config, ctx) -> BacktestResult:
    return await hass.async_add_executor_job(backtest, config, ctx.events, ctx.tz, ctx.sun, ctx.entities)


async def _idea(hass: HomeAssistant, item: Any, known: set[str],
                ctx: A.Context) -> tuple[A.Suggestion | None, str, list[str]]:
    if not isinstance(item, dict):
        return None, "not an object", []
    kind = str(item.get("kind") or "create").lower()
    if kind not in ("create", "modify", "delete", "disable"):
        return None, str(item.get("why") or f"unknown kind {kind!r}"), []
    title = str(item.get("title") or (item.get("automation") or {}).get("alias") or "AI idea")[:80]
    why_text = str(item.get("why") or item.get("description") or "").strip()
    target = None
    if kind in ("modify", "delete", "disable"):
        target = next((a for a in ctx.automations if a.entity_id == item.get("target_automation")), None)
        if target is None:
            return None, f"unknown automation {item.get('target_automation')!r}", []

    if kind in ("delete", "disable"):
        before = await _replay(hass, target.config, ctx) if target.config else BacktestResult(supported=False, reason="no config")
        broken = any(not known.__contains__(e) for e in target.trigger_entities | target.action_entities
                     if "." in e and "{" not in e)
        if not (broken or (before.supported and before.actions >= 3 and before.unwanted_rate >= 0.3)):
            return None, ("no evidence that it misbehaves: " + before.summary()), []
        options = (["delete"] if kind == "delete" and target.editable else []) + (["disable"] if target.enabled else [])
        if not options:
            return None, "nothing to do (already off and not deletable)", []
        return A.Suggestion(
            kind=A.KIND_REVIEW, category="ai",
            fingerprint=A.fingerprint("ai", kind, target.entity_id, A.config_hash(target.config)),
            title=title, description=why_text, confidence=0.8,
            evidence={"source": "ai", "ai_kind": kind, "backtest_before": before.as_dict(),
                      "config_hash": A.config_hash(target.config), "broken": broken},
            target_automation=target.entity_id, options=options,
        ), "", []

    config = item.get("automation")
    if (err := _check_config(config, known)) is not None:
        return None, err, []
    if kind == "modify" and target is not None:
        base_alias = target.alias
    else:
        kind, base_alias = "create", title
    config = {
        "alias": base_alias,
        "description": f"Suggested by Automation Advisor (AI). {why_text}".strip(),
        "mode": config.get("mode", "single"),
        **{k: v for k, v in config.items() if k in ("triggers", "trigger", "conditions",
                                                    "condition", "actions", "action")},
    }
    try:
        await autos.validate(hass, config)
    except autos.AdvisorError as err:
        return None, f"invalid automation ({str(err)[:120]})", []
    after = await _replay(hass, config, ctx)

    if kind == "modify":
        before = await _replay(hass, target.config, ctx) if target.config else BacktestResult(supported=False)
        good_before = before.agreed + before.helpful if before.supported else 0
        better = after.supported and (
            after.passes() or (after.agreed + after.helpful > good_before
                               and after.unwanted_rate <= before.unwanted_rate + 0.05))
        if not better:
            return None, "not better than the current automation: " + after.summary(), after.examples[:3]
        evidence = {"source": "ai", "ai_kind": "modify", "backtest": after.as_dict(),
                    "backtest_before": before.as_dict(), "config_hash": A.config_hash(target.config)}
        if target.editable and target.config_id:
            return A.Suggestion(
                kind=A.KIND_REVIEW, category="ai",
                fingerprint=A.fingerprint("ai", "modify", target.entity_id,
                                          json.dumps(config.get("triggers"), sort_keys=True, default=str)),
                title=title, description=why_text, confidence=min(0.95, 0.4 + 0.5 * after.good_rate),
                evidence=evidence, automation={"id": target.config_id, **config},
                target_automation=target.entity_id,
                options=["modify"] + (["disable"] if target.enabled else []),
            ), "", []
        # YAML automation: offer the fixed version as a new automation (turn the old one off)
        config["alias"] = f"{target.alias} (fixed)"
        why_text += f" Your current \"{target.alias}\" is defined in YAML: turn it off after adding this."
        return A.Suggestion(
            kind=A.KIND_CREATE, category="ai",
            fingerprint=A.fingerprint("ai", "fix-yaml", target.entity_id,
                                      json.dumps(config.get("triggers"), sort_keys=True, default=str)),
            title=title, description=why_text, confidence=min(0.95, 0.4 + 0.5 * after.good_rate),
            evidence=evidence, automation=config, options=["accept"],
        ), "", []

    if not after.passes():
        return None, "failed the replay: " + after.summary(), after.examples[:3]
    t_ent, _, a_ent = autos.summarize_config(config)
    return A.Suggestion(
        kind=A.KIND_CREATE, category="ai",
        fingerprint=A.fingerprint("ai", sorted(t_ent), sorted(a_ent),
                                  json.dumps(config.get("triggers"), sort_keys=True, default=str),
                                  json.dumps(config.get("conditions"), sort_keys=True, default=str)),
        title=title, description=why_text, confidence=min(0.95, 0.4 + 0.5 * after.good_rate),
        evidence={"source": "ai", "ai_kind": "create", "entities": sorted(
            {e for e in t_ent | a_ent}), "backtest": after.as_dict()},
        automation=config, options=["accept"],
    ), "", []


REFINE = """You are editing ONE Home Assistant automation for this home. Change it as the person
asks and keep everything else the same.

The person asks: "{instruction}"

Current automation (JSON): {config}
What it does now: {does}

DEVICES (entity_id | name | room | kind). These are the ONLY entities that exist (plus sun.sun):
{devices}

Rules: Home Assistant syntax ("triggers" with "trigger": state | numeric_state | time | sun |
time_pattern; "conditions" with "condition": state | numeric_state | time | sun | and | or | not;
"actions" with "action": "<domain>.<service>" and "target": {{"entity_id": ...}}). No templates,
no device triggers, no "attribute" keys. Durations as {{"minutes": N}}, times "HH:MM:SS", sun
offsets "-01:00:00". Never unlock locks, open garage doors or disarm alarms.
- Buttons and remotes must feel instant: "mode": "restart" (a new press takes over at once;
  "queued" makes quick presses lag), "transition": 0 in the data of brightness steps, and for
  holding a button a "repeat" loop (count 20: step + 250 ms delay) that the release event ends.

Answer with ONE JSON object and nothing else:
{{"title": "<what it does, max 60 chars>", "does": "<plain words, device names, times>",
  "why": "<1 sentence>", "automation": {{ ...the complete changed automation... }}}}
"""


async def async_refine_one(hass: HomeAssistant, ai_entity: str, sg: dict, instruction: str,
                           ctx: A.Context, names: dict[str, str]) -> dict:
    """Ask the AI to change one suggestion. Returns the AI's item (not yet verified)."""
    from .digest import _kind

    devices = "\n".join(f"{e} | {i.name} | {i.area or '-'} | {_kind(ctx, e)}"
                        for e, i in sorted(ctx.entities.items()))
    cfg = {k: v for k, v in sg["automation"].items() if k in ("triggers", "trigger", "conditions",
                                                              "condition", "actions", "action", "mode")}
    prompt = REFINE.format(instruction=instruction.replace('"', "'"),
                           config=json.dumps(cfg, default=str),
                           does=sg.get("evidence", {}).get("does") or sg.get("evidence", {}).get("exactly") or sg["title"],
                           devices=devices)
    item = await _ask_json(hass, task_name="Automation Advisor (edit)", entity_id=ai_entity,
                             instructions=prompt, on_delta=lambda kind, text: None)
    if not isinstance(item.get("automation"), dict):
        raise autos.AdvisorError("The AI did not return an automation. Try rephrasing.")
    return item


async def verify(hass: HomeAssistant, config: dict, known: set[str], ctx: A.Context,
                 catalog: dict | None = None) -> tuple[BacktestResult | None, str | None]:
    """Check an automation the person edited or the AI fixed: real entities (and devices and
    areas, with a catalog), safety, Home Assistant's validator, then replay it.
    Returns (replay result, error message)."""
    if catalog is not None:
        if "sun.sun" in known and "sun.sun" not in catalog["entities"]:
            catalog = {**catalog, "entities": {**catalog["entities"], "sun.sun": {"name": "Sun"}}}
        err = check_build(config, catalog)
    else:
        err = _check_config(config, known)
    if err is not None:
        return None, f"Not possible: {err}."
    try:
        await autos.validate(hass, config)
    except autos.AdvisorError as err:
        return None, str(err)
    return await _replay(hass, config, ctx), None


FIX = """You are fixing ONE existing Home Assistant automation that is not working the way the
people living here want. Read their complaint, the evidence and the current automation, find the
cause, and return a corrected automation.

The person says: "{problem}"

Automation: "{alias}" ({entity_id})
Current automation (JSON): {config}
In plain words it currently does: {exactly}

EVIDENCE
Replay of the current automation on the recorded history: {replay}
Its last runs (Home Assistant traces, newest first):
{traces}
Recent activity of the devices it uses (local time; who did it in brackets):
{activity}

DEVICES (entity_id | name | room | kind). These are the ONLY entities that exist (plus sun.sun):
{devices}

Rules: Home Assistant syntax ("triggers" with "trigger": state | numeric_state | time | sun |
time_pattern; "conditions" with "condition": state | numeric_state | time | sun | and | or | not;
"actions" with "action": "<domain>.<service>" and "target": {{"entity_id": ...}}). No templates,
no device triggers, no "attribute" keys. Durations as {{"minutes": N}}, times "HH:MM:SS", sun
offsets "-01:00:00". Never unlock locks, open garage doors or disarm alarms. Change only what is
needed to fix the complaint.
- Buttons and remotes must feel instant: "mode": "restart" (a new press takes over at once;
  "queued" makes quick presses lag), "transition": 0 in the data of brightness steps, and for
  holding a button a "repeat" loop (count 20: step + 250 ms delay) that the release event ends.

Answer with ONE JSON object and nothing else:
{{"title": "<what the fixed automation does, max 60 chars>",
  "does": "<plain words, device names, times>",
  "why": "<what was wrong (cite the evidence) and what you changed, 1-3 sentences>",
  "automation": {{ ...the complete corrected automation... }}}}
"""


def _trace_lines(traces: list[dict], tz) -> str:
    from datetime import datetime

    lines = []
    for t in sorted(traces, key=lambda x: str((x.get("timestamp") or {}).get("start")), reverse=True)[:5]:
        start = (t.get("timestamp") or {}).get("start")
        try:
            when = datetime.fromisoformat(str(start)).astimezone(tz).strftime("%a %d %b %H:%M")
        except ValueError:
            when = str(start)
        lines.append(f"- {when}: triggered by {t.get('trigger') or '?'}; result: "
                     f"{t.get('script_execution') or t.get('state')}; stopped at {t.get('last_step')}"
                     + (f"; error: {t['error']}" if t.get("error") else ""))
    return "\n".join(lines) or "(no traces kept)"


async def async_fix_automation(hass: HomeAssistant, ai_entity: str, auto: A.ExistingAutomation,
                               problem: str, ctx: A.Context, traces: list[dict],
                               names: dict[str, str], progress=None) -> dict:
    """Ask the AI to fix one existing automation. Returns the AI's item (not yet verified)."""
    from .describe import describe
    from .digest import _kind, timeline

    progress = progress or (lambda **kw: None)
    before = await _replay(hass, auto.config, ctx) if auto.config else None
    involved = set(auto.trigger_entities) | set(auto.action_entities)
    for c in (auto.config or {}).get("conditions", []) or []:
        refs: set[str] = set()
        autos._collect_entities(c, refs)
        involved |= refs
    sub = A.Context(events=[e for e in ctx.events if e.entity_id in involved
                            or e.cause_ref == auto.entity_id],
                    entities=ctx.entities, automations=ctx.automations, tz=ctx.tz, sun=ctx.sun,
                    settings=ctx.settings, now=ctx.now)
    days = sorted({__import__("datetime").datetime.fromtimestamp(e.ts, ctx.tz).date()
                   for e in sub.events})[-3:]
    activity = []
    for d in days:
        activity.append(d.strftime("%A %d %b:"))
        activity += timeline(sub, d)[:60]
    devices = "\n".join(f"{e} | {i.name} | {i.area or '-'} | {_kind(ctx, e)}"
                        for e, i in sorted(ctx.entities.items()))
    cfg = {k: v for k, v in (auto.config or {}).items() if k in (
        "triggers", "trigger", "conditions", "condition", "actions", "action", "mode")}
    prompt = FIX.format(
        problem=problem.replace('"', "'"), alias=auto.alias, entity_id=auto.entity_id,
        config=json.dumps(cfg, default=str), exactly=describe(auto.config, names),
        replay=(before.summary() + (" e.g. " + "; ".join(before.examples[:3]) if before and before.examples else ""))
        if before else "not available",
        traces=_trace_lines(traces, ctx.tz), activity="\n".join(activity) or "(none recorded)",
        devices=devices,
    )
    progress(step=f"Asking {ai_entity} to fix \"{auto.alias}\"")
    item = await _ask_json(hass, task_name="Automation Advisor (fix)", entity_id=ai_entity,
                             instructions=prompt,
                             on_delta=lambda kind, text: progress(delta=(kind, text)))
    if not isinstance(item.get("automation"), dict):
        raise autos.AdvisorError("The AI did not return an automation. Try describing the problem differently.")
    item["_before"] = before
    return item


BUILD = """You are setting up Home Assistant automations for the person who lives here. Do exactly
what they ask, nothing more.

They ask: "{request}"
{device_section}
AREAS (area_id | name):
{areas}

ENTITIES (entity_id | name | area | kind). Use ONLY these ids:
{entities}

How to write it:
- Home Assistant automation syntax. Every trigger is a mapping with a "trigger" key, e.g.
  {{"trigger": "state", "entity_id": "binary_sensor.x", "to": "on"}},
  {{"trigger": "state", "entity_id": "binary_sensor.x", "to": "off", "for": {{"minutes": 2}}}},
  {{"trigger": "time", "at": "23:00:00"}}, {{"trigger": "sun", "event": "sunset"}}; device triggers
  are copied exactly as listed. Conditions: {{"condition": "time", "after": "23:00:00",
  "before": "06:00:00"}}, {{"condition": "state", "entity_id": "...", "state": "on"}}. Actions:
  {{"action": "light.turn_on", "target": {{"entity_id": "light.x"}}}}. No templates.
- Buttons and remotes: use the DEVICE TRIGGERS listed above, copied exactly (every key and value),
  or the event entity listed for the device ("trigger": "state" on the event entity with
  "attribute": "event_type" is NOT allowed; use the device triggers when they exist).
- Several buttons that do different things: ONE automation. Give every trigger an "id" (e.g.
  "up"), and use "actions": [{{"choose": [{{"conditions": [{{"condition": "trigger", "id": "up"}}],
  "sequence": [...]}}, ...]}}].
- A whole room: "target": {{"area_id": "<area_id>"}}. Single lights: "target": {{"entity_id": ...}}.
- Dimming: "light.turn_on" with "data": {{"brightness_step_pct": 10}} (brighter) or -10 (dimmer).
  On: "light.turn_on"; off: "light.turn_off".
- Buttons and remotes must feel instant: "mode": "restart" (a new press takes over at once;
  "queued" makes quick presses lag), "transition": 0 in the data of brightness steps, and for
  holding a button a "repeat" loop (count 20: step + 250 ms delay) that the release event ends.
- Never unlock locks, open garage doors or disarm alarms.
- If something they ask for is impossible with these devices, say so in "notes" instead of
  guessing.

Answer with ONE JSON object and nothing else:
{{"items": [{{"title": "<what it does, max 60 chars>", "does": "<plain words: which button does
what to which lights>", "why": "<1 sentence>", "automation": {{"alias": "...", "mode": "restart",
"triggers": [...], "conditions": [], "actions": [...]}}}}],
 "notes": "<anything the person must know or do, or an empty string>"}}
"""

BUILD_DOMAINS = ("light", "switch", "fan", "cover", "media_player", "climate", "scene", "script",
                 "input_boolean", "input_number", "input_select", "button", "lock", "event",
                 "binary_sensor", "vacuum", "humidifier", "siren", "valve")


def _catalog_entities(catalog: dict, device: dict | None, limit: int = 300) -> str:
    rows = []
    mine = set(device["entities"]) if device else set()
    for eid, info in sorted(catalog["entities"].items(),
                            key=lambda kv: (kv[0] not in mine, kv[1].get("area") or "~", kv[0])):
        if eid.split(".", 1)[0] not in BUILD_DOMAINS and eid not in mine:
            continue
        kind = eid.split(".", 1)[0] + (f"/{info['device_class']}" if info.get("device_class") else "")
        extra = f" (event types: {', '.join(map(str, info['event_types']))})" if info.get("event_types") else ""
        rows.append(f"{eid} | {info['name']} | {info.get('area') or '-'} | {kind}{extra}")
    if len(rows) > limit:
        rows = rows[:limit] + [f"... and {len(rows) - limit} more (not listed)"]
    return "\n".join(rows)


def _refs(config: Any, key: str, out: set[str]) -> set[str]:
    if isinstance(config, dict):
        for k, v in config.items():
            if k == key:
                out.update(x for x in (v if isinstance(v, list) else [v]) if isinstance(x, str))
            else:
                _refs(v, key, out)
    elif isinstance(config, list):
        for x in config:
            _refs(x, key, out)
    return out


def _choose_problems(config: dict) -> str | None:
    """Every 'trigger' condition in a choose must name a trigger id that exists, and every
    trigger with an id should be used, otherwise a button silently does nothing."""
    trig_ids = {str(t.get("id")) for t in (config.get("triggers") or config.get("trigger") or [])
                if isinstance(t, dict) and t.get("id") is not None}
    used: set[str] = set()
    for step in config.get("actions") or config.get("action") or []:
        if not isinstance(step, dict):
            return "an action is not an object"
        for opt in step.get("choose", []) if isinstance(step.get("choose"), list) else []:
            if not isinstance(opt, dict):
                return "a choose option is not an object"
            for c in opt.get("conditions", []) or []:
                if isinstance(c, dict) and c.get("condition") == "trigger":
                    ids = c.get("id") if isinstance(c.get("id"), list) else [c.get("id")]
                    used |= {str(i) for i in ids}
    if missing := sorted(used - trig_ids):
        return f"a choose option refers to trigger id(s) {missing} that no trigger has"
    if used and (unused := sorted(trig_ids - used)):
        return f"trigger id(s) {unused} have no choose option, so they would do nothing"
    return None


def check_build(config: Any, catalog: dict) -> str | None:
    """Like _check_config, but device triggers and area targets are allowed (and checked)."""
    if not isinstance(config, dict):
        return "no automation in the answer"
    if (err := _choose_problems(config)) is not None:
        return err
    ents = _refs(config, "entity_id", set())
    devs = _refs(config, "device_id", set())
    areas = _refs(config, "area_id", set())
    if not (ents or devs or areas):
        return "refers to no devices"
    known_devs = {d["id"] for d in catalog["devices"]}
    known_areas = {a["id"] for a in catalog["areas"]}
    if bad := sorted(ents - set(catalog["entities"]) - {"sun.sun"}):
        return "unknown entities: " + ", ".join(bad[:3])
    if bad := sorted(devs - known_devs):
        return "unknown devices: " + ", ".join(bad[:3])
    if bad := sorted(areas - known_areas):
        return "unknown areas: " + ", ".join(bad[:3])
    if why := _unsafe(config):
        return f"not allowed ({why})"
    return None


BUILD_BUTTONS = """You are setting up a button device (remote, switch, dimmer) in Home Assistant for
the person who lives here.

They ask: "{request}"

THE DEVICE: {device_name} ({manufacturer} {model}), area {device_area}.
The events it can report, numbered:
{events}

AREAS (area_id | name):
{areas}

LIGHTS AND OTHER THINGS IT CAN CONTROL (entity_id | name | area | kind). Use ONLY these ids:
{entities}

Decide which numbered event each button press in the request means, and what it should do.
- A plain press is a SHORT press ("remote_button_short_press", "press", "single", ...). Use long
  press / hold events only if they ask for holding.
- Button words: up/top/on/I usually = "turn_on" or "on"; down/bottom/off/O = "turn_off" or "off";
  left = "left" or "arrow_left"; right = "right" or "arrow_right"; dim up = brighter.
- "do" is one of: "on", "off", "toggle", "brighter", "dimmer" (one step per press),
  "hold_brighter", "hold_dimmer" (keeps stepping while the button is held), "stop".
- Holding to dim or brighten: map that button's long-press / hold START event to "hold_brighter"
  or "hold_dimmer", AND that button's RELEASE event ("long_release", "release", "hold_release",
  "..._release") to "stop". A hold without its release would never stop.
- "target": {{"entity_id": [...]}} for specific lights, or {{"area_id": "<area_id>"}} for a room.
- Every press they mention must appear exactly once. Do not add buttons they did not ask for.

Answer with ONE JSON object and nothing else:
{{"title": "<what it does, max 60 chars>",
  "does": "<plain words: which button does what>",
  "buttons": [{{"event": <number>, "button": "<their word, e.g. up>", "do": "on",
               "target": {{"entity_id": ["light.x"]}}, "step_pct": 10}}],
  "notes": "<anything the person must know, or an empty string>"}}
"""

BUTTON_ACTIONS = {"on": ("turn_on", None), "off": ("turn_off", None), "toggle": ("toggle", None),
                  "brighter": ("turn_on", 1), "dimmer": ("turn_on", -1),
                  "hold_brighter": ("turn_on", 1), "hold_dimmer": ("turn_on", -1), "stop": (None, None)}
HOLD_REPEATS = 20  # 20 x 10 % covers the whole range
HOLD_INTERVAL_MS = 250


def buttons_to_automation(answer: dict, triggers: list[dict]) -> tuple[dict | None, str | None]:
    """Turn the AI's button mapping into one well-formed automation: one trigger per button
    event (with an id) and one 'choose' option per button. Returns (config, error)."""
    buttons = answer.get("buttons")
    if not isinstance(buttons, list) or not buttons:
        return None, "the answer has no buttons"
    trigs, options, seen = [], [], set()
    for b in buttons:
        if not isinstance(b, dict):
            return None, "a button entry is not an object"
        try:
            n = int(b.get("event"))
        except (TypeError, ValueError):
            return None, f"button {b.get('button')!r} has no valid event number"
        if not 1 <= n <= len(triggers):
            return None, f"event {n} does not exist (the device has {len(triggers)})"
        if n in seen:
            return None, f"event {n} is used twice"
        seen.add(n)
        what = str(b.get("do", "")).lower()
        if what not in BUTTON_ACTIONS:
            return None, f"unknown action {what!r} for button {b.get('button')!r}"
        target = b.get("target")
        if what != "stop" and (not isinstance(target, dict) or not (target.get("entity_id") or target.get("area_id"))):
            return None, f"button {b.get('button')!r} has no target"
        tid = re.sub(r"[^a-z0-9_]+", "_", str(b.get("button") or f"event_{n}").lower()).strip("_") or f"event_{n}"
        if any(t.get("id") == tid for t in trigs):  # same button, another kind of press
            kind = str(triggers[n - 1].get("type", "")).lower()
            suffix = ("release" if "release" in kind else "hold" if ("long" in kind or "hold" in kind)
                      else "double" if "double" in kind else str(n))
            tid = f"{tid}_{suffix}"
            if any(t.get("id") == tid for t in trigs):
                tid = f"{tid}_{n}"
        trigs.append({**{k: v for k, v in triggers[n - 1].items() if k != "metadata"}, "id": tid})
        if what == "stop":
            # Any new trigger restarts the automation (mode: restart), which ends a running hold
            # loop at once; this option just makes the release explicit.
            options.append({"conditions": [{"condition": "trigger", "id": tid}],
                            "sequence": [{"stop": "Button released"}]})
            continue
        service, sign = BUTTON_ACTIONS[what]
        ents = target.get("entity_id")
        ents = ents if isinstance(ents, list) else [ents] if ents else []
        domains = {str(e).split(".", 1)[0] for e in ents}
        domain = "light" if (sign is not None or not ents or domains == {"light"}) else (
            domains.pop() if len(domains) == 1 else "homeassistant")
        step = {"action": f"{domain}.{service}", "target": {k: v for k, v in target.items() if k in ("entity_id", "area_id")}}
        if sign is not None:
            try:
                pct = abs(int(b.get("step_pct") or 10)) or 10
            except (TypeError, ValueError):
                pct = 10
            # transition 0: no fade, so every step is felt immediately
            step["data"] = {"brightness_step_pct": sign * min(pct, 100), "transition": 0}
        if what.startswith("hold_"):
            step["data"]["transition"] = HOLD_INTERVAL_MS / 1000  # smooth while held
            sequence = [{"repeat": {"count": HOLD_REPEATS, "sequence": [
                step, {"delay": {"milliseconds": HOLD_INTERVAL_MS}}]}}]
        else:
            sequence = [step]
        options.append({"conditions": [{"condition": "trigger", "id": tid}], "sequence": sequence})
    holds = [b for b in buttons if str(b.get("do", "")).lower().startswith("hold_")]
    if holds and not any(str(b.get("do", "")).lower() == "stop" for b in buttons):
        return None, ("a hold action has no 'stop' on a release event, so it would keep going for "
                      f"{HOLD_REPEATS * HOLD_INTERVAL_MS / 1000:g} s after letting go")
    # restart: a new press takes over immediately instead of waiting for the previous one to
    # finish (queued made quick presses lag), and it ends a running hold loop.
    return {"mode": "restart", "triggers": trigs, "conditions": [],
            "actions": [{"choose": options}]}, None


async def async_build(hass: HomeAssistant, ai_entity: str, request: str, catalog: dict,
                      device: dict | None, triggers: list[dict], progress=None) -> dict:
    """Ask the AI to set something up. Returns {"items": [...], "notes": str} (unverified)."""
    progress = progress or (lambda **kw: None)
    areas = "\n".join(f"{a['id']} | {a['name']}" for a in catalog["areas"]) or "(no areas)"
    if device and triggers:
        # A button device: the AI only maps buttons to actions; the automation itself is written
        # here, so its structure is always right.
        events = "\n".join(
            f"{i}. {str(t.get('type', '')).replace('_', ' ')} / {t.get('subtype', '')}"
            for i, t in enumerate(triggers, 1))
        prompt = BUILD_BUTTONS.format(
            request=request.replace('"', "'"), device_name=device["name"],
            manufacturer=device.get("manufacturer") or "", model=device.get("model") or "",
            device_area=device.get("area") or "none", events=events, areas=areas,
            entities=_catalog_entities(catalog, None))
        progress(step=f"Asking {ai_entity} which button does what")
        answer = await _ask_json(hass, task_name="Automation Advisor (set up)", entity_id=ai_entity,
                                 instructions=prompt,
                                 on_delta=lambda kind, text: progress(delta=(kind, text)))
        config, error = buttons_to_automation(answer, triggers)
        item = {"title": answer.get("title") or f"{device['name']} buttons",
                "does": answer.get("does"), "why": "Set up as you asked.",
                "automation": config, "_error": error}
        return {"items": [item], "notes": answer.get("notes") or "", "_prompt": prompt,
                "_buttons": True}
    if device:
        trig = "\n".join("- " + json.dumps(t, default=str) for t in triggers) or "(none reported)"
        device_section = (f"\nTHE DEVICE THEY MEAN: {device['name']} ({device.get('manufacturer') or ''} "
                          f"{device.get('model') or ''}), device_id {device['id']}, area "
                          f"{device.get('area') or 'none'}.\nIts entities: {', '.join(device['entities']) or '-'}"
                          f"\nIts DEVICE TRIGGERS (copy exactly):\n{trig}\n")
    else:
        device_section = ""
    prompt = BUILD.format(request=request.replace('"', "'"), device_section=device_section,
                          areas=areas, entities=_catalog_entities(catalog, device))
    progress(step=f"Asking {ai_entity} to set it up")
    answer = await _ask_json(hass, task_name="Automation Advisor (set up)", entity_id=ai_entity,
                             instructions=prompt, on_delta=lambda kind, text: progress(delta=(kind, text)))
    if not isinstance(answer.get("items"), list):
        answer["items"] = []
    answer["_prompt"] = prompt
    return answer


BUILD_FIX = """Some of the automations you wrote were rejected. Fix each one so Home Assistant accepts
it, keeping what it is meant to do. Same rules and the same entity, device and area ids as before.

{failures}

Answer with ONE JSON object in the same format as before: {{"items": [...], "notes": "..."}}
"""


async def async_build_fix(hass: HomeAssistant, ai_entity: str, first_prompt: str,
                          failures: list[tuple[dict, str]], progress=None) -> dict:
    """One repair round for set-up items that did not pass the checks."""
    progress = progress or (lambda **kw: None)
    text = "\n\n".join(f"{i + 1}. {item.get('title')}\nautomation: "
                        f"{json.dumps(item.get('automation'), default=str)}\nproblem: {err}"
                        for i, (item, err) in enumerate(failures))
    progress(step=f"Asking {ai_entity} to fix {len(failures)} rejected automation(s)")
    answer = await _ask_json(hass, task_name="Automation Advisor (set up, fix)", entity_id=ai_entity,
                             instructions=first_prompt + "\n\n" + BUILD_FIX.format(failures=text),
                             on_delta=lambda kind, t: progress(delta=(kind, t)))
    return answer if isinstance(answer.get("items"), list) else {"items": [], "notes": ""}


async def async_buttons_fix(hass: HomeAssistant, ai_entity: str, prompt: str, error: str,
                            triggers: list[dict], progress=None) -> dict:
    """Repair round for a button mapping: same simple format, with the problem spelled out."""
    progress = progress or (lambda **kw: None)
    progress(step=f"Asking {ai_entity} to correct the button mapping")
    answer = await _ask_json(
        hass, task_name="Automation Advisor (set up, fix)", entity_id=ai_entity,
        instructions=prompt + f"\n\nYour previous answer had a problem: {error}. Answer again in the "
        "same JSON format, with every button they asked for mapped to exactly one event.",
        on_delta=lambda kind, t: progress(delta=(kind, t)))
    config, err = buttons_to_automation(answer, triggers)
    return {"items": [{"title": answer.get("title"), "does": answer.get("does"),
                       "why": "Set up as you asked.", "automation": config, "_error": err}],
            "notes": answer.get("notes") or ""}


def device_of(config: dict | None) -> str | None:
    """The one device whose events trigger this automation (a remote / button), if any."""
    trigs = (config or {}).get("triggers") or (config or {}).get("trigger") or []
    devs = {t.get("device_id") for t in (trigs if isinstance(trigs, list) else [trigs])
            if isinstance(t, dict) and (t.get("trigger") or t.get("platform")) == "device"}
    devs.discard(None)
    return devs.pop() if len(devs) == 1 else None
