"""Data models for Control4 Dimmers."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .const import (
    DEVICE_TYPE_KEYPADDIM,
    DEVICE_TYPE_SWITCH,
    DEVICE_TYPES,
    SWITCH_DIM_CODES,
)

# Inbound MQTT state is untrusted: any client that can publish to the
# broker can write a device's state topic. These bounds keep a forged or
# corrupt payload from poisoning entity state or growing memory.
_HEX6_RE = re.compile(r"#?[0-9a-fA-F]{6}")
# Keys kept in DeviceState.raw. Everything the integration reads from raw
# matches one of these; other Zigbee2MQTT fields (linkquality, update,
# ...) are kept only if they are short scalars.
_RAW_KEY_RE = re.compile(r"[a-z0-9_]{1,48}")
_RAW_MAX_KEYS = 200
_RAW_MAX_STR = 256
_RAW_MAX_NESTED_JSON = 1024
_ACTION_MAX_LEN = 64
# Keys whose value an entity reports as a number. A string, NaN or
# Infinity here would make Home Assistant's sensor or number entity raise.
_NUMERIC_KEY_RE = re.compile(r"ambient_light|c4_load_[a-z0-9_]+|c4_tv_[0-9a-f]{2}")


@dataclass
class SlotConfig:
    """Configuration for a single button slot."""

    slot_id: int
    size: int = 1
    name: str = ""
    behavior: str = "keypad"
    led_mode: str = "programmed"
    led_on_color: str = "0000ff"
    led_off_color: str = "000000"
    target_entity_id: str | None = None
    tap_action: dict | None = None
    double_tap_action: dict | None = None
    hold_action: dict | None = None
    led_track_entity_id: str | None = None

    @property
    def tracks_other_entity(self) -> bool:
        """
        Return True when a fixed LED follows an entity other than its own load.

        Only fixed LEDs count: in push_release and follow_load the firmware
        draws from the load-on and load-off colors itself, so overwriting
        them with the tracked color would break the press flash or the
        load-following colors.
        """
        return (
            self.led_mode == "fixed"
            and bool(self.led_track_entity_id)
            and self.led_track_entity_id != "__self_load__"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        d = {
            "slot_id": self.slot_id,
            "size": self.size,
            "name": self.name,
            "behavior": self.behavior,
            "led_mode": self.led_mode,
            "led_on_color": self.led_on_color,
            "led_off_color": self.led_off_color,
        }
        if self.target_entity_id:
            d["target_entity_id"] = self.target_entity_id
        if self.tap_action is not None:
            d["tap_action"] = self.tap_action
        if self.double_tap_action is not None:
            d["double_tap_action"] = self.double_tap_action
        if self.hold_action is not None:
            d["hold_action"] = self.hold_action
        if self.led_track_entity_id is not None:
            d["led_track_entity_id"] = self.led_track_entity_id
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SlotConfig:
        """Deserialize from dict."""
        return cls(
            slot_id=data["slot_id"],
            size=data.get("size", 1),
            name=data.get("name", ""),
            behavior=data.get("behavior", "keypad"),
            led_mode=data.get("led_mode", "programmed"),
            led_on_color=data.get("led_on_color", "0000ff"),
            led_off_color=data.get("led_off_color", "000000"),
            target_entity_id=data.get("target_entity_id"),
            tap_action=data.get("tap_action"),
            double_tap_action=data.get("double_tap_action"),
            hold_action=data.get("hold_action"),
            led_track_entity_id=data.get("led_track_entity_id"),
        )


@dataclass
class DeviceConfig:
    """Persisted configuration for a Control4 device."""

    ieee_address: str
    friendly_name: str
    device_type: str = ""
    device_type_override: str | None = None
    slots: list[SlotConfig] = field(default_factory=list)
    faceplate_color: str | None = None
    # Keypad backlight color (c4.dmx.blc 00 05). None leaves the device's
    # own setting alone; "000000" turns the backlight off.
    backlight_color: str | None = None

    @property
    def effective_type(self) -> str:
        """Return override type if set, otherwise detected type."""
        return self.device_type_override or self.device_type

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        d = {
            "ieee_address": self.ieee_address,
            "friendly_name": self.friendly_name,
            "device_type": self.device_type,
            "device_type_override": self.device_type_override,
            "slots": [s.to_dict() for s in self.slots],
        }
        if self.faceplate_color:
            d["faceplate_color"] = self.faceplate_color
        if self.backlight_color is not None:
            d["backlight_color"] = self.backlight_color
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceConfig:
        """Deserialize from dict."""
        return cls(
            ieee_address=data["ieee_address"],
            friendly_name=data["friendly_name"],
            device_type=data.get("device_type", ""),
            device_type_override=data.get("device_type_override"),
            slots=[SlotConfig.from_dict(s) for s in data.get("slots", [])],
            faceplate_color=data.get("faceplate_color"),
            backlight_color=data.get("backlight_color"),
        )


@dataclass
class DeviceState:
    """Live state of a Control4 device from Z2M MQTT."""

    ieee_address: str
    friendly_name: str
    model_id: str = ""
    device_type: str | None = None
    # The converter's raw c4.dmx.dim answer, published inside
    # c4_detect_result. Remembered so a later bare c4_device_type can be
    # refined the same way (see refine_device_type).
    dim_code: str | None = None
    available: bool = True
    brightness: int | None = None
    state: str | None = None
    led_colors: dict[int, dict[str, str]] = field(default_factory=dict)
    button_configs: dict[int, dict[str, str]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def update_from_mqtt(self, payload: dict[str, Any]) -> None:  # noqa: PLR0912
        """
        Update state from an MQTT state payload.

        Values that do not have the shape the contract promises are
        dropped instead of stored: state is ON/OFF, brightness an integer
        0-255, colors six-digit hex, numeric readings finite numbers, and
        c4_device_type one of the known types.
        """
        payload = sanitize_payload(payload)
        for key, value in payload.items():
            if key in self.raw or len(self.raw) < _RAW_MAX_KEYS:
                self.raw[key] = value

        if "state" in payload:
            self.state = payload["state"]
        if "brightness" in payload:
            self.brightness = payload["brightness"]
        detect_result = payload.get("c4_detect_result")
        if isinstance(detect_result, dict) and "dim_code" in detect_result:
            dim_code = detect_result["dim_code"]
            if dim_code is None or (isinstance(dim_code, str) and len(dim_code) <= 8):  # noqa: PLR2004
                self.dim_code = dim_code
        if "c4_device_type" in payload:
            self.device_type = refine_device_type(
                payload["c4_device_type"], self.dim_code
            )

        for btn in range(1, 7):
            # Flat hex LED attributes from the Z2M device state (c4_led_N_on/off)
            led_on_key = f"c4_led_{btn}_on"
            led_off_key = f"c4_led_{btn}_off"
            if led_on_key in payload:
                self.led_colors.setdefault(btn, {})["on"] = payload[led_on_key]
            if led_off_key in payload:
                self.led_colors.setdefault(btn, {})["off"] = payload[led_off_key]

            # Legacy Z2M HS color format (for backwards compatibility)
            color_on_key = f"color_button_{btn}_on"
            color_off_key = f"color_button_{btn}_off"
            if color_on_key in payload:
                self.led_colors.setdefault(btn, {})["on"] = payload[color_on_key]
            if color_off_key in payload:
                self.led_colors.setdefault(btn, {})["off"] = payload[color_off_key]

            beh_key = f"button_{btn}_behavior"
            mode_key = f"button_{btn}_led_mode"
            if beh_key in payload:
                self.button_configs.setdefault(btn, {})["behavior"] = payload[beh_key]
            if mode_key in payload:
                self.button_configs.setdefault(btn, {})["led_mode"] = payload[mode_key]


def valid_hex(value: Any) -> str | None:
    """Return a color as lowercase six-digit hex without "#", or None."""
    if isinstance(value, str) and _HEX6_RE.fullmatch(value):
        return value.lstrip("#").lower()
    return None


def _is_finite_number(value: Any) -> bool:
    """Return whether value is an int or float (not a bool) and finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _clean_color(value: Any) -> str | None:
    """Return a payload color as lowercase six-digit hex, or None if invalid."""
    if isinstance(value, str):
        return valid_hex(value)
    if isinstance(value, dict):
        hue = value.get("hue", 0)
        sat = value.get("saturation", 0)
        if not (_is_finite_number(hue) and _is_finite_number(sat)):
            return None
        return _hs_to_hex(hue % 360, min(max(sat, 0), 100))
    return None


