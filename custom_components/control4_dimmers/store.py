"""Persistent storage for Control4 device configurations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .const import (
    DEVICE_TYPE_OUTLET,
    DEVICE_TYPE_SLOTS,
    DEVICE_TYPES,
    LOGGER,
    SLOT_COUNT,
    SNAPSHOT_STORAGE_KEY,
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
)
from .models import DeviceConfig, SlotConfig, valid_hex

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


DEFAULT_LED_ON_COLOR = "0000ff"
DEFAULT_LED_OFF_COLOR = "000000"
SELF_LOAD_SENTINEL = "__self_load__"
_ACTION_FIELDS = ("tap_action", "double_tap_action", "hold_action")


def sanitize_device_config(
    config: DeviceConfig, fallback_type: str | None = None
) -> list[str]:
    """
    Repair stored values that would be unsafe to act on; return what changed.

    Stored config can predate input validation, come from a hand-edited
    file, or be replayed from a snapshot. Colors are spliced into firmware
    commands and the card's styles, slot ids into selectors, and sizes into
    layout loops, so each is checked here: invalid colors fall back to the
    defaults, a slot size is clamped to what the device type can hold, slots
    whose id is not an integer 1..6 are dropped, and unknown types are
    cleared. fallback_type is used for the slot limits when the config has
    no type of its own.
    """
    fixes: list[str] = []
    if config.device_type_override not in (None, *DEVICE_TYPES):
        fixes.append(f"device_type_override {config.device_type_override!r}")
        config.device_type_override = None
    if config.device_type and config.device_type not in (
        *DEVICE_TYPES,
        DEVICE_TYPE_OUTLET,
    ):
        fixes.append(f"device_type {config.device_type!r}")
        config.device_type = ""
    for attr in ("faceplate_color", "backlight_color"):
        value = getattr(config, attr)
        if value is None or value == "":
            continue
        clean = valid_hex(value)
        if clean != value:
            if clean is None:
                fixes.append(f"{attr} {value!r}")
            setattr(config, attr, clean)

    allowed = DEVICE_TYPE_SLOTS.get(config.effective_type or fallback_type or "")
    max_slot = max(allowed) if allowed else SLOT_COUNT
    slots: list[SlotConfig] = []
    for slot in config.slots:
        sid = slot.slot_id
        if isinstance(sid, bool) or not isinstance(sid, int):
            fixes.append(f"slot_id {sid!r}")
            continue
        if not 1 <= sid <= SLOT_COUNT:
            fixes.append(f"slot_id {sid!r}")
            continue
        _sanitize_slot(slot, max(1, max_slot - sid + 1), fixes)
        slots.append(slot)
    config.slots = slots
    return fixes


def _sanitize_slot(slot: SlotConfig, max_size: int, fixes: list[str]) -> None:
    """Clamp one slot's size and repair its colors and actions in place."""
    size = slot.size
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        fixes.append(f"slot {slot.slot_id} size {size!r}")
        slot.size = 1
    elif size > max_size:
        fixes.append(f"slot {slot.slot_id} size {size!r}")
        slot.size = max_size
    for attr, default in (
        ("led_on_color", DEFAULT_LED_ON_COLOR),
        ("led_off_color", DEFAULT_LED_OFF_COLOR),
    ):
        value = getattr(slot, attr)
        clean = valid_hex(value)
        if clean is None:
            fixes.append(f"slot {slot.slot_id} {attr} {value!r}")
            setattr(slot, attr, default)
        elif clean != value:
            setattr(slot, attr, clean)
    for attr in _ACTION_FIELDS:
        value = getattr(slot, attr)
        if value not in (None, SELF_LOAD_SENTINEL) and not isinstance(value, dict):
            fixes.append(f"slot {slot.slot_id} {attr}")
            setattr(slot, attr, None)
    if not isinstance(slot.name, str):
        fixes.append(f"slot {slot.slot_id} name")
        slot.name = ""


