"""
Admin-only enforcement for websocket commands and services.

Slot actions are stored as arbitrary ``domain.service`` strings that the
manager later runs through ``hass.services.async_call`` on a button
press, so anything that writes device configuration, restores it, or
sends raw commands to a device must be limited to administrators. These
tests run the real registration code against a real HomeAssistant
instance and exercise each entry point as a read-only user and as an
admin.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.service import async_register_admin_service
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.control4_dimmers import (
    _register_services,
    _register_websocket_handlers,
)
from custom_components.control4_dimmers.const import DOMAIN
from custom_components.control4_dimmers.manager import Control4Manager
from custom_components.control4_dimmers.models import (
    DeviceConfig,
    DeviceState,
    SlotConfig,
)
from custom_components.control4_dimmers.store import Control4Store

IEEE = "0x000fff0000ccc001"
ENTITY_ID = "sensor.theater"
EVENT_ID = "event.theater_button_1"


@pytest.fixture
def manager(hass: HomeAssistant) -> MagicMock:
    """Wire a spec'd manager mock into hass.data and seed one keypad."""
    mgr = MagicMock(spec=Control4Manager)
    mgr.devices = {
        IEEE: DeviceState(
            ieee_address=IEEE, friendly_name="Theater", device_type="keypad"
        )
    }
    mgr.store = MagicMock(spec=Control4Store)
    mgr.store.get_device.return_value = DeviceConfig(
        ieee_address=IEEE,
        friendly_name="Theater",
        device_type="keypad",
        slots=[SlotConfig(slot_id=1, name="One", behavior="keypad", led_mode="fixed")],
    )
    mgr.store.get_snapshot.return_value = {"slots": []}
    mgr.store.list_snapshots.return_value = []
    mgr.get_device_info.return_value = {"ieee_address": IEEE}
    mgr.async_push_config.return_value = True
    mgr.async_audit_device.return_value = {"ok": True}
    hass.data[DOMAIN] = {"entry1": {"manager": mgr}}
    hass.states.async_set(ENTITY_ID, "keypad", {"ieee_address": IEEE, "slot_id": 1})
    return mgr


# ── websocket commands ───────────────────────────────────────────────

WS_ADMIN_ONLY: list[dict[str, Any]] = [
    {"type": f"{DOMAIN}/device_config", "ieee_address": IEEE, "slots": []},
    {"type": f"{DOMAIN}/send_mqtt", "ieee_address": IEEE, "payload": {"state": "ON"}},
]

WS_OPEN: list[dict[str, Any]] = [
    {"type": f"{DOMAIN}/version"},
    {"type": f"{DOMAIN}/devices"},
    {"type": f"{DOMAIN}/device_by_entity", "entity_id": ENTITY_ID},
    {"type": f"{DOMAIN}/event_entities", "ieee_address": IEEE},
]


@pytest.mark.parametrize("msg", WS_ADMIN_ONLY, ids=lambda m: m["type"])
async def test_ws_rejects_non_admin(
    hass: HomeAssistant,
    hass_ws_client,
    hass_read_only_access_token: str,
    manager: MagicMock,
    msg: dict[str, Any],
) -> None:
    await _register_websocket_handlers(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, **msg})
    resp = await client.receive_json()
    assert resp["success"] is False
    assert resp["error"]["code"] == "unauthorized"
    manager.async_configure_device.assert_not_called()
    manager.async_send_mqtt.assert_not_called()


@pytest.mark.parametrize("msg", WS_ADMIN_ONLY, ids=lambda m: m["type"])
async def test_ws_accepts_admin(
    hass: HomeAssistant,
    hass_ws_client,
    manager: MagicMock,
    msg: dict[str, Any],
) -> None:
    await _register_websocket_handlers(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, **msg})
    resp = await client.receive_json()
    assert resp["success"] is True
    if msg["type"].endswith("device_config"):
        manager.async_configure_device.assert_awaited_once()
    else:
        manager.async_send_mqtt.assert_awaited_once_with(IEEE, {"state": "ON"})


