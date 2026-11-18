"""Runs the analysis on a schedule and keeps the suggestion list.

Suggestion record (stored in .storage/automation_advisor.suggestions):
    id, status (pending | accepted | dismissed), created, last_seen, decided_at, dismissed_at,
    suggestion (analysis.Suggestion.as_dict()), result (what was done), previous_config (undo)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
import re
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import dump

from . import analysis as A, automations as autos
from .archive import Archive
from .collector import Collector
from .describe import describe
from .sources import LocalSource, RemoteSource, Source
from .const import (
    ACTION_ACCEPT,
    ACTION_DELETE,
    ACTION_DISABLE,
    ACTION_DISMISS,
    ACTION_MODIFY,
    ACTION_RESTORE,
    ARCHIVE_FILE,
    CONF_AI_EXTRA,
    CONF_ALLOW_REMOTE_WRITE,
    CONF_MODE,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    MODE_REMOTE,
    CONF_AI_TASK_ENTITY,
    CONF_EXCLUDED,
    CONF_IGNORE_EXISTING,
    CONF_SHOW_STATISTICAL,
    CONF_INTERVAL_HOURS,
    CONF_LOOKBACK_DAYS,
    CONF_MIN_CONFIDENCE,
    CONF_MIN_OCCURRENCES,
    CONF_NOTIFY,
    CONF_OVERRIDE_WINDOW,
    CONF_RETENTION_DAYS,
    DEFAULTS,
    FIRST_RUN_DELAY_S,
    NOTIFICATION_ID,
    PANEL_URL,
    PENDING_GRACE_DAYS,
    SIGNAL_LIVE,
    SIGNAL_UPDATED,
    STATUS_ACCEPTED,
    STATUS_DISMISSED,
    STATUS_PENDING,
    STORE_KEY,
    STORE_VERSION,
)

_LOGGER = logging.getLogger(__name__)
DAY = 86400
AI_RETRY_S = 3600


def _replay_all(found: list[A.Suggestion], ctx: A.Context) -> None:
    """Attach a backtest to every suggestion that carries an automation (blocking). Leads with
    a time window first get the window fitted to the history."""
    from .backtest import backtest, tune_delay, tune_window

    for sg in found:
        if sg.automation and sg.category == "auto_off":
            tuned = tune_delay(sg.automation, ctx.events, ctx.tz, ctx.sun, ctx.entities)
            if tuned is not None:
                cfg, minutes, result = tuned
                old = sg.evidence.get("delay_min")
                sg.automation = cfg
                sg.evidence.update(delay_min=minutes, delay_before_tuning=old,
                                   backtest=result.as_dict())
                sg.title = re.sub(r"after \d+ min", f"after {minutes} min", sg.title)
                sg.automation["alias"] = sg.title
                sg.description += f" Fitted to your history: {minutes} min."
        if sg.automation and sg.evidence.get("window") and sg.automation.get("conditions"):
            tuned = tune_window(sg.automation, sg.evidence["window"], ctx.events, ctx.tz,
                                ctx.sun, ctx.entities)
            if tuned is not None:
                cfg, window, result = tuned
                sg.automation = cfg
                sg.evidence["window_before_tuning"] = sg.evidence["window"]
                sg.evidence["window"] = window
                sg.evidence["backtest"] = result.as_dict()
                sg.description += f" Fitted to your history: {window['text']}."
        if sg.automation and "backtest" not in sg.evidence:
            sg.evidence["backtest"] = backtest(
                sg.automation, ctx.events, ctx.tz, ctx.sun, ctx.entities
            ).as_dict()


class AdvisorManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.options = {**DEFAULTS, **entry.options}
        self.store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
        self.data: dict[str, Any] = {"suggestions": {}, "last_run": None, "last_error": None}
        self.archive = Archive(hass.config.path(ARCHIVE_FILE))
        excluded = set(self.options[CONF_EXCLUDED])
        self.collector = Collector(hass, self.archive, excluded)
        if entry.data.get(CONF_MODE) == MODE_REMOTE:
            self.source: Source = RemoteSource(
                hass, entry.data[CONF_URL], entry.data[CONF_TOKEN],
                entry.data.get(CONF_VERIFY_SSL, True), excluded,
                bool(self.options.get(CONF_ALLOW_REMOTE_WRITE)), int(self.options[CONF_LOOKBACK_DAYS]),
            )
        else:
            self.source = LocalSource(hass, excluded)
        self.running = False
        self.live: dict[str, Any] | None = None
        self._live_sent = 0.0
        self.next_run: float | None = None
        self._unsubs: list = []

    # ---------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        self.data = await self.store.async_load() or self.data
        self.data.setdefault("suggestions", {})
        await self.hass.async_add_executor_job(self.archive.open)
        self.collector.async_start()
        await self.source.async_start(
            self.collector, self.archive, int(self.options[CONF_LOOKBACK_DAYS]) * DAY
        )

        if not self.manual_only:
            interval = timedelta(hours=self.options[CONF_INTERVAL_HOURS])
            self._unsubs.append(async_track_time_interval(self.hass, self._scheduled, interval))
            last = self.data.get("last_run") or 0
            due = last + interval.total_seconds() - time.time()
            delay = max(FIRST_RUN_DELAY_S, due)
            self.next_run = time.time() + delay
            self._unsubs.append(async_call_later(self.hass, delay, self._scheduled))

    @property
    def manual_only(self) -> bool:
        """'Analyse every 0 h': only when someone presses Analyse now (or calls the service)."""
        return not self.options.get(CONF_INTERVAL_HOURS)

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.source.async_stop()
        await self.collector.async_stop()
        await self.hass.async_add_executor_job(self.archive.close)

    async def _retry(self, *_: Any) -> None:
        if not self.running:
            await self.async_run()

    async def _scheduled(self, *_: Any) -> None:
        self.next_run = time.time() + self.options[CONF_INTERVAL_HOURS] * 3600
        await self.async_run()

    # ---------------------------------------------------------------- analysis

    def settings(self) -> A.Settings:
        return A.Settings(
            min_occurrences=int(self.options[CONF_MIN_OCCURRENCES]),
            min_confidence=float(self.options[CONF_MIN_CONFIDENCE]),
            override_window_s=float(self.options[CONF_OVERRIDE_WINDOW]),
            ignore_existing=bool(self.options.get(CONF_IGNORE_EXISTING)),
        )

    async def async_run(self) -> dict:
        """Collect, analyse, merge into the suggestion list. Returns a small summary."""
        if self.running:
            return {"skipped": "already running"}
        self.running = True
        self._notify_listeners()
        try:
            return await self._run()
        except Exception as err:
            _LOGGER.exception("Automation Advisor analysis failed")
            self.data["last_error"] = str(err)
            await self.store.async_save(self.data)
            raise
        finally:
            self.running = False
            self._notify_listeners()

    # ---------------------------------------------------------------- live progress

    def _progress(self, step: str | None = None, delta: tuple[str, str] | None = None) -> None:
        """Live state for the panel: finished steps, the current step, streamed AI text."""
        live = self.live
        if live is None:
            return
        if step is not None:
            if live["step"]:
                live["done"].append({"step": live["step"], "seconds": round(time.time() - live["step_started"])})
            live.update(step=step, step_started=time.time(), thinking="", answer="")
        if delta is not None:
            kind, text = delta
            if kind in ("thinking", "answer"):
                live[kind] = (live[kind] + text)[-6000:]
                live[f"{kind}_chars"] = live.get(f"{kind}_chars", 0) + len(text)
        if step is not None or time.time() - self._live_sent > 0.4 or (delta and delta[0] == "done"):
            self._live_sent = time.time()
            async_dispatcher_send(self.hass, SIGNAL_LIVE)

    async def _run(self) -> dict:
        now = time.time()
        self.live = {"started": now, "step": None, "step_started": now, "done": [],
                     "thinking": "", "answer": ""}
        try:
            return await self._run_inner(now)
        finally:
            self._progress(step="Finished")
            self.live = None
            async_dispatcher_send(self.hass, SIGNAL_LIVE)

    async def _context(self, now: float, blank: bool | None = None) -> A.Context:
        """Everything the statistics, the replay and the AI look at."""
        since = now - int(self.options[CONF_LOOKBACK_DAYS]) * DAY
        events = await self.hass.async_add_executor_job(self.archive.since, since)
        tz = self.source.tz
        ctx = A.Context(
            events=events,
            entities=self.source.entity_info({e.entity_id for e in events}),
            automations=await self.source.async_automations(),
            tz=tz,
            sun=self.source.sun_table({datetime.fromtimestamp(e.ts, tz).date() for e in events}),
            settings=self.settings(),
            now=now,
        )
        if (ctx.settings.ignore_existing if blank is None else blank):
            # Test mode: a blank slate. The statistics, the replay and the AI all see the home as
            # if it had no automations: what they did counts as done by hand, and the AI gets no
            # automation list, no automation labels and no leads about existing automations.
            ctx = A.blank_slate(ctx)
        return ctx

    async def _run_inner(self, now: float) -> dict:
        lookback = int(self.options[CONF_LOOKBACK_DAYS])
        since = now - lookback * DAY
        self._progress(step=f"Reading the activity log ({self.source.label})")
        await self.source.async_prepare(since)
        await self.collector.async_flush()
        await self.hass.async_add_executor_job(self.archive.purge, now - (lookback + 1) * DAY)
        ctx = await self._context(now)
        events = ctx.events
        test_mode = bool(self.options.get(CONF_IGNORE_EXISTING))
        self._progress(step=f"Finding patterns in {len(events)} events")
        found = await self.hass.async_add_executor_job(A.analyze, ctx)
        found += self._broken_automations(ctx.automations)
        self._progress(step=f"Replaying {len(found)} suggestion(s) on your history")
        await self.hass.async_add_executor_job(_replay_all, found, ctx)

        # The statistics are the AI's analyst: their findings are leads for the model and are
        # only shown to the user directly when CONF_SHOW_STATISTICAL is set (tests, debugging).
        leads = found
        shown: list[A.Suggestion] = list(found) if self.options.get(CONF_SHOW_STATISTICAL) else []
        ai_report = None
        if ai_entity := self.options.get(CONF_AI_TASK_ENTITY):
            from .llm import async_ai_suggestions

            try:
                ai_items, ai_report = await async_ai_suggestions(
                    self.hass, ai_entity, leads, ctx, self.source.entity_ids(), self._progress,
                    blank_slate=test_mode,
                )
                shown += ai_items
            except Exception as err:
                _LOGGER.warning("AI step failed: %s", err)
                ai_report = {"entity": ai_entity, "error": f"{type(err).__name__}: {err}"}

        report = await self.hass.async_add_executor_job(A.diagnose, ctx, shown)
        report["leads"] = len(leads)
        report["test_mode"] = test_mode
        if ai_report is not None:
            report["ai"] = ai_report
        if not ai_entity and not self.options.get(CONF_SHOW_STATISTICAL):
            report["headline"] = ("Connect an AI to get suggestions: Automation Advisor → Configure "
                                  "→ AI Task entity. The activity log keeps being collected.")
        elif ai_report and ai_report.get("error"):
            report["headline"] = "The AI step failed, so there are no new suggestions this time."
        elif ai_entity:
            n = len(ai_report.get("accepted", [])) if ai_report else 0
            report["headline"] = (f"The AI proposed {ai_report.get('proposed', 0)} item(s); {n} held up "
                                  "on your history." if n else
                                  "The AI found nothing that held up on your history this time.")
        found = shown
        names = {e: i.name for e, i in ctx.entities.items()}
        for sg in found:
            if sg.automation:
                sg.evidence["exactly"] = describe(sg.automation, names)
        new = self._merge(found, now)
        self._purge_dismissed(now)
        self.data["last_run"] = now
        self.data["last_error"] = None
        self.data["last_stats"] = {"events": len(events), "found": len(found), "new": new}
        report["new"] = len(new)
        report["pending"] = len(self.pending())
        report["finished"] = now
        self.data["last_report"] = report
        await self.store.async_save(self.data)
        if new and self.options[CONF_NOTIFY]:
            self._notify_new(new)
        if ai_report and ai_report.get("error") and not self.manual_only:
            # The AI may be on a computer that is asleep or off: try again in an hour instead of
            # waiting for the next scheduled run.
            report["retry_at"] = time.time() + AI_RETRY_S
            self.next_run = min(self.next_run or report["retry_at"], report["retry_at"])
            self._unsubs.append(async_call_later(self.hass, AI_RETRY_S, self._retry))
        return self.data["last_stats"]

    def _broken_automations(self, automations: list[A.ExistingAutomation]) -> list[A.Suggestion]:
        """Automations that point at entities which no longer exist."""
        out = []
        for auto in automations:
            refs = auto.trigger_entities | auto.action_entities
            missing = sorted(
                r for r in refs
                if "." in r and "{" not in r and not self.source.entity_exists(r)
            )
            if not missing:
                continue
            options = ([ACTION_DELETE] if auto.editable else []) + (
                [ACTION_DISABLE] if auto.enabled else [])
            if not options:
                continue
            out.append(A.Suggestion(
                kind=A.KIND_REVIEW,
                category="broken",
                fingerprint=A.fingerprint("broken", auto.entity_id, A.config_hash(auto.config)),
                title=f"\"{auto.alias}\" refers to devices that no longer exist",
                description=("These entities are gone: " + ", ".join(missing) +
                             ". The automation cannot work as intended; delete it, disable it, "
                             "or fix it in the automation editor."),
                confidence=1.0,
                evidence={"automation": auto.entity_id, "missing": missing,
                          "config_hash": A.config_hash(auto.config)},
                target_automation=auto.entity_id,
                options=options,
            ))
        return out

    # ---------------------------------------------------------------- suggestion list

    def _merge(self, found: list[A.Suggestion], now: float) -> list[str]:
        records: dict[str, dict] = self.data["suggestions"]
        if not self.options.get(CONF_SHOW_STATISTICAL):
            # AI-only: statistics-only suggestions from older versions are dropped
            for fid, rec in list(records.items()):
                if rec["status"] == STATUS_PENDING and rec["suggestion"].get("category") != "ai":
                    del records[fid]
        seen = set()
        new_ids = []
        for sg in found:
            seen.add(sg.fingerprint)
            rec = records.get(sg.fingerprint)
            if rec is None:
                records[sg.fingerprint] = {
                    "id": sg.fingerprint,
                    "status": STATUS_PENDING,
                    "created": now,
                    "last_seen": now,
                    "suggestion": sg.as_dict(),
                }
                new_ids.append(sg.fingerprint)
            elif rec["status"] == STATUS_PENDING:
                rec["suggestion"] = sg.as_dict()
                rec["last_seen"] = now
            else:
                rec["last_seen"] = now  # accepted / dismissed: remember, do not re-propose
        for fid, rec in list(records.items()):
            if (rec["status"] == STATUS_PENDING and fid not in seen and not rec.get("asked")
                    and now - rec["last_seen"] > PENDING_GRACE_DAYS * DAY):
                del records[fid]  # the pattern went away
        return new_ids

    def _purge_dismissed(self, now: float) -> None:
        keep = int(self.options[CONF_RETENTION_DAYS]) * DAY
        records = self.data["suggestions"]
        for fid, rec in list(records.items()):
            if rec["status"] == STATUS_DISMISSED and now - (rec.get("dismissed_at") or now) > keep:
                del records[fid]

    def snapshot(self) -> dict:
        now = time.time()
        keep = int(self.options[CONF_RETENTION_DAYS]) * DAY
        items = []
        for rec in self.data["suggestions"].values():
            item = dict(rec)
            if (cfg := rec["suggestion"].get("automation")):
                item["yaml"] = dump({k: v for k, v in cfg.items() if k != "id"})
            if rec["status"] == STATUS_DISMISSED:
                item["expires"] = (rec.get("dismissed_at") or now) + keep
            items.append(item)
        items.sort(key=lambda r: (-(r.get("decided_at") or r["created"])))
        return {
            "suggestions": items,
            "last_run": self.data.get("last_run"),
            "last_error": self.data.get("last_error"),
            "last_stats": self.data.get("last_stats"),
            "last_report": self.data.get("last_report"),
            "next_run": self.next_run,
            "running": self.running,
            "ai_entity": self.options.get(CONF_AI_TASK_ENTITY),
            "retention_days": int(self.options[CONF_RETENTION_DAYS]),
            "interval_hours": self.options[CONF_INTERVAL_HOURS],
            "manual_only": self.manual_only,
            "lookback_days": self.options[CONF_LOOKBACK_DAYS],
            "source": self.source.status(),
            "test_mode": bool(self.options.get(CONF_IGNORE_EXISTING)),
        }

    def pending(self) -> list[dict]:
        return [r for r in self.data["suggestions"].values() if r["status"] == STATUS_PENDING]

    async def async_clear(self) -> int:
        """Forget every suggestion (pending, dismissed and applied), e.g. for a clean run.
        Automations already created are not touched."""
        n = len(self.data["suggestions"])
        self.data["suggestions"] = {}
        self.data["last_report"] = None
        await self.store.async_save(self.data)
        persistent_notification.async_dismiss(self.hass, NOTIFICATION_ID)
        self._notify_listeners()
        return n

    async def async_edit(self, suggestion_id: str, *, yaml_text: str | None = None,
                         instruction: str | None = None) -> dict:
        """Change one suggestion's automation, by hand (YAML) or by asking the AI (instruction).
        The result is checked (devices, safety, Home Assistant's validator) and replayed on the
        history; the card then shows the new numbers."""
        from homeassistant.util.yaml import parse_yaml

        from .llm import async_refine_one, verify

        rec = self.data["suggestions"].get(suggestion_id)
        if rec is None:
            raise autos.AdvisorError("Unknown suggestion (it may have expired).")
        if rec["status"] == STATUS_ACCEPTED:
            raise autos.AdvisorError("This suggestion was already applied.")
        sg = rec["suggestion"]
        if not sg.get("automation"):
            raise autos.AdvisorError("This suggestion has no automation to edit.")
        if self.running:
            raise autos.AdvisorError("An analysis is running; try again when it finishes.")
        ctx = await self._context(time.time())
        names = {e: i.name for e, i in ctx.entities.items()}
        known = self.source.entity_ids() | {"sun.sun"}
        keep_id = sg["automation"].get("id")
        edit: dict[str, Any] = {"at": time.time()}
        if yaml_text is not None:
            try:
                config = parse_yaml(yaml_text)
            except Exception as err:
                raise autos.AdvisorError(f"That is not valid YAML: {err}") from err
            if not isinstance(config, dict):
                raise autos.AdvisorError("The YAML must be one automation (a mapping).")
            edit["how"] = "yaml"
        elif instruction and instruction.strip():
            ai_entity = self.options.get(CONF_AI_TASK_ENTITY)
            if not ai_entity:
                raise autos.AdvisorError("Connect an AI first (Automation Advisor → Configure).")
            item = await async_refine_one(self.hass, ai_entity, sg, instruction.strip(), ctx, names)
            config = item["automation"]
            for key in ("title", "does", "why"):
                if isinstance(item.get(key), str) and item[key].strip():
                    if key == "title":
                        sg["title"] = item[key].strip()[:80]
                    elif key == "does":
                        sg["evidence"]["does"] = item[key].strip()
                    else:
                        sg["description"] = item[key].strip()
            edit.update(how="prompt", instruction=instruction.strip())
        else:
            raise autos.AdvisorError("Nothing to change.")
        config = {k: v for k, v in config.items() if k != "id"}
        config.setdefault("alias", sg["title"])
        config.setdefault("mode", "single")
        result, error = await verify(self.hass, config, known, ctx,
                                     await self.source.async_catalog())
        if error:
            raise autos.AdvisorError(error)
        if keep_id:
            config = {"id": keep_id, **config}
        if edit["how"] == "yaml":
            # The AI's wording described the previous version; the generated "Exactly" text is
            # always right, and the title follows the alias the person wrote.
            sg["evidence"].pop("does", None)
            sg["title"] = str(config.get("alias") or sg["title"])[:80]
        sg["automation"] = config
        sg["evidence"]["backtest"] = result.as_dict()
        sg["evidence"]["exactly"] = describe(config, names)
        sg["evidence"].setdefault("edits", []).append(edit)
        if rec["status"] == STATUS_DISMISSED:
            rec.update(status=STATUS_PENDING, dismissed_at=None)
        await self.store.async_save(self.data)
        self._notify_listeners()
        return rec

    async def async_device_list(self) -> list[dict]:
        """Devices, newest first, for the "set something up" picker."""
        catalog = await self.source.async_catalog()
        return [{k: d[k] for k in ("id", "name", "area", "manufacturer", "model", "created")}
                for d in catalog["devices"]]

    async def async_build(self, request: str, device_id: str | None = None) -> dict:
        """'Set this up for me' -> verified automation suggestion(s) from the AI."""
        from .llm import async_build, async_build_fix, async_buttons_fix

        ai_entity = self.options.get(CONF_AI_TASK_ENTITY)
        if not ai_entity:
            raise autos.AdvisorError("Connect an AI first (Automation Advisor → Configure).")
        if not request or not request.strip():
            raise autos.AdvisorError("Describe what you want first.")
        if self.running:
            raise autos.AdvisorError("An analysis is running; try again when it finishes.")
        now = time.time()
        catalog = await self.source.async_catalog()
        device = next((d for d in catalog["devices"] if d["id"] == device_id), None) if device_id else None
        if device_id and device is None:
            raise autos.AdvisorError("That device does not exist (any more).")
        triggers = await self.source.async_device_triggers(device_id) if device else []
        ctx = await self._context(now, blank=False)
        names = {e: i["name"] for e, i in catalog["entities"].items()}
        names.update({d["id"]: d["name"] for d in catalog["devices"]})
        names.update({a["id"]: a["name"] for a in catalog["areas"]})
        self.running = True
        self.live = {"started": now, "step": None, "step_started": now, "done": [],
                     "thinking": "", "answer": ""}
        self._notify_listeners()
        made, problems = [], []
        try:
            answer = await async_build(self.hass, ai_entity, request.strip(), catalog, device,
                                       triggers, self._progress)
            self._progress(step="Checking what the AI wrote")
            failures = await self._take_built(answer["items"][:4], request, device, catalog, ctx,
                                              names, now, made, problems, retry_ok=True)
            if failures:
                if answer.get("_buttons"):
                    repaired = await async_buttons_fix(self.hass, ai_entity, answer["_prompt"],
                                                       failures[0][1], triggers, self._progress)
                else:
                    repaired = await async_build_fix(self.hass, ai_entity, answer["_prompt"],
                                                     failures, self._progress)
                self._progress(step="Checking the repaired automation(s)")
                problems[:] = [p for p in problems if not p.startswith(tuple(
                    f"{str(f[0].get('title') or 'AI set-up')[:80]}:" for f in failures))]
                await self._take_built(repaired["items"][:len(failures)], request, device, catalog,
                                       ctx, names, now + 0.001, made, problems, retry_ok=False)
                if repaired.get("notes"):
                    answer["notes"] = (str(answer.get("notes") or "") + " " + repaired["notes"]).strip()
        finally:
            self.running = False
            self.live = None
            async_dispatcher_send(self.hass, SIGNAL_LIVE)
        await self.store.async_save(self.data)
        self._notify_listeners()
        notes = str(answer.get("notes") or "").strip() if "answer" in locals() else ""
        if not made:
            raise autos.AdvisorError(
                "The AI could not set this up. " + (f"It said: {notes} " if notes else "")
                + (f"Problems: {'; '.join(problems)}" if problems else ""))
        return {"suggestions": made, "notes": notes, "problems": problems}

    async def _take_built(self, items, request, device, catalog, ctx, names, now, made, problems,
                          retry_ok: bool) -> list[tuple[dict, str]]:
        """Check the AI's set-up items; good ones become pending suggestions. Returns the ones
        that failed (with the reason) when a repair round is still allowed."""
        from .backtest import backtest
        from .llm import check_build

        failures = []
        for i, item in enumerate(items):
            config = item.get("automation") if isinstance(item, dict) else None
            title = str((item or {}).get("title") or "AI set-up")[:80]
            err = item.get("_error") if isinstance(item, dict) else None
            err = err or check_build(config, catalog)
            if err is None:
                config = {"alias": title, "mode": config.get("mode", "restart"),
                          **{k: v for k, v in config.items() if k in (
                              "triggers", "trigger", "conditions", "condition", "actions", "action")}}
                config["description"] = f"Set up by Automation Advisor (AI) on request: {request.strip()}"
                try:
                    await autos.validate(self.hass, config)
                except autos.AdvisorError as exc:
                    err = str(exc)
            if err is not None:
                problems.append(f"{title}: {err}")
                if retry_ok and isinstance(item, dict):
                    failures.append((item, err))
                continue
            result = await self.hass.async_add_executor_job(
                backtest, config, ctx.events, ctx.tz, ctx.sun, ctx.entities)
            bt = result.as_dict()
            if not result.supported and any(w in (result.reason or "") for w in ("'device'", "'event'")):
                bt["summary"] = ("New set-up: button presses and other device events have no "
                                 "history to replay, so try it after accepting.")
            sg = A.Suggestion(
                kind=A.KIND_CREATE, category="ai",
                fingerprint=A.fingerprint("ai-build", request.strip(), device and device["id"], now, i),
                title=title, description=str(item.get("why") or "").strip(), confidence=0.8,
                evidence={"source": "ai", "ai_kind": "create", "asked": request.strip(),
                          "built": True, "device": device["name"] if device else None,
                          "does": str(item.get("does") or "").strip() or None,
                          "exactly": describe(config, names), "backtest": bt},
                automation=config, options=["accept"],
            )
            rec = {"id": sg.fingerprint, "status": STATUS_PENDING, "created": now,
                   "last_seen": now, "suggestion": sg.as_dict(), "asked": True}
            self.data["suggestions"][sg.fingerprint] = rec
            made.append(rec)
        return failures

    async def async_automation_list(self) -> list[dict]:
        """Every automation, for the panel's "ask the AI about an automation" picker."""
        autos_ = await self.source.async_automations()
        return sorted(({"entity_id": a.entity_id, "alias": a.alias, "editable": a.editable,
                        "enabled": a.enabled} for a in autos_), key=lambda x: x["alias"].lower())

    async def async_fix(self, automation_entity_id: str, problem: str) -> dict:
        """'This automation does not work: <problem>' -> a verified fix suggestion (pending), which
        the person applies like any other. Uses the automation's traces, its replay and the recent
        activity of its devices as evidence."""
        from .llm import _body, async_build, async_buttons_fix, async_fix_automation, device_of, verify

        ai_entity = self.options.get(CONF_AI_TASK_ENTITY)
        if not ai_entity:
            raise autos.AdvisorError("Connect an AI first (Automation Advisor → Configure).")
        if not problem or not problem.strip():
            raise autos.AdvisorError("Describe what is wrong first.")
        if self.running:
            raise autos.AdvisorError("An analysis is running; try again when it finishes.")
        now = time.time()
        ctx = await self._context(now, blank=False)  # a fix always looks at the real automations
        auto = next((a for a in ctx.automations if a.entity_id == automation_entity_id), None)
        if auto is None:
            raise autos.AdvisorError(f"{automation_entity_id} does not exist (any more).")
        catalog = await self.source.async_catalog()
        names = {e: i.name for e, i in ctx.entities.items()}
        names.update({e: i["name"] for e, i in catalog["entities"].items() if e not in names})
        names.update({d["id"]: d["name"] for d in catalog["devices"]})
        names.update({a["id"]: a["name"] for a in catalog["areas"]})
        traces = await self.source.async_traces(auto)
        device_id = device_of(auto.config)
        device = next((d for d in catalog["devices"] if d["id"] == device_id), None)
        device_triggers = await self.source.async_device_triggers(device_id) if device else []
        self.running = True
        self.live = {"started": now, "step": None, "step_started": now, "done": [],
                     "thinking": "", "answer": ""}
        self._notify_listeners()
        try:
            if device and device_triggers:
                # A remote / button automation: re-map the buttons through the reliable button
                # path (numbered events -> the advisor writes the automation), told how it is set
                # up now and what is wrong.
                request = (f"This {device['name']} is currently set up like this: "
                           f"{describe(auto.config, names)} The person now says: \"{problem.strip()}\". "
                           "Map the buttons again: keep everything that works and change what they ask.")
                answer = await async_build(self.hass, ai_entity, request, catalog, device,
                                           device_triggers, self._progress)
                item = answer["items"][0]
                if item.get("_error"):
                    repaired = await async_buttons_fix(self.hass, ai_entity, answer["_prompt"],
                                                       item["_error"], device_triggers, self._progress)
                    item = repaired["items"][0]
                if item.get("_error") or not item.get("automation"):
                    raise autos.AdvisorError(f"The AI's button mapping did not work: {item.get('_error')}")
                item["why"] = (f"Re-mapped the buttons as you asked. {answer.get('notes') or ''}").strip()
            else:
                item = await async_fix_automation(self.hass, ai_entity, auto, problem.strip(), ctx,
                                                  traces, names, self._progress)
            self._progress(step="Checking the fix: validator and replay on your history")
            config = {k: v for k, v in item["automation"].items() if k != "id"}
            config.setdefault("alias", auto.alias)
            config.setdefault("mode", "single")
            if _body(config) == _body(auto.config or {}):
                raise autos.AdvisorError(
                    "The AI did not change anything: it found no cause for this in the "
                    f"automation's runs and your history. Its explanation: {item.get('why') or '-'} "
                    "Try describing when it happens and what you expected, e.g. \"around 7 pm, "
                    "while I'm at the stove, the light goes off after 5 minutes\"."
                )
            result, error = await verify(self.hass, config, self.source.entity_ids() | {"sun.sun"},
                                         ctx, catalog)
            if error:
                raise autos.AdvisorError(f"The AI's fix did not pass the checks: {error}")
        finally:
            self.running = False
            self.live = None
            async_dispatcher_send(self.hass, SIGNAL_LIVE)
            self._notify_listeners()
        before = item.get("_before")
        title = str(item.get("title") or f"Fix \"{auto.alias}\"")[:80]
        evidence = {"source": "ai", "ai_kind": "modify", "asked": problem.strip(),
                    "backtest": result.as_dict(),
                    "backtest_before": before.as_dict() if before else None,
                    "config_hash": A.config_hash(auto.config),
                    "exactly": describe(config, names), "traces_used": len(traces)}
        if item.get("does"):
            evidence["does"] = str(item["does"]).strip()
        if auto.editable and auto.config_id:
            sg = A.Suggestion(
                kind=A.KIND_REVIEW, category="ai",
                fingerprint=A.fingerprint("ai-fix", auto.entity_id, problem.strip(), now),
                title=title, description=str(item.get("why") or "").strip(), confidence=0.8,
                evidence=evidence, automation={"id": auto.config_id, **config},
                target_automation=auto.entity_id, options=["modify"],
            )
        else:  # YAML automation: offer the fixed version as a new one
            config["alias"] = f"{auto.alias} (fixed)"
            sg = A.Suggestion(
                kind=A.KIND_CREATE, category="ai",
                fingerprint=A.fingerprint("ai-fix", auto.entity_id, problem.strip(), now),
                title=title, description=(str(item.get("why") or "").strip() + f" \"{auto.alias}\" is "
                                          "defined in YAML, so this is added as a new automation; turn "
                                          "the old one off afterwards."),
                confidence=0.8, evidence=evidence, automation=config, options=["accept"],
            )
        rec = {"id": sg.fingerprint, "status": STATUS_PENDING, "created": now, "last_seen": now,
               "suggestion": sg.as_dict(), "asked": True}
        self.data["suggestions"][sg.fingerprint] = rec
        await self.store.async_save(self.data)
        self._notify_listeners()
        return rec

    async def async_decide(self, suggestion_id: str, action: str) -> dict:
        rec = self.data["suggestions"].get(suggestion_id)
        if rec is None:
            raise autos.AdvisorError("Unknown suggestion (it may have expired).")
        sg = rec["suggestion"]
        now = time.time()

        if action == ACTION_DISMISS:
            rec.update(status=STATUS_DISMISSED, dismissed_at=now, decided_at=now)
        elif action == ACTION_RESTORE:
            if rec["status"] != STATUS_DISMISSED:
                raise autos.AdvisorError("Only dismissed suggestions can be restored.")
            rec.update(status=STATUS_PENDING, dismissed_at=None, decided_at=None)
        else:
            if rec["status"] == STATUS_ACCEPTED:
                raise autos.AdvisorError("This suggestion was already applied.")
            if action not in sg["options"]:
                raise autos.AdvisorError(f"'{action}' is not possible for this suggestion.")
            result = await self._apply(sg, action, rec)
            rec.update(status=STATUS_ACCEPTED, decided_at=now, action=action, result=result,
                       dismissed_at=None)
            rec = self._merge_applied(rec)
        await self.store.async_save(self.data)
        self._notify_listeners()
        if not self.pending():
            persistent_notification.async_dismiss(self.hass, NOTIFICATION_ID)
        return rec

    def _merge_applied(self, rec: dict) -> dict:
        """One Applied card per automation: when a change was applied to an automation that
        already has an Applied card (it was created or fixed here before), update that card
        instead of adding another, and keep the earlier versions in its history."""
        entity_id = (rec.get("result") or {}).get("entity_id")
        if not entity_id or (rec.get("result") or {}).get("manual"):
            return rec
        older = [r for r in self.data["suggestions"].values()
                 if r is not rec and r["status"] == STATUS_ACCEPTED
                 and (r.get("result") or {}).get("entity_id") == entity_id
                 and not (r.get("result") or {}).get("manual")]
        if not older:
            return rec
        keep = min(older, key=lambda r: r.get("created", 0))
        for other in older:  # earlier merges may have left more than one; fold them all
            if other is not keep:
                keep.setdefault("history", []).extend(other.get("history", []))
                self.data["suggestions"].pop(other["id"], None)
        keep.setdefault("history", []).append({
            "at": rec.get("decided_at"), "action": rec.get("action"),
            "asked": rec["suggestion"].get("evidence", {}).get("asked"),
            "previous": keep["suggestion"],
        })
        if (rec.get("result") or {}).get("deleted"):
            keep["result"] = rec["result"]
        else:
            keep["result"] = {**(keep.get("result") or {}), **rec["result"]}
        keep["suggestion"] = rec["suggestion"]
        keep["decided_at"] = rec.get("decided_at")
        keep["action"] = rec.get("action")
        self.data["suggestions"].pop(rec["id"], None)
        return keep

    async def _apply(self, sg: dict, action: str, rec: dict) -> dict:
        src = self.source
        if not src.can_write:
            return self._manual_result(sg, action)
        if action == ACTION_ACCEPT:
            return {"entity_id": await src.async_create(sg["automation"])}
        target = sg["target_automation"]
        auto = next((a for a in await src.async_automations() if a.entity_id == target), None)
        if auto is None:
            raise autos.AdvisorError(f"{target} no longer exists.")
        if action == ACTION_DISABLE:
            await src.async_disable(target)
            return {"entity_id": target, "disabled": True}
        if not (auto.editable and auto.config_id):
            raise autos.AdvisorError("Only automations made in the UI can be changed here.")
        base = sg.get("evidence", {}).get("config_hash")
        if base and base != A.config_hash(auto.config):
            raise autos.AdvisorError(
                "This automation was edited after the suggestion was made. Run the analysis "
                "again for an up-to-date suggestion."
            )
        if action == ACTION_MODIFY:
            rec["previous_config"] = await src.async_update(auto.config_id, sg["automation"])
            return {"entity_id": target, "modified": True}
        if action == ACTION_DELETE:
            rec["previous_config"] = await src.async_delete(auto.config_id)
            return {"entity_id": target, "deleted": True}
        raise autos.AdvisorError(f"Unknown action {action}")

    def _manual_result(self, sg: dict, action: str) -> dict:
        """Read-only connection: hand the change to the user instead of making it."""
        where = self.source.label
        target = sg.get("target_automation")
        if action in (ACTION_ACCEPT, ACTION_MODIFY):
            how = ("Settings → Automations & scenes → Create automation → ⋮ → Edit in YAML, "
                   "paste, Save." if action == ACTION_ACCEPT else
                   f"Settings → Automations & scenes → open {target} → ⋮ → Edit in YAML, "
                   "replace with this, Save.")
            return {"manual": True, "yaml": dump(sg["automation"]),
                    "instructions": f"Nothing was changed on {where}. To use it: {how}"}
        verb = "Delete" if action == ACTION_DELETE else "Turn off"
        return {"manual": True,
                "instructions": f"Nothing was changed on {where}. {verb} {target} in "
                                "Settings → Automations & scenes."}

    # ---------------------------------------------------------------- notifications

    @callback
    def _notify_listeners(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    @callback
    def _notify_new(self, new_ids: list[str]) -> None:
        pending = self.pending()
        titles = [self.data["suggestions"][i]["suggestion"]["title"] for i in new_ids][:5]
        lines = "\n".join(f"- {t}" for t in titles)
        more = f"\n- … and {len(new_ids) - 5} more" if len(new_ids) > 5 else ""
        persistent_notification.async_create(
            self.hass,
            f"{len(new_ids)} new idea(s) from your daily activity:\n{lines}{more}\n\n"
            f"[Review {len(pending)} suggestion(s)](/{PANEL_URL})",
            title="Automation Advisor",
            notification_id=NOTIFICATION_ID,
        )
