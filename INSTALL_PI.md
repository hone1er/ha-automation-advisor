# Installing Automation Advisor on a Home Assistant Pi (AI on a Mac)

**Where things run:**
- **On the Pi:** the advisor, inside Home Assistant. It reads the Pi's own activity, including
  who did what.
- **On the Mac:** the AI (Ollama), and only when you want an analysis. The Pi reaches it over your
  home network.

**Requirements:** Home Assistant OS 2025.9 or newer on the Pi; Ollama with
`gemma4:12b-it-q4_K_M` on the Mac, and this repository cloned on the Mac (for its scripts):
`git clone` it anywhere; `<repo>` below means that folder.

Each step says where it happens: **[Pi]** means in Home Assistant's web page (from any browser,
e.g. http://homeassistant.local:8123); **[Mac]** means on the Mac itself.

---

## Step 1 — Make a backup [Pi]
Settings → System → Backups → **Backup now**. (Done.)

## Step 2 — Install the "Samba share" add-on [Pi]
The add-on makes the Pi's configuration folder appear as a network drive, so the Mac can copy
the integration onto it.

1. Settings → **Add-ons** → **Add-on store** (bottom right).
2. Search **Samba share**, open it, click **Install**.
3. When it is installed, open its **Configuration** tab:
   - **Username:** e.g. `homeassistant`
   - **Password:** a password of your choice (this is only for the network drive)
   - Click **Save**.
4. Go back to the **Info** tab and click **Start**.

## Step 3 — Unzip the integration [Mac]
1. Open **Finder** (the smiling-face icon in the Dock).
2. Press **⇧⌘H** (shift+command+H) to open your home folder, then open
   `ha-automation-advisor` → `dist`.
3. Double-click `automation_advisor-<version>.zip`. A folder named `automation_advisor` appears next
   to it.

## Step 4 — Connect the Mac to the Pi's drive and copy the folder [Mac]
1. Click on the desktop or a Finder window so that **Finder** is the active app. Its name shows
   next to the Apple logo at the top left of the screen.
2. In the menu bar at the very top of the screen, click **Go**, then **Connect to Server…** (or
   press **⌘K**).
3. Type `smb://homeassistant.local` and click **Connect**.
   - If that is not found, use the Pi's IP address instead, e.g. `smb://192.168.1.50`. You can
     find it in Home Assistant under Settings → System → Network.
4. When asked, choose **Registered User** and enter the username and password from Step 2.
5. A list of shares appears. Select **config** and click **OK**. A Finder window opens showing
   the Pi's configuration folder (you will see files like `configuration.yaml`).
6. If there is no folder named `custom_components`, create it: right-click an empty spot →
   **New Folder** → name it `custom_components` (exactly like that).
7. Open `custom_components` and drag the `automation_advisor` folder from Step 3 into it.

**Check:** `config` → `custom_components` → `automation_advisor` → there is a file
`manifest.json` directly inside.