class Control4Store:
    """Persistent storage for device slot configurations."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the store."""
        # Minor 1.1 -> 1.2 needs no reshaping, so there is no migrate
        # function: Home Assistant hands same-major data through unchanged,
        # and async_load runs every stored value through
        # sanitize_device_config, which is what 1.2 introduced.
        self._store = Store[dict[str, Any]](
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY}.{entry_id}",
            minor_version=STORAGE_MINOR_VERSION,
        )
        self._devices: dict[str, DeviceConfig] = {}
        # Named snapshots of device configs, kept in a separate HA Store so
        # the device store's schema and migration path stay untouched.
        # Shape: {ieee_address: {snapshot_name: config_dict}}.
        self._snapshot_store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, f"{SNAPSHOT_STORAGE_KEY}.{entry_id}"
        )
        self._snapshots: dict[str, dict[str, dict[str, Any]]] = {}

    @property
    def devices(self) -> dict[str, DeviceConfig]:
        """Return all stored device configs keyed by IEEE address."""
        return self._devices

    async def async_load(self) -> None:
        """Load device configs and snapshots from persistent storage."""
        await self._load_snapshots()

        stored = await self._store.async_load()
        self._devices = {}
        if not stored or not isinstance(stored, dict):
            return
        for ieee, data in stored.get("devices", {}).items():
            try:
                self._devices[ieee] = DeviceConfig.from_dict(data)
            except (KeyError, TypeError, ValueError, AttributeError) as err:
                LOGGER.error("Dropping unreadable stored config for %r: %r", ieee, err)

        # One-time migration: convert legacy behavior fields to action dicts
        migrated = False
        for config in self._devices.values():
            for slot in config.slots:
                if _migrate_slot(slot):
                    migrated = True
        if migrated:
            LOGGER.info("Migrated legacy behavior configs to action system")

        repaired = False
        for ieee, config in self._devices.items():
            fixes = sanitize_device_config(config)
            if fixes:
                repaired = True
                LOGGER.warning(
                    "Repaired invalid stored config for %r: %s",
                    ieee,
                    ", ".join(fixes),
                )
        if migrated or repaired:
            await self.async_save()

    async def _load_snapshots(self) -> None:
        """Load named config snapshots from persistent storage."""
        stored = await self._snapshot_store.async_load()
        self._snapshots = {}
        if not stored or not isinstance(stored, dict):
            return
        for ieee, named in stored.get("snapshots", {}).items():
            self._snapshots[ieee] = dict(named)

    async def async_save(self) -> None:
        """Persist device configs to storage."""
        payload = {
            "devices": {
                ieee: config.to_dict() for ieee, config in self._devices.items()
            }
        }
        await self._store.async_save(payload)

    def get_device(self, ieee_address: str) -> DeviceConfig | None:
        """Get a device config by IEEE address."""
        return self._devices.get(ieee_address)

    async def async_save_device(self, config: DeviceConfig) -> None:
        """Save or update a device config."""
        self._devices[config.ieee_address] = config
        await self.async_save()

    async def async_save_snapshots(self) -> None:
        """Persist named config snapshots to storage."""
        payload = {"snapshots": self._snapshots}
        await self._snapshot_store.async_save(payload)

    def get_snapshot(self, ieee_address: str, name: str) -> dict[str, Any] | None:
        """Get a named snapshot config dict for a device, or None."""
        return self._snapshots.get(ieee_address, {}).get(name)

    def list_snapshots(self, ieee_address: str) -> list[str]:
        """Return the sorted snapshot names stored for a device."""
        return sorted(self._snapshots.get(ieee_address, {}))

    async def async_save_snapshot(
        self, ieee_address: str, name: str, config_dict: dict[str, Any]
    ) -> None:
        """Save (or overwrite) a named snapshot for a device."""
        self._snapshots.setdefault(ieee_address, {})[name] = config_dict
        await self.async_save_snapshots()

    async def async_delete_snapshot(self, ieee_address: str, name: str) -> bool:
        """Delete a named snapshot. Returns True if one was removed."""
        named = self._snapshots.get(ieee_address)
        if not named or name not in named:
            return False
        del named[name]
        if not named:
            del self._snapshots[ieee_address]
        await self.async_save_snapshots()
        return True


