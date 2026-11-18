# Security

## Reporting a problem

Please do **not** open a public issue for security problems. Use GitHub's private reporting
instead: **Security → Report a vulnerability** on this repository. You will get an answer as soon
as possible.

## How the integration protects your home

- **Nothing changes without you.** Every suggestion, fix or set-up from the AI is a card that an
  admin reviews, and can edit, before clicking *Create & enable* or *Apply change*. The panel and
  all of its API calls are admin-only.
- **AI output is checked before you see it.** It must:
  - use only entities, devices and areas that exist;
  - pass Home Assistant's own automation validator;
  - for suggestions from your habits, hold up when replayed on your recorded history.
- **Forbidden actions.** An AI suggestion (or an edit made in the panel) can never contain these,
  including inside nested `choose` blocks:
  - unlocking or opening locks, disarming alarms, opening garage doors or gates;
  - shell, Python or command-line services;
  - add-on or host control;
  - restarting, stopping or reloading Home Assistant, or changing its location;
  - backups, purging the recorder, logger changes;
  - turning off or reloading automations;
  - `rest_command`, `input_text.set_value`, and templated actions.

  Do those in Home Assistant's own editor.
- **Prompt injection.** Device and entity names are part of what the AI reads, so a malicious
  device name could try to steer it. The checks above, and your review, are the safeguard; the
  panel escapes all text it shows.

## Data and network

- **Without an AI, nothing leaves Home Assistant.** With one, the AI receives a digest of your
  home: device names and rooms, your automations, and timelines of a few typical days. Use a local
  model (Ollama) to keep this on your network; a cloud provider receives it otherwise. See
  *Privacy* in the README.
- **Ollama has no authentication.** `scripts/ollama_lan.sh start` lets any device on your local
  network use the model until you run `stop`. Do not expose port 11434 to the internet.
- **The optional "another Home Assistant" mode** stores a long-lived access token in that
  config entry (Home Assistant's normal storage). Use a dedicated token and revoke it when you are
  done. That mode is read-only unless you enable changes in its options.
- **The activity log** is a SQLite file in your Home Assistant's `.storage` folder. It holds only
  state changes of the device types the integration needs, for the look-back period.
