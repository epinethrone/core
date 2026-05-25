"""Component to integrate ambilight for TVs exposing the Joint Space API."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, cast

from haphilipsjs import PhilipsTV
from haphilipsjs.typing import AmbilightCurrentConfiguration

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.color import color_hsv_to_RGB, color_RGB_to_hsv

from .coordinator import PhilipsTVConfigEntry, PhilipsTVDataUpdateCoordinator
from .entity import PhilipsJsEntity

LOGGER = logging.getLogger(__name__)

# Ambilight brightness via menuitems API (verified 2026-05-25 by probing
# the 8 ambilight nodeids the official Android app queries). Node 710 is
# MLM_PHM_KEY_PC_ID_BRIGH_L12 with a single `value` field, range 0-9
# (current/default = 5). This is the only way to scale ambilight brightness
# without leaving the current style/preset — the cached-pixel path bakes
# brightness into the RGB values but only works while in expert mode, and
# the colorSettings.color["brightness"] read-back is stale on API 6.x. The
# Android app uses this node + posts the same way for its slider.
_AMBILIGHT_BRIGHTNESS_NODEID = 710
_AMBILIGHT_BRIGHTNESS_TV_MAX = 9

# Persistent style selection via menuitems (verified 2026-05-25). Node 300
# (MLM_PHM_KEY_MAIN_AMBILIGHT_STYLE) holds an `activenode_id` field which
# points to one of the child style nodes (320/330/340/310 = OFF). When
# activenode_id == 0 the TV is in "API-driven" / Follow App mode — which is
# what setAmbilightCurrentConfiguration leaves it in, and what causes
# rwjack's "TV reverts to my original Fixed Color on power cycle even
# though menu shows Follow App" report on #156776 (the TV remembers a
# separate persistent default that the API doesn't update).
# Setting activenode_id to a real parent node makes the TV's persistent
# state match what HA selected, so it survives power cycles.
_AMBILIGHT_STYLE_PARENT_NODE = 300
_STYLE_TO_PARENT_NODE: dict[str, int] = {
    "FOLLOW_VIDEO": 320,
    "FOLLOW_AUDIO": 330,
    "FOLLOW_COLOR": 340,
    # "OFF": 310 — not parallel-written from turn_off (our turn_off uses
    #   cached zeros, not styleName=OFF; persisting OFF would change the
    #   user's preferred "on" style on power cycle, which they probably
    #   don't want).
    # "FOLLOW_APP": 360 — that's the "API-driven" mode; we WANT to leave
    #   it as the API-driven mode by NOT writing to node 300 for these.
    # "MANUAL": no node mapping documented.
}

EFFECT_PARTITION = ": "
EFFECT_MODE = "Mode"
EFFECT_EXPERT = "Expert"
EFFECT_AUTO = "Auto"
EFFECT_EXPERT_STYLES = {"FOLLOW_AUDIO", "FOLLOW_COLOR", "Lounge light"}

# The /ambilight/supportedstyles endpoint on API 6.x firmware (verified on
# 55OLED759/12) lies about FOLLOW_VIDEO — it returns the style with no
# menuSettings field, so the integration's effect list ends up empty for
# the "track image" modes the user actually has on their TV remote.
# These fallback lists are injected when the TV doesn't enumerate them.
# Hardcoded names match what the official Philips app captures show, plus
# the existing haphilipsjs MSAF quirk list, plus common Philips preset
# names. Picking a name the TV doesn't support is harmless — the TV
# silently ignores currentconfiguration POSTs for unknown menuSettings.
KNOWN_FOLLOW_VIDEO_MENU_SETTINGS = [
    "STANDARD", "NATURAL", "VIVID", "CINEMA", "GAME", "SPORTS", "COMFORT",
]
KNOWN_FOLLOW_AUDIO_MENU_SETTINGS = [
    "ENERGY_ADAPTIVE_BRIGHTNESS", "ENERGY_ADAPTIVE_COLORS",
    "VU_METER", "SPECTRUM_ANALYZER",
    "KNIGHT_RIDER_CLOCKWISE", "KNIGHT_RIDER_ALTERNATING",
    "RANDOM_PIXEL_FLASH", "STROBE", "PARTY",
]
KNOWN_FOLLOW_COLOR_MENU_SETTINGS = [
    "PTA_LOUNGE", "DEEP_WATER", "HOT_LAVA", "FRESH_NATURE", "ISF_BLUE",
]

# Pretty user-facing labels for the effect dropdown. HA's dropdown is a
# flat list (no section headers), so we use a "Category · Name" prefix
# and sort alphabetically — modes then group visually as Audio · …,
# Color · …, Video · …. Anything not in this map falls back to a generic
# Title-Cased rendering.
PRETTY_LABELS: dict[tuple[str, str | None], str] = {
    # Video presets — track image on screen
    ("FOLLOW_VIDEO", "STANDARD"):  "Video · Standard",
    ("FOLLOW_VIDEO", "NATURAL"):   "Video · Natural",
    ("FOLLOW_VIDEO", "VIVID"):     "Video · Vivid",
    ("FOLLOW_VIDEO", "CINEMA"):    "Video · Cinema",
    ("FOLLOW_VIDEO", "GAME"):      "Video · Game",
    ("FOLLOW_VIDEO", "SPORTS"):    "Video · Sports",
    ("FOLLOW_VIDEO", "COMFORT"):   "Video · Comfort",
    # Audio presets — react to sound
    ("FOLLOW_AUDIO", "ENERGY_ADAPTIVE_BRIGHTNESS"): "Audio · Energy (Brightness)",
    ("FOLLOW_AUDIO", "ENERGY_ADAPTIVE_COLORS"):     "Audio · Energy (Colors)",
    ("FOLLOW_AUDIO", "VU_METER"):                   "Audio · Retro",
    ("FOLLOW_AUDIO", "SPECTRUM_ANALYZER"):          "Audio · Spectrum",
    ("FOLLOW_AUDIO", "KNIGHT_RIDER_CLOCKWISE"):     "Audio · Knight Rider",
    ("FOLLOW_AUDIO", "KNIGHT_RIDER_ALTERNATING"):   "Audio · Knight Rider (Alt)",
    ("FOLLOW_AUDIO", "RANDOM_PIXEL_FLASH"):         "Audio · Rhythm",
    ("FOLLOW_AUDIO", "STROBE"):                     "Audio · Strobe",
    ("FOLLOW_AUDIO", "PARTY"):                      "Audio · Party",
    # Color presets — fixed color schemes
    ("FOLLOW_COLOR", "PTA_LOUNGE"):    "Color · Cool White",
    ("FOLLOW_COLOR", "DEEP_WATER"):    "Color · Deep Water",
    ("FOLLOW_COLOR", "HOT_LAVA"):      "Color · Hot Lava",
    ("FOLLOW_COLOR", "FRESH_NATURE"):  "Color · Fresh Nature",
    ("FOLLOW_COLOR", "ISF_BLUE"):      "Color · ISF Blue",
    ("FOLLOW_COLOR", "AUTOMATIC_HUE"): "Color · Auto Hue",
    ("FOLLOW_COLOR", "MANUAL_HUE"):    "Color · Manual Hue",
}
PRETTY_TO_KEY = {v: k for k, v in PRETTY_LABELS.items()}
PRETTY_CUSTOM = "Custom"  # shown when TV is in expert mode (color picker)

# When the TV receives setAmbilightCurrentConfiguration({styleName: X,
# isExpert: false}), it auto-derives its internal mode from styleName.
# Verified mappings from direct probing (see MemPalace
# home-assistant/philips-integration/api-patterns):
#   FOLLOW_VIDEO / FOLLOW_AUDIO → "internal"
#   FOLLOW_COLOR                → "lounge"
# The haphilipsjs library tracks ambilight_current_configuration after the
# POST but NOT ambilight_mode_raw, which leaves the cached mode stale and
# causes gating decisions in turn_off / turn_on / color_worker that read
# ambilight_mode_raw to use wrong values (manifests as the "turn on/off
# needs second press" bug — cache says "expert" but TV is actually in
# "internal"/"lounge", so cached pixel writes are silently ignored).
_STYLE_TO_AUTO_MODE: dict[str, str] = {
    "FOLLOW_VIDEO": "internal",
    "FOLLOW_AUDIO": "internal",
    "FOLLOW_COLOR": "lounge",
    # Other styleNames (OFF, FOLLOW_APP, MANUAL, etc.) — leave cache as-is.
    # We don't have verified data on their auto-derived mode.
}


def _pretty_label(effect: AmbilightEffect) -> str:
    """User-facing label for the effect dropdown."""
    if effect.mode == EFFECT_EXPERT:
        return PRETTY_CUSTOM
    if effect.mode != EFFECT_AUTO:
        return str(effect)  # fallback for unexpected modes
    key = (effect.style, effect.algorithm)
    if key in PRETTY_LABELS:
        return PRETTY_LABELS[key]
    style = effect.style.replace("FOLLOW_", "").replace("_", " ").title()
    algo = (effect.algorithm or "").replace("_", " ").title()
    return f"{style} · {algo}" if algo else style


def _label_to_effect(label: str) -> AmbilightEffect:
    """Reverse of _pretty_label — accepts a pretty label or a raw effect string."""
    if label in PRETTY_TO_KEY:
        style, algo = PRETTY_TO_KEY[label]
        return AmbilightEffect(EFFECT_AUTO, style, algo)
    if label == PRETTY_CUSTOM:
        return AmbilightEffect(EFFECT_EXPERT, "FOLLOW_COLOR", "MANUAL_HUE")
    return AmbilightEffect.from_str(label)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: PhilipsTVConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the configuration entry."""
    coordinator = config_entry.runtime_data
    async_add_entities([PhilipsTVLightEntity(coordinator)])


