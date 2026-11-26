"""
Number platform for Control4 Dimmers.

Exposes the dimming table (c4.dm.tv): ramp rates, level limits and cold start
values. An entity appears once the converter has read its parameter, or right
away when the entity registry shows the device had it before. Relays get none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .manager import Control4Manager

MAX_RAMP_MS = 60000


def _level(param: str, name: str) -> NumberEntityDescription:
    """Describe a parameter holding a load level."""
    return NumberEntityDescription(
        key=param,
        name=name,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        mode=NumberMode.BOX,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
    )


def _time(param: str, name: str) -> NumberEntityDescription:
    """Describe a parameter holding a duration."""
    return NumberEntityDescription(
        key=param,
        name=name,
        device_class=NumberDeviceClass.DURATION,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        mode=NumberMode.BOX,
        native_min_value=0,
        native_max_value=MAX_RAMP_MS,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
    )


DIMMING_NUMBERS: tuple[NumberEntityDescription, ...] = (
    _level("01", "Default on level"),
    _time("02", "Click ramp up"),
    _time("03", "Click ramp down"),
    _time("04", "Hold ramp up"),
    _time("05", "Hold ramp down"),
    _level("06", "Maximum level"),
    _level("08", "Minimum level"),
    _time("09", "Cold start time"),
    _level("0a", "Cold start level"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up dimming table number entities from a config entry."""
    runtime = hass.data[DOMAIN].get(entry.entry_id)
    if runtime is None:
        return

    manager: Control4Manager = runtime["manager"]
    known: set[tuple[str, str]] = set()

    def _check_new_numbers() -> None:
        new: list[Control4DimmingNumber] = []
        for ieee, state in manager.devices.items():
            # Keypads, the outlet and relays have no dimmable load, so a
            # c4_tv_* value from a raw command must not make entities there.
            # Before the type is known (it arrives in the device's state,
            # which a reload of this integration doesn't bring back), only
            # the entities the registry remembers are recreated.
            dimmable = manager.has_dimming_table(ieee)
            if not dimmable and manager.effective_type(ieee) is not None:
                continue
            for description in DIMMING_NUMBERS:
                key = (ieee, description.key)
                if key in known:
                    continue
                read = dimmable and f"c4_tv_{description.key}" in state.raw
                if not read and not manager.is_registered(
                    "number", f"{ieee}_dimming_{description.key}"
                ):
                    continue
                known.add(key)
                new.append(
                    Control4DimmingNumber(
                        manager=manager,
                        ieee_address=ieee,
                        friendly_name=state.friendly_name,
                        model_id=state.model_id,
                        description=description,
                    )
                )
        if new:
            async_add_entities(new)
            LOGGER.debug("Added %d dimming number entities", len(new))

    _check_new_numbers()
    manager.add_listener(_check_new_numbers)


class Control4DimmingNumber(RestoreNumber):
    """
    One writable dimming table parameter.

    Shows the last known value until the device reports again (issue #265):
    the values only arrive in the device's state, and a restart or reload
    can come long before the device next reports.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        manager: Control4Manager,
        ieee_address: str,
        friendly_name: str,
        model_id: str,
        description: NumberEntityDescription,
    ) -> None:
        """Initialize the dimming number entity."""
        self.entity_description = description
        self._manager = manager
        self._ieee = ieee_address
        self._unsub_listener: Callable[[], None] | None = None
        self._restored_value: float | None = None
        self._attr_unique_id = f"{ieee_address}_dimming_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, ieee_address)},
            "name": friendly_name,
            "manufacturer": "Control4",
            "model": model_id or None,
        }

    @property
    def available(self) -> bool:
        """
        Return False once the device is known to have no dimming table.

        The registry brings these entities back on a reload before the type
        is known. A device that then identifies as a relay, keypad or outlet
        keeps the entity, but unavailable, so it can't send dimming writes.
        """
        if self._manager.effective_type(self._ieee) is None:
            return True
        return self._manager.has_dimming_table(self._ieee)

    @property
    def native_value(self) -> float | None:
        """Return the value last read from the device, else the restored one."""
        device = self._manager.devices.get(self._ieee)
        key = f"c4_tv_{self.entity_description.key}"
        value = device.raw.get(key) if device else None
        return value if value is not None else self._restored_value

    async def async_set_native_value(self, value: float) -> None:
        """Write the value to the device; the read-back updates the state."""
        await self._manager.async_set_dimming(
            self._ieee, self.entity_description.key, int(value)
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last value and subscribe to manager updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None:
            self._restored_value = last.native_value
        self._unsub_listener = self._manager.add_listener(self._on_manager_update)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from manager updates."""
        if self._unsub_listener:
            self._unsub_listener()
            self._unsub_listener = None

    def _on_manager_update(self) -> None:
        """Refresh state when new data arrives."""
        self.async_write_ha_state()
