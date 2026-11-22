"""Tests for the Mug data update coordinator."""

from __future__ import annotations

import logging
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from ember_mug.consts import LiquidState, MugCharacteristic
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ember_mug import DOMAIN
from custom_components.ember_mug.const import CONFIG_VERSION
from tests import DEFAULT_CONFIG_DATA, TEST_BLE_DEVICE, TEST_MAC_UNIQUE_ID, TEST_MUG_NAME
from tests.conftest import inject_ble_device_discovery_info

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant

    from custom_components.ember_mug.coordinator import MugDataUpdateCoordinator

TIMESTAMP = 1_700_000_000
TIMESTAMP_BYTES = TIMESTAMP.to_bytes(4, "little")

# Raw characteristic values, as a Mug 2 returns them over Bluetooth
MUG_CHARACTERISTICS: dict[MugCharacteristic, bytes] = {
    MugCharacteristic.MUG_NAME: b"Test Mug",
    MugCharacteristic.CURRENT_TEMPERATURE: (5300).to_bytes(2, "little"),
    MugCharacteristic.TARGET_TEMPERATURE: (4500).to_bytes(2, "little"),
    MugCharacteristic.TEMPERATURE_UNIT: b"\x00",
    MugCharacteristic.LIQUID_LEVEL: b"\x14",
    MugCharacteristic.BATTERY: b"\x21\x00",
    MugCharacteristic.LIQUID_STATE: bytes([LiquidState.TARGET_TEMPERATURE]),
    MugCharacteristic.LED: b"\xf4\x00\xa1\xff",
    MugCharacteristic.FIRMWARE: b"\x0f\x00\xc8\x00\x0a\x00",
    MugCharacteristic.MUG_ID: b"abcdef-serial",
    MugCharacteristic.DSK: b"\x00" * 20,
    MugCharacteristic.UDSK: b"\x00" * 20,
}


def _patch_mug(date_time_zone: bytes) -> ExitStack:
    """Patch the Bluetooth side of the mug so reads return the given raw characteristic values."""
    characteristics = MUG_CHARACTERISTICS | {MugCharacteristic.DATE_TIME_AND_ZONE: date_time_zone}

    async def fake_read(characteristic: MugCharacteristic) -> bytearray:
        return bytearray(characteristics[characteristic])

    stack = ExitStack()
    stack.enter_context(patch("custom_components.ember_mug.EmberMug.pair", AsyncMock(return_value=None)))
    stack.enter_context(patch("custom_components.ember_mug.EmberMug._ensure_connection", AsyncMock(return_value=None)))
    stack.enter_context(patch("custom_components.ember_mug.EmberMug.make_writable", AsyncMock(return_value=True)))
    stack.enter_context(patch("custom_components.ember_mug.EmberMug._read", side_effect=fake_read))
    return stack


async def _setup_with_time_zone_bytes(hass: HomeAssistant, date_time_zone: bytes) -> MugDataUpdateCoordinator:
    """Set up the integration with a mug that reports the given date/time-zone bytes."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_MUG_NAME,
        data=DEFAULT_CONFIG_DATA,
        options=None,
        unique_id=TEST_MAC_UNIQUE_ID,
        version=CONFIG_VERSION,
    )
    config_entry.add_to_hass(hass)
    inject_ble_device_discovery_info(hass, TEST_BLE_DEVICE)
    with _patch_mug(date_time_zone):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    return config_entry.runtime_data


async def test_unset_time_zone_does_not_break_update(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    """A mug with an unset time zone byte (0x80 -> -128 hours) still updates everything else."""
    unset_time_zone = TIMESTAMP_BYTES + b"\x80"
    caplog.set_level(logging.DEBUG, logger="custom_components.ember_mug.coordinator")
    coordinator = await _setup_with_time_zone_bytes(hass, unset_time_zone)

    assert coordinator.last_update_success is True
    assert coordinator.last_exception is None
    data = coordinator.data
    assert data.date_time_zone is None
    # Other initial and regular attributes are populated
    assert data.firmware is not None
    assert data.firmware.version == 15
    assert data.meta is not None
    assert data.meta.mug_id
    assert data.name == "Test Mug"
    assert data.current_temp == 53
    assert data.target_temp == 45
    assert data.liquid_level == 20
    assert data.battery is not None
    assert data.battery.percent == 33
    assert data.liquid_state == LiquidState.TARGET_TEMPERATURE

    # Reading it again does not raise, and the warning is only logged once
    with _patch_mug(unset_time_zone):
        await coordinator.mug.update_initial()
    assert coordinator.data.date_time_zone is None
    messages = [r for r in caplog.records if "invalid date/time zone" in r.getMessage()]
    assert [r.levelno for r in messages] == [logging.WARNING, logging.DEBUG]


async def test_valid_time_zone_is_unchanged(hass: HomeAssistant) -> None:
    """A mug with a valid time zone is decoded exactly as before."""
    coordinator = await _setup_with_time_zone_bytes(hass, TIMESTAMP_BYTES + (-7).to_bytes(1, "big", signed=True))

    assert coordinator.last_update_success is True
    date_time_zone = coordinator.data.date_time_zone
    assert date_time_zone is not None
    assert date_time_zone == datetime.fromtimestamp(TIMESTAMP, UTC)
    assert date_time_zone.utcoffset().total_seconds() == -7 * 3600
