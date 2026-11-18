# Automation Advisor for Home Assistant

An AI-native custom integration for [Home Assistant](https://www.home-assistant.io/). An AI
studies how your home is actually used, proposes new automations and fixes for the ones you
have, and sets up new devices when you ask. Every proposal is checked against your real devices
and replayed on your own recorded history before you see it. You review, edit, accept or
dismiss. It runs fully local with [Ollama](https://ollama.com), or with any AI provider that Home
Assistant supports.

## What it does

- **Suggests new automations from your habits.** Example: you switch the kitchen light on right
  after the kitchen motion sensor fires, but only after dark. The proposal is a motion-triggered
  automation limited to that window.
- **Fixes automations that misbehave.** Examples: lights that stay on for hours because the
  off-automation only watches one of the room's sensors; an automation you switch off again
  within seconds in the daytime. The card shows the replay *today* and *with the fix*.
- **Fixes on request.** On any automation: *"it turns the light on during the day"*. The AI reads
  the automation, its last runs (traces) and what your devices did, and proposes a fix.
- **Sets things up on request.** Example: *"Use my new bedroom 4-way remote: up = on, down = off,
  left = brighter, right = dimmer."* Pick the device, and the AI maps its real button events.
- **Explains every suggestion clearly.** **What it does** in plain words, **Why**, and
  **Exactly**, which is generated from the YAML itself so it is always accurate. Each card also
  shows the replay numbers.
- **Lets you change anything before accepting**, with **Edit YAML** or **Change with AI**
  (*"only after 10 pm"*). Changes are re-checked and replayed.
- **Accept** creates or updates the automation and checks it is running. **Dismiss** hides a
  suggestion; it stays restorable for 30 days. **Clear all** starts over.
- **Shows its work live** while it runs: each step, and the model's answer as it is written.


## Requirements

- **Home Assistant 2025.9 or newer**, any installation type. Tested on 2025.9.4 and 2026.10.4.
- **An AI for Home Assistant's "AI Task" feature.** Recommended: Ollama on a computer in your home
  network (below). Cloud providers (OpenAI, Anthropic, Google, …) also work, but they receive a
  summary of your home's activity (see [Privacy](#privacy)).
- **An admin account.** The panel is admin-only.

## Install

### 1. Get the integration into Home Assistant

**HACS** (only if this repository is public): HACS → ⋮ → Custom repositories → add this
repository's URL as type **Integration** → install **Automation Advisor** → restart Home
Assistant.

**Manual:**
1. Download `automation_advisor-<version>.zip` from the
   [latest release](../../releases/latest) and unzip it. This gives a folder `automation_advisor`.
2. Copy that folder into your Home Assistant configuration folder under `custom_components`, so
   that `config/custom_components/automation_advisor/manifest.json` exists.
   - **Home Assistant OS** (e.g. on a Raspberry Pi): use the **Samba share** add-on and connect
     from your computer. Step-by-step instructions: **[INSTALL_PI.md](INSTALL_PI.md)**.
   - **Container / Core:** copy it into the mounted `config` directory.
3. Restart Home Assistant (Settings → System → ⋮ → Restart).

### 2. Set up the AI (Ollama, local and free)

On a computer with enough memory (Apple Silicon Mac with 16 GB+, or a PC with a recent GPU):

```bash
brew install ollama                       # macOS; see ollama.com for Linux/Windows
ollama pull gemma4:12b-it-q4_K_M          # about 8 GB
```

Home Assistant must be able to reach Ollama over the network. On a Mac, this repository's
script starts Ollama listening on your network, only while you want it (nothing starts at
login):

```bash
scripts/ollama_lan.sh start               # prints the URL to use in Home Assistant
scripts/ollama_lan.sh stop                # when you are done
```

Elsewhere, start Ollama with `OLLAMA_HOST=0.0.0.0:11434 ollama serve`.

| Model | RAM while loaded | Result in our comparison on a real home |
|---|---|---|
| `gemma4:12b-it-q4_K_M` (recommended) | ≈ 9–10 GB | best: 3–5 ideas per analysis held up on the replay |
| `qwen3.5:9b-q4_K_M` (for 16 GB machines) | ≈ 7 GB | fewer usable ideas |

### 3. Connect Home Assistant to the AI

1. Settings → Devices & services → **Add integration** → **Ollama**. URL:
   `http://<computer-name>.local:11434` or `http://<its-ip>:11434`.
2. On the Ollama entry, **Add AI Task**:
   - Model: `gemma4:12b-it-q4_K_M`
   - Context window: `16384`
   - Keep alive: `600` (unloads the model after 10 idle minutes)
   - Thinking: off (it made results worse and slower in our tests)

### 4. Add Automation Advisor

1. Settings → Devices & services → **Add integration** → **Automation Advisor** → **This Home
   Assistant** → pick the AI Task entity.
2. If the AI is not always on (e.g. Ollama on a laptop), open the integration's **Configure** and
   set **Analyse every** to `0`, so it only runs when you press *Analyse now*.
3. Open **Automation Advisor** in the sidebar → **Analyse now**.

On its first run it reads up to 10 days of history from Home Assistant's recorder, so you do not
start empty.

## Using it

| You want to … | Do this in the panel |
|---|---|
| Get suggestions from your habits | **Analyse now**. A few minutes; the live card shows progress. |
| Understand a suggestion | Read **What it does**, **Why** and **Exactly**, plus the replay box. |
| Change it before accepting | **Edit YAML**, or **Change with AI** (*"wait 15 minutes instead"*). |
| Use it | **Create & enable** (new) or **Apply change** (fix). |
| Fix an automation that misbehaves | **Ask the AI about an automation that doesn't work right**, or **Not working as expected?** on its card in **Applied**. |
| Set up a new device or idea | **Set something up with AI**; pick the device if it is about one. |
| Start over | **Clear all** removes all suggestions; your automations stay. |

Applying a fix to an automation that already has a card in **Applied** updates that card and
that automation; it never adds a duplicate.

## How it works

1. **Event log with causes.** HA's history API drops the *context* of a state change, and that is
   the only way to tell "wall switch / app" from "an automation did it". So the integration logs
   lights, switches, fans, covers, locks, media players, climate, scenes, scripts, people,
   motion/door/window/occupancy (and similar) sensors, and light-level (lux) crossings, each with
   a cause:
   - **automation**: the change carries the context of an `automation_triggered` event, or of a
     script that automation started.
   - **user**: the context carries a user id (dashboard, app, voice).
   - **manual**: neither, so it came from the device itself (wall switch, remote).

   The log lives in `.storage/automation_advisor.db`. On first start it is backfilled from the
   recorder database (~10 days by default).
2. **Statistics as the AI's analyst** (`analysis.py`). Every 24 h by default it mines the last 14
   days: sensor → action habits, auto-off, daily routines, automations you keep undoing, lights
   left on, broken automations. Each finding is replayed (step 4). The findings become **leads for
   the AI**; they are not shown to you directly.
3. **The AI decides** (`llm.py`, via Home Assistant's AI Task; Ollama is streamed directly).
   - **What it gets:** a compact digest (`digest.py`): your devices with rooms, your automations,
     three typical days as timelines saying who switched what, and the leads with their replay
     results.
   - **What it returns:** its own list: new automations, including multi-condition and
     whole-house ones, and fixes, deletions or turn-offs of existing automations.
4. **Nothing is shown unless it holds up** (`backtest.py` replays automations over the history).
   Every AI item must:
   - use only real entities, with no unsafe actions (unlocking, opening garage doors, disarming
     alarms);
   - pass Home Assistant's validator;
   - hold up on the replay:
     - **new automation:** at least 3 actions, ≥ 60 % right, ≤ 25 % unwanted;
     - **fix:** better than today's automation;
     - **delete or turn off:** the automation is broken, or misbehaves in the replay.

   Items that fail go back to the AI once, with the replay result, to be fixed or dropped. What
   still fails is listed in the run report with the reason.
5. **Review** in the **Automation Advisor** sidebar panel (admins only).
   - **During a run:** a live card shows each step and the model's answer as it streams in.
   - **Afterwards:** the run report says what was looked at, what the AI proposed and what was
     dropped.
   - **When something is new:** a persistent notification and
     `sensor.automation_advisor_pending_suggestions` tell you.

Without an AI connected there are no suggestions; the activity log keeps being collected.

## Settings (Configure)

| Option | Default | Notes |
|---|---|---|
| Analyse every | 24 h | `0` = only when you press Analyse now |
| Look back over | 14 days | Longer catches weekly habits, reacts more slowly to change |
| Minimum times a pattern must happen | 5 | Also requires ≥ 3 different days |
| Minimum confidence | 0.6 | Share of your manual actions a pattern must explain |
| Undo window | 90 s | Reversing an automation within this counts as undoing it |
| Keep dismissed suggestions for | 30 days | Restorable, and not re-suggested, meanwhile |
| AI Task entity | – | Required for suggestions |
| Test mode: ignore my existing automations | off | The AI designs as if you had no automations (for trying it out) |
| Ignore these entities | – | Never logged or suggested |
| Notify me | on | Persistent notification when there are new suggestions |
| Allow changes on the connected Home Assistant | off | Only for the optional "another Home Assistant" mode |

Services: `automation_advisor.run_analysis` and `automation_advisor.decide`
(`suggestion_id`, `action`: accept / modify / delete / disable / dismiss / restore).

## Privacy

Without an AI Task entity everything stays local. With one, the model receives the digest:
- entity ids, names and rooms;
- your automations;
- the statistical findings;
- room-level timelines of three typical days.

That shows when people are home and which rooms they use. With **Ollama** this never leaves your
Mac; with a cloud provider it goes to that provider.

## Limits and good to know

- **Only automations made in the UI can be edited or deleted.** These are the ones in
  `automations.yaml`, including everything this integration creates. Automations in YAML packages
  can only be turned off; a fix for one is offered as a new automation.
- **Some changes cannot be replayed.** Button presses and other device events have no history, so
  set-ups of remotes show "not replayed". Try them after accepting.
- **Small local models are inconsistent.** That is why everything is checked and replayed, why
  failed ideas get a revision round, and why button set-ups use a numbered mapping instead of
  free-form YAML. Still read **Exactly** before accepting.
- **The advisor cannot always tell who switched something.** For 5 s after a service call, Home
  Assistant labels the device's own state reports with that call's context, so a wall-switch undo
  within 5 s of an automation looks like the automation reversing itself. The advisor counts it
  as your undo.
- **"Manual" means "not from Home Assistant".** An integration that changes a device on its own
  (e.g. a hub's schedule) looks like a manual action.
- **Dismissed ideas can come back.** One can be suggested again after 30 days if the habit is
  still there.

## Optional: try it without installing it in your Home Assistant

You can run the advisor in a separate, private Home Assistant on your Mac that **connects to your
real Home Assistant read-only**. It reads activity live over the network, plus the last ~10 days
from your logbook, and sees your devices, areas and automations. It changes nothing there:
"accept" gives you the YAML to paste yourself.

1. On the Mac, start the private instance (the first run installs Python packages):
   ```bash
   <repo>/scripts/mac_advisor.sh   # <repo> = where you cloned this repository
   ```
2. Open http://localhost:8124 and create an account. This one is only for the Mac instance.
3. On your **real** Home Assistant, open your profile (bottom left), go to Security → Long-lived
   access tokens → Create token, and copy it. Use an administrator account so areas and
   automation configs can be read.
4. On the Mac instance, go to Settings → Devices & services → Add integration → Automation
   Advisor → **Another Home Assistant on my network**. Enter its address (for example
   `http://homeassistant.local:8123`) and paste the token.
5. Open **Automation Advisor** in the Mac instance's sidebar. Use **Analyse now**, or wait for the
   daily run.

Good to know:
- **Live collection needs the Mac awake.** After sleep or a restart, the gap is re-read from your
  logbook, which keeps about 10 days by default.
- **Turning on changes.** In the integration's options, **Allow changes on the connected Home
  Assistant** makes accept, modify, delete and turn off act directly on your real system. Make
  a backup there first (Settings → System → Backups → Backup now).
- **AI wording needs a model on the Mac instance.** Add an AI integration there (Anthropic,
  OpenAI, Ollama ...) and pick its AI Task entity in the options.
- **The token** is stored only in the Mac instance's `mac_advisor/.storage` folder (git-ignored).
  You can revoke it on your real Home Assistant at any time.

## Security and license

See [SECURITY.md](SECURITY.md) for how suggestions are kept safe and how to report a problem
privately. MIT licensed, see [LICENSE](LICENSE).

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv pytest-homeassistant-custom-component home-assistant-frontend ollama
.venv/bin/python -m pytest tests -q

# the oldest supported Home Assistant (2025.9.4)
uv venv --python 3.13 .venv-2025.9
uv pip install --python .venv-2025.9 "pytest-homeassistant-custom-component==0.13.281" \
    "home-assistant-frontend==20250903.5" ollama "pycares<5"
.venv-2025.9/bin/python -m pytest tests -q
```

CI runs both on every push. Layout:

| File | What |
|---|---|
| `analysis.py` | statistics (leads for the AI) |
| `backtest.py` | replay of automations over the history |
| `llm.py` | prompts, Ollama streaming, checks, revision rounds, fixes, set-ups |
| `digest.py` | the home summary the AI reads |
| `describe.py` | plain-English "Exactly" text |
| `manager.py` | runs, the suggestion store, applying changes |
| `sources.py` | this Home Assistant, or another one over the network |
| `frontend/` | the sidebar panel (plain web component, no build step) |

`scripts/mac_advisor.sh` runs a private Home Assistant on a Mac (http://localhost:8124) for
trying the integration against another Home Assistant read-only.

Build a release zip:

```bash
cd custom_components && zip -r ../automation_advisor-$(python3 -c "import json;print(json.load(open('automation_advisor/manifest.json'))['version'])").zip automation_advisor -x "*/__pycache__/*"
```