def _clean_scalar(value: Any) -> tuple[bool, Any]:
    """Validate a generic payload value; return (keep, value)."""
    if value is None or isinstance(value, bool):
        return True, value
    if isinstance(value, (int, float)):
        return _is_finite_number(value), value
    if isinstance(value, str):
        return len(value) <= _RAW_MAX_STR, value
    if isinstance(value, (dict, list)):
        try:
            encoded = json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            return False, None
        return len(encoded) <= _RAW_MAX_NESTED_JSON, value
    return False, None


def _clean_value(key: str, value: Any) -> tuple[bool, Any]:  # noqa: PLR0911
    """Validate one payload field against the shape its key promises."""
    if key == "state":
        if isinstance(value, str) and value.upper() in ("ON", "OFF"):
            return True, value.upper()
        return False, None
    if key == "brightness":
        ok = isinstance(value, int) and not isinstance(value, bool)
        return (ok and 0 <= value <= 255), value  # noqa: PLR2004
    if key == "action":
        return isinstance(value, str) and len(value) <= _ACTION_MAX_LEN, value
    if key == "c4_device_type":
        return value in DEVICE_TYPES, value
    if key.startswith(("c4_led_", "color_button_")):
        color = _clean_color(value)
        return color is not None, color
    if _NUMERIC_KEY_RE.fullmatch(key):
        return _is_finite_number(value), value
    return _clean_scalar(value)


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the fields of an inbound state payload that pass validation."""
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not _RAW_KEY_RE.fullmatch(key):
            continue
        keep, cleaned = _clean_value(key, value)
        if keep:
            clean[key] = cleaned
    return clean


def refine_device_type(reported: str | None, dim_code: str | None) -> str | None:
    """
    Turn the converter's c4_device_type verdict into the integration's type.

    The converter files any dim answer it does not recognize under
    keypaddim. A relay-only switch answers one of SWITCH_DIM_CODES, so that
    verdict plus that code is a switch (issue #189). Every other verdict,
    including a converter that already reports "switch" itself, passes
    through untouched.
    """
    if reported == DEVICE_TYPE_KEYPADDIM and dim_code in SWITCH_DIM_CODES:
        return DEVICE_TYPE_SWITCH
    return reported


def _extract_color(color_data: Any) -> str:
    """Extract RGB hex string from Z2M color payload."""
    if isinstance(color_data, str):
        return color_data.lstrip("#")
    if isinstance(color_data, dict):
        h = color_data.get("hue", 0)
        s = color_data.get("saturation", 0)
        return _hs_to_hex(h, s)
    return "000000"


def _hs_to_hex(hue: float, saturation: float) -> str:
    """Convert hue/saturation to RGB hex (full brightness)."""
    h = hue / 360.0
    s = saturation / 100.0
    v = 1.0
    if s == 0.0:
        r = g = b = int(v * 255)
        return f"{r:02x}{g:02x}{b:02x}"

    i = int(h * 6.0)
    f = (h * 6.0) - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i = i % 6

    if i == 0:
        r, g, b = v, t, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:  # noqa: PLR2004
        r, g, b = p, v, t
    elif i == 3:  # noqa: PLR2004
        r, g, b = p, q, v
    elif i == 4:  # noqa: PLR2004
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q

    return f"{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
