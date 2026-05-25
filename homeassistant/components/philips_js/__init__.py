"""The Philips TV integration."""

from __future__ import annotations

import logging

import haphilipsjs
from haphilipsjs import PhilipsTV
from haphilipsjs.typing import SystemType

from homeassistant.const import (
    CONF_API_VERSION,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant

from .const import CONF_SYSTEM
from .coordinator import PhilipsTVConfigEntry, PhilipsTVDataUpdateCoordinator

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.LIGHT,
    Platform.MEDIA_PLAYER,
    Platform.REMOTE,
    Platform.SWITCH,
]

LOGGER = logging.getLogger(__name__)


# --- haphilipsjs library monkey-patches (API 6.x firmware workaround) ---
#
# Several endpoints in the library's per-cycle update() are wasteful on
# Philips API 6.x consumer Linux/Titan OS firmware (verified TPN248E /
# 55OLED759/12). Two failure modes:
#
#   1. DEAD: endpoint always returns 403/404 regardless of what the TV's
#      featuring manifest claims. The library uses json_feature_supported()
#      to guard these calls, but the TV's feature flags are aspirational
#      not authoritative — it claims to support recordings, hue, etc. but
#      403/404s on the actual calls.
#
#   2. LYING: endpoint returns 200 OK with stale or fixed values regardless
#      of actual TV state (e.g. screenstate always "Off", ambilight/power
#      echoes the last write). Reading them costs a request but gives no
#      useful information.
#
# Both are pure waste — they hit the TV's HTTP server with no useful result
# and contribute to the cumulative load that wedges the server over hours.
#
# We short-circuit them at the getReq() layer ONLY when the connected TV
# matches the affected firmware family. Older / non-Linux Philips TVs (where
# these endpoints may genuinely work) fall through to the real network call,
# so this override doesn't break behavior on TVs we haven't characterized.
#
# See home-assistant/philips-integration broken-paths drawer + upstream-pr-plan
# (Proposal 1). When upstream lands the equivalent fix in ha-philipsjs, this
# monkey-patch becomes a no-op and the whole block can be removed alongside
# the rest of the override.

# Endpoints that ALWAYS 403/404 on the affected firmware. Library code
# downstream already handles None gracefully (it's the current behavior
# for these — they just go via a wasted network round-trip first).
_DEAD_ENDPOINTS: set[str] = {
    "sources/current",   # 403 — getSourceId(), per-cycle
    "HueLamp/power",     # 403 — getHueLampPower(), per-cycle (TV lies in jsonfeatures.ambilight.Hue)
    "recordings/list",   # 404 — getRecordings(), per-cycle (TV lies in jsonfeatures.recordings.List)
    "applications",      # 403 — getApplications(), initial connect only
}

# Endpoints that respond 200 OK but with stale/fixed values on the affected
# firmware. Skipping turns the corresponding state attribute to None — a
# small behavior change vs the "lying value reported as truth" current
# state. We only include endpoints where None is harmless or already-better.
#
# NOTE: powerstate is INTENTIONALLY excluded. Skipping it would change
# media_player.async_turn_on's path (currently uses setPowerState when
# powerstate is truthy; with None it'd run the configured _turn_on script
# instead — probably better since setPowerState is unreliable on this
# firmware, but it IS a behavior change that warrants separate testing).
_LYING_ENDPOINTS: set[str] = {
    "screenstate",       # always "Off" — screen_state switch is broken on this firmware regardless
    "ambilight/power",   # echoes last setAmbilightPower POST — nothing in our override reads this
}


def _is_affected_firmware(tv) -> bool:
    """True for Philips API 6.x consumer Linux/Titan OS firmware (TPN248E
    family and similar). Returns False (safe default — no skipping, real
    network calls happen) for older firmwares or anything we haven't
    characterized.

    Matches the predicate used by the existing haphilipsjs
    `quirk_ambilight_mode_ignored` for the same firmware family.
    """
    try:
        return tv.api_version >= 6 and tv.os_type == "Linux"
    except Exception:  # be ultra-defensive — never let detection error skip a call
        return False


def _apply_haphilipsjs_patches() -> None:
    """Idempotent. Skip wasteful endpoints when connected to the affected
    firmware family. Older / non-Linux Philips TVs get unchanged behavior."""
    current_get_req = haphilipsjs.PhilipsTV.getReq
    if getattr(current_get_req, "_dead_endpoint_skip", False):
        return  # already patched in this Python process

    # If a previous load wrapped getReq, recover the real one to avoid chain
    real_get_req = getattr(current_get_req, "_real_getReq", current_get_req)

    async def patched_getReq(self, path, protocol=None):
        if _is_affected_firmware(self) and (
            path in _DEAD_ENDPOINTS or path in _LYING_ENDPOINTS
        ):
            return None
        return await real_get_req(self, path, protocol)

    patched_getReq._dead_endpoint_skip = True
    patched_getReq._real_getReq = real_get_req
    haphilipsjs.PhilipsTV.getReq = patched_getReq
    LOGGER.info(
        "philips_js override: monkey-patched getReq — will skip %d dead + %d lying "
        "endpoints when connected to affected firmware (api_version>=6, os_type=Linux). "
        "Dead: %s. Lying: %s.",
        len(_DEAD_ENDPOINTS),
        len(_LYING_ENDPOINTS),
        ", ".join(sorted(_DEAD_ENDPOINTS)),
        ", ".join(sorted(_LYING_ENDPOINTS)),
    )


async def async_setup_entry(hass: HomeAssistant, entry: PhilipsTVConfigEntry) -> bool:
    """Set up Philips TV from a config entry."""

    _apply_haphilipsjs_patches()

    system: SystemType | None = entry.data.get(CONF_SYSTEM)
    tvapi = PhilipsTV(
        entry.data[CONF_HOST],
        entry.data[CONF_API_VERSION],
        username=entry.data.get(CONF_USERNAME),
        password=entry.data.get(CONF_PASSWORD),
        system=system,
    )
    coordinator = PhilipsTVDataUpdateCoordinator(hass, entry, tvapi)

    await coordinator.async_refresh()

    if (actual_system := tvapi.system) and actual_system != system:
        data = {**entry.data, CONF_SYSTEM: actual_system}
        hass.config_entries.async_update_entry(entry, data=data)

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_update_entry))

    return True


async def async_update_entry(hass: HomeAssistant, entry: PhilipsTVConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PhilipsTVConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