def _get_settings(style: AmbilightCurrentConfiguration):
    """Extract the color settings data from a style."""
    if style["styleName"] in ("FOLLOW_COLOR", "Lounge light"):
        return style["colorSettings"]
    if style["styleName"] == "FOLLOW_AUDIO":
        return style["audioSettings"]
    return None


@dataclass
class AmbilightEffect:
    """Data class describing the ambilight effect."""

    mode: str
    style: str
    algorithm: str | None = None

    def is_on(self, powerstate) -> bool:
        """Check whether the ambilight is considered on."""
        if self.mode in (EFFECT_AUTO, EFFECT_EXPERT):
            if self.style in ("FOLLOW_VIDEO", "FOLLOW_AUDIO"):
                return powerstate in ("On", None)
            if self.style == "OFF":
                return False
            return True

        if self.mode == EFFECT_MODE:
            if self.style == "internal":
                return powerstate in ("On", None)
            return True

        return False

    def is_valid(self) -> bool:
        """Validate the effect configuration."""
        if self.mode == EFFECT_EXPERT:
            return self.style in EFFECT_EXPERT_STYLES
        return True

    @staticmethod
    def from_str(effect_string: str) -> AmbilightEffect:
        """Create AmbilightEffect object from string."""
        style, _, algorithm = effect_string.partition(EFFECT_PARTITION)
        if style == EFFECT_MODE:
            return AmbilightEffect(mode=EFFECT_MODE, style=algorithm, algorithm=None)
        algorithm, _, expert = algorithm.partition(EFFECT_PARTITION)
        if expert:
            return AmbilightEffect(mode=EFFECT_EXPERT, style=style, algorithm=algorithm)
        return AmbilightEffect(mode=EFFECT_AUTO, style=style, algorithm=algorithm)

    def __str__(self) -> str:
        """Get a string representation of the effect."""
        if self.mode == EFFECT_MODE:
            return f"{EFFECT_MODE}{EFFECT_PARTITION}{self.style}"
        if self.mode == EFFECT_EXPERT:
            return f"{self.style}{EFFECT_PARTITION}{self.algorithm}{EFFECT_PARTITION}{EFFECT_EXPERT}"
        return f"{self.style}{EFFECT_PARTITION}{self.algorithm}"


