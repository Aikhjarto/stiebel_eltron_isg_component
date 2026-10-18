"""Climate targets follow the SG Ready load temperatures in states 3 and 4."""

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from modbus_connection.mock import MockModbusConnection, WriteEvent
from pystiebeleltron import UNAVAILABLE, ControllerModel
from pystiebeleltron.wpm import (
    WPM_HOLDING_RANGES,
    WPM_INPUT_RANGES,
    WpmStiebelEltronAPI,
)
import pytest

from custom_components.stiebel_eltron_isg.climate import (
    CLIMATE_HK_1,
    CLIMATE_HK_2,
    CLIMATE_HK_3,
    LWZ_CLIMATE_TYPES,
    WPM_3I_CLIMATE_TYPES,
    WPM_CLIMATE_TYPES,
    StiebelEltronClimateEntityDescription,
    StiebelEltronWPMClimateEntity,
)
from custom_components.stiebel_eltron_isg.const import DOMAIN, UNIT_ID
from custom_components.stiebel_eltron_isg.entity import build_unique_id

SG_READY_STATE = 5000
HEATING_BUFFER = 4251
LOAD_TEMPERATURE_HC1 = 4253
LOAD_TEMPERATURE_HC2 = 4254
LOAD_TEMPERATURE_HC1_2 = 4271
LOAD_TEMPERATURE_HC2_2 = 4272
LOAD_TEMPERATURE_HC3 = 4273


async def _setup_wpmsystem(hass, mock_config_entry, mock_get_controller_model, unit):
    """Set up a WPMsystem on the real API over the mock connection."""
    raw = {
        "input": {
            address: 0
            for start, end in WPM_INPUT_RANGES
            for address in range(start, end + 1)
        },
        "holding": {
            address: 0
            for start, end in WPM_HOLDING_RANGES
            for address in range(start, end + 1)
        },
    }
    raw["input"][SG_READY_STATE] = 2
    raw["holding"].update({
        1500: 2,  # program mode
        1501: 240,  # comfort / eco HK 1
        1502: 200,
        1504: 160,  # comfort / eco HK 2
        1505: 140,
        1550: 170,  # comfort / eco HK 3
        1551: 150,
        HEATING_BUFFER: 2,  # buffer without mixer
        LOAD_TEMPERATURE_HC1: 500,
        LOAD_TEMPERATURE_HC2: 235,
        LOAD_TEMPERATURE_HC1_2: 500,
        LOAD_TEMPERATURE_HC2_2: 235,
        LOAD_TEMPERATURE_HC3: 225,
    })
    unit.load_raw(raw)
    api = WpmStiebelEltronAPI(unit)
    mock_get_controller_model.return_value = ControllerModel.WPMsystem
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.stiebel_eltron_isg.wpm_coordinator.WpmStiebelEltronAPI",
        return_value=api,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    return {
        key: registry.async_get_entity_id(
            "climate", DOMAIN, build_unique_id(mock_config_entry, key)
        )
        for key in (CLIMATE_HK_1, CLIMATE_HK_2, CLIMATE_HK_3)
    }


def _targets(hass: HomeAssistant, entity_ids: dict[str, str]) -> list:
    return [
        hass.states.get(entity_ids[key]).attributes["temperature"]
        for key in (CLIMATE_HK_1, CLIMATE_HK_2, CLIMATE_HK_3)
    ]


async def test_wpmsystem_targets_follow_sg_ready_state(
    hass: HomeAssistant,
    mock_config_entry,
    mock_get_controller_model,
    mock_modbus_connection,
) -> None:
    """States 3 and 4 show the load temperatures, other states the program target."""
    unit = mock_modbus_connection.for_unit(UNIT_ID)
    entity_ids = await _setup_wpmsystem(
        hass, mock_config_entry, mock_get_controller_model, unit
    )
    coordinator = mock_config_entry.runtime_data

    assert _targets(hass, entity_ids) == [24.0, 16.0, 17.0]

    for state in (3, 4):
        unit.input[SG_READY_STATE] = state
        await coordinator.async_refresh()
        # With a buffer cylinder the HC 1 load temperature is the buffer's, so
        # the HC 1 room target stays the program target.
        assert _targets(hass, entity_ids) == [24.0, 23.5, 22.5]

    unit.holding[HEATING_BUFFER] = 1  # no buffer
    unit.holding[LOAD_TEMPERATURE_HC1_2] = 230
    await coordinator.async_refresh()
    assert _targets(hass, entity_ids) == [23.0, 23.5, 22.5]

    for state in (1, 2, UNAVAILABLE):
        unit.input[SG_READY_STATE] = state
        await coordinator.async_refresh()
        assert _targets(hass, entity_ids) == [24.0, 16.0, 17.0]


