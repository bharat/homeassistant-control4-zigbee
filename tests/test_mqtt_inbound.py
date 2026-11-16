"""
Inbound MQTT is untrusted input.

Any client that can publish to the broker can write a device's state topic
or the bridge/devices list. These tests feed forged, replayed and malformed
messages into the manager and check that they cannot run actions, poison
entity state, remove devices, or grow memory without bound.
"""

from __future__ import annotations

import json
import math
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.control4_dimmers.const import DOMAIN
from custom_components.control4_dimmers.manager import (
    ACTION_RATE_LIMIT,
    PENDING_STATES_MAX,
    Control4Manager,
)
from custom_components.control4_dimmers.models import (
    DeviceConfig,
    DeviceState,
    SlotConfig,
)
from custom_components.control4_dimmers.sensor import (
    async_setup_entry as sensor_async_setup_entry,
)

from .conftest import IEEE_DIMMER, IEEE_KEYPAD, make_bridge_device


def _msg(topic: str, payload: Any, *, retain: bool = False) -> MagicMock:
    msg = MagicMock()
    msg.topic = topic
    msg.payload = json.dumps(payload) if isinstance(payload, (dict, list)) else payload
    msg.retain = retain
    return msg


def _devices_msg(*entries: dict[str, Any]) -> MagicMock:
    return _msg("zigbee2mqtt/bridge/devices", list(entries))


def _close_tasks(manager: Control4Manager) -> list[str]:
    """Make async_create_task close the coroutine and record the task name."""
    names: list[str] = []

    def _create(coro: Any, name: str | None = None) -> None:
        coro.close()
        names.append(name or "")

    manager._hass.async_create_task = _create
    return names


# ── retained messages ────────────────────────────────────────────────