def _get_cache_keys(device: PhilipsTV):
    """Return a cache keys to avoid always updating."""
    return (
        device.on,
        device.powerstate,
        device.ambilight_current_configuration,
        device.ambilight_mode,
    )


def _average_pixels(data):
    """Calculate an average color over all ambilight pixels."""
    color_c = 0
    color_r = 0.0
    color_g = 0.0
    color_b = 0.0
    for layer in data.values():
        for side in layer.values():
            for pixel in side.values():
                color_c += 1
                color_r += pixel["r"]
                color_g += pixel["g"]
                color_b += pixel["b"]

    if color_c:
        color_r /= color_c
        color_g /= color_c
        color_b /= color_c
        return color_r, color_g, color_b
    return 0.0, 0.0, 0.0


class PhilipsTVLightEntity(PhilipsJsEntity, LightEntity):
    """Representation of a Philips TV exposing the JointSpace API."""

    _attr_effect: str
    _attr_translation_key = "ambilight"
    _attr_supported_color_modes = {ColorMode.HS}
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(self, coordinator: PhilipsTVDataUpdateCoordinator) -> None:
        """Initialize light."""
        self._tv = coordinator.api
        self._hs = None
        self._brightness = None
        self._cache_keys = None
        self._last_selected_effect: AmbilightEffect | None = None
        # Snapshot of (mode, currentconfiguration) taken on turn_off so a plain
        # turn_on can restore the exact prior state. Local fix for #156776:
        # the bundled async_turn_off path returns HTML "Ok" on API 6.x but
        # does not black the LEDs. The official Philips app uses
        # mode=expert + cached zero pixels, so we do the same.
        self._pre_off_snapshot: dict[str, Any] | None = None
        # Serialize on/off operations so rapid toggling can't corrupt the
        # snapshot mid-restore.
        self._toggle_lock = asyncio.Lock()
        # Track what we last painted via cached so subsequent color/brightness
        # service calls can use these as the baseline (rather than reading
        # self.hs_color / self.brightness, which are computed from the TV's
        # stale currentconfiguration.colorSettings on every poll and would
        # cause drift each round-trip).
        self._last_painted_hs: tuple[float, float] | None = None
        self._last_painted_brightness: int | None = None
        # Remember the last currentconfiguration we explicitly set via an
        # effect pick. self._tv.ambilight_current_configuration drifts when
        # the TV powers off (or briefly during state transitions), and we
        # want the snapshot used by turn_off to capture what the user
        # actually chose — not whatever the TV happened to report at that
        # moment.
        self._last_set_config: AmbilightCurrentConfiguration | None = None
        # Latest-wins color update queue. HA fires dozens of light.turn_on
        # calls per second during a color-wheel drag; if each one blocks on
        # a TV round-trip the UI feels stuck and updates pile up. Instead
        # we store the latest pending color and let a single background
        # worker process it, coalescing intermediate values.
        self._pending_color: tuple[tuple[float, float], int] | None = None
        self._color_task: asyncio.Task | None = None
        super().__init__(coordinator)

        self._attr_unique_id = coordinator.unique_id

        self._update_from_coordinator()

    def _calculate_effect_list(self):
        """Calculate an effect list based on current status."""
        effects: list[AmbilightEffect] = []

        # Inject known menuSettings when the TV's supportedstyles endpoint
        # omits them (API 6.x firmware doesn't enumerate FOLLOW_VIDEO menu
        # settings even though they exist). See module-level KNOWN_* lists.
        _FALLBACK_MENU = {
            "FOLLOW_VIDEO": KNOWN_FOLLOW_VIDEO_MENU_SETTINGS,
            "FOLLOW_AUDIO": KNOWN_FOLLOW_AUDIO_MENU_SETTINGS,
            "FOLLOW_COLOR": KNOWN_FOLLOW_COLOR_MENU_SETTINGS,
        }

        for style, data in self._tv.ambilight_styles.items():
            menu_settings = list(data.get("menuSettings") or [])
            if not menu_settings:
                menu_settings = _FALLBACK_MENU.get(style, [])
            effects.extend(
                AmbilightEffect(mode=EFFECT_AUTO, style=style, algorithm=setting)
                for setting in menu_settings
            )

        # Skip the EXPERT variants — they're redundant with the same modes
        # in AUTO above (FOLLOW_AUDIO algorithms are exposed as AUTO menu
        # settings via _FALLBACK_MENU) and custom color picking is done
        # via the dedicated color wheel, not via an effect entry.
        #
        # Skip the "Mode: internal / expert / manual" entries — they set raw
        # ambilight modes that on API 6.x firmware either no-op visibly or
        # trigger the broken internal_invalid quirk.

        filtered_effects = [
            _pretty_label(effect)
            for effect in effects
            if effect.is_valid() and effect.is_on(self._tv.powerstate)
        ]

        return sorted(filtered_effects)

    def _calculate_effect(self) -> AmbilightEffect:
        """Return the current effect."""
        current = self._tv.ambilight_current_configuration
        if current and self._tv.ambilight_mode != "manual":
            if current["isExpert"]:
                if settings := _get_settings(current):
                    return AmbilightEffect(
                        EFFECT_EXPERT, current["styleName"], settings["algorithm"]
                    )
                return AmbilightEffect(EFFECT_EXPERT, current["styleName"], None)

            return AmbilightEffect(
                EFFECT_AUTO, current["styleName"], current.get("menuSetting", None)
            )

        return AmbilightEffect(EFFECT_MODE, self._tv.ambilight_mode, None)

    @property
    def color_mode(self) -> ColorMode:
        """Return the current color mode.

        Always ColorMode.HS now that brightness works on presets too via
        menuitems node 710 (and color wheel still triggers expert-mode
        cached pixels). Returning ONOFF for presets — which the upstream
        code did — caused HA to hide the brightness slider entirely on
        any preset effect, making brightness control inaccessible.

        hs_color may legitimately be None on a preset (no single color
        to report), which HA handles fine. The wheel still shows a
        default-positioned indicator and touching it switches to expert.
        """
        return ColorMode.HS

    @property
    def is_on(self) -> bool:
        """Return if the light is turned on."""
        # When we've put the TV into our custom off state (mode=expert +
        # cached zeros), the TV reports mode=expert and styleName=FOLLOW_COLOR
        # with isExpert=true. The upstream effect.is_on() logic treats any
        # non-OFF expert style as "on", which would make the HA toggle bounce
        # right back to on — and the next click would call turn_off again
        # and overwrite our snapshot. Trust the snapshot as the source of
        # truth for our off state instead.
        if self._pre_off_snapshot is not None:
            return False
        if self._tv.on:
            effect = _label_to_effect(self._attr_effect)
            return effect.is_on(self._tv.powerstate)

        return False

    def _update_from_coordinator(self):
        current = self._tv.ambilight_current_configuration
        color = None

        if (cache_keys := _get_cache_keys(self._tv)) != self._cache_keys:
            self._cache_keys = cache_keys
            self._attr_effect_list = self._calculate_effect_list()
            self._attr_effect = _pretty_label(self._calculate_effect())

        if current and current["isExpert"]:
            if settings := _get_settings(current):
                color = settings["color"]

        effect = AmbilightEffect.from_str(self._attr_effect)
        if effect.is_on(self._tv.powerstate):
            self._last_selected_effect = effect

        # Brightness: ALWAYS prefer _last_painted_brightness over the TV's
        # reported colorSettings.color["brightness"] because the latter is
        # stale on this firmware (doesn't reflect cached writes or the
        # menuitems brightness node). Without this preference, the slider
        # snaps back every coordinator poll — rwjack's "HA state resets to
        # 1% just by switching tabs" symptom in #156776.
        if effect.mode == EFFECT_EXPERT and color:
            self._attr_hs_color = (
                color["hue"] * 360.0 / 255.0,
                color["saturation"] * 100.0 / 255.0,
            )
            self._attr_brightness = (
                self._last_painted_brightness
                if self._last_painted_brightness is not None
                else color["brightness"]
            )
        elif effect.mode == EFFECT_MODE and self._tv.ambilight_cached:
            hsv_h, hsv_s, hsv_v = color_RGB_to_hsv(
                *_average_pixels(self._tv.ambilight_cached)
            )
            self._attr_hs_color = hsv_h, hsv_s
            self._attr_brightness = (
                self._last_painted_brightness
                if self._last_painted_brightness is not None
                else hsv_v * 255.0 / 100.0
            )
        else:
            # On a preset (EFFECT_AUTO) the TV doesn't expose a per-preset
            # color, but we DO have brightness via the menuitems API (node
            # 710, MLM_PHM_KEY_PC_ID_BRIGH_L12, range 0-9). The slider
            # reflects _last_painted_brightness, which is set by both the
            # color-worker path (expert) and the menuitems-brightness path
            # (presets) — so it persists across coordinator polls regardless
            # of which control the user last touched.
            self._attr_hs_color = None
            self._attr_brightness = self._last_painted_brightness

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_coordinator()
        super()._handle_coordinator_update()

    async def _color_worker_loop(self) -> None:
        """Drain the pending color queue, coalescing rapid drag updates.

        HA's color wheel fires ~30 light.turn_on calls per second during a
        drag. Without this worker, each call would block on a TV round-trip
        and the UI would feel unresponsive. With it, intermediate values
        are dropped and we send at most one cached POST per worker cycle.
        Each iteration runs under the toggle lock so off/on operations can
        still serialize with color writes.
        """
        try:
            while True:
                async with self._toggle_lock:
                    target = self._pending_color
                    if target is None:
                        return
                    self._pending_color = None
                    # Color write means we're in an on state; consume any
                    # snapshot from a prior turn_off.
                    self._pre_off_snapshot = None

                    hs_color, brightness = target
                    rgb = color_hsv_to_RGB(hs_color[0], hs_color[1], brightness * 100 / 255)
                    pixel = {"r": rgb[0], "g": rgb[1], "b": rgb[2]}
                    layer = await self._build_layer(pixel)
                    if not layer:
                        continue
                    # Skip the unstick + mode change if we can — saves up
                    # to two round-trips per color update and removes the
                    # visible flicker during drag.
                    #
                    # Verified-safe skip conditions:
                    #   - mode == "expert": already where we need to be
                    #   - mode == "internal": POST mode=expert is accepted
                    #     directly from internal (tested 2026-05-24).
                    # Only "lounge" + a FOLLOW_COLOR menuSetting is the
                    # mode-sticky state that requires the full unstick.
                    # "manual" is untested so we conservatively keep the
                    # unstick for it too.
                    if self._tv.ambilight_mode_raw not in ("expert", "internal"):
                        await self._set_current_config({
                            "styleName": "FOLLOW_VIDEO",
                            "isExpert": False,
                            "menuSetting": "STANDARD",
                        })
                    if self._tv.ambilight_mode_raw != "expert":
                        await self._tv.setAmbilightMode("expert")
                    await self._tv.setAmbilightCached({"layer1": layer})

                    self._last_painted_hs = hs_color
                    self._last_painted_brightness = brightness
                    self._update_from_coordinator()
                    self.async_write_ha_state()
                # Brief debounce outside the lock: if more updates arrive
                # within this window they coalesce into the next iteration.
                await asyncio.sleep(0.05)
        except Exception:
            # Worker must not crash silently leave _color_task in a "done"
            # state without surfacing; but we also don't want a transient
            # network blip to kill all future color updates.
            self._pending_color = None
            raise

    async def _build_layer(self, pixel: dict[str, int]) -> dict[str, dict[str, dict[str, int]]]:
        """Build a single-layer pixel dict shaped to match the TV's actual LED layout.

        Uses the ambilight_cached structure first (always populated after first
        connect, exactly matches the shape the TV expects). Falls back to
        ambilight_topology. Skips empty-dict sides — including bottom:{} on TVs
        with no bottom LEDs causes the TV to silently ignore the whole payload.
        """
        cached = self._tv.ambilight_cached
        if not cached:
            cached = await self._tv.getAmbilightCached()
        layer1 = (cached or {}).get("layer1") or {}

        layer: dict[str, dict[str, dict[str, int]]] = {}
        for side, pixels in layer1.items():
            if isinstance(pixels, dict) and pixels:
                layer[side] = {idx: dict(pixel) for idx in pixels}

        if not layer:
            topology = self._tv.ambilight_topology
            if not topology:
                topology = await self._tv.getAmbilightTopology()
            topology = topology or {}
            for side in ("top", "left", "right", "bottom"):
                n = topology.get(side, 0)
                if n:
                    layer[side] = {str(i): dict(pixel) for i in range(n)}

        return layer

    async def _set_ambilight_cached(
        self, effect: AmbilightEffect, hs_color: tuple[float, float], brightness: int
    ):
        """Set ambilight via the manual or expert mode."""
        rgb = color_hsv_to_RGB(hs_color[0], hs_color[1], brightness * 100 / 255)

        data = {
            "r": rgb[0],
            "g": rgb[1],
            "b": rgb[2],
        }

        if not await self._tv.setAmbilightCached(data):
            raise HomeAssistantError("Failed to set ambilight color")

        if effect.style != self._tv.ambilight_mode:
            if not await self._tv.setAmbilightMode(effect.style):
                raise HomeAssistantError("Failed to set ambilight mode")

    async def _set_ambilight_expert_config(
        self, effect: AmbilightEffect, hs_color: tuple[float, float], brightness: int
    ):
        """Set ambilight via current configuration."""
        config: AmbilightCurrentConfiguration = {
            "styleName": effect.style,
            "isExpert": True,
        }

        setting = {
            "algorithm": effect.algorithm,
            "color": {
                "hue": round(hs_color[0] * 255.0 / 360.0),
                "saturation": round(hs_color[1] * 255.0 / 100.0),
                "brightness": round(brightness),
            },
            "colorDelta": {
                "hue": 0,
                "saturation": 0,
                "brightness": 0,
            },
        }

        if effect.style in ("FOLLOW_COLOR", "Lounge light"):
            config["colorSettings"] = setting
            config["speed"] = 2

        elif effect.style == "FOLLOW_AUDIO":
            config["audioSettings"] = setting
            config["tuning"] = 0

        if not await self._set_current_config(config):
            raise HomeAssistantError("Failed to set ambilight mode")
        self._last_set_config = config

    async def _set_ambilight_config(self, effect: AmbilightEffect):
        """Set ambilight via current configuration."""
        config: AmbilightCurrentConfiguration = {
            "styleName": effect.style,
            "isExpert": False,
            "menuSetting": effect.algorithm,
        }

        if await self._set_current_config(config) is False:
            raise HomeAssistantError("Failed to set ambilight mode")
        self._last_set_config = config

    async def _set_menuitems_active_style(self, style_name: str | None) -> None:
        """Parallel-write the TV's persistent ambilight style via menuitems.

        Without this, setAmbilightCurrentConfiguration only writes the
        session-state style. The TV maintains a separate `activenode_id`
        in node 300 that survives power cycles — when it's 0 (API-driven),
        the TV reverts to its menu-set default on next boot. By writing
        the parent node ID here (320 for FOLLOW_VIDEO, 330 for FOLLOW_AUDIO,
        340 for FOLLOW_COLOR), the TV's persistent state matches HA's
        intent — addressing rwjack's "TV uses original Fixed Color on power
        cycle even though menu shows Follow App" report in #156776.

        Silent no-op for styleName values we don't have a mapping for
        (OFF, FOLLOW_APP, MANUAL) — those leave the persistent state alone.
        """
        parent_node = _STYLE_TO_PARENT_NODE.get(style_name) if style_name else None
        if parent_node is None:
            return
        payload = {
            "values": [
                {
                    "value": {
                        "Nodeid": _AMBILIGHT_STYLE_PARENT_NODE,
                        "data": {"activenode_id": parent_node},
                    }
                }
            ]
        }
        result = await self._tv.postReq("menuitems/settings/update", payload)
        if result is None:
            # Don't raise — this is a polish/persistence concern, not
            # essential to the operation succeeding. Log and continue.
            LOGGER.warning(
                "menuitems persistent-style sync failed for %s (parent=%d)",
                style_name, parent_node,
            )
        else:
            LOGGER.debug(
                "menuitems persistent style: %s → node 300 activenode_id=%d",
                style_name, parent_node,
            )

    async def _set_menuitems_brightness(self, ha_brightness: int) -> None:
        """POST ambilight brightness via the TV's menuitems settings API.

        Scales HA's 0-255 brightness to the TV's 0-9 range (node 710). This
        is the same path the official Android Ambilight app uses for its
        brightness slider, and it's the only way to scale brightness
        without leaving the current preset / style.

        The library doesn't expose a wrapper for this — we go directly via
        postReq. Maps HA 0→TV 1 (the slider min is usually 1, treating 0
        as "min visible" rather than "off") through HA 255→TV 9.
        """
        # Map: HA 1..255 → TV 1..9, with HA 0 also → TV 1 (don't hit zero,
        # which on some TVs equals "off" for the menuitems brightness path).
        ha_clamped = max(1, min(255, int(ha_brightness)))
        tv_value = max(1, round(ha_clamped * _AMBILIGHT_BRIGHTNESS_TV_MAX / 255))
        payload = {
            "values": [
                {
                    "value": {
                        "Nodeid": _AMBILIGHT_BRIGHTNESS_NODEID,
                        "data": {"value": tv_value},
                    }
                }
            ]
        }
        result = await self._tv.postReq("menuitems/settings/update", payload)
        if result is None:
            raise HomeAssistantError(
                f"Failed to set ambilight brightness to {tv_value}/9 via menuitems"
            )
        LOGGER.debug(
            "Set ambilight brightness via menuitems: HA %d → TV %d/9",
            ha_brightness, tv_value,
        )

    async def _set_current_config(self, cfg: dict) -> bool | None:
        """setAmbilightCurrentConfiguration + sync the cached mode_raw.

        The haphilipsjs library updates ambilight_current_configuration
        after this POST but does NOT update ambilight_mode_raw, even
        though the TV auto-derives its mode from the styleName. Without
        this sync, gating decisions in async_turn_off / async_turn_on /
        _color_worker_loop that read self._tv.ambilight_mode_raw will use
        a stale value, leading to the "turn on/off needs second press"
        bug — cache says we're in "expert" so the unstick + setAmbilightMode
        steps are skipped, but the TV is actually in "internal" or "lounge"
        (after a prior setAmbilightCurrentConfiguration that auto-flipped
        mode) and silently ignores subsequent cached pixel writes.

        isExpert:true configs are silently ignored by the TV per
        api-patterns (the TV doesn't render from them), so they don't
        change the mode either — skip the sync for those.
        """
        result = await self._tv.setAmbilightCurrentConfiguration(cfg)
        if result is not False and not cfg.get("isExpert"):
            style_name = cfg.get("styleName")
            derived = _STYLE_TO_AUTO_MODE.get(style_name)
            if derived is not None:
                self._tv.ambilight_mode_raw = derived
            # NOTE: parallel-writing menuitems node 300 activenode_id to
            # persist style across power cycles was attempted and reverted
            # 2026-05-25 — see [[home-assistant/philips-integration]]
            # (persistent-style-attempt-reverted). The menuitems API only
            # exposes "Fixed Colour" as the FOLLOW_COLOR sub-style; setting
            # activenode_id=340 silently switches the TV from the
            # currentconfiguration-set lounge preset (Hot Lava etc.) to
            # Fixed Colour with the TV's stored default color (blue on this
            # firmware). Same risk for VIDEO/AUDIO families where the
            # selected_item enums are incomplete. Restoring persistence
            # without this regression requires a full sub-style enum map
            # for every style family — out of scope for now.
        return result

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the bulb on."""
        if not self._tv.on:
            raise HomeAssistantError("TV is not available")

        # Plain turn-on with no user-specified effect/color/brightness:
        # restore snapshot if we have one, else use a safe default. Run
        # under the toggle lock so rapid on/off can't race.
        plain_toggle = (
            ATTR_EFFECT not in kwargs
            and ATTR_HS_COLOR not in kwargs
            and ATTR_BRIGHTNESS not in kwargs
        )
        if plain_toggle:
            # Cancel any queued color write so the worker doesn't paint over
            # the restored state after we release the lock.
            self._pending_color = None
            async with self._toggle_lock:
                snap = self._pre_off_snapshot
                self._pre_off_snapshot = None
                if snap:
                    # If the prior state was expert mode with user-painted
                    # cached pixels, restore by repainting cached. Restoring
                    # only mode+config won't bring the LEDs back because
                    # currentconfiguration.colorSettings doesn't drive cached.
                    hs = snap.get("rendered_hs_color")
                    br = snap.get("rendered_brightness")
                    if snap.get("mode") == "expert" and hs is not None and br is not None:
                        rgb = color_hsv_to_RGB(hs[0], hs[1], br * 100 / 255)
                        pixel = {"r": rgb[0], "g": rgb[1], "b": rgb[2]}
                        layer = await self._build_layer(pixel)
                        if layer:
                            # Already in expert mode (from prior turn_off),
                            # so just need cached.
                            if await self._tv.setAmbilightCached({"layer1": layer}) is False:
                                raise HomeAssistantError("Failed to repaint ambilight")
                            self._last_painted_hs = hs
                            self._last_painted_brightness = br
                            self._update_from_coordinator()
                            self.async_write_ha_state()
                            return
                    # Standard restore: a single setAmbilightCurrentConfiguration
                    # is enough — the TV auto-derives the matching mode
                    # (FOLLOW_COLOR → lounge, FOLLOW_VIDEO → internal). We skip
                    # the separate setAmbilightMode call because (a) it adds a
                    # visible flicker as the TV transitions through modes, and
                    # (b) for mode="internal" it triggers the broken
                    # internal_invalid quirk in haphilipsjs.
                    cfg = snap.get("config")
                    if cfg:
                        if await self._set_current_config(cfg) is False:
                            raise HomeAssistantError("Failed to restore ambilight configuration")
                else:
                    # Snapshot was lost (e.g. consumed by a racing call before
                    # rapid spam settled). Falling through to the upstream
                    # turn_on would call setAmbilightMode("internal") which
                    # triggers the broken internal_invalid quirk on API 6.x.
                    # Use a safe default that we know works: FOLLOW_VIDEO/STANDARD.
                    if await self._set_current_config({
                        "styleName": "FOLLOW_VIDEO",
                        "isExpert": False,
                        "menuSetting": "STANDARD",
                    }) is False:
                        raise HomeAssistantError("Failed to set default ambilight")
                self._update_from_coordinator()
                self.async_write_ha_state()
                return

        # Color/brightness adjustment, or explicit effect.
        # For color/brightness on API 6.x, the only verified-working path is
        # the official-app pattern: unstick lounge → mode=expert → cached
        # pixels painted with the requested RGB. The upstream
        # _set_ambilight_expert_config + currentconfiguration{isExpert:true}
        # is silently ignored by these TVs.
        async with self._toggle_lock:
            # Any explicit change cancels our custom-off snapshot — we're
            # definitively entering an on state.
            self._pre_off_snapshot = None

            attr_effect = cast(str, kwargs.get(ATTR_EFFECT, self.effect))

            # Direct color/brightness request → schedule background worker
            # and return immediately. See _color_worker_loop for details.
            # IMPORTANT: don't fall back to self.hs_color/self.brightness for
            # the unset attribute, because those come from the upstream
            # coordinator computation off currentconfiguration (stale) and
            # would cause drift. Use _last_painted_* (what we actually wrote
            # last) as the baseline.
            if ATTR_HS_COLOR in kwargs or ATTR_BRIGHTNESS in kwargs:
                # Brightness-only change on a preset effect: route via the
                # menuitems brightness node (710, MLM_PHM_KEY_PC_ID_BRIGH_L12).
                # This is what the official Android Ambilight app uses for
                # brightness. Without this, falling through to the worker
                # would paint cached pixels using _last_painted_hs (default
                # (0,0) = white) for the unset color → user's preset (e.g.
                # Hot Lava) snaps to white AND switches the TV out of lounge.
                # The menuitems route keeps the preset intact and actually
                # changes the LED brightness.
                brightness_only = (
                    ATTR_BRIGHTNESS in kwargs
                    and ATTR_HS_COLOR not in kwargs
                )
                if brightness_only:
                    current_effect = _label_to_effect(self._attr_effect or "")
                    if current_effect.mode != EFFECT_EXPERT:
                        new_brightness = int(kwargs[ATTR_BRIGHTNESS])
                        try:
                            await self._set_menuitems_brightness(new_brightness)
                        except Exception as err:  # noqa: BLE001
                            LOGGER.warning(
                                "menuitems brightness POST failed (%s); "
                                "falling back to optimistic slider only", err
                            )
                        # Update HA state regardless — slider should reflect
                        # the user's intent even if the TV write failed.
                        self._last_painted_brightness = new_brightness
                        self._attr_brightness = new_brightness
                        self.async_write_ha_state()
                        return

                hs_color = kwargs.get(ATTR_HS_COLOR)
                if hs_color is None:
                    hs_color = self._last_painted_hs or (0, 0)
                brightness = kwargs.get(ATTR_BRIGHTNESS)
                if brightness is None:
                    brightness = self._last_painted_brightness
                    if brightness is None:
                        brightness = 255

                # Queue the latest target and kick off the worker if it's
                # not already running. This returns immediately so HA's UI
                # stays responsive during a drag, and intermediate values
                # get coalesced into at most one TV write per worker cycle.
                self._pending_color = (hs_color, brightness)
                if self._color_task is None or self._color_task.done():
                    self._color_task = asyncio.create_task(self._color_worker_loop())
                return
            else:
                # Effect-only change (e.g. user picked FOLLOW_VIDEO: STANDARD
                # from the effect dropdown). Use the existing routing.
                # Default color/brightness used when an EXPERT effect needs
                # them — fall back to whatever we last painted, then to a
                # neutral white at full brightness.
                hs_color = self._last_painted_hs or (0, 0)
                brightness = self._last_painted_brightness or 255
                effect = _label_to_effect(attr_effect)
                if effect.mode == EFFECT_AUTO:
                    await self._set_ambilight_config(effect)
                elif effect.mode == EFFECT_EXPERT:
                    await self._set_ambilight_expert_config(effect, hs_color, brightness)
                elif effect.mode == EFFECT_MODE:
                    # The "internal"/"manual" route triggers the broken
                    # internal_invalid quirk on API 6.x. Fall back to default.
                    if await self._set_current_config({
                        "styleName": "FOLLOW_VIDEO",
                        "isExpert": False,
                        "menuSetting": "STANDARD",
                    }) is False:
                        raise HomeAssistantError("Failed to set default ambilight")
                self._update_from_coordinator()
                self.async_write_ha_state()

            self._update_from_coordinator()
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off ambilight.

        The bundled implementation calls setAmbilightMode("internal") and
        setAmbilightCurrentConfiguration({"styleName":"OFF",...}). On API 6.x
        firmware (verified on 55OLED759/12, 75PUS8079/12) the TV returns HTML
        "Ok" for both but the LEDs do not actually go dark — see
        https://github.com/home-assistant/core/issues/156776 and the official
        Philips app mitmproxy capture, which only ever uses mode=expert plus
        per-pixel cached writes to control the LEDs.

        Snapshot the current mode + currentconfiguration so turn_on can
        restore the exact prior color/menuSetting, then go to mode=expert
        and write zero pixels for every side reported by the topology.
        """
        if not self._tv.on:
            raise HomeAssistantError("TV is not available")

        # Cancel any queued color write so the worker doesn't repaint over
        # the off state after we acquire the lock.
        self._pending_color = None

        async with self._toggle_lock:
            # Already in our custom off state — don't overwrite the snapshot
            # we need for restore. Just no-op.
            if self._pre_off_snapshot is not None:
                return

            # Snapshot for later restore. Prefer _last_set_config (what the
            # user explicitly picked via HA) over the TV's reported
            # currentconfiguration — the latter drifts when the TV powers
            # off and would cause a wrong-preset restore. Also capture
            # _last_painted_* so we can repaint cached pixels on turn_on
            # when mode was expert (custom color via wheel).
            self._pre_off_snapshot = {
                "mode": self._tv.ambilight_mode_raw or self._tv.ambilight_mode,
                "config": self._last_set_config or self._tv.ambilight_current_configuration,
                "rendered_hs_color": self._last_painted_hs,
                "rendered_brightness": self._last_painted_brightness,
            }

            layer = await self._build_layer({"r": 0, "g": 0, "b": 0})
            if not layer:
                # Clear snapshot since we never wrote the off state.
                self._pre_off_snapshot = None
                raise HomeAssistantError("Cannot determine Ambilight LED layout")

            # IMPORTANT: when the TV is in FOLLOW_COLOR + a lounge preset (e.g.
            # menuSetting=PTA_LOUNGE), the mode endpoint silently rejects mode
            # changes — POST returns "Ok" but mode stays at "lounge", and any
            # subsequent cached writes are ignored because lounge keeps generating
            # its own colors. Break the sticky state first by POSTing a benign
            # currentconfiguration (FOLLOW_VIDEO/STANDARD), which auto-flips
            # mode to "internal", after which mode=expert + cached zeros are
            # accepted. The snapshot above preserves the prior state for restore.
            #
            # Skip the unstick when not needed: mode=expert is already there,
            # mode=internal can transition directly (verified 2026-05-24).
            # Only "lounge" actually needs the unstick; "manual" is untested
            # so we keep it conservatively.
            unstick = {
                "styleName": "FOLLOW_VIDEO",
                "isExpert": False,
                "menuSetting": "STANDARD",
            }
            if self._tv.ambilight_mode_raw not in ("expert", "internal"):
                if await self._set_current_config(unstick) is False:
                    raise HomeAssistantError("Failed to break sticky lounge state")
            if self._tv.ambilight_mode_raw != "expert":
                if await self._tv.setAmbilightMode("expert") is False:
                    raise HomeAssistantError("Failed to set ambilight mode to expert")
            if await self._tv.setAmbilightCached({"layer1": layer}) is False:
                raise HomeAssistantError("Failed to write zero pixels")

            self._update_from_coordinator()
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return true if entity is available."""
        if not super().available:
            return False
        if not self._tv.on:
            return False
        return True
