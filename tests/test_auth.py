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
from unittest.mock import MagicMock

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized

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
    hass.data[DOMAIN] = {"entry1": {"manager": mgr}}
    hass.states.async_set(ENTITY_ID, "keypad", {"ieee_address": IEEE})
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
]

SVC_OPEN: list[tuple[str, dict[str, Any]]] = [
    ("push_config", {"ieee_address": IEEE}),
    ("audit_config", {"ieee_address": IEEE, "mode": "report"}),
    ("list_snapshots", {"ieee_address": IEEE}),
]


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
    manager.async_configure_device.assert_not_called()
    manager.async_send_mqtt.assert_not_called()
    manager.store.async_save_snapshot.assert_not_called()
    manager.store.async_delete_snapshot.assert_not_called()


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
    reached = (
        manager.async_configure_device.await_count
        + manager.async_send_mqtt.await_count
        + manager.store.async_save_snapshot.await_count
        + manager.store.async_delete_snapshot.await_count
    )
    assert reached == 1


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
