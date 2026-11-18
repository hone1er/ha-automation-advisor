// Automation Advisor sidebar panel. Plain web component, no build step, no dependencies.

const CATEGORY = {
  trigger_action: "Reacts to a sensor",
  auto_off: "Auto off",
  routine: "Daily routine",
  overridden: "You keep undoing it",
  broken: "Broken automation",
  stuck_on: "Stays on too long",
  ai: "AI idea (checked)",
};
const READONLY_LABEL = {
  accept: "Get YAML to add",
  modify: "Get changed YAML",
  delete: "I'll delete it",
  disable: "I'll turn it off",
};
const ACTION_LABEL = {
  accept: "Create & enable",
  modify: "Apply change",
  delete: "Delete automation",
  disable: "Turn off",
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function yScalar(v) {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") {
    return /^[A-Za-z_][\w .:\-/]*$/.test(v) && !/^(on|off|yes|no|true|false|null)$/i.test(v) ? v : JSON.stringify(v);
  }
  return String(v);
}

function yLines(v) {
  const isEmpty = (x) => x !== null && typeof x === "object" && Object.keys(x).length === 0;
  const isScalar = (x) => x === null || typeof x !== "object";
  if (isScalar(v)) return [yScalar(v)];
  if (isEmpty(v)) return [Array.isArray(v) ? "[]" : "{}"];
  const out = [];
  if (Array.isArray(v)) {
    for (const item of v) {
      if (isScalar(item) || isEmpty(item)) { out.push(`- ${yLines(item)[0]}`); continue; }
      yLines(item).forEach((line, i) => out.push((i === 0 ? "- " : "  ") + line));
    }
    return out;
  }
  for (const [k, val] of Object.entries(v)) {
    if (isScalar(val) || isEmpty(val)) { out.push(`${k}: ${yLines(val)[0]}`); continue; }
    out.push(`${k}:`);
    yLines(val).forEach((line) => out.push("  " + line));
  }
  return out;
}

function toYaml(v) {
  return yLines(v).join("\n");
}

function ago(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 0) {
    const m = Math.round(-s / 60);
    if (m < 90) return `in ${m} min`;
    return m < 2880 ? `in ${Math.round(m / 60)} h` : `in ${Math.round(m / 1440)} days`;
  }
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

class AutomationAdvisorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "pending";
    this._data = null;
    this._busy = {};
    this._error = null;
    this._open = {};
    this._editing = {};
    this._drafts = {};
    this._asks = {};
    this._editErr = {};
    this._fix = { open: false, list: null, automation: "", problem: "", busy: false, error: null, done: null };
    this._applyAsk = {};
    this._build = { open: false, devices: null, device: "", request: "", busy: false, error: null, notes: null };
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._load();
      this._subscribe();
    }
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = hass;
  }

  set narrow(v) {
    this._narrow = v;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.narrow = v;
  }

  disconnectedCallback() {
    if (this._unsub) this._unsub.then((u) => u()).catch(() => {});
    if (this._unsubLive) this._unsubLive.then((u) => u()).catch(() => {});
    clearInterval(this._tick);
    this._unsub = null;
  }

  connectedCallback() {
    if (this._hass && !this._unsub) this._subscribe();
    this._render();
  }

  _subscribe() {
    this._unsub = this._hass.connection.subscribeMessage(() => this._load(), { type: "automation_advisor/subscribe" });
    this._unsubLive = this._hass.connection.subscribeMessage((ev) => {
      const was = !!this._live;
      this._live = ev.live;
      if (!!this._live !== was) this._render(); else this._renderLive();
    }, { type: "automation_advisor/live" });
    clearInterval(this._tick);
    this._tick = setInterval(() => { if (this._live) this._renderLive(); }, 1000);
  }

  _renderLive() {
    const box = this.shadowRoot.getElementById("live");
    if (!box) return;
    const l = this._live;
    if (!l) { box.innerHTML = ""; return; }
    const secs = (t) => Math.max(0, Math.round(Date.now() / 1000 - t));
    const fmt = (n) => (n >= 60 ? `${Math.floor(n / 60)}m ${n % 60}s` : `${n}s`);
    const done = (l.done || []).map((d) => `<li class="ok">✓ ${esc(d.step)} <span class="small">${fmt(d.seconds)}</span></li>`).join("");
    const cur = l.step ? `<li class="cur"><span class="spin"></span> ${esc(l.step)} <span class="small">${fmt(secs(l.step_started))}</span></li>` : "";
    const stream = (label, text, n) => text ? `<p class="small"><b>${label}</b> (${n || text.length} characters so far)</p><pre class="stream" data-k="${label}">${esc(text)}</pre>` : "";
    const keep = {};
    box.querySelectorAll("pre.stream").forEach((p) => { keep[p.dataset.k] = p.scrollHeight - p.scrollTop - p.clientHeight < 30; });
    box.innerHTML = `<div class="live"><p><b>Analysing…</b> <span class="small">${fmt(secs(l.started))} so far</span></p>
      <ul class="steps">${done}${cur}</ul>
      ${stream("Model thinking", l.thinking, l.thinking_chars)}
      ${stream("Model writing", l.answer, l.answer_chars)}
      ${l.step && l.step.startsWith("Asking") && !l.thinking && !l.answer ? `<p class="small">Waiting for the model to start (loading it into memory can take ~20 s)…</p>` : ""}</div>`;
    box.querySelectorAll("pre.stream").forEach((p) => { if (keep[p.dataset.k] !== false) p.scrollTop = p.scrollHeight; });
  }

  async _load(force = false) {
    this._forceNext = force;
    try {
      this._data = await this._hass.callWS({ type: "automation_advisor/list" });
      this._error = null;
    } catch (e) {
      this._error = e.message || String(e);
    }
    this._render();
  }

  async _decide(id, action) {
    if (action === "delete" && !confirm("Delete this automation? A copy is kept with this suggestion.")) return;
    this._busy[id] = action;
    this._render();
    try {
      await this._hass.callWS({ type: "automation_advisor/decide", suggestion_id: id, action });
      this._error = null;
    } catch (e) {
      this._error = e.message || String(e);
    }
    delete this._busy[id];
    await this._load();
  }

  async _run() {
    this._busy.__run = true;
    this._render();
    this._result = null;
    try {
      const res = await this._hass.callWS({ type: "automation_advisor/run" });
      this._error = null;
      this._result = res.skipped ? { skipped: true } : res.report || {};
      this._tab = "pending";
    } catch (e) {
      this._error = `Analysis failed: ${e.message || String(e)}`;
    }
    delete this._busy.__run;
    await this._load();
  }

  _buildBox() {
    const b = this._build;
    const notes = b.notes ? `<div class="result"><button class="close" id="build-notes-close">✕</button><p><b>Notes from the AI:</b> ${esc(b.notes)}</p></div>` : "";
    if (!b.open) {
      return `${notes}<div class="fixbar"><button class="link" id="build-open">Set something up with AI (e.g. a new remote or sensor)</button></div>`;
    }
    const opts = (b.devices || []).map((d) => `<option value="${esc(d.id)}" ${d.id === b.device ? "selected" : ""}>${esc(d.name)}${d.area ? ` — ${esc(d.area)}` : ""}${d.model ? ` (${esc(d.model)})` : ""}</option>`).join("");
    return `${notes}<div class="card fixcard">
      <h3>Set something up with AI</h3>
      <p class="small">Say what you want. If it is about one device (a new remote, sensor or plug), pick it: the AI then sees exactly which buttons or events it has. The result is a suggestion you can review, edit and accept.</p>
      ${b.devices === null ? `<p class="small">Loading your devices…</p>` : `
      <select id="build-device"><option value="">No specific device</option>${opts}</select>
      <textarea id="build-request" class="problem" placeholder="e.g. Use my new IKEA 4-button remote for the bedroom: up = lights on, down = off, left/right = dimmer/brighter.">${esc(b.request)}</textarea>
      <div class="actions">
        <button class="primary" id="build-go" ${b.busy || !b.request.trim() ? "disabled" : ""}>${b.busy ? "The AI is setting it up… (1–3 min)" : "Set it up"}</button>
        <button class="ghost" id="build-close" ${b.busy ? "disabled" : ""}>Close</button>
      </div>`}
      ${b.error ? `<div class="error">${esc(b.error)}</div>` : ""}
    </div>`;
  }

  async _askBuild() {
    const b = this._build;
    b.busy = true; b.error = null; b.notes = null;
    this._render(true);
    try {
      const res = await this._hass.callWS({ type: "automation_advisor/build", request: b.request.trim(), device_id: b.device || null });
      this._highlight = res.suggestions[0]?.id;
      this._tab = "pending";
      this._build = { open: false, devices: b.devices, device: "", request: "", busy: false, error: null,
        notes: [res.notes, ...(res.problems || []).map((p) => `Not shown: ${p}`)].filter(Boolean).join(" ") || null };
    } catch (e) {
      b.error = e.message || String(e);
      b.busy = false;
    }
    await this._load(true);
  }

  _fixBox() {
    const f = this._fix;
    if (!f.open) {
      return `<div class="fixbar"><button class="link" id="fix-open">Ask the AI about an automation that doesn't work right</button></div>`;
    }
    const opts = (f.list || []).map((a) => `<option value="${esc(a.entity_id)}" ${a.entity_id === f.automation ? "selected" : ""}>${esc(a.alias)}${a.enabled ? "" : " (off)"}</option>`).join("");
    return `<div class="card fixcard">
      <h3>Ask the AI to fix an automation</h3>
      <p class="small">Pick the automation and say what goes wrong. The AI looks at the automation, its last runs and what your devices did, and proposes a fix. You review it (with a replay on your history) before anything changes.</p>
      ${f.list === null ? `<p class="small">Loading your automations…</p>` : `
      <select id="fix-auto"><option value="">Choose an automation…</option>${opts}</select>
      <textarea id="fix-problem" class="problem" placeholder="e.g. The basement lights stay on for hours after everyone has left. / It turns the light on during the day. / It never runs.">${esc(f.problem)}</textarea>
      <div class="actions">
        <button class="primary" id="fix-go" ${f.busy || !f.automation || !f.problem.trim() ? "disabled" : ""}>${f.busy ? "The AI is looking at it… (1–3 min)" : "Ask AI for a fix"}</button>
        <button class="ghost" id="fix-close" ${f.busy ? "disabled" : ""}>Close</button>
      </div>`}
      ${f.error ? `<div class="error">${esc(f.error)}</div>` : ""}
    </div>`;
  }

  async _askFix(entityId, problem, applyId) {
    const target = applyId ? this._applyAsk[applyId] : this._fix;
    target.busy = true; target.error = null;
    this._render(true);
    try {
      const rec = await this._hass.callWS({ type: "automation_advisor/fix", automation: entityId, problem });
      this._highlight = rec.id;
      this._tab = "pending";
      if (applyId) delete this._applyAsk[applyId];
      else this._fix = { open: false, list: this._fix.list, automation: "", problem: "", busy: false, error: null };
    } catch (e) {
      target.error = e.message || String(e);
      target.busy = false;
    }
    await this._load(true);
  }

  _applyFix(rec) {
    const r = rec.result || {};
    if (rec.status !== "accepted" || !r.entity_id || r.deleted || !this._data?.ai_entity) return "";
    const st = this._applyAsk[rec.id] || { text: "", busy: false, error: null };
    this._applyAsk[rec.id] = st;
    return `<div class="ask applyask">
      <input type="text" data-applyask="${esc(rec.id)}" placeholder="Not working as expected? Tell the AI what's wrong" value="${esc(st.text)}">
      <button class="primary" data-applygo="${esc(rec.id)}" data-entity="${esc(r.entity_id)}" ${st.busy ? "disabled" : ""}>${st.busy ? "The AI is looking at it…" : "Ask AI for a fix"}</button>
    </div>${st.error ? `<div class="error">${esc(st.error)}</div>` : ""}`;
  }

  _edited(s) {
    const e = (s.evidence?.edits || []).slice(-1)[0];
    if (!e) return "";
    return `<p class="small edited">${e.how === "yaml" ? "Edited by you (YAML)." : `Changed by the AI as you asked: “${esc(e.instruction)}”.`} The replay below is for this version.</p>`;
  }

  _editors(rec) {
    const s = rec.suggestion;
    if (!s.automation) return "";
    const id = esc(rec.id);
    const mode = this._editing[rec.id];
    const busy = this._busy[rec.id];
    const editable = rec.status !== "accepted";
    let out = `<div class="edit-row">
      <button class="link" data-edit="${id}" data-mode="yaml">${mode === "yaml" ? "Close YAML" : editable ? "Edit YAML" : "Show YAML"}</button>
      ${editable ? `<button class="link" data-edit="${id}" data-mode="ai">${mode === "ai" ? "Close" : "Change with AI"}</button>` : ""}
    </div>`;
    if (mode === "yaml") {
      const draft = this._drafts[rec.id] ?? rec.yaml ?? "";
      out += `<textarea class="yaml" data-draft="${id}" spellcheck="false" ${editable ? "" : "readonly"}>${esc(draft)}</textarea>`;
      if (editable) out += `<div class="actions"><button class="primary" data-save="${id}" ${busy ? "disabled" : ""}>${busy === "save" ? "Checking and replaying…" : "Save and re-check"}</button>
        <button class="ghost" data-reset="${id}">Undo my changes</button></div>`;
    }
    if (mode === "ai") {
      out += `<div class="ask"><input type="text" data-ask="${id}" placeholder="e.g. only after 10 pm · also turn on the strip · wait 15 minutes instead"
        value="${esc(this._asks[rec.id] || "")}">
        <button class="primary" data-ask-go="${id}" ${busy ? "disabled" : ""}>${busy === "ask" ? "AI is editing… (up to a minute)" : "Ask AI"}</button></div>`;
    }
    if (this._editErr[rec.id]) out += `<div class="error">${esc(this._editErr[rec.id])}</div>`;
    return out;
  }

  async _edit(id, payload, kind) {
    this._busy[id] = kind;
    this._editErr[id] = null;
    this._render(true);
    try {
      await this._hass.callWS({ type: "automation_advisor/edit", suggestion_id: id, ...payload });
      delete this._drafts[id];
      delete this._asks[id];
      this._editing[id] = null;
    } catch (e) {
      this._editErr[id] = e.message || String(e);
    }
    delete this._busy[id];
    await this._load(true);
  }

  _chip(s) {
    if (s.category !== "ai") return CATEGORY[s.category] || s.category;
    if (s.evidence?.built) return "AI: set up on request";
    const k = s.evidence?.ai_kind;
    return k === "modify" ? "AI: fix an automation" : k === "delete" || k === "disable" ? "AI: remove an automation" : "AI: new automation";
  }

  _replay(bt, label) {
    if (!bt) return "";
    if (label === "Today") {
      return `<div class="replay warn"><b>Today.</b> ${esc(bt.summary)}${bt.examples?.length ? `<br><span class="small">e.g. ${esc(bt.examples[0])}</span>` : ""}</div>`;
    }
    const cls = !bt.supported ? "warn" : bt.passes ? "ok" : bt.unwanted_pct > 25 ? "bad" : "warn";
    const badge = label ? label : !bt.supported ? "Not replayed" : bt.passes ? "Checked on your history" : "Weak on your history";
    return `<div class="replay ${cls}"><b>${badge}.</b> ${esc(bt.summary)}${
      bt.examples?.length && !bt.passes ? `<br><span class="small">e.g. ${esc(bt.examples[0])}</span>` : ""}</div>`;
  }

  _report(rep, fresh) {
    if (!rep) return "";
    if (rep.skipped) return `<div class="result">An analysis is already running; results appear here when it finishes.</div>`;
    const lead = fresh
      ? (rep.new ? `<b>${rep.new} new suggestion${rep.new === 1 ? "" : "s"}</b> below.` : "<b>No new suggestions.</b>")
      : "Last analysis:";
    const health = (rep.automations || []).map((h) =>
      `<li>${esc(h.alias)}: ran ${h.runs}×, kept ${h.kept_pct}%${h.undone ? ` (undone ${h.undone}×)` : ""}</li>`).join("");
    const near = (rep.near_misses || []).map((n) => `<li>${esc(n)}</li>`).join("");
    const ai = rep.ai;
    const aiLine = !ai ? "" : ai.error
      ? `<p class="small bad">AI step failed (${esc(ai.entity)}): ${esc(ai.error)}. Statistical suggestions are unaffected.</p>`
      : `<p class="small">AI (${esc(ai.entity)}) got ${rep.leads ?? 0} lead(s) from the statistics, proposed ${ai.proposed} item(s)${ai.revised ? `, revised ${ai.revised}` : ""}; ${ai.accepted.length} held up on your history.</p>`;
    const aiRejected = (ai?.rejected || []).map((r) => `<li>${esc(r.title)}: ${esc(r.reason)}</li>`).join("");
    const open = this._open.__report;
    return `
      <div class="result">
        ${fresh ? `<button class="close" id="close-result" title="Close">✕</button>` : ""}
        <p>${lead} ${esc(rep.headline || "")}</p>
        <p class="small">Looked at ${rep.events ?? 0} events over ${rep.days ?? 0} day(s): ${rep.targets ?? 0} lights/switches, ${rep.sensors ?? 0} sensors,
          ${rep.manual_actions ?? 0} changes by hand, ${rep.automated_actions ?? 0} by automations.${fresh && rep.pending != null ? ` ${rep.pending} suggestion(s) waiting for you.` : ""}</p>
        ${aiLine}
        ${health || near || aiRejected ? `<button class="link" id="toggle-report">${open ? "Hide" : "Show"} details</button>` : ""}
        ${open && health ? `<p class="small"><b>Your automations</b></p><ul class="small">${health}</ul>` : ""}
        ${open && near ? `<p class="small"><b>Closest to a suggestion</b></p><ul class="small">${near}</ul>` : ""}
        ${open && aiRejected ? `<p class="small"><b>AI ideas that were not shown</b></p><ul class="small">${aiRejected}</ul>` : ""}
      </div>`;
  }

  async _clear() {
    if (!confirm("Remove all suggestions, including dismissed and applied ones? Automations you already created stay.")) return;
    try {
      await this._hass.callWS({ type: "automation_advisor/clear" });
      this._result = null;
      this._error = null;
    } catch (e) {
      this._error = e.message || String(e);
    }
    await this._load();
  }

  _items(status) {
    return (this._data?.suggestions || []).filter((s) => s.status === status);
  }

  _card(rec) {
    const s = rec.suggestion;
    const busy = this._busy[rec.id];
    const pct = Math.round((s.confidence || 0) * 100);
    let buttons = "";
    if (rec.status === "pending" || rec.status === "dismissed") {
      const ro = this._data?.source && !this._data.source.can_write;
      const label = (o) => (ro ? READONLY_LABEL[o] : ACTION_LABEL[o]) || o;
      buttons = s.options.map((o, i) =>
        `<button class="${i === 0 ? "primary" : ""} ${o === "delete" ? "danger" : ""}" data-id="${esc(rec.id)}" data-action="${o}" ${busy ? "disabled" : ""}>${busy === o ? "Working…" : label(o)}</button>`
      ).join("");
      buttons += rec.status === "pending"
        ? `<button class="ghost" data-id="${esc(rec.id)}" data-action="dismiss" ${busy ? "disabled" : ""}>Dismiss</button>`
        : `<button class="ghost" data-id="${esc(rec.id)}" data-action="restore" ${busy ? "disabled" : ""}>Move back to suggestions</button>`;
    }
    let meta = "";
    if (rec.status === "dismissed") meta = `Dismissed ${ago(rec.dismissed_at)} · removed ${ago(rec.expires)}`;
    if (rec.status === "accepted") {
      const r = rec.result || {};
      const what = r.deleted ? "deleted" : r.modified ? "changed" : r.disabled ? "turned off" : "created";
      const hist = rec.history || [];
      const fixes = hist.length ? ` · changed ${hist.length}× since it was created${hist[hist.length - 1].asked ? ` (last after you asked: “${esc(hist[hist.length - 1].asked)}”)` : ""}` : "";
      meta = r.manual
        ? `Handed to you ${ago(rec.decided_at)}`
        : `${what[0].toUpperCase() + what.slice(1)} ${ago(rec.decided_at)}` +
          (r.entity_id && !r.deleted ? ` · <a href="/config/automation/dashboard">${esc(r.entity_id)}</a>` : "") + fixes;
    }

    return `
      <div class="card ${this._highlight === rec.id ? "highlight" : ""}">
        <div class="head">
          <span class="chip ${s.kind}">${esc(this._chip(s))}</span>
          <span class="conf" title="Confidence"><span style="width:${pct}%"></span></span>
          <span class="pct">${pct}%</span>
        </div>
        <h3>${esc(s.title)}</h3>
        ${s.evidence?.does ? `<p class="does"><b>What it does:</b> ${esc(s.evidence.does)}</p>` : ""}
        ${s.description ? `<p><b>Why:</b> ${esc(s.description)}</p>` : ""}
        ${s.evidence?.exactly ? `<p class="small exactly"><b>Exactly:</b> ${esc(s.evidence.exactly)}</p>` : ""}
        ${s.evidence?.asked ? `<p class="small edited">You asked: “${esc(s.evidence.asked)}”${s.evidence.device ? ` (device: ${esc(s.evidence.device)})` : ""}</p>` : ""}
        ${this._edited(s)}
        ${s.evidence?.backtest_before ? this._replay(s.evidence.backtest_before, "Today") : ""}
        ${this._replay(s.evidence?.backtest, s.evidence?.backtest_before ? "With this change" : null)}
        ${s.target_automation ? `<p class="small">Automation: <code>${esc(s.target_automation)}</code></p>` : ""}
        ${this._editors(rec)}
        ${meta ? `<p class="small">${meta}</p>` : ""}
        ${rec.result?.manual ? `<div class="manual"><p>${esc(rec.result.instructions)}</p>
          ${rec.result.yaml ? `<pre>${esc(rec.result.yaml)}</pre><button class="primary" data-copy="${esc(rec.id)}">Copy YAML</button>` : ""}</div>` : ""}
        ${buttons ? `<div class="actions">${buttons}</div>` : ""}
        ${this._applyFix(rec)}
      </div>`;
  }

  _render(force = false) {
    const active = this.shadowRoot.activeElement;
    if (!force && !this._forceNext && active && (active.tagName === "TEXTAREA" || active.tagName === "INPUT")) {
      this._deferred = true;  // do not wipe what someone is typing; redraw when they leave the field
      return;
    }
    this._forceNext = false;
    this._deferred = false;
    const d = this._data;
    const counts = { pending: this._items("pending").length, dismissed: this._items("dismissed").length, accepted: this._items("accepted").length };
    const list = this._items(this._tab);
    const empty = {
      pending: d?.last_run
        ? (d.ai_entity ? "No suggestions right now. The advisor keeps watching in the background and asks the AI again on schedule." : "No suggestions: connect an AI first.")
        : "The first analysis runs a few minutes after start-up. You can also run it now.",
      dismissed: `Dismissed suggestions stay here for ${d?.retention_days ?? 30} days.`,
      accepted: "Nothing applied yet.",
    }[this._tab];
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; background: var(--primary-background-color); min-height:100vh; color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
        .toolbar { display:flex; align-items:center; gap:8px; height:56px; padding:0 12px; background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); font-size:20px; }
        .toolbar .title { flex:1; }
        main { max-width: 860px; margin: 0 auto; padding: 16px; box-sizing: border-box; }
        .status { display:flex; flex-wrap:wrap; align-items:center; gap:12px; color: var(--secondary-text-color); font-size:14px; margin-bottom:12px; }
        .status .grow { flex:1; }
        .tabs { display:flex; gap:4px; border-bottom:1px solid var(--divider-color); margin-bottom:16px; overflow-x:auto; }
        .tabs button { background:none; border:none; border-bottom:2px solid transparent; color: var(--secondary-text-color); padding:10px 14px; font-size:14px; cursor:pointer; white-space:nowrap; }
        .tabs button.on { color: var(--primary-color); border-bottom-color: var(--primary-color); }
        .card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius, 12px); border: 1px solid var(--divider-color); padding:16px; margin-bottom:12px; }
        .head { display:flex; align-items:center; gap:8px; }
        .chip { font-size:12px; padding:2px 8px; border-radius:10px; background: var(--secondary-background-color); }
        .chip.review { background: rgba(255,152,0,.18); }
        .conf { flex: 0 0 64px; height:6px; border-radius:3px; background: var(--divider-color); overflow:hidden; margin-left:auto; }
        .conf span { display:block; height:100%; background: var(--primary-color); }
        .pct { font-size:12px; color: var(--secondary-text-color); width:34px; text-align:right; }
        h3 { margin:10px 0 6px; font-size:17px; font-weight:500; }
        p { margin:6px 0; line-height:1.45; }
        .small { font-size:13px; color: var(--secondary-text-color); }
        pre { background: var(--secondary-background-color); padding:10px; border-radius:8px; overflow-x:auto; font-size:12px; }
        .actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
        button { font: inherit; font-size:14px; border-radius:18px; padding:7px 16px; border:1px solid var(--divider-color); background:none; color: var(--primary-text-color); cursor:pointer; }
        button.primary { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
        button.danger { color: var(--error-color); border-color: var(--error-color); }
        button.ghost { border-color: transparent; color: var(--secondary-text-color); }
        button.link { border:none; padding:0; color: var(--primary-color); font-size:13px; }
        button[disabled] { opacity:.5; cursor:default; }
        .error { background: rgba(219,68,55,.12); color: var(--error-color); padding:10px 12px; border-radius:8px; margin-bottom:12px; }
        .empty { color: var(--secondary-text-color); padding: 24px 4px; }
        code { font-size:12px; }
        a { color: var(--primary-color); }
        .manual { margin-top:10px; padding:10px 12px; border-radius:8px; background: rgba(3,169,244,.08); }
        .bad { color: var(--error-color); }
        .does { font-size:15px; }
        .fixbar { margin: 0 0 10px; }
        .fixcard select, textarea.problem { width:100%; box-sizing:border-box; margin-top:8px; font: inherit; font-size:14px; padding:8px 10px;
          border-radius:8px; border:1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); }
        textarea.problem { min-height:70px; resize: vertical; }
        .card.highlight { border-color: var(--primary-color); box-shadow: 0 0 0 2px var(--primary-color) inset; }
        .applyask { margin-top:10px; }
        .exactly { font-style: italic; }
        .edited { color: var(--primary-color); }
        .edit-row { display:flex; gap:16px; margin-top:8px; }
        textarea.yaml { width:100%; box-sizing:border-box; min-height:240px; margin-top:8px; font: 12px/1.45 ui-monospace, Menlo, monospace;
          background: var(--secondary-background-color); color: var(--primary-text-color); border:1px solid var(--divider-color); border-radius:8px; padding:10px; resize: vertical; }
        .ask { display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; }
        .ask input { flex:1; min-width:220px; font: inherit; font-size:14px; padding:7px 12px; border-radius:18px; border:1px solid var(--divider-color);
          background: var(--card-background-color); color: var(--primary-text-color); }
        .live { background: var(--card-background-color); border:1px solid var(--primary-color); border-radius:12px; padding:12px 14px; margin-bottom:12px; }
        .steps { list-style:none; padding:0; margin:6px 0; font-size:14px; }
        .steps li { margin:3px 0; }
        .steps li.ok { color: var(--secondary-text-color); }
        .spin { display:inline-block; width:10px; height:10px; border:2px solid var(--primary-color); border-right-color:transparent; border-radius:50%; animation: sp 0.8s linear infinite; vertical-align:-1px; }
        @keyframes sp { to { transform: rotate(360deg); } }
        pre.stream { max-height: 220px; overflow-y:auto; white-space:pre-wrap; word-break:break-word; }
        .replay { font-size:13px; margin:8px 0 4px; padding:8px 10px; border-radius:8px; background: var(--secondary-background-color); border-left:3px solid var(--divider-color); }
        .replay.ok { border-left-color: var(--success-color, #43a047); }
        .replay.warn { border-left-color: var(--warning-color, #ffa600); }
        .replay.bad { border-left-color: var(--error-color); color: var(--primary-text-color); }
        .result { position:relative; background: rgba(3,169,244,.10); border:1px solid rgba(3,169,244,.35); border-radius:12px; padding:12px 40px 12px 14px; margin-bottom:12px; }
        .result ul { margin:4px 0 8px; padding-left:20px; }
        .result li { margin:2px 0; }
        button.close { position:absolute; top:6px; right:6px; border:none; padding:4px 8px; color: var(--secondary-text-color); }
      </style>
      <div class="toolbar"><ha-menu-button></ha-menu-button><span class="title">Automation Advisor</span></div>
      <main>
        <div class="status">
          <span>Last analysis: ${ago(d?.last_run)}${d?.last_stats ? ` · ${d.last_stats.events} events looked at` : ""}</span>
          <span>Next: ${d?.manual_only ? "when you press Analyse now" : ago(d?.next_run)}</span>
          <span class="${d?.ai_entity ? "" : "bad"}">${d?.ai_entity ? `AI: ${esc(d.ai_entity)}` : "AI: not connected"}</span>
          ${d?.test_mode ? `<span class="chip review">Test mode: existing automations ignored</span>` : ""}
          ${d?.source?.mode === "remote" ? `<span class="${d.source.connected ? "" : "bad"}">Source: ${esc(d.source.location || d.source.label)} · ${d.source.connected ? "connected" : "not connected"} · ${d.source.can_write ? "changes allowed" : "read-only"}</span>` : ""}
          <span class="grow"></span>
          <button class="ghost" id="clear" ${this._busy.__run || d?.running || !(d?.suggestions || []).length ? "disabled" : ""}>Clear all</button>
          <button class="primary" id="run" ${this._busy.__run || d?.running ? "disabled" : ""}>${this._busy.__run || d?.running ? "Analysing…" : "Analyse now"}</button>
        </div>
        <div id="live"></div>
        ${d && !d.ai_entity ? `<div class="result"><p><b>Connect an AI to get suggestions.</b> Suggestions are designed by an AI and checked against your history.
          Add an AI integration (for example Ollama, which runs locally), then pick its AI Task entity in
          <a href="/config/integrations/integration/automation_advisor">Automation Advisor → Configure</a>.</p></div>` : ""}
        ${this._error ? `<div class="error">${esc(this._error)}</div>` : ""}
        ${d?.last_error ? `<div class="error">Last analysis failed: ${esc(d.last_error)}</div>` : ""}
        ${d?.source?.error && !d.source.connected ? `<div class="error">Connection to ${esc(d.source.label)}: ${esc(d.source.error)}</div>` : ""}
        ${d && d.ai_entity ? this._buildBox() + this._fixBox() : ""}
        <div class="tabs">
          <button data-tab="pending" class="${this._tab === "pending" ? "on" : ""}">Suggestions (${counts.pending})</button>
          <button data-tab="dismissed" class="${this._tab === "dismissed" ? "on" : ""}">Dismissed (${counts.dismissed})</button>
          <button data-tab="accepted" class="${this._tab === "accepted" ? "on" : ""}">Applied (${counts.accepted})</button>
        </div>
        ${this._result && this._tab === "pending" ? this._report(this._result, true) : ""}
        ${d ? (list.length ? list.map((r) => this._card(r)).join("")
          : `<div class="empty">${empty}</div>${!this._result && this._tab === "pending" ? this._report(d.last_report, false) : ""}`)
          : `<div class="empty">Loading…</div>`}
      </main>`;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) { menu.hass = this._hass; menu.narrow = this._narrow; }
    this._renderLive();
    this.shadowRoot.getElementById("run")?.addEventListener("click", () => this._run());
    this.shadowRoot.getElementById("clear")?.addEventListener("click", () => this._clear());
    this.shadowRoot.getElementById("close-result")?.addEventListener("click", () => { this._result = null; this._render(); });
    this.shadowRoot.getElementById("toggle-report")?.addEventListener("click", () => { this._open.__report = !this._open.__report; this._render(); });
    this.shadowRoot.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => { this._tab = b.dataset.tab; this._render(); }));
    this.shadowRoot.querySelectorAll("[data-action]").forEach((b) => b.addEventListener("click", () => this._decide(b.dataset.id, b.dataset.action)));
    this.shadowRoot.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
      const rec = (this._data?.suggestions || []).find((r) => r.id === b.dataset.copy);
      try { await navigator.clipboard.writeText(rec.result.yaml); b.textContent = "Copied"; }
      catch (e) { b.textContent = "Select the text above and copy it"; }
    }));
    const root = this.shadowRoot;
    root.getElementById("build-open")?.addEventListener("click", async () => {
      this._build.open = true; this._build.error = null;
      this._render(true);
      try { this._build.devices = await this._hass.callWS({ type: "automation_advisor/devices" }); }
      catch (e) { this._build.error = e.message || String(e); this._build.devices = []; }
      this._render(true);
    });
    root.getElementById("build-close")?.addEventListener("click", () => { this._build.open = false; this._render(true); });
    root.getElementById("build-notes-close")?.addEventListener("click", () => { this._build.notes = null; this._render(true); });
    root.getElementById("build-device")?.addEventListener("change", (ev) => { this._build.device = ev.target.value; });
    const req = root.getElementById("build-request");
    req?.addEventListener("input", () => {
      this._build.request = req.value;
      const go = root.getElementById("build-go");
      if (go) go.disabled = this._build.busy || !req.value.trim();
    });
    req?.addEventListener("blur", () => { if (this._deferred) setTimeout(() => this._render(), 0); });
    root.getElementById("build-go")?.addEventListener("click", () => this._askBuild());
    root.getElementById("fix-open")?.addEventListener("click", async () => {
      this._fix.open = true; this._fix.error = null;
      this._render(true);
      try { this._fix.list = await this._hass.callWS({ type: "automation_advisor/automations" }); }
      catch (e) { this._fix.error = e.message || String(e); this._fix.list = []; }
      this._render(true);
    });
    root.getElementById("fix-close")?.addEventListener("click", () => { this._fix.open = false; this._render(true); });
    root.getElementById("fix-auto")?.addEventListener("change", (ev) => { this._fix.automation = ev.target.value; this._render(true); });
    const prob = root.getElementById("fix-problem");
    prob?.addEventListener("input", () => {
      this._fix.problem = prob.value;
      const go = root.getElementById("fix-go");
      if (go) go.disabled = this._fix.busy || !this._fix.automation || !prob.value.trim();
    });
    prob?.addEventListener("blur", () => { if (this._deferred) setTimeout(() => this._render(), 0); });
    root.getElementById("fix-go")?.addEventListener("click", () => this._askFix(this._fix.automation, this._fix.problem.trim()));
    root.querySelectorAll("[data-applyask]").forEach((t) => {
      t.addEventListener("input", () => { this._applyAsk[t.dataset.applyask].text = t.value; });
      t.addEventListener("blur", () => { if (this._deferred) setTimeout(() => this._render(), 0); });
    });
    root.querySelectorAll("[data-applygo]").forEach((b) => b.addEventListener("click", () => {
      const st = this._applyAsk[b.dataset.applygo];
      if (st?.text.trim()) this._askFix(b.dataset.entity, st.text.trim(), b.dataset.applygo);
    }));
    root.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => {
      const id = b.dataset.edit;
      this._editing[id] = this._editing[id] === b.dataset.mode ? null : b.dataset.mode;
      this._editErr[id] = null;
      this._render(true);
    }));
    root.querySelectorAll("[data-draft]").forEach((t) => {
      t.addEventListener("input", () => { this._drafts[t.dataset.draft] = t.value; });
      t.addEventListener("blur", () => { if (this._deferred) setTimeout(() => this._render(), 0); });
    });
    root.querySelectorAll("[data-ask]").forEach((t) => {
      t.addEventListener("input", () => { this._asks[t.dataset.ask] = t.value; });
      t.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && t.value.trim()) this._edit(t.dataset.ask, { instruction: t.value.trim() }, "ask"); });
      t.addEventListener("blur", () => { if (this._deferred) setTimeout(() => this._render(), 0); });
    });
    root.querySelectorAll("[data-save]").forEach((b) => b.addEventListener("click", () => {
      const id = b.dataset.save;
      const rec = (this._data?.suggestions || []).find((r) => r.id === id);
      this._edit(id, { yaml: this._drafts[id] ?? rec?.yaml ?? "" }, "save");
    }));
    root.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", () => { delete this._drafts[b.dataset.reset]; this._render(true); }));
    root.querySelectorAll("[data-ask-go]").forEach((b) => b.addEventListener("click", () => {
      const id = b.dataset.askGo;
      const text = (this._asks[id] || "").trim();
      if (text) this._edit(id, { instruction: text }, "ask");
    }));
    this.shadowRoot.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", () => { this._open[b.dataset.toggle] = !this._open[b.dataset.toggle]; this._render(); }));
  }
}

customElements.define("automation-advisor-panel", AutomationAdvisorPanel);
