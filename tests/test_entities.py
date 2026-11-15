"""Tests for entity platforms (event, light, sensor)."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.control4_dimmers.const import (
    BUTTON_EVENT_TYPES,
    DOMAIN,
)
from custom_components.control4_dimmers.event import (
    Control4ButtonEvent,
    Control4PaddleEvent,
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
from custom_components.control4_dimmers.sensor import (
    Control4AmbientLightSensor,
    Control4AuditSensor,
    Control4DeviceSensor,
)
from custom_components.control4_dimmers.store import Control4Store

from .conftest import IEEE_DIMMER, IEEE_KEYPAD

# ── Event entity ─────────────────────────────────────────────────────


class TestControl4ButtonEvent:
    """Tests for the event entity."""

    def _make_entity(
        self,
        manager: Control4Manager,
        slot_id: int = 2,
    ) -> Control4ButtonEvent:
        return Control4ButtonEvent(
            manager=manager,
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            model_id="C4-APD120",
            slot_id=slot_id,
        )

    def test_unique_id(self, manager: Control4Manager) -> None:
        entity = self._make_entity(manager)
        assert entity.unique_id == f"{IEEE_DIMMER}_event_2"

    def test_default_name_no_device(self, manager: Control4Manager) -> None:
        entity = self._make_entity(manager)
        assert entity.name == "Button 2"

    def test_default_name_from_device_type(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        manager._devices[IEEE_DIMMER] = dimmer_state
        entity = self._make_entity(manager, slot_id=2)
        assert entity.name == "Top"

    def test_default_name_bottom_slot(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        manager._devices[IEEE_DIMMER] = dimmer_state
        entity = self._make_entity(manager, slot_id=5)
        assert entity.name == "Bottom"

    def test_event_types(self, manager: Control4Manager) -> None:
        entity = self._make_entity(manager)
        assert entity.event_types == BUTTON_EVENT_TYPES

    def test_name_from_config(self, manager: Control4Manager) -> None:
        manager.store._devices[IEEE_DIMMER] = DeviceConfig(
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            device_type="dimmer",
            slots=[SlotConfig(slot_id=2, name="Interior")],
        )
        entity = self._make_entity(manager)
        assert entity.name == "Interior"

    def test_sync_name_from_config_returns_true_on_change(
        self, manager: Control4Manager
    ) -> None:
        entity = self._make_entity(manager)
        manager.store._devices[IEEE_DIMMER] = DeviceConfig(
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            device_type="dimmer",
            slots=[SlotConfig(slot_id=2, name="NewName")],
        )
        changed = entity._sync_name_from_config()
        assert changed is True
        assert entity.name == "NewName"

    def test_sync_name_returns_false_when_unchanged(
        self, manager: Control4Manager
    ) -> None:
        entity = self._make_entity(manager)
        changed = entity._sync_name_from_config()
        assert changed is False

    def test_on_button_event(self, manager: Control4Manager) -> None:
        entity = self._make_entity(manager)
        entity.async_write_ha_state = MagicMock()
        entity._on_button_event("pressed")
        entity.async_write_ha_state.assert_called_once()

    def test_update_entity_id_on_rename(self, manager: Control4Manager) -> None:
        entity = self._make_entity(manager)
        entity.hass = MagicMock()
        entity.async_write_ha_state = MagicMock()
        mock_registry_entry = MagicMock()
        mock_registry_entry.entity_id = "event.kitchen_button_2"
        entity.registry_entry = mock_registry_entry
        entity.entity_id = "event.kitchen_button_2"
        mock_ent_reg = MagicMock()
        with patch(
            "custom_components.control4_dimmers.event.er.async_get",
            return_value=mock_ent_reg,
        ):
            manager.store._devices[IEEE_DIMMER] = DeviceConfig(
                ieee_address=IEEE_DIMMER,
                friendly_name="Kitchen",
                device_type="dimmer",
                slots=[SlotConfig(slot_id=2, name="Main")],
            )
            entity._on_manager_update()
        mock_ent_reg.async_update_entity.assert_called_once_with(
            "event.kitchen_button_2", new_entity_id="event.kitchen_main"
        )

    def test_extra_state_attributes_from_config(self, manager: Control4Manager) -> None:
        manager.store._devices[IEEE_DIMMER] = DeviceConfig(
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
                )
            ],
        )
        entity = self._make_entity(manager, slot_id=2)
        attrs = entity.extra_state_attributes
        assert attrs["on_color"] == "#ffffff"
        assert attrs["off_color"] == "#000000"
        assert attrs["behavior"] == "load_on"
        assert attrs["led_mode"] == "follow_load"
        assert attrs["ieee_address"] == IEEE_DIMMER
        assert attrs["slot_id"] == 2

    def test_extra_state_attributes_defaults(self, manager: Control4Manager) -> None:
        entity = self._make_entity(manager, slot_id=3)
        attrs = entity.extra_state_attributes
        assert attrs["on_color"] == "#0000ff"
        assert attrs["off_color"] == "#000000"
        assert attrs["behavior"] == "keypad"
        assert attrs["led_mode"] == "fixed"


# ── Paddle event entity (issue #117) ─────────────────────────────────


class TestControl4PaddleEvent:
    """Tests for the local load paddle event entity."""

    def _make_entity(
        self,
        manager: Control4Manager,
        paddle_id: str = "paddle_up",
    ) -> Control4PaddleEvent:
        return Control4PaddleEvent(
            manager=manager,
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            model_id="C4-APD120",
            paddle_id=paddle_id,
        )

    def test_unique_id_up(self, manager: Control4Manager) -> None:
        entity = self._make_entity(manager, "paddle_up")
        assert entity.unique_id == f"{IEEE_DIMMER}_event_paddle_up"

    def test_unique_id_down(self, manager: Control4Manager) -> None:
        entity = self._make_entity(manager, "paddle_down")
        assert entity.unique_id == f"{IEEE_DIMMER}_event_paddle_down"

    def test_names(self, manager: Control4Manager) -> None:
        assert self._make_entity(manager, "paddle_up").name == "Paddle Up"
        assert self._make_entity(manager, "paddle_down").name == "Paddle Down"

    def test_event_types(self, manager: Control4Manager) -> None:
        entity = self._make_entity(manager)
        assert entity.event_types == BUTTON_EVENT_TYPES

    def test_extra_state_attributes(self, manager: Control4Manager) -> None:
        entity = self._make_entity(manager, "paddle_down")
        attrs = entity.extra_state_attributes
        assert attrs["ieee_address"] == IEEE_DIMMER
        assert attrs["paddle"] == "paddle_down"

    def test_on_paddle_event(self, manager: Control4Manager) -> None:
        entity = self._make_entity(manager)
        entity.async_write_ha_state = MagicMock()
        entity._on_paddle_event("pressed")
        entity.async_write_ha_state.assert_called_once()


class TestEventSetupCreatesPaddles:
    """Tests for paddle entity creation via async_setup_entry."""

    @pytest.fixture(autouse=True)
    def _stub_entity_registry(self) -> Iterator[None]:
        """Stub the entity registry, which the mock hass does not carry."""
        with (
            patch("custom_components.control4_dimmers.event.er.async_get"),
            patch(
                "custom_components.control4_dimmers.event."
                "er.async_entries_for_config_entry",
                return_value=[],
            ),
        ):
            yield

    async def _run_setup(
        self,
        manager: Control4Manager,
        mock_hass: MagicMock,
        mock_entry: MagicMock,
    ) -> list:
        mock_hass.data = {DOMAIN: {mock_entry.entry_id: {"manager": manager}}}
        added: list = []
        await event_async_setup_entry(mock_hass, mock_entry, added.extend)
        return added

    async def test_paddles_created_for_dimmer(
        self,
        manager: Control4Manager,
        mock_hass: MagicMock,
        mock_entry: MagicMock,
        dimmer_state: DeviceState,
    ) -> None:
        manager._devices[IEEE_DIMMER] = dimmer_state
        added = await self._run_setup(manager, mock_hass, mock_entry)
        uids = {e.unique_id for e in added}
        assert f"{IEEE_DIMMER}_event_paddle_up" in uids
        assert f"{IEEE_DIMMER}_event_paddle_down" in uids

    async def test_paddles_created_for_keypaddim(
        self,
        manager: Control4Manager,
        mock_hass: MagicMock,
        mock_entry: MagicMock,
    ) -> None:
        manager._devices[IEEE_DIMMER] = DeviceState(
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            model_id="C4-KD120",
            device_type="keypaddim",
        )
        added = await self._run_setup(manager, mock_hass, mock_entry)
        uids = {e.unique_id for e in added}
        assert f"{IEEE_DIMMER}_event_paddle_up" in uids
        assert f"{IEEE_DIMMER}_event_paddle_down" in uids

    async def test_paddles_and_rocker_buttons_for_switch(
        self,
        manager: Control4Manager,
        mock_hass: MagicMock,
        mock_entry: MagicMock,
    ) -> None:
        # A relay-only switch drives a load, so it gets the paddle halves,
        # plus button slots 2 and 6 (issue #189).
        ieee = "0x000fff0000ddd001"
        manager._devices[ieee] = DeviceState(
            ieee_address=ieee,
            friendly_name="Bath Fan",
            model_id="C4-Zigbee",
            device_type="switch",
        )
        added = await self._run_setup(manager, mock_hass, mock_entry)
        uids = {e.unique_id for e in added}
        assert uids == {
            f"{ieee}_event_2",
            f"{ieee}_event_6",
            f"{ieee}_event_paddle_up",
            f"{ieee}_event_paddle_down",
        }
        names = {
            e.unique_id: e.name for e in added if isinstance(e, Control4ButtonEvent)
        }
        assert names[f"{ieee}_event_2"] == "Top"
        assert names[f"{ieee}_event_6"] == "Bottom"

    async def test_no_paddles_for_keypad(
        self,
        manager: Control4Manager,
        mock_hass: MagicMock,
        mock_entry: MagicMock,
        keypad_state: DeviceState,
    ) -> None:
        manager._devices[IEEE_KEYPAD] = keypad_state
        added = await self._run_setup(manager, mock_hass, mock_entry)
        uids = {e.unique_id for e in added}
        assert not any("paddle" in uid for uid in uids)
        # Six button slots, no paddle entities.
        assert len([e for e in added if isinstance(e, Control4PaddleEvent)]) == 0

    async def test_paddles_appear_after_reclassification(
        self,
        manager: Control4Manager,
        mock_hass: MagicMock,
        mock_entry: MagicMock,
    ) -> None:
        # A device that starts unknown gets no paddle entities, then appears
        # once the self-heal reclassifies it to keypaddim (issue #117).
        state = DeviceState(
            ieee_address=IEEE_DIMMER,
            friendly_name="Entry",
            model_id="C4-APD120",
            device_type="",
        )
        manager._devices[IEEE_DIMMER] = state
        mock_hass.data = {DOMAIN: {mock_entry.entry_id: {"manager": manager}}}
        added: list = []
        await event_async_setup_entry(mock_hass, mock_entry, added.extend)
        assert added == []

        # Reclassification arrives via the manager listener path.
        state.device_type = "keypaddim"
        manager.notify_listeners()
        uids = {e.unique_id for e in added}
        assert f"{IEEE_DIMMER}_event_paddle_up" in uids
        assert f"{IEEE_DIMMER}_event_paddle_down" in uids


# ── Orphaned slot entities after a type change (issue #247) ──────────


class TestOrphanedSlotEntities:
    """A type change removes event entities for slots the device lost."""

    IEEE_OTHER = "0x000fff0000eee001"

    @staticmethod
    def _uid(ieee: str, slot: int | str) -> str:
        return f"{ieee}_event_{slot}"

    async def _setup(
        self, hass: HomeAssistant, *states: DeviceState
    ) -> tuple[Control4Manager, MockConfigEntry, er.EntityRegistry, list]:
        """
        Run the event platform against a real entity registry.

        The registry is pre-seeded, as if left by a previous run, with the
        full keypad slot set and both paddles for every device, plus the
        device anchor sensor and an unrelated entity.
        """
        entry = MockConfigEntry(domain=DOMAIN, data={"mqtt_topic": "zigbee2mqtt"})
        entry.add_to_hass(hass)
        with patch("custom_components.control4_dimmers.store.Store"):
            store = Control4Store(hass, entry.entry_id)
        manager = Control4Manager(hass, entry, store)
        ent_reg = er.async_get(hass)
        for state in states:
            ieee = state.ieee_address
            manager._devices[ieee] = state
            for slot in (1, 2, 3, 4, 5, 6, "paddle_up", "paddle_down"):
                ent_reg.async_get_or_create(
                    "event", DOMAIN, self._uid(ieee, slot), config_entry=entry
                )
            ent_reg.async_get_or_create(
                "sensor", DOMAIN, f"{ieee}_sensor", config_entry=entry
            )
        ent_reg.async_get_or_create("event", "other_integration", "unrelated_1")
        hass.data[DOMAIN] = {entry.entry_id: {"manager": manager}}
        added: list = []
        await event_async_setup_entry(hass, entry, added.extend)
        return manager, entry, ent_reg, added

    def _event_uids(self, ent_reg: er.EntityRegistry, ieee: str) -> set[str]:
        prefix = f"{ieee}_event_"
        return {
            e.unique_id
            for e in ent_reg.entities.values()
            if e.domain == "event" and e.unique_id.startswith(prefix)
        }

    async def test_keypaddim_to_dimmer_removes_orphaned_slots(
        self, hass: HomeAssistant
    ) -> None:
        state = DeviceState(
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            model_id="C4-KD120",
            device_type="keypaddim",
        )
        other = DeviceState(
            ieee_address=self.IEEE_OTHER,
            friendly_name="Theater",
            model_id="C4-KC120277",
            device_type="keypad",
        )
        manager, entry, ent_reg, added = await self._setup(hass, state, other)
        # A keypad dimmer has all six slots and both paddles: nothing goes.
        assert self._event_uids(ent_reg, IEEE_DIMMER) == {
            self._uid(IEEE_DIMMER, s)
            for s in (1, 2, 3, 4, 5, 6, "paddle_up", "paddle_down")
        }
        assert len(added) == 8 + 6

        # Reclassified as a plain dimmer: only slots 2 and 5 remain.
        state.device_type = "dimmer"
        manager.notify_listeners()
        assert self._event_uids(ent_reg, IEEE_DIMMER) == {
            self._uid(IEEE_DIMMER, s) for s in (2, 5, "paddle_up", "paddle_down")
        }
        # The other device only lost the paddles a pure keypad never has;
        # its six button slots, both anchor sensors and the unrelated
        # entity are untouched.
        assert self._event_uids(ent_reg, self.IEEE_OTHER) == {
            self._uid(self.IEEE_OTHER, s) for s in (1, 2, 3, 4, 5, 6)
        }
        assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{IEEE_DIMMER}_sensor")
        assert ent_reg.async_get_entity_id("event", "other_integration", "unrelated_1")
        assert len(er.async_entries_for_config_entry(ent_reg, entry.entry_id)) == (
            4 + 6 + 2
        )

    async def test_removed_slots_come_back_when_type_flips_back(
        self, hass: HomeAssistant
    ) -> None:
        state = DeviceState(
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            model_id="C4-KD120",
            device_type="keypaddim",
        )
        manager, _entry, ent_reg, added = await self._setup(hass, state)
        state.device_type = "dimmer"
        manager.notify_listeners()
        assert self._uid(IEEE_DIMMER, 1) not in self._event_uids(ent_reg, IEEE_DIMMER)
        before = len(added)

        state.device_type = "keypaddim"
        manager.notify_listeners()
        new_uids = {e.unique_id for e in added[before:]}
        assert new_uids == {self._uid(IEEE_DIMMER, s) for s in (1, 3, 4, 6)}

    async def test_switch_recognized_from_keypaddim_keeps_rocker_slots(
        self, hass: HomeAssistant
    ) -> None:
        """A relay first typed keypaddim, then switch, keeps slots 2 and 6."""
        state = DeviceState(
            ieee_address=IEEE_DIMMER,
            friendly_name="Bath Fan",
            model_id="C4-SW120277",
            device_type="keypaddim",
        )
        manager, _entry, ent_reg, _added = await self._setup(hass, state)
        state.device_type = "switch"
        manager.notify_listeners()
        assert self._event_uids(ent_reg, IEEE_DIMMER) == {
            self._uid(IEEE_DIMMER, s) for s in (2, 6, "paddle_up", "paddle_down")
        }

    async def test_stored_type_override_drives_the_slot_list(
        self, hass: HomeAssistant
    ) -> None:
        state = DeviceState(
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            model_id="C4-KD120",
            device_type="keypaddim",
        )
        manager, _entry, ent_reg, _added = await self._setup(hass, state)
        manager.store._devices[IEEE_DIMMER] = DeviceConfig(
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            device_type="keypaddim",
            device_type_override="keypad",
        )
        manager.notify_listeners()
        # A pure keypad has no load, so the paddle halves go.
        assert self._event_uids(ent_reg, IEEE_DIMMER) == {
            self._uid(IEEE_DIMMER, s) for s in (1, 2, 3, 4, 5, 6)
        }

    async def test_undetected_device_keeps_restored_entities(
        self, hass: HomeAssistant
    ) -> None:
        """Nothing is removed while a device's type is still unknown."""
        state = DeviceState(
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            model_id="C4-APD120",
            device_type=None,
        )
        _manager, _entry, ent_reg, added = await self._setup(hass, state)
        assert added == []
        assert len(self._event_uids(ent_reg, IEEE_DIMMER)) == 8


