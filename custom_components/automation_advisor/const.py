"""Constants for Automation Advisor."""

from __future__ import annotations

DOMAIN = "automation_advisor"
TITLE = "Automation Advisor"

PANEL_URL = "automation-advisor"
PANEL_COMPONENT = "automation-advisor-panel"
PANEL_JS_URL = "/automation_advisor_static/automation-advisor-panel.js"

STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.suggestions"
ARCHIVE_FILE = f".storage/{DOMAIN}.db"

SIGNAL_UPDATED = f"{DOMAIN}_updated"
SIGNAL_LIVE = f"{DOMAIN}_live"
NOTIFICATION_ID = f"{DOMAIN}_new"

# Entry data (where the data comes from)
CONF_MODE = "mode"
MODE_LOCAL = "local"
MODE_REMOTE = "remote"
CONF_URL = "url"
CONF_TOKEN = "token"
CONF_VERIFY_SSL = "verify_ssl"

# Options
CONF_ALLOW_REMOTE_WRITE = "allow_remote_write"
CONF_IGNORE_EXISTING = "test_ignore_existing"
# Not in the UI: also show the statistics engine's own suggestions (tests, debugging).
CONF_SHOW_STATISTICAL = "show_statistical"
CONF_INTERVAL_HOURS = "interval_hours"
CONF_LOOKBACK_DAYS = "lookback_days"
CONF_MIN_OCCURRENCES = "min_occurrences"
CONF_MIN_CONFIDENCE = "min_confidence"
CONF_OVERRIDE_WINDOW = "override_window_s"
CONF_RETENTION_DAYS = "dismissed_retention_days"
CONF_AI_TASK_ENTITY = "ai_task_entity"
CONF_AI_EXTRA = "ai_extra_ideas"
CONF_EXCLUDED = "excluded_entities"
CONF_NOTIFY = "notify"

DEFAULTS = {
    CONF_INTERVAL_HOURS: 24,
    CONF_LOOKBACK_DAYS: 14,
    CONF_MIN_OCCURRENCES: 5,
    CONF_MIN_CONFIDENCE: 0.6,
    CONF_OVERRIDE_WINDOW: 90,
    CONF_RETENTION_DAYS: 30,
    CONF_AI_TASK_ENTITY: None,
    CONF_AI_EXTRA: True,
    CONF_EXCLUDED: [],
    CONF_NOTIFY: True,
    CONF_ALLOW_REMOTE_WRITE: False,
    CONF_IGNORE_EXISTING: False,
    CONF_SHOW_STATISTICAL: False,
}

# Suggestion lifecycle
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_DISMISSED = "dismissed"

ACTION_ACCEPT = "accept"
ACTION_MODIFY = "modify"
ACTION_DELETE = "delete"
ACTION_DISABLE = "disable"
ACTION_DISMISS = "dismiss"
ACTION_RESTORE = "restore"
APPLY_ACTIONS = (ACTION_ACCEPT, ACTION_MODIFY, ACTION_DELETE, ACTION_DISABLE)

# How long a pending suggestion survives after its pattern was last seen.
PENDING_GRACE_DAYS = 3
# Delay of the first analysis after start, so start-up noise is not analysed and
# Home Assistant finishes loading first.
FIRST_RUN_DELAY_S = 300
