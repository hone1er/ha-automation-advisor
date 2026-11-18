"""Differences between the Home Assistant versions this integration supports (2025.9+).

Home Assistant replaced voluptuous with probatio (a fork with the same API) during 2026; older
installs only have voluptuous.
"""

try:
    import probatio as vol  # noqa: F401
except ImportError:  # Home Assistant before the switch
    import voluptuous as vol  # noqa: F401