# ── Sensor anchor entity ─────────────────────────────────────────────


class TestControl4DeviceSensor:
    """Tests for the sensor anchor entity."""

    def _make_entity(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> Control4DeviceSensor:
        manager._devices[IEEE_DIMMER] = dimmer_state
        return Control4DeviceSensor(
            manager=manager,
            ieee_address=IEEE_DIMMER,
            friendly_name="Kitchen",
            model_id="C4-APD120",
        )

    def test_unique_id(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        entity = self._make_entity(manager, dimmer_state)
        assert entity.unique_id == f"{IEEE_DIMMER}_sensor"

    def test_name_is_none(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        entity = self._make_entity(manager, dimmer_state)
        assert entity.name is None

    def test_native_value_connected(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        entity = self._make_entity(manager, dimmer_state)
        assert entity.native_value == "connected"

    def test_native_value_disconnected(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        dimmer_state.available = False
        entity = self._make_entity(manager, dimmer_state)
        assert entity.native_value == "disconnected"

    def test_extra_state_attributes(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        entity = self._make_entity(manager, dimmer_state)
        attrs = entity.extra_state_attributes
        assert attrs["ieee_address"] == IEEE_DIMMER
        assert attrs["detected_type"] == "dimmer"
        assert attrs["model_id"] == "C4-APD120"

    def test_extra_state_attributes_with_config(
        self,
        manager: Control4Manager,
        dimmer_state: DeviceState,
        dimmer_config: DeviceConfig,
    ) -> None:
        manager.store._devices[IEEE_DIMMER] = dimmer_config
        entity = self._make_entity(manager, dimmer_state)
        attrs = entity.extra_state_attributes
        assert attrs["device_type"] == "dimmer"

    def test_listener_writes_state(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        entity = self._make_entity(manager, dimmer_state)
        entity.async_write_ha_state = MagicMock()
        entity._on_manager_update()
        entity.async_write_ha_state.assert_called_once()


# ── Push-only entities ───────────────────────────────────────────────


class TestEntitiesDoNotPoll:
    """
    Every entity is push-based and must say so.

    The entity platform arms a scan-interval timer as soon as one added
    entity reports should_poll, and the audit sensor is added on every
    entry setup. Leaving the default would make the whole integration
    poll for nothing and leave that timer behind on unload.
    """

    def test_sensor_entities_do_not_poll(
        self, manager: Control4Manager, dimmer_state: DeviceState
    ) -> None:
        manager._devices[IEEE_DIMMER] = dimmer_state
        entities = [
            Control4AuditSensor(manager, "entry1"),
            Control4DeviceSensor(
                manager=manager,
                ieee_address=IEEE_DIMMER,
                friendly_name="Kitchen",
                model_id="C4-APD120",
            ),
            Control4AmbientLightSensor(
                manager=manager,
                ieee_address=IEEE_DIMMER,
                friendly_name="Kitchen",
                model_id="C4-APD120",
            ),
        ]
        assert [entity.should_poll for entity in entities] == [False] * 3

    def test_event_entities_do_not_poll(self, manager: Control4Manager) -> None:
        entities = [
            Control4ButtonEvent(
                manager=manager,
                ieee_address=IEEE_KEYPAD,
                friendly_name="Theater",
                model_id="C4-KC120277",
                slot_id=1,
            ),
            Control4PaddleEvent(
                manager=manager,
                ieee_address=IEEE_DIMMER,
                friendly_name="Kitchen",
                model_id="C4-APD120",
                paddle_id="paddle_up",
            ),
        ]
        assert [entity.should_poll for entity in entities] == [False] * 2
