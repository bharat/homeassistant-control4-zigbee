"""
Entities survive a reload of the integration.

A device's type, dimming values and relay state arrive in its Zigbee2MQTT
state, which is not sent again when only this integration reloads. Until
it is, the platforms recreate what the entity registry remembers, so the
entities don't sit as restored placeholders (unavailable, and offered for
deletion), and show their last known values.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.number import NumberExtraStoredData
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.control4_dimmers.const import DOMAIN
from custom_components.control4_dimmers.event import (
    Control4ButtonEvent,
)
from custom_components.control4_dimmers.event import (
    async_setup_entry as event_async_setup_entry,
)
from custom_components.control4_dimmers.manager import Control4Manager
from custom_components.control4_dimmers.models import (
    DeviceConfig,
    DeviceState,
    SlotConfig,
)
from custom_components.control4_dimmers.number import (
    Control4DimmingNumber,
)
from custom_components.control4_dimmers.number import (
    async_setup_entry as number_async_setup_entry,
)
from custom_components.control4_dimmers.sensor import (
    LOAD_SENSORS,
    Control4LoadSensor,
)
from custom_components.control4_dimmers.sensor import (
    async_setup_entry as sensor_async_setup_entry,
)
from custom_components.control4_dimmers.store import Control4Store
from custom_components.control4_dimmers.switch import (
    Control4LoadSwitch,
)
from custom_components.control4_dimmers.switch import (
    async_setup_entry as switch_async_setup_entry,
)

from .conftest import IEEE_DIMMER, IEEE_KEYPAD


def _untyped(ieee: str = IEEE_DIMMER) -> DeviceState:
    """Return a device as discovered after a reload: no state reported yet."""
    return DeviceState(ieee_address=ieee, friendly_name="Kitchen", model_id="C4-APD120")


async def _setup(
    hass: HomeAssistant, setup, state: DeviceState, registered: list[tuple[str, str]]
) -> tuple[Control4Manager, list]:
    """Run a platform's setup with entities left in the registry by a prior run."""
    entry = MockConfigEntry(domain=DOMAIN, data={"mqtt_topic": "zigbee2mqtt"})
    entry.add_to_hass(hass)
    with patch("custom_components.control4_dimmers.store.Store"):
        store = Control4Store(hass, entry.entry_id)
    manager = Control4Manager(hass, entry, store)
    manager._devices[state.ieee_address] = state
    ent_reg = er.async_get(hass)
    for domain, unique_id in registered:
        ent_reg.async_get_or_create(domain, DOMAIN, unique_id, config_entry=entry)
    hass.data[DOMAIN] = {entry.entry_id: {"manager": manager}}
    added: list = []
    await setup(hass, entry, added.extend)
    return manager, added


class TestEvents:
    """Button and paddle entities."""

    async def test_registered_buttons_are_recreated_before_the_type(
        self, hass: HomeAssistant
    ) -> None:
        uids = [
            f"{IEEE_DIMMER}_event_2",
            f"{IEEE_DIMMER}_event_5",
            f"{IEEE_DIMMER}_event_paddle_up",
            f"{IEEE_DIMMER}_event_paddle_down",
        ]
        _manager, added = await _setup(
            hass,
            event_async_setup_entry,
            _untyped(),
            [("event", uid) for uid in uids],
        )
        assert sorted(e.unique_id for e in added) == sorted(uids)

    async def test_nothing_registered_creates_nothing(
        self, hass: HomeAssistant
    ) -> None:
        _manager, added = await _setup(hass, event_async_setup_entry, _untyped(), [])
        assert added == []

    async def test_type_arriving_later_adds_missing_slots_once(
        self, hass: HomeAssistant
    ) -> None:
        state = _untyped()
        manager, added = await _setup(
            hass,
            event_async_setup_entry,
            state,
            [("event", f"{IEEE_DIMMER}_event_2")],
        )
        state.device_type = "dimmer"
        manager.notify_listeners()
        uids = [e.unique_id for e in added]
        assert len(uids) == len(set(uids))
        assert f"{IEEE_DIMMER}_event_2" in uids
        assert f"{IEEE_DIMMER}_event_paddle_up" in uids


