"""Shared test fixtures for Control4 Dimmers tests."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, NonCallableMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.control4_dimmers.manager import Control4Manager
from custom_components.control4_dimmers.models import (
    DeviceConfig,
    DeviceState,
    SlotConfig,
)
from custom_components.control4_dimmers.store import Control4Store

# ── Fake registries for MagicMock-based hass ──────────────────────────


class FakeEntityRegistry:
    """
    The slice of the entity registry the integration reads.

    Entities are added with add(). Any light.* that was not added resolves
    to an MQTT-platform entry, the way Zigbee2MQTT's lights do.
    """

    def __init__(self) -> None:
        """Start empty."""
        self.entries: dict[str, SimpleNamespace] = {}

    def add(
        self,
        entity_id: str,
        unique_id: str,
        platform: str = "control4_dimmers",
        device_id: str | None = None,
    ) -> None:
        """Register an entity."""
        self.entries[entity_id] = SimpleNamespace(
            entity_id=entity_id,
            domain=entity_id.split(".", 1)[0],
            platform=platform,
            unique_id=unique_id,
            device_id=device_id,
        )

    def async_get(self, entity_id: str) -> SimpleNamespace | None:
        """Return an entry by entity_id."""
        if entity_id in self.entries:
            return self.entries[entity_id]
        if entity_id.startswith("light."):
            return SimpleNamespace(
                entity_id=entity_id,
                domain="light",
                platform="mqtt",
                unique_id=entity_id,
                device_id=None,
            )
        return None

    def async_get_entity_id(
        self, domain: str, platform: str, unique_id: str
    ) -> str | None:
        """Return the entity_id registered for a unique_id."""
        for entry in self.entries.values():
            if (entry.domain, entry.platform, entry.unique_id) == (
                domain,
                platform,
                unique_id,
            ):
                return entry.entity_id
        return None


class FakeDeviceRegistry:
    """A device registry with no devices in it."""

    def async_get_device(self, **_kwargs: Any) -> None:
        """No device matches."""

    def async_get(self, _device_id: str) -> None:
        """No device matches."""

    def async_update_device(self, *_args: Any, **_kwargs: Any) -> None:
        """Accept and ignore updates."""


_FAKE_ENT_REGS: dict[int, FakeEntityRegistry] = {}


def fake_entity_registry(hass: Any) -> FakeEntityRegistry:
    """Return the fake entity registry used for a MagicMock hass."""
    return _FAKE_ENT_REGS.setdefault(id(hass), FakeEntityRegistry())


@pytest.fixture(autouse=True)
def _fake_registries() -> Iterator[None]:
    """
    Give MagicMock-based hass objects an entity and device registry.

    A real hass (or a mock whose hass.data already carries a registry)
    gets the real lookup.
    """
    real_er, real_dr = er.async_get, dr.async_get

    def _er(hass: Any) -> Any:
        if isinstance(hass, NonCallableMock) and er.DATA_REGISTRY not in getattr(
            hass, "data", {}
        ):
            return fake_entity_registry(hass)
        return real_er(hass)

    def _dr(hass: Any) -> Any:
        if isinstance(hass, NonCallableMock) and dr.DATA_REGISTRY not in getattr(
            hass, "data", {}
        ):
            return FakeDeviceRegistry()
        return real_dr(hass)

    with patch.object(er, "async_get", _er), patch.object(dr, "async_get", _dr):
        yield
    _FAKE_ENT_REGS.clear()


# ── Fixtures: mock Home Assistant core ────────────────────────────────


@pytest.fixture
def mock_hass() -> MagicMock:
    """Return a lightweight mock HomeAssistant instance."""
    hass = MagicMock(spec=HomeAssistant)
    hass.data = {}
    hass.states = MagicMock()
    hass.config_entries = MagicMock()
    hass.bus = MagicMock()
    hass.state = MagicMock()
    hass.services = MagicMock()
    return hass


@pytest.fixture
def mock_entry() -> MagicMock:
    """Return a mock ConfigEntry."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.title = "Control4 Zigbee"
    entry.data = {"mqtt_topic": "zigbee2mqtt"}
    entry.options = {}
    return entry


@pytest.fixture
def mock_store(mock_hass: MagicMock, mock_entry: MagicMock) -> Control4Store:
    """Return a Control4Store backed by a fake HA Store."""
    with patch("custom_components.control4_dimmers.store.Store"):
        store = Control4Store(mock_hass, mock_entry.entry_id)
    return store


@pytest.fixture
def manager(
    mock_hass: MagicMock,
    mock_entry: MagicMock,
    mock_store: Control4Store,
) -> Control4Manager:
    """Return a Control4Manager with mocked MQTT."""
    mgr = Control4Manager(mock_hass, mock_entry, mock_store)
    return mgr


# ── Fixtures: test data ──────────────────────────────────────────────

IEEE_DIMMER = "0x000fff0000aaa001"
IEEE_KEYPAD = "0x000fff0000ccc001"


@pytest.fixture
def dimmer_state() -> DeviceState:
    """Return a typical dimmer DeviceState."""
    return DeviceState(
        ieee_address=IEEE_DIMMER,
        friendly_name="Kitchen",
        model_id="C4-APD120",
        device_type="dimmer",
        brightness=200,
        state="ON",
    )


@pytest.fixture
def keypad_state() -> DeviceState:
    """Return a typical keypad DeviceState."""
    return DeviceState(
        ieee_address=IEEE_KEYPAD,
        friendly_name="Theater",
        model_id="C4-KC120277",
        device_type="keypad",
    )


@pytest.fixture
def dimmer_config() -> DeviceConfig:
    """Return a typical dimmer DeviceConfig with load-control buttons."""
    return DeviceConfig(
        ieee_address=IEEE_DIMMER,
        friendly_name="Kitchen",
        device_type="dimmer",
        slots=[
            SlotConfig(
                slot_id=2,
                name="Top",
                behavior="load_on",
                led_mode="follow_load",
                led_on_color="ffffff",
                led_off_color="000000",
            ),
            SlotConfig(
                slot_id=5,
                name="Bottom",
                behavior="load_off",
                led_mode="follow_load",
                led_on_color="000000",
                led_off_color="0000ff",
            ),
        ],
    )


@pytest.fixture
def keypad_config() -> DeviceConfig:
    """Return a typical keypad DeviceConfig with 6 programmable slots."""
    return DeviceConfig(
        ieee_address=IEEE_KEYPAD,
        friendly_name="Theater",
        device_type="keypad",
        slots=[
            SlotConfig(
                slot_id=i, name=f"Button {i}", behavior="keypad", led_mode="fixed"
            )
            for i in range(1, 7)
        ],
    )


def make_bridge_device(
    *,
    ieee: str = IEEE_DIMMER,
    friendly_name: str = "Kitchen",
    vendor: str = "Control4",
    manufacturer: str = "Control4",
    model: str = "C4-Zigbee",
) -> dict[str, Any]:
    """Build a single Z2M bridge/devices entry matching real Z2M output."""
    return {
        "ieee_address": ieee,
        "friendly_name": friendly_name,
        "manufacturer": manufacturer,
        "type": "Router",
        "supported": True,
        "definition": {
            "model": model,
            "vendor": vendor,
        },
    }
