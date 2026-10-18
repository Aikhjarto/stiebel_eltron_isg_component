"""SG Ready load temperature number entities on the WPM family."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from modbus_connection.mock import WriteEvent
from pystiebeleltron import ControllerModel
from pystiebeleltron.wpm import (
    WPM_HOLDING_RANGES,
    WPM_INPUT_RANGES,
    WpmStiebelEltronAPI,
)
import pytest

from custom_components.stiebel_eltron_isg import number as number_module
from custom_components.stiebel_eltron_isg.const import (
    DOMAIN,
    SG_READY_LOAD_TEMPERATURE_BUFFER,
    SG_READY_LOAD_TEMPERATURE_HK1,
    SG_READY_LOAD_TEMPERATURE_HK2,
    SG_READY_LOAD_TEMPERATURE_HK3,
    SG_READY_LOAD_TEMPERATURE_HK4,
    SG_READY_LOAD_TEMPERATURE_HK5,
    SG_READY_LOAD_TEMPERATURE_WATER,
    UNIT_ID,
)
from custom_components.stiebel_eltron_isg.entity import build_unique_id

# key, holding register (0-based like the library), raw value, ISG range
LOAD_TEMPERATURES = [
    (SG_READY_LOAD_TEMPERATURE_HK1, 4271, 500, (20, 50)),
    (SG_READY_LOAD_TEMPERATURE_HK2, 4272, 240, (20, 30)),
    (SG_READY_LOAD_TEMPERATURE_HK3, 4273, 235, (20, 30)),
    (SG_READY_LOAD_TEMPERATURE_HK4, 4274, 230, (20, 30)),
    (SG_READY_LOAD_TEMPERATURE_HK5, 4275, 225, (20, 30)),
    (SG_READY_LOAD_TEMPERATURE_BUFFER, 4276, 230, (20, 50)),
    (SG_READY_LOAD_TEMPERATURE_WATER, 4277, 550, (40, 65)),
]


async def test_wpmsystem_load_temperatures_read_and_write(
    hass: HomeAssistant,
    mock_config_entry,
    mock_get_controller_model,
    mock_modbus_connection,
) -> None:
    """Each entity reads its own register and writes back to it."""
    unit = mock_modbus_connection.for_unit(UNIT_ID)
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
    raw["holding"].update({
        address: value for _, address, value, _ in LOAD_TEMPERATURES
    })
    unit.load_raw(raw)
    api = WpmStiebelEltronAPI(unit)
    mock_get_controller_model.return_value = ControllerModel.WPMsystem
    mock_config_entry.add_to_hass(hass)

    # HK 4 and HK 5 are disabled by default; enable them to check their registers.
    registry = er.async_get(hass)
    for key in (SG_READY_LOAD_TEMPERATURE_HK4, SG_READY_LOAD_TEMPERATURE_HK5):
        registry.async_get_or_create(
            "number",
            DOMAIN,
            build_unique_id(mock_config_entry, key),
            config_entry=mock_config_entry,
        )

    with patch(
        "custom_components.stiebel_eltron_isg.wpm_coordinator.WpmStiebelEltronAPI",
        return_value=api,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    writes: list[WriteEvent] = []
    unit.on_write(writes.append)
    for key, address, value, (low, high) in LOAD_TEMPERATURES:
        entity_id = registry.async_get_entity_id(
            "number", DOMAIN, build_unique_id(mock_config_entry, key)
        )
        state = hass.states.get(entity_id)
        assert float(state.state) == value / 10
        assert (state.attributes["min"], state.attributes["max"]) == (low, high)

        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": low + 1},
            blocking=True,
        )
        assert (writes[-1].register_type, writes[-1].address, writes[-1].values) == (
            "holding",
            address,
            [(low + 1) * 10],
        )
    assert len(writes) == len(LOAD_TEMPERATURES)


def test_hk4_and_hk5_load_temperatures_are_disabled_by_default() -> None:
    """Like the other circuit 4/5 entities, they need the extension module."""
    enabled = {
        description.key: description.entity_registry_enabled_default
        for description in number_module.SG_READY_LOAD_TEMPERATURE_NUMBER_TYPES
    }
    assert enabled == {
        SG_READY_LOAD_TEMPERATURE_HK1: True,
        SG_READY_LOAD_TEMPERATURE_HK2: True,
        SG_READY_LOAD_TEMPERATURE_HK3: True,
        SG_READY_LOAD_TEMPERATURE_HK4: False,
        SG_READY_LOAD_TEMPERATURE_HK5: False,
        SG_READY_LOAD_TEMPERATURE_BUFFER: True,
        SG_READY_LOAD_TEMPERATURE_WATER: True,
    }


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (ControllerModel.WPM_3, True),
        (ControllerModel.WPMsystem, True),
        (ControllerModel.LWZ_R290, True),
        (ControllerModel.WPM_3i, False),
        (ControllerModel.LWZ, False),
    ],
)
async def test_load_temperatures_only_on_the_wpm_family(model, expected) -> None:
    """WPM 3i and LWZ do not get the WPM load temperature registers."""
    entry = SimpleNamespace(runtime_data=SimpleNamespace(model=model))
    add_entities = MagicMock()
    with patch.object(
        number_module,
        "StiebelEltronISGNumberEntity",
        side_effect=lambda coordinator, config_entry, description: description.key,
    ):
        await number_module.async_setup_entry(None, entry, add_entities)

    keys = set(add_entities.call_args.args[0])
    assert (
        {key for key, *_ in LOAD_TEMPERATURES} <= keys
        if expected
        else not ({key for key, *_ in LOAD_TEMPERATURES} & keys)
    )