@pytest.mark.parametrize("msg", WS_OPEN, ids=lambda m: m["type"])
async def test_ws_read_only_commands_stay_open(
    hass: HomeAssistant,
    hass_ws_client,
    hass_read_only_access_token: str,
    manager: MagicMock,
    msg: dict[str, Any],
) -> None:
    manager.get_all_devices_info.return_value = []
    await _register_websocket_handlers(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, **msg})
    resp = await client.receive_json()
    assert resp["success"] is True


# ── services ─────────────────────────────────────────────────────────

SVC_ADMIN_ONLY: list[tuple[str, dict[str, Any]]] = [
    ("set_device_config", {"ieee_address": IEEE, "device_type_override": "keypad"}),
    ("set_slot", {"ieee_address": IEEE, "slot_id": 1, "name": "Renamed"}),
    ("set_device_type", {"entity_id": ENTITY_ID, "device_type": "keypad"}),
    (
        "send_raw_command",
        {"entity_id": ENTITY_ID, "command": "c4.dmx.led 00 05 ff0000"},
    ),
    ("snapshot", {"ieee_address": IEEE, "name": "before"}),
    ("restore", {"ieee_address": IEEE, "name": "before"}),
    ("delete_snapshot", {"ieee_address": IEEE, "name": "before"}),
    ("set_led", {"entity_id": ENTITY_ID, "mode": "on", "color": "ff0000"}),
    ("set_slot_led", {"entity_id": ENTITY_ID, "on_color": "ff0000"}),
    ("push_config", {"ieee_address": IEEE}),
    ("audit_config", {"ieee_address": IEEE, "mode": "heal"}),
]

SVC_OPEN: list[tuple[str, dict[str, Any]]] = [
    ("list_snapshots", {"ieee_address": IEEE}),
]


def _side_effects(manager: MagicMock) -> int:
    """Count every way a service can reach the device or the store."""
    return (
        manager.async_configure_device.await_count
        + manager.async_send_mqtt.await_count
        + manager.async_push_config.await_count
        + manager.async_audit_device.await_count
        + manager._push_single_slot.await_count
        + manager.store.async_save.await_count
        + manager.store.async_save_device.await_count
        + manager.store.async_save_snapshot.await_count
        + manager.store.async_delete_snapshot.await_count
    )


@pytest.mark.parametrize(
    ("service", "data"), SVC_ADMIN_ONLY, ids=[s for s, _ in SVC_ADMIN_ONLY]
)
async def test_service_rejects_non_admin(
    hass: HomeAssistant,
    hass_read_only_user,
    manager: MagicMock,
    service: str,
    data: dict[str, Any],
) -> None:
    await _register_services(hass)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            service,
            data,
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    assert _side_effects(manager) == 0


@pytest.mark.parametrize(
    ("service", "data"), SVC_ADMIN_ONLY, ids=[s for s, _ in SVC_ADMIN_ONLY]
)
async def test_service_accepts_admin(
    hass: HomeAssistant,
    hass_admin_user,
    manager: MagicMock,
    service: str,
    data: dict[str, Any],
) -> None:
    await _register_services(hass)
    await hass.services.async_call(
        DOMAIN,
        service,
        data,
        blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )
    assert _side_effects(manager) >= 1


@pytest.mark.parametrize(("service", "data"), SVC_OPEN, ids=[s for s, _ in SVC_OPEN])
async def test_service_stays_open_to_non_admin(
    hass: HomeAssistant,
    hass_read_only_user,
    manager: MagicMock,
    service: str,
    data: dict[str, Any],
) -> None:
    manager.async_audit_device.return_value = {"ok": True}
    await _register_services(hass)
    await hass.services.async_call(
        DOMAIN,
        service,
        data,
        blocking=True,
        context=Context(user_id=hass_read_only_user.id),
    )