class TestRetained:
    async def test_retained_action_does_not_run(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        tasks = _close_tasks(manager)
        cb = MagicMock()
        manager.register_event_callback(IEEE_KEYPAD, 1, cb)
        await manager._handle_device_state(
            _msg("zigbee2mqtt/Theater", {"action": "button_1_press"}, retain=True)
        )
        cb.assert_not_called()
        assert tasks == []

    async def test_retained_state_is_still_applied(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        await manager._handle_device_state(
            _msg("zigbee2mqtt/Theater", {"state": "ON"}, retain=True)
        )
        assert keypad_state.state == "ON"

    async def test_retained_message_does_not_resolve_verify(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        future = MagicMock()
        future.done.return_value = False
        manager._pending_verifies[(IEEE_KEYPAD, 1)] = future
        await manager._handle_device_state(
            _msg("zigbee2mqtt/Theater", {"c4_verified_slot": 1}, retain=True)
        )
        future.set_result.assert_not_called()

    async def test_live_action_still_runs(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        tasks = _close_tasks(manager)
        await manager._handle_device_state(
            _msg("zigbee2mqtt/Theater", {"action": "button_1_press"})
        )
        assert len(tasks) == 1


# ── action grammar ───────────────────────────────────────────────────


class TestActionGrammar:
    @pytest.mark.parametrize(
        "action",
        [
            "button_1_pressx",
            "xbutton_1_press",
            "button_1_press button_2_press",
            "button_7_press",
            "button_0_press",
            "button_" + "9" * 50 + "_press",
            "button_1_click_2junk",
            "button_1_click_0",
            "button_1_releasex",
            "paddle_up_pressx",
            "paddle_sideways_press",
        ],
    )
    def test_malformed_action_fires_nothing(
        self, manager: Control4Manager, keypad_state: DeviceState, action: str
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        tasks = _close_tasks(manager)
        cbs = {}
        for slot in (1, 2, 3, 4, 5, 6, "paddle_up", "paddle_down"):
            cbs[slot] = MagicMock()
            manager.register_event_callback(IEEE_KEYPAD, slot, cbs[slot])
        manager._dispatch_button_action(keypad_state, action)
        for cb in cbs.values():
            cb.assert_not_called()
        assert tasks == []
        assert manager._press_pairs == {}

    def test_non_string_action_ignored(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        manager._dispatch_button_action(keypad_state, 5)  # type: ignore[arg-type]

    def test_button_outside_device_slots_ignored(
        self, manager: Control4Manager
    ) -> None:
        """The outlet has one button; a report for button 3 matches nothing."""
        outlet = DeviceState(
            ieee_address=IEEE_DIMMER, friendly_name="Outlet", device_type="outlet"
        )
        manager._devices[IEEE_DIMMER] = outlet
        tasks = _close_tasks(manager)
        manager._dispatch_button_action(outlet, "button_3_press")
        assert tasks == []
        assert manager._press_pairs == {}

    def test_click_above_four_still_clamps(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        """The converter passes click_5+ through (issue #178); keep clamping."""
        manager._devices[IEEE_KEYPAD] = keypad_state
        cb = MagicMock()
        manager.register_event_callback(IEEE_KEYPAD, 1, cb)
        manager._dispatch_button_action(keypad_state, "button_1_click_5")
        assert cb.call_args.args[0] == "triple_tap"


# ── rate limit ───────────────────────────────────────────────────────


class TestRateLimit:
    def test_flood_of_presses_is_capped(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        tasks = _close_tasks(manager)
        with patch(
            "custom_components.control4_dimmers.manager.time.monotonic",
            return_value=100.0,
        ):
            for _ in range(50):
                manager._dispatch_button_action(keypad_state, "button_1_press")
        assert len(tasks) == ACTION_RATE_LIMIT

    def test_limit_is_per_slot_and_recovers(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        tasks = _close_tasks(manager)
        clock = [100.0]
        with patch(
            "custom_components.control4_dimmers.manager.time.monotonic",
            side_effect=lambda: clock[0],
        ):
            for _ in range(10):
                manager._dispatch_button_action(keypad_state, "button_1_press")
            manager._dispatch_button_action(keypad_state, "button_2_press")
            assert len(tasks) == ACTION_RATE_LIMIT + 1
            clock[0] += 5
            manager._dispatch_button_action(keypad_state, "button_1_press")
        assert len(tasks) == ACTION_RATE_LIMIT + 2


# ── listeners ────────────────────────────────────────────────────────


class TestListenerIsolation:
    def test_one_failing_listener_does_not_stop_the_rest(
        self, manager: Control4Manager
    ) -> None:
        bad = MagicMock(side_effect=ValueError("non-numeric sensor value"))
        good = MagicMock()
        manager.add_listener(bad)
        manager.add_listener(good)
        manager.notify_listeners()
        good.assert_called_once()

    def test_listener_may_unsubscribe_while_notified(
        self, manager: Control4Manager
    ) -> None:
        calls: list[str] = []
        unsub: list = []

        def _once() -> None:
            calls.append("once")
            unsub[0]()

        unsub.append(manager.add_listener(_once))
        manager.add_listener(lambda: calls.append("other"))
        manager.notify_listeners()
        assert calls == ["once", "other"]


# ── value validation ─────────────────────────────────────────────────


class TestValueValidation:
    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("c4_load_power", math.nan),
            ("c4_load_power", math.inf),
            ("c4_load_power", "12"),
            ("c4_load_voltage", True),
            ("ambient_light", "x"),
            ("ambient_light", -math.inf),
            ("c4_tv_01", "fast"),
            ("brightness", 999),
            ("brightness", -1),
            ("brightness", "200"),
            ("brightness", 12.5),
            ("state", "maybe"),
            ("state", 1),
            ("action", 5),
            ("action", "x" * 500),
            ("c4_device_type", "constructor"),
            ("c4_device_type", "__proto__"),
            ("c4_device_type", ["keypad"]),
            ("c4_led_1_on", "zzzzzz"),
            ("c4_led_1_on", "ff0000 05 000000"),
            ("c4_led_1_on", 5),
            ("color_button_1_on", {"hue": "x", "saturation": 100}),
            ("color_button_1_on", {"hue": math.inf, "saturation": 100}),
            ("some_text", "y" * 5000),
            ("Bad Key", 1),
        ],
    )
    def test_garbage_is_dropped(self, key: str, value: Any) -> None:
        state = DeviceState(ieee_address=IEEE_DIMMER, friendly_name="Kitchen")
        state.update_from_mqtt({key: value})
        assert key not in state.raw
        assert state.brightness is None
        assert state.state is None
        assert state.device_type is None
        assert state.led_colors == {}

    def test_good_values_are_kept(self) -> None:
        state = DeviceState(ieee_address=IEEE_DIMMER, friendly_name="Kitchen")
        state.update_from_mqtt(
            {
                "state": "on",
                "brightness": 0,
                "c4_load_power": 12.5,
                "ambient_light": 3,
                "c4_device_type": "keypad",
                "c4_led_1_on": "#FF0000",
                "color_button_2_on": {"hue": 240, "saturation": 100},
                "linkquality": 120,
                "c4_detect_result": {"dim_code": "02"},
            }
        )
        assert state.state == "ON"
        assert state.brightness == 0
        assert state.raw["c4_load_power"] == 12.5
        assert state.device_type == "keypad"
        assert state.led_colors[1]["on"] == "ff0000"
        assert state.led_colors[2]["on"] == "0000ff"
        assert state.dim_code == "02"

    def test_raw_is_capped(self) -> None:
        state = DeviceState(ieee_address=IEEE_DIMMER, friendly_name="Kitchen")
        for i in range(50):
            state.update_from_mqtt({f"junk_{i}_{j}": j for j in range(20)})
        assert len(state.raw) <= 200

    async def test_nan_payload_through_the_handler(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        """json.loads accepts NaN and Infinity; they never reach an entity."""
        manager._devices[IEEE_DIMMER] = dimmer_state
        msg = _msg(
            "zigbee2mqtt/Kitchen",
            '{"c4_load_power": NaN, "ambient_light": Infinity, "action": 5}',
        )
        await manager._handle_device_state(msg)
        assert "c4_load_power" not in dimmer_state.raw
        assert "ambient_light" not in dimmer_state.raw

    @pytest.mark.parametrize("slot", [math.inf, "x", 10**400, None, True])
    async def test_bad_verified_slot_is_harmless(
        self, manager: Control4Manager, dimmer_state: DeviceState, slot: Any
    ) -> None:
        manager._devices[IEEE_DIMMER] = dimmer_state
        manager._resolve_verify(IEEE_DIMMER, slot, {"c4_verified_slot": slot})
        assert manager._verify_reads == {}

    def test_verify_read_kept_only_when_pending(self, manager: Control4Manager) -> None:
        for slot in range(1, 50):
            manager._resolve_verify(IEEE_DIMMER, slot, {"c4_verified_slot": slot})
        assert manager._verify_reads == {}


# ── bridge/devices ───────────────────────────────────────────────────


class TestDeviceList:
    async def test_forged_empty_list_does_not_drop_devices_at_once(
        self, manager: Control4Manager
    ) -> None:
        await manager._handle_bridge_devices(_devices_msg(make_bridge_device()))
        await manager._handle_bridge_devices(_devices_msg())
        assert IEEE_DIMMER in manager.devices

    async def test_name_already_used_by_another_device_rejected(
        self, manager: Control4Manager
    ) -> None:
        await manager._handle_bridge_devices(
            _devices_msg(make_bridge_device(ieee=IEEE_DIMMER, friendly_name="Kitchen"))
        )
        await manager._handle_bridge_devices(
            _devices_msg(
                make_bridge_device(ieee=IEEE_DIMMER, friendly_name="Kitchen"),
                make_bridge_device(ieee=IEEE_KEYPAD, friendly_name="Kitchen"),
            )
        )
        assert IEEE_KEYPAD not in manager.devices
        assert manager.devices[IEEE_DIMMER].friendly_name == "Kitchen"

    async def test_forged_name_swap_rejected(self, manager: Control4Manager) -> None:
        await manager._handle_bridge_devices(
            _devices_msg(
                make_bridge_device(ieee=IEEE_DIMMER, friendly_name="Kitchen"),
                make_bridge_device(ieee=IEEE_KEYPAD, friendly_name="Theater"),
            )
        )
        with patch("custom_components.control4_dimmers.manager.dr.async_get"):
            await manager._handle_bridge_devices(
                _devices_msg(
                    make_bridge_device(ieee=IEEE_DIMMER, friendly_name="Theater"),
                    make_bridge_device(ieee=IEEE_KEYPAD, friendly_name="Kitchen"),
                )
            )
        assert manager.devices[IEEE_DIMMER].friendly_name == "Kitchen"
        assert manager.devices[IEEE_KEYPAD].friendly_name == "Theater"

    @pytest.mark.parametrize(
        "ieee", ["0xIKEA", "0x000FFF0000AAA001", "0x000fff0000aaa0011", 5, None]
    )
    async def test_invalid_ieee_ignored(
        self, manager: Control4Manager, ieee: Any
    ) -> None:
        entry = make_bridge_device()
        entry["ieee_address"] = ieee
        await manager._handle_bridge_devices(_devices_msg(entry))
        assert manager.devices == {}

    @pytest.mark.parametrize(
        "name", ["Kitchen/#", "Kit+chen", "Line\nINFO forged", "", 5, "x" * 500]
    )
    async def test_invalid_name_ignored(
        self, manager: Control4Manager, name: Any
    ) -> None:
        await manager._handle_bridge_devices(
            _devices_msg(make_bridge_device(friendly_name=name))
        )
        assert manager.devices == {}

    async def test_invalid_rename_keeps_known_device(
        self, manager: Control4Manager
    ) -> None:
        await manager._handle_bridge_devices(_devices_msg(make_bridge_device()))
        await manager._handle_bridge_devices(
            _devices_msg(make_bridge_device(friendly_name="a+b"))
        )
        await manager._handle_bridge_devices(
            _devices_msg(make_bridge_device(friendly_name="a+b"))
        )
        assert manager.devices[IEEE_DIMMER].friendly_name == "Kitchen"

    async def test_null_fields_do_not_drop_the_list(
        self, manager: Control4Manager
    ) -> None:
        odd = {
            "ieee_address": IEEE_KEYPAD,
            "friendly_name": "Odd",
            "manufacturer": None,
            "model_id": None,
            "definition": None,
        }
        await manager._handle_bridge_devices(_devices_msg(odd, make_bridge_device()))
        assert IEEE_DIMMER in manager.devices

    async def test_send_to_unknown_device_raises(
        self, manager: Control4Manager
    ) -> None:
        with pytest.raises(HomeAssistantError):
            await manager.async_send_mqtt(IEEE_DIMMER, {"state": "ON"})
        assert await manager.async_try_send_mqtt(IEEE_DIMMER, {"x": 1}, "test") is False

    async def test_removal_prunes_per_device_bookkeeping(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        manager._devices[IEEE_DIMMER] = dimmer_state
        manager._detect_sent.add(IEEE_DIMMER)
        manager._dimming_requested.add(IEEE_DIMMER)
        manager._press_pairs[(IEEE_DIMMER, 2)] = ("press", 1.0)
        manager._allow_action(IEEE_DIMMER, 2)
        manager._led_cooldowns[(IEEE_DIMMER, 2)] = 1.0
        await manager._handle_bridge_devices(_devices_msg())
        await manager._handle_bridge_devices(_devices_msg())
        assert IEEE_DIMMER not in manager.devices
        assert manager._detect_sent == set()
        assert manager._dimming_requested == set()
        assert manager._press_pairs == {}
        assert manager._action_times == {}
        assert manager._led_cooldowns == {}


# ── pending (pre-discovery) state ────────────────────────────────────


class TestPendingStates:
    async def test_buffer_is_capped(self, manager: Control4Manager) -> None:
        for i in range(PENDING_STATES_MAX + 25):
            await manager._handle_device_state(
                _msg(f"zigbee2mqtt/junk_{i}", {"state": "ON"})
            )
        assert len(manager._pending_states) == PENDING_STATES_MAX

    async def test_no_buffering_after_first_list(
        self, manager: Control4Manager
    ) -> None:
        await manager._handle_device_state(_msg("zigbee2mqtt/Lamp", {"state": "ON"}))
        await manager._handle_bridge_devices(_devices_msg(make_bridge_device()))
        assert manager._pending_states == {}
        await manager._handle_device_state(_msg("zigbee2mqtt/Lamp", {"state": "ON"}))
        assert manager._pending_states == {}

    async def test_bad_buffered_payload_does_not_break_the_list(
        self, manager: Control4Manager
    ) -> None:
        await manager._handle_device_state(_msg("zigbee2mqtt/Kitchen", {"state": "ON"}))
        listener = MagicMock()
        manager.add_listener(listener)
        with patch.object(
            DeviceState, "update_from_mqtt", side_effect=TypeError("bad payload")
        ):
            await manager._handle_bridge_devices(_devices_msg(make_bridge_device()))
        assert manager._pending_states == {}
        listener.assert_called()
        # The next list is processed normally.
        await manager._handle_bridge_devices(_devices_msg(make_bridge_device()))
        assert IEEE_DIMMER in manager.devices


# ── types from MQTT ──────────────────────────────────────────────────


class TestLiveTypes:
    async def test_live_type_does_not_replace_stored_type(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        manager._store._devices[IEEE_KEYPAD] = DeviceConfig(
            ieee_address=IEEE_KEYPAD,
            friendly_name="Theater",
            device_type="keypad",
            slots=[SlotConfig(slot_id=1)],
        )
        manager._store._store.async_save = AsyncMock()
        keypad_state.update_from_mqtt({"c4_device_type": "dimmer"})
        with patch.object(manager, "_push_slot_config", new=AsyncMock()):
            await manager.async_configure_device(IEEE_KEYPAD, faceplate_color="fff")
        assert manager.store.get_device(IEEE_KEYPAD).device_type == "keypad"

    async def test_live_type_seeds_a_new_config(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        manager._store._store.async_save = AsyncMock()
        with patch.object(manager, "_push_slot_config", new=AsyncMock()):
            await manager.async_configure_device(IEEE_KEYPAD)
        assert manager.store.get_device(IEEE_KEYPAD).device_type == "keypad"

    async def test_no_load_sensors_for_a_keypad(
        self, manager: Control4Manager, keypad_state: DeviceState
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        keypad_state.update_from_mqtt({"c4_load_power": 5})
        hass = MagicMock()
        registry = MagicMock()
        registry.async_get_entity_id.return_value = None
        hass.data = {
            DOMAIN: {"entry": {"manager": manager}},
            er.DATA_REGISTRY: registry,
        }
        added: list = []
        await sensor_async_setup_entry(hass, MagicMock(entry_id="entry"), added.extend)
        assert not any("c4_load" in (e.unique_id or "") for e in added)