You can now eject the drive (the ⏏ button next to it in Finder's sidebar). You can also stop the
Samba add-on until you need it again.

## Step 5 — Restart Home Assistant [Pi]
Settings → System → **⋮** (three dots, top right) → **Restart Home Assistant** → **Restart**.
Wait until the page comes back (about a minute).

## Step 6 — Start the AI [Mac]
Open **Terminal** and run:

```bash
<repo>/scripts/ollama_lan.sh start
```

**Check:** it prints "Ollama is running and reachable on your network" and the URL to use.

When you are done with an analysis:

```bash
<repo>/scripts/ollama_lan.sh stop
```

Nothing starts by itself at login. While it runs, anything on your Wi-Fi can use the model
(Ollama has no password), so stop it when you do not need it.

## Step 7 — Connect Home Assistant to Ollama [Pi] (with the AI started, Step 6)
1. Settings → Devices & services → **Add integration** → search **Ollama**.
2. **URL:** the one `ollama_lan.sh start` printed, e.g. `http://My-MacBook.local:11434`.
   - If it cannot connect, use the IP address it printed, e.g. `http://192.168.1.20:11434`. Then
     give the Mac a fixed address in your router (a "DHCP reservation"), so it never changes.
3. On the new Ollama entry, click **Add AI Task** and set:
   - Model `gemma4:12b-it-q4_K_M`
   - Context window `16384`
   - Keep alive `600`
   - Thinking off
   - Click **Submit**.

## Step 8 — Add Automation Advisor [Pi]
1. Settings → Devices & services → **Add integration** → **Automation Advisor** → **This Home
   Assistant**.
2. Pick the Ollama AI Task entity → **Submit**.
3. On the Automation Advisor entry, click **Configure**:
   - **Analyse every:** `0`, meaning only when you press "Analyse now". The AI is not always on,
     so nothing should run by itself.
   - Optional: **Test mode: ignore my existing automations**, to see what the AI designs from
     scratch.
   - **Submit**.
4. Open **Automation Advisor** in the left sidebar → **Analyse now**.

On the first run it reads up to 10 days of history from the Pi's recorder.

**Accept** creates and enables the automation directly in this Home Assistant. Read "What it does"
and "Exactly" first, or use **Edit YAML** / **Change with AI**.

## Every time you want new suggestions
1. **[Mac]** `<repo>/scripts/ollama_lan.sh start`
2. **[Pi]** Automation Advisor → **Analyse now**. Watch the live card; it takes a few minutes.
3. **[Mac]** `<repo>/scripts/ollama_lan.sh stop`

The activity log keeps being collected on the Pi all the time, whether or not the Mac is on.

## When something doesn't work right: ask the AI for a fix
- **On an automation the advisor created:** Applied tab → its card → "Not working as expected?
  Tell the AI what's wrong" → Ask AI for a fix.
- **On any automation, including ones you wrote yourself:** at the top of the panel → "Ask the AI
  about an automation that doesn't work right" → pick it, describe the problem → Ask AI for a fix.

The AI looks at the automation, its last runs (Home Assistant's traces) and what your devices
did, and returns a fix card with the replay **Today** vs **With this change**. Nothing changes
until you click **Apply change**. If the AI finds nothing to change, the panel says so; then
describe *when* it happens.

## Setting something up with the AI (e.g. a new remote)
At the top of the panel → **Set something up with AI**:
1. If it is about one device, pick it. The newest devices are at the top, so a remote you just
   paired is first. The AI then sees exactly which button presses the device reports.
2. Say what you want, e.g. *"Use my new IKEA 4-button remote for the bedroom: up = lights on,
   down = off, left/right = dimmer/brighter."*
3. **Set it up**. The result is a suggestion card ("AI: set up on request") with What it does and
   Exactly. Use **Edit YAML** or **Change with AI** if needed, then **Create & enable**.

Button presses have no history yet, so those cards say "not replayed": press the buttons after
accepting to try it. If something does not work, ask for a fix on its card in **Applied**. A fix
updates the same automation and the same card; it does not add a new one.

## Updating to a new version
1. **[Mac]** Download the new zip from the repository's **Releases** page and unzip it.
2. **[Mac]** Connect to `smb://homeassistant.local` (Step 4), open
   `config/custom_components`, delete the old `automation_advisor` folder and drag in the new one.
3. **[Pi]** Restart Home Assistant (Step 5). Your settings, suggestions and history are kept.

## Tidy up (once)
- **Stop the advisor running on the Mac:** Ctrl-C in its Terminal window. It is not needed any
  more.
- **Delete its token:** [Pi] your profile (bottom left) → Security → Long-lived access tokens →
  delete the token you made for the Mac.

## Troubleshooting
- **"Automation Advisor" is not in the integration list.** Check Step 4's check, then restart
  again. Settings → System → Logs, search "automation_advisor".
- **Ollama cannot connect.** Run `ollama_lan.sh status` on the Mac, make sure the Mac is awake and
  on the same Wi-Fi, and try the IP address URL.
- **"The AI step failed".** Ollama was not running. Start it (Step 6) and press Analyse now again.
- **Undo everything.** Delete the integration (Settings → Devices & services), delete the
  `custom_components/automation_advisor` folder, restart. Or restore the backup.
