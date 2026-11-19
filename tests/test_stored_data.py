"""
Stored configuration is validated again wherever it is used.

Values can reach .storage or a snapshot without passing today's input
checks (an older release, a hand-edited file, a restored snapshot), and
they end up in firmware commands, the card's styles and selectors, and its
layout loops. These tests cover the load-time repair, the restore path,
the strict websocket schema, the command-time color checks, registry-based
entity resolution, and the MQTT base topic check.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.control4_dimmers import (
    _register_websocket_handlers,
    _svc_restore,
)
from custom_components.control4_dimmers.config_flow import valid_base_topic
from custom_components.control4_dimmers.const import (
    CONF_MQTT_TOPIC,
    DOMAIN,
    STORAGE_MINOR_VERSION,
)
from custom_components.control4_dimmers.manager import Control4Manager
from custom_components.control4_dimmers.models import (
    DeviceConfig,
    DeviceState,
    SlotConfig,
)
from custom_components.control4_dimmers.store import (
    Control4Store,
    sanitize_device_config,
)

from .conftest import IEEE_DIMMER, IEEE_KEYPAD, fake_entity_registry

# ── load-time repair ─────────────────────────────────────────────────


def _store_with(data: dict[str, Any]) -> Control4Store:
    with patch("custom_components.control4_dimmers.store.Store") as mock_cls:
        backend = mock_cls.return_value
        backend.async_load = AsyncMock(return_value=data)
        backend.async_save = AsyncMock()
        store = Control4Store(MagicMock(), "entry1")
    store._store = backend
    store._snapshot_store = MagicMock(
        async_load=AsyncMock(return_value=None), async_save=AsyncMock()
    )
    return store


def _raw_device(**overrides: Any) -> dict[str, Any]:
    device = {
        "ieee_address": IEEE_KEYPAD,
        "friendly_name": "Theater",
        "device_type": "keypad",
        "slots": [
            {
                "slot_id": 1,
                "size": 1,
                "name": "One",
                "behavior": "keypad",
                "led_mode": "fixed",
                "led_on_color": "ff0000",
                "led_off_color": "000000",
            }
        ],
    }
    device.update(overrides)
    return device


class TestLoadRepair:
    async def test_store_uses_new_minor_version(self) -> None:
        with patch("custom_components.control4_dimmers.store.Store") as mock_cls:
            Control4Store(MagicMock(), "entry1")
        kwargs = mock_cls.__getitem__.return_value.call_args_list[0].kwargs
        assert kwargs["minor_version"] == STORAGE_MINOR_VERSION == 2

    async def test_clean_config_is_untouched(self) -> None:
        store = _store_with({"devices": {IEEE_KEYPAD: _raw_device()}})
        await store.async_load()
        store._store.async_save.assert_not_awaited()
        assert store.get_device(IEEE_KEYPAD).slots[0].led_on_color == "ff0000"

    @pytest.mark.parametrize(
        "color", ["000 url(https://attacker.example/beacon)", "ff0000 05 0", 5, None]
    )
    async def test_invalid_colors_reset_to_defaults(self, color: Any) -> None:
        raw = _raw_device()
        raw["slots"][0]["led_on_color"] = color
        raw["slots"][0]["led_off_color"] = color
        store = _store_with({"devices": {IEEE_KEYPAD: raw}})
        await store.async_load()
        slot = store.get_device(IEEE_KEYPAD).slots[0]
        assert slot.led_on_color == "0000ff"
        assert slot.led_off_color == "000000"
        store._store.async_save.assert_awaited()

    async def test_colors_are_normalized(self) -> None:
        raw = _raw_device()
        raw["slots"][0]["led_on_color"] = "#FF00AA"
        store = _store_with({"devices": {IEEE_KEYPAD: raw}})
        await store.async_load()
        assert store.get_device(IEEE_KEYPAD).slots[0].led_on_color == "ff00aa"

    @pytest.mark.parametrize(
        ("slot_id", "size", "expected"),
        [(1, 1000000000, 6), (4, 6, 3), (6, 2, 1), (2, 0, 1), (3, "2", 1)],
    )
    async def test_size_clamped_to_the_device(
        self, slot_id: int, size: Any, expected: int
    ) -> None:
        raw = _raw_device()
        raw["slots"][0]["slot_id"] = slot_id
        raw["slots"][0]["size"] = size
        store = _store_with({"devices": {IEEE_KEYPAD: raw}})
        await store.async_load()
        assert store.get_device(IEEE_KEYPAD).slots[0].size == expected

    async def test_dimmer_slot_size_bounded_by_its_last_slot(self) -> None:
        raw = _raw_device(device_type="dimmer")
        raw["slots"][0].update(slot_id=2, size=6, behavior="load_on")
        store = _store_with({"devices": {IEEE_KEYPAD: raw}})
        await store.async_load()
        assert store.get_device(IEEE_KEYPAD).slots[0].size == 4

    @pytest.mark.parametrize("slot_id", ['1"]x', "1", 1.5, True, 0, 7, -1, None])
    async def test_bad_slot_ids_dropped(self, slot_id: Any) -> None:
        raw = _raw_device()
        raw["slots"].append({**raw["slots"][0], "slot_id": slot_id})
        store = _store_with({"devices": {IEEE_KEYPAD: raw}})
        await store.async_load()
        assert [s.slot_id for s in store.get_device(IEEE_KEYPAD).slots] == [1]

    async def test_bad_types_and_device_colors_cleared(self) -> None:
        raw = _raw_device(
            device_type="constructor",
            device_type_override="__proto__",
            faceplate_color="red",
            backlight_color="0080FF",
        )
        store = _store_with({"devices": {IEEE_KEYPAD: raw}})
        await store.async_load()
        config = store.get_device(IEEE_KEYPAD)
        assert config.device_type == ""
        assert config.device_type_override is None
        assert config.faceplate_color is None
        assert config.backlight_color == "0080ff"

    async def test_unreadable_device_dropped_not_fatal(self) -> None:
        broken = {"friendly_name": "No IEEE"}
        store = _store_with(
            {"devices": {"0xbroken": broken, IEEE_KEYPAD: _raw_device()}}
        )
        await store.async_load()
        assert set(store.devices) == {IEEE_KEYPAD}

    def test_non_dict_action_cleared(self) -> None:
        config = DeviceConfig(
            ieee_address=IEEE_KEYPAD,
            friendly_name="Theater",
            device_type="keypad",
            slots=[
                SlotConfig(slot_id=1, tap_action=["x"], hold_action="__self_load__")
            ],  # type: ignore[arg-type]
        )
        fixes = sanitize_device_config(config)
        assert config.slots[0].tap_action is None
        assert config.slots[0].hold_action == "__self_load__"
        assert fixes


# ── restore ──────────────────────────────────────────────────────────


def _runtime(mock_hass: MagicMock) -> tuple[Control4Manager, Control4Store]:
    entry = MagicMock()
    entry.data = {"mqtt_topic": "zigbee2mqtt"}
    entry.options = {}
    with patch("custom_components.control4_dimmers.store.Store"):
        store = Control4Store(mock_hass, "entry1")
    store._store.async_save = AsyncMock()
    store._snapshot_store.async_save = AsyncMock()
    mgr = Control4Manager(mock_hass, entry, store)
    mgr._devices[IEEE_KEYPAD] = DeviceState(
        ieee_address=IEEE_KEYPAD, friendly_name="Theater", device_type="keypad"
    )
    mgr._push_slot_config = AsyncMock()
    mock_hass.data = {DOMAIN: {"entry1": {"manager": mgr, "store": store}}}
    return mgr, store


class TestRestoreValidation:
    async def test_restore_repairs_a_bad_snapshot(self, mock_hass: MagicMock) -> None:
        _, store = _runtime(mock_hass)
        store._snapshots[IEEE_KEYPAD] = {
            "old": {
                "device_type_override": "constructor",
                "faceplate_color": "red",
                "slots": [
                    {"slot_id": 1, "size": 99, "led_on_color": "zz"},
                    {"slot_id": '1"]x'},
                ],
            }
        }
        call = MagicMock()
        call.data = {"ieee_address": IEEE_KEYPAD, "name": "old"}
        await _svc_restore(mock_hass, call)
        config = store.get_device(IEEE_KEYPAD)
        assert [s.slot_id for s in config.slots] == [1]
        assert config.slots[0].size == 6
        assert config.slots[0].led_on_color == "0000ff"
        assert config.device_type_override is None
        assert config.faceplate_color is None

    async def test_malformed_snapshot_raises(self, mock_hass: MagicMock) -> None:
        from homeassistant.exceptions import ServiceValidationError  # noqa: PLC0415

        _, store = _runtime(mock_hass)
        store._snapshots[IEEE_KEYPAD] = {"bad": {"slots": [{"name": "no id"}]}}
        call = MagicMock()
        call.data = {"ieee_address": IEEE_KEYPAD, "name": "bad"}
        with pytest.raises(ServiceValidationError):
            await _svc_restore(mock_hass, call)


# ── websocket device_config ──────────────────────────────────────────


@pytest.fixture
def ws_manager(hass: HomeAssistant) -> MagicMock:
    mgr = MagicMock(spec=Control4Manager)
    mgr.devices = {
        IEEE_KEYPAD: DeviceState(
            ieee_address=IEEE_KEYPAD, friendly_name="Theater", device_type="keypad"
        ),
        IEEE_DIMMER: DeviceState(
            ieee_address=IEEE_DIMMER, friendly_name="Kitchen", device_type="dimmer"
        ),
    }
    mgr.store = MagicMock(spec=Control4Store)
    mgr.store.get_device.return_value = None
    mgr.get_device_info.return_value = {}
    hass.data[DOMAIN] = {"entry1": {"manager": mgr}}
    return mgr


async def _send(hass_ws_client, hass: HomeAssistant, **msg: Any) -> dict:
    await _register_websocket_handlers(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/device_config", **msg})
    return await client.receive_json()


@pytest.mark.parametrize(
    ("ieee", "msg"),
    [
        (IEEE_KEYPAD, {"slots": [{"slot_id": 1, "size": 1000000000}]}),
        (IEEE_KEYPAD, {"slots": [{"slot_id": 5, "size": 3}]}),
        (IEEE_KEYPAD, {"slots": [{"slot_id": '1"]x'}]}),
        (IEEE_KEYPAD, {"slots": [{"slot_id": 7}]}),
        (IEEE_DIMMER, {"slots": [{"slot_id": 1}]}),
        (IEEE_KEYPAD, {"slots": [{"slot_id": 1, "behavior": "explode"}]}),
        (IEEE_KEYPAD, {"slots": [{"slot_id": 1, "tap_action": {"action": "x"}}]}),
        (IEEE_KEYPAD, {"slots": ["not a slot"]}),
        (IEEE_KEYPAD, {"device_type_override": "constructor"}),
        (IEEE_KEYPAD, {"faceplate_color": "red; background:url(x)"}),
    ],
)
async def test_ws_device_config_rejects(
    hass: HomeAssistant,
    hass_ws_client,
    ws_manager: MagicMock,
    ieee: str,
    msg: dict[str, Any],
) -> None:
    resp = await _send(hass_ws_client, hass, ieee_address=ieee, **msg)
    assert resp["success"] is False
    assert resp["error"]["code"] == "invalid_format"
    ws_manager.async_configure_device.assert_not_called()


async def test_ws_device_config_accepts_the_cards_slot_shape(
    hass: HomeAssistant, hass_ws_client, ws_manager: MagicMock
) -> None:
    card_slot = {
        "slot_id": 1,
        "size": 2,
        "name": "Lights",
        "behavior": "keypad",
        "led_mode": "programmed",
        "led_on_color": "FFFFFF",
        "led_off_color": "#000000",
        "led_track_entity_id": "light.kitchen",
        "target_entity_id": None,
        "tap_action": {"action": "light.toggle", "target": {"entity_id": "light.x"}},
        "double_tap_action": None,
        "something_new": True,
    }
    resp = await _send(
        hass_ws_client,
        hass,
        ieee_address=IEEE_KEYPAD,
        slots=[card_slot],
        faceplate_color="adb2bd",
    )
    assert resp["success"] is True
    kwargs = ws_manager.async_configure_device.await_args.kwargs
    [saved] = kwargs["slots"]
    assert "something_new" not in saved
    assert saved["led_on_color"] == "ffffff"
    assert saved["led_mode"] == "programmed"
    assert kwargs["faceplate_color"] == "adb2bd"


async def test_ws_device_config_uses_override_for_slot_limits(
    hass: HomeAssistant, hass_ws_client, ws_manager: MagicMock
) -> None:
    """A dimmer re-typed as a keypad in the same save may use slot 1."""
    resp = await _send(
        hass_ws_client,
        hass,
        ieee_address=IEEE_DIMMER,
        device_type_override="keypad",
        slots=[{"slot_id": 1}],
    )
    assert resp["success"] is True


# ── colors re-checked where commands are built ───────────────────────


def _cmds(send: AsyncMock) -> list[str]:
    return [c.args[1].get("c4_cmd", "") for c in send.await_args_list]


class TestCommandColors:
    async def test_invalid_slot_colors_never_reach_c4_cmd(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        slot = SlotConfig(
            slot_id=1,
            led_mode="fixed",
            led_on_color="ff0000 05 000000",
            led_off_color="zz",
        )
        send = AsyncMock()
        with patch.object(manager, "async_send_mqtt", new=send):
            await manager._send_slot_commands(keypad_state, slot)
        cmds = _cmds(send)
        assert not any("ff0000 05" in c or "zz" in c for c in cmds)
        assert not any(
            c.startswith(("c4.dmx.led 00 03", "c4.dmx.led 00 05")) for c in cmds
        )

    async def test_valid_colors_still_sent(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        slot = SlotConfig(slot_id=2, led_mode="fixed", led_on_color="FF0000")
        send = AsyncMock()
        with patch.object(manager, "async_send_mqtt", new=send):
            await manager._send_slot_commands(keypad_state, slot)
        assert "c4.dmx.led 01 03 ff0000" in _cmds(send)
        assert "c4.dmx.led 01 05 000000" in _cmds(send)

    async def test_invalid_backlight_not_sent(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        config = DeviceConfig(
            ieee_address=IEEE_KEYPAD,
            friendly_name="Theater",
            backlight_color="0080ff 05 x",
        )
        send = AsyncMock()
        with patch.object(manager, "async_send_mqtt", new=send):
            await manager._send_backlight(keypad_state, config)
        send.assert_not_awaited()

    async def test_invalid_tracked_color_not_sent(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        send = AsyncMock()
        with patch.object(manager, "async_send_mqtt", new=send):
            assert await manager._send_tracked_led(IEEE_KEYPAD, 1, "x y") is False
        send.assert_not_awaited()

    def test_tracking_skips_invalid_colors(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        manager._store._devices[IEEE_KEYPAD] = DeviceConfig(
            ieee_address=IEEE_KEYPAD,
            friendly_name="Theater",
            slots=[
                SlotConfig(slot_id=1, led_track_entity_id="light.a", led_on_color="x"),
                SlotConfig(slot_id=2, led_track_entity_id="light.b"),
            ],
        )
        manager.setup_light_tracking()
        assert set(manager._led_tracking) == {"light.b"}


# ── load entity resolution ───────────────────────────────────────────


class TestLoadEntity:
    def _light(self, entity_id: str, name: str) -> MagicMock:
        state = MagicMock()
        state.entity_id = entity_id
        state.attributes = {"friendly_name": name}
        return state

    def test_other_integrations_light_with_same_name_ignored(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        manager._devices[IEEE_DIMMER] = dimmer_state
        fake_entity_registry(manager._hass).add(
            "light.kitchen_hue", "hue-1", platform="hue"
        )
        manager._hass.states.async_all.return_value = [
            self._light("light.kitchen_hue", "Kitchen")
        ]
        assert manager._find_light_entity(IEEE_DIMMER) is None

    def test_mqtt_light_with_same_name_used(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        manager._devices[IEEE_DIMMER] = dimmer_state
        manager._hass.states.async_all.return_value = [
            self._light("light.kitchen", "Kitchen")
        ]
        assert manager._find_light_entity(IEEE_DIMMER) == "light.kitchen"
        assert manager.get_device_info(IEEE_DIMMER)["load_entity_id"] == (
            "light.kitchen"
        )

    def test_switch_uses_its_own_switch_entity(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        dimmer_state.device_type = "switch"
        manager._devices[IEEE_DIMMER] = dimmer_state
        fake_entity_registry(manager._hass).add(
            "switch.kitchen_load", f"{IEEE_DIMMER}_switch"
        )
        manager._hass.states.async_all.return_value = []
        assert manager.load_entity_id(IEEE_DIMMER) == "switch.kitchen_load"


async def test_z2m_device_light_found_through_the_registries(
    hass: HomeAssistant,
) -> None:
    from homeassistant.helpers import device_registry as dr  # noqa: PLC0415
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        identifiers={("mqtt", f"zigbee2mqtt_{IEEE_DIMMER}")},
    )
    er.async_get(hass).async_get_or_create(
        "light",
        "mqtt",
        f"{IEEE_DIMMER}_light_zigbee2mqtt",
        device_id=device.id,
        suggested_object_id="renamed_in_ha",
    )
    entry = MagicMock()
    entry.data = {"mqtt_topic": "zigbee2mqtt"}
    entry.options = {}
    with patch("custom_components.control4_dimmers.store.Store"):
        store = Control4Store(hass, "entry1")
    mgr = Control4Manager(hass, entry, store)
    mgr._devices[IEEE_DIMMER] = DeviceState(
        ieee_address=IEEE_DIMMER, friendly_name="Kitchen", device_type="dimmer"
    )
    assert mgr._find_light_entity(IEEE_DIMMER) == "light.renamed_in_ha"


# ── MQTT base topic ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("topic", "ok"),
    [
        ("zigbee2mqtt", True),
        ("home/zigbee2mqtt", True),
        ("", False),
        ("   ", False),
        ("zigbee2mqtt/#", False),
        ("zigbee2mqtt/+", False),
        ("/zigbee2mqtt", False),
        ("zigbee2mqtt/", False),
        ("zig\0bee", False),
        (None, False),
    ],
)
def test_valid_base_topic(topic: Any, ok: bool) -> None:  # noqa: FBT001
    assert valid_base_topic(topic) is ok


@pytest.fixture
def _custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations."""


@pytest.mark.usefixtures("_custom_integrations")
async def test_user_flow_rejects_wildcard_topic(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_MQTT_TOPIC: "zigbee2mqtt/#"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MQTT_TOPIC: "invalid_topic"}


@pytest.mark.usefixtures("_custom_integrations")
async def test_options_flow_rejects_trailing_slash(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_MQTT_TOPIC: "zigbee2mqtt"})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.control4_dimmers.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_MQTT_TOPIC: "zigbee2mqtt/",
                "audit_interval": 10,
                "audit_mode": "heal",
            },
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MQTT_TOPIC: "invalid_topic"}