# ── websocket slot color validation ──────────────────────────────────


@pytest.mark.parametrize(
    "color", ["ff0000 05 000000", "ff0000\nc4.dmx.led 00 05 000000", "c4.", "ff00"]
)
async def test_ws_device_config_rejects_non_hex_slot_colors(
    hass: HomeAssistant,
    hass_ws_client,
    manager: MagicMock,
    color: str,
) -> None:
    """Slot colors from the card must be six-digit hex before they reach c4_cmd."""
    await _register_websocket_handlers(hass)
    client = await hass_ws_client(hass)
    for field in ("led_on_color", "led_off_color"):
        await client.send_json(
            {
                "id": 1 if field == "led_on_color" else 2,
                "type": f"{DOMAIN}/device_config",
                "ieee_address": IEEE,
                "slots": [{"slot_id": 1, field: color}],
            }
        )
        resp = await client.receive_json()
        assert resp["success"] is False
        assert resp["error"]["code"] == "invalid_format"
    manager.async_configure_device.assert_not_called()


async def test_ws_device_config_normalizes_slot_colors(
    hass: HomeAssistant,
    hass_ws_client,
    manager: MagicMock,
) -> None:
    await _register_websocket_handlers(hass)
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/device_config",
            "ieee_address": IEEE,
            "slots": [
                {
                    "slot_id": 1,
                    "name": "One",
                    "led_on_color": "#FF0000",
                    "led_off_color": "0000FF",
                }
            ],
        }
    )
    resp = await client.receive_json()
    assert resp["success"] is True
    slots = manager.async_configure_device.await_args.kwargs["slots"]
    assert slots == [
        {
            "slot_id": 1,
            "name": "One",
            "led_on_color": "ff0000",
            "led_off_color": "0000ff",
        }
    ]


# ── press_button runs with the caller's permissions ──────────────────


@pytest.fixture
def live_manager(hass: HomeAssistant) -> Control4Manager:
    """
    Wire a real manager with one keypad whose slot 1 has bound actions.

    tap runs test.record (with data that should stay admin-only to read);
    hold runs test.admin_only, an admin-gated service.
    """
    entry = MagicMock()
    entry.entry_id = "entry1"
    entry.data = {"mqtt_topic": "zigbee2mqtt"}
    entry.options = {}
    with patch("custom_components.control4_dimmers.store.Store"):
        store = Control4Store(hass, "entry1")
    mgr = Control4Manager(hass, entry, store)
    mgr._devices[IEEE] = DeviceState(
        ieee_address=IEEE, friendly_name="Theater", device_type="keypad"
    )
    store._devices[IEEE] = DeviceConfig(
        ieee_address=IEEE,
        friendly_name="Theater",
        device_type="keypad",
        slots=[
            SlotConfig(
                slot_id=1,
                name="One",
                behavior="keypad",
                led_mode="fixed",
                tap_action={"action": "test.record", "data": {"code": "1234"}},
                hold_action={"action": "test.admin_only"},
            )
        ],
    )
    hass.data[DOMAIN] = {"entry1": {"manager": mgr}}
    hass.states.async_set(EVENT_ID, "unknown", {"ieee_address": IEEE, "slot_id": 1})
    return mgr


@pytest.fixture
def recorded(hass: HomeAssistant) -> list[ServiceCall]:
    """Record calls to test.record and register an admin-only test service."""
    calls = async_mock_service(hass, "test", "record")

    async def _admin_only(call: ServiceCall) -> None:
        calls.append(call)

    async_register_admin_service(hass, "test", "admin_only", _admin_only)
    return calls