class TestButtonName:
    """The default name arriving with the type is not a rename."""

    def _entity(self, manager: Control4Manager) -> Control4ButtonEvent:
        return Control4ButtonEvent(
            manager=manager,
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            model_id="C4-APD120",
            slot_id=2,
        )

    def test_type_arriving_keeps_the_entity_id(self, manager: Control4Manager) -> None:
        state = _untyped()
        manager._devices[IEEE_DIMMER] = state
        entity = self._entity(manager)
        assert entity.name == "Button 2"

        state.device_type = "dimmer"
        default = next(
            s.name for s in manager.get_default_slots("dimmer") if s.slot_id == 2
        )
        assert entity._sync_name_from_config() is False
        assert entity.name == default

    def test_user_rename_still_moves_the_entity_id(
        self, manager: Control4Manager
    ) -> None:
        state = _untyped()
        state.device_type = "dimmer"
        manager._devices[IEEE_DIMMER] = state
        entity = self._entity(manager)
        manager._store._devices[IEEE_DIMMER] = DeviceConfig(
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            device_type="dimmer",
            slots=[SlotConfig(slot_id=2, name="Reading")],
        )
        assert entity._sync_name_from_config() is True
        assert entity.name == "Reading"


class TestDimmingNumbers:
    """Dimming table settings (issue #265)."""

    async def test_registered_numbers_are_recreated_before_the_type(
        self, hass: HomeAssistant
    ) -> None:
        _manager, added = await _setup(
            hass,
            number_async_setup_entry,
            _untyped(),
            [("number", f"{IEEE_DIMMER}_dimming_01")],
        )
        assert [e.unique_id for e in added] == [f"{IEEE_DIMMER}_dimming_01"]

    async def test_known_keypad_gets_no_numbers_even_if_registered(
        self, hass: HomeAssistant
    ) -> None:
        state = _untyped(IEEE_KEYPAD)
        state.device_type = "keypad"
        _manager, added = await _setup(
            hass,
            number_async_setup_entry,
            state,
            [("number", f"{IEEE_KEYPAD}_dimming_01")],
        )
        assert added == []

    async def test_restored_value_shows_until_the_device_reports(
        self, hass: HomeAssistant
    ) -> None:
        _manager, added = await _setup(
            hass,
            number_async_setup_entry,
            _untyped(),
            [("number", f"{IEEE_DIMMER}_dimming_01")],
        )
        number: Control4DimmingNumber = added[0]
        number.hass = hass
        with patch.object(
            number,
            "async_get_last_number_data",
            AsyncMock(
                return_value=NumberExtraStoredData(
                    native_max_value=100,
                    native_min_value=0,
                    native_step=1,
                    native_unit_of_measurement="%",
                    native_value=80,
                )
            ),
        ):
            await number.async_added_to_hass()
        assert number.native_value == 80

        number._manager.devices[IEEE_DIMMER].raw["c4_tv_01"] = 65
        assert number.native_value == 65


class TestLoadSwitch:
    """Relay load switches."""

    async def test_registered_switch_is_recreated_and_restored(
        self, hass: HomeAssistant
    ) -> None:
        state = _untyped()
        _manager, added = await _setup(
            hass, switch_async_setup_entry, state, [("switch", f"{IEEE_DIMMER}_switch")]
        )
        switch: Control4LoadSwitch = added[0]
        switch.hass = hass
        switch.async_write_ha_state = MagicMock()
        with patch.object(
            switch,
            "async_get_last_state",
            AsyncMock(return_value=State("switch.kitchen_load", "on")),
        ):
            await switch.async_added_to_hass()
        assert switch.available
        assert switch.is_on is True

        state.state = "OFF"
        switch._on_manager_update()
        assert switch.is_on is False

    async def test_switch_goes_unavailable_if_the_type_is_not_switch(
        self, hass: HomeAssistant
    ) -> None:
        state = _untyped()
        _manager, added = await _setup(
            hass, switch_async_setup_entry, state, [("switch", f"{IEEE_DIMMER}_switch")]
        )
        switch: Control4LoadSwitch = added[0]
        switch.async_write_ha_state = MagicMock()
        state.device_type = "dimmer"
        switch._on_manager_update()
        assert not switch.available


class TestLoadSensors:
    """Power, voltage and the other load readings."""

    async def test_registered_load_sensors_are_recreated_before_the_type(
        self, hass: HomeAssistant
    ) -> None:
        _manager, added = await _setup(
            hass,
            sensor_async_setup_entry,
            _untyped(),
            [("sensor", f"{IEEE_DIMMER}_c4_load_power")],
        )
        load = [e for e in added if isinstance(e, Control4LoadSensor)]
        assert len(load) == len(LOAD_SENSORS)

    async def test_known_keypad_gets_no_load_sensors_even_if_registered(
        self, hass: HomeAssistant
    ) -> None:
        state = _untyped(IEEE_KEYPAD)
        state.device_type = "keypad"
        _manager, added = await _setup(
            hass,
            sensor_async_setup_entry,
            state,
            [("sensor", f"{IEEE_KEYPAD}_c4_load_power")],
        )
        assert not [e for e in added if isinstance(e, Control4LoadSensor)]