async def test_wpmsystem_writes_the_load_temperature_in_force(
    hass: HomeAssistant,
    mock_config_entry,
    mock_get_controller_model,
    mock_modbus_connection,
) -> None:
    """The target shown is the one changed, falling back to the older register."""
    unit = mock_modbus_connection.for_unit(UNIT_ID)
    entity_ids = await _setup_wpmsystem(
        hass, mock_config_entry, mock_get_controller_model, unit
    )
    coordinator = mock_config_entry.runtime_data
    writes: list[WriteEvent] = []
    unit.on_write(writes.append)

    async def set_hk2(temperature: float) -> None:
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": entity_ids[CLIMATE_HK_2], "temperature": temperature},
            blocking=True,
        )

    unit.input[SG_READY_STATE] = 3
    await coordinator.async_refresh()
    await set_hk2(23.0)
    assert hass.states.get(entity_ids[CLIMATE_HK_2]).attributes["temperature"] == 23.0

    unit.holding[LOAD_TEMPERATURE_HC2_2] = UNAVAILABLE
    await coordinator.async_refresh()
    assert hass.states.get(entity_ids[CLIMATE_HK_2]).attributes["temperature"] == 23.5
    await set_hk2(22.0)

    unit.input[SG_READY_STATE] = 2
    await coordinator.async_refresh()
    assert hass.states.get(entity_ids[CLIMATE_HK_2]).attributes["temperature"] == 16.0
    await set_hk2(18.0)

    assert [(event.register_type, event.address, event.values) for event in writes] == [
        ("holding", LOAD_TEMPERATURE_HC2_2, [230]),
        ("holding", LOAD_TEMPERATURE_HC2, [220]),
        ("holding", 1504, [180]),
    ]


class _StubCoordinator:
    def __init__(self, api) -> None:
        self._api = api
        self.writes: list[tuple] = []
        self.refresh_generation = 0
        self.last_successful_refresh_generation = 0

    def get_value(self, accessor):
        return accessor(self._api)

    async def write_component_value(self, component, field, value) -> None:
        self.writes.append((component, field, value))


async def test_assumed_target_does_not_survive_an_sg_ready_change() -> None:
    """A comfort write no longer applies once SG Ready makes the load temperature the target."""
    api = SimpleNamespace(
        system_parameters=SimpleNamespace(
            operating_mode=2, comfort_temperature_hk_2=16.0, eco_temperature_hk_2=14.0
        ),
        energy_system_information=SimpleNamespace(sg_ready_operating_state=2),
        extended_energy_management_settings=SimpleNamespace(
            load_temperature_hc2_2=23.5, load_temperature_hc2=23.5, heating_buffer=2
        ),
    )
    entity = StiebelEltronWPMClimateEntity(
        SimpleNamespace(device_info={}),
        SimpleNamespace(entry_id="test"),
        WPM_CLIMATE_TYPES[1],
    )
    entity.coordinator = _StubCoordinator(api)
    entity.async_write_ha_state = lambda: None

    await entity.async_set_temperature(temperature=17.0)
    assert entity.target_temperature == 17.0

    api.energy_system_information.sg_ready_operating_state = 3
    assert entity.target_temperature == 23.5

    await entity.async_set_temperature(temperature=22.0)
    assert entity.coordinator.writes == [
        ("system_parameters", "comfort_temperature_hk_2", 17.0),
        ("extended_energy_management_settings", "load_temperature_hc2_2", 22.0),
    ]
    assert entity.target_temperature == 22.0


def test_only_wpm_family_follows_sg_ready() -> None:
    """WPM 3i and LWZ have no load temperatures to follow."""
    assert all(
        description.sg_ready_load_temp_registers for description in WPM_CLIMATE_TYPES
    )
    assert [
        description.sg_ready_load_temp_requires_no_buffer
        for description in WPM_CLIMATE_TYPES
    ] == [True, False, False]
    assert not any(
        description.sg_ready_load_temp_registers
        for description in (*WPM_3I_CLIMATE_TYPES, *LWZ_CLIMATE_TYPES)
    )


def test_description_rejects_non_callable_load_temperature_register() -> None:
    """Load temperature references must be API accessors like all others."""

    def valid(api):
        return 1

    with pytest.raises(TypeError, match="sg_ready_load_temp_registers"):
        StiebelEltronClimateEntityDescription(
            key="invalid",
            humidity_modbus_register=[valid],
            actual_temperature_register=[valid],
            eco_target_temp_register=valid,
            comfort_target_temp_register=valid,
            min_temp=5,
            max_temp=30,
            sg_ready_load_temp_registers=(("legacy", "field"),),
        )


def test_load_temperature_references_resolve_against_the_wpm_api() -> None:
    """Every accessor reads and every write field exists on the real WPM API."""
    unit = MockModbusConnection().for_unit(UNIT_ID)
    api = WpmStiebelEltronAPI(unit)
    component = api.extended_energy_management_settings

    for description in WPM_CLIMATE_TYPES:
        for accessor, field in description.sg_ready_load_temp_registers:
            accessor(api)
            assert hasattr(component, field), field