async def test_read_only_user_cannot_trigger_bound_action(
    hass: HomeAssistant,
    hass_read_only_user,
    live_manager: Control4Manager,
    recorded: list[ServiceCall],
) -> None:
    await _register_services(hass)
    fired: list[str] = []
    live_manager.register_event_callback(IEEE, 1, lambda e, _c: fired.append(e))
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "press_button",
            {"entity_id": EVENT_ID},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    assert recorded == []
    assert fired == []


async def test_press_button_carries_caller_context(
    hass: HomeAssistant,
    hass_admin_user,
    live_manager: Control4Manager,
    recorded: list[ServiceCall],
) -> None:
    await _register_services(hass)
    contexts: list[Context | None] = []
    live_manager.register_event_callback(IEEE, 1, lambda _e, c: contexts.append(c))
    context = Context(user_id=hass_admin_user.id)
    await hass.services.async_call(
        DOMAIN, "press_button", {"entity_id": EVENT_ID}, blocking=True, context=context
    )
    assert len(recorded) == 1
    assert recorded[0].context.user_id == hass_admin_user.id
    assert recorded[0].data == {"code": "1234"}
    # The event entity's state write shares the caller's context.
    assert contexts == [context]


async def test_non_admin_press_cannot_run_admin_only_action(
    hass: HomeAssistant,
    hass_admin_user,
    live_manager: Control4Manager,
    recorded: list[ServiceCall],
) -> None:
    """
    A bound admin-only service is refused for a non-admin who presses.

    The bound action is called without blocking, so Home Assistant's admin
    check refuses it in the background rather than failing press_button.
    """
    await _register_services(hass)
    user = await hass.auth.async_create_user("Regular", group_ids=["system-users"])
    assert not user.is_admin
    await hass.services.async_call(
        DOMAIN,
        "press_button",
        {"entity_id": EVENT_ID, "event_type": "hold"},
        blocking=True,
        context=Context(user_id=user.id),
    )
    await hass.async_block_till_done()
    assert recorded == []


async def test_physical_press_shares_one_integration_context(
    hass: HomeAssistant,
    live_manager: Control4Manager,
    recorded: list[ServiceCall],
) -> None:
    contexts: list[Context | None] = []
    live_manager.register_event_callback(IEEE, 1, lambda _e, c: contexts.append(c))
    live_manager._dispatch_button_action(live_manager.devices[IEEE], "button_1_press")
    await hass.async_block_till_done()
    assert len(recorded) == 1
    assert len(contexts) == 1
    assert contexts[0] is not None
    assert contexts[0].user_id is None
    assert recorded[0].context is contexts[0]


# ── stored action data is admin-only to read ─────────────────────────


@pytest.mark.parametrize(
    "msg",
    [
        {"type": f"{DOMAIN}/devices"},
        {"type": f"{DOMAIN}/device_by_entity", "entity_id": EVENT_ID},
    ],
    ids=lambda m: m["type"],
)
@pytest.mark.parametrize("admin", [False, True], ids=["read_only", "admin"])
async def test_ws_action_data_visible_only_to_admin(  # noqa: PLR0913, PLR0917
    hass: HomeAssistant,
    hass_ws_client,
    hass_read_only_access_token: str,
    live_manager: Control4Manager,
    msg: dict[str, Any],
    admin: bool,  # noqa: FBT001
) -> None:
    await _register_websocket_handlers(hass)
    client = (
        await hass_ws_client(hass)
        if admin
        else await hass_ws_client(hass, hass_read_only_access_token)
    )
    await client.send_json({"id": 1, **msg})
    resp = await client.receive_json()
    assert resp["success"] is True
    info = resp["result"][0] if isinstance(resp["result"], list) else resp["result"]
    tap = info["config"]["slots"][0]["tap_action"]
    assert tap["action"] == "test.record"
    if admin:
        assert tap["data"] == {"code": "1234"}
    else:
        assert "data" not in tap
    # The stored config itself is untouched.
    stored = live_manager.store.get_device(IEEE).slots[0].tap_action
    assert stored["data"] == {"code": "1234"}