def _migrate_slot(slot: SlotConfig) -> bool:
    """Migrate a slot to HA-native action format. Returns True if migrated."""
    a = _migrate_behavior(slot)
    b = _migrate_actions(slot)
    c = _migrate_load_actions_to_behavior(slot)
    d = _migrate_led_mode(slot)
    return a or b or c or d


_LOAD_BEHAVIORS = {"load_on", "load_off", "toggle_load"}


def _migrate_behavior(slot: SlotConfig) -> bool:
    """Convert legacy behavior field → HA-native action dicts."""
    if slot.tap_action is not None or not slot.behavior or slot.behavior == "keypad":
        return False
    # Load behaviors are firmware-native — don't convert to software actions
    if slot.behavior in _LOAD_BEHAVIORS:
        return False

    behavior = slot.behavior
    target = slot.target_entity_id

    if behavior == "control_light" and target:
        slot.tap_action = {
            "action": "light.toggle",
            "target": {"entity_id": target},
        }
        slot.led_track_entity_id = target

    slot.behavior = "keypad"
    slot.target_entity_id = None
    return True


def _migrate_actions(slot: SlotConfig) -> bool:
    """Convert intermediate action format to HA-native."""
    migrated = False
    for field in ("tap_action", "double_tap_action", "hold_action"):
        action = getattr(slot, field)
        if not action:
            continue
        action_type = action.get("action", "")
        if action_type in ("fire-event", "none"):
            setattr(slot, field, None)
            migrated = True
        elif action_type == "toggle":
            target_eid = (action.get("target") or {}).get("entity_id", "")
            domain = "light" if target_eid == "__self_load__" else "homeassistant"
            if target_eid and "." in target_eid:
                domain = target_eid.split(".")[0]
            setattr(
                slot,
                field,
                {"action": f"{domain}.toggle", "target": action.get("target", {})},
            )
            migrated = True
        elif action_type == "call-service":
            service = action.get("service", "")
            new_action = {"action": service, "target": action.get("target", {})}
            if action.get("data"):
                new_action["data"] = action["data"]
            setattr(slot, field, new_action)
            migrated = True
    return migrated


def _migrate_load_actions_to_behavior(slot: SlotConfig) -> bool:
    """
    Convert __self_load__ tap_actions back to firmware behaviors.

    Load control should be handled by firmware (fast, reliable) not
    software actions (slow round-trip). Restores the proper behavior
    field and clears the tap_action.
    """
    if not slot.tap_action:
        return False
    target = (slot.tap_action.get("target") or {}).get("entity_id", "")
    if target != "__self_load__":
        return False

    service = slot.tap_action.get("action", "")
    behavior_map = {
        "light.turn_on": "load_on",
        "light.turn_off": "load_off",
        "light.toggle": "toggle_load",
    }
    behavior = behavior_map.get(service)
    if not behavior:
        return False

    slot.behavior = behavior
    slot.tap_action = None
    slot.led_mode = "follow_load"
    return True


def _migrate_led_mode(slot: SlotConfig) -> bool:
    """Ensure LED mode is correct for the slot's behavior."""
    # Load-control buttons should always follow the load
    if slot.behavior in _LOAD_BEHAVIORS and slot.led_mode != "follow_load":
        slot.led_mode = "follow_load"
        return True
    # Programmed without tracking → fixed
    if slot.led_mode == "programmed" and not slot.led_track_entity_id:
        slot.led_mode = "fixed"
        return True
    return False
