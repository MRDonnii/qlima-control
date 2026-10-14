"""Firmware of the unit's ESP: the version it runs against the newest release."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_DEVICE, DOMAIN, SIGNAL_UPDATE
from .esp import LATEST_FIRMWARE, firmware_version, newer
from .entity import QlimaEntity

_LOGGER = logging.getLogger(__name__)

MANIFEST_URL = "https://mrdonnii.github.io/qlima-control/firmware/qlima-control-esp32c3.manifest.json"
RELEASES_URL = "https://github.com/MRDonnii/qlima-control/releases"
_CHECK_EVERY = timedelta(hours=6)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    async_add_entities([FirmwareUpdate(hass, entry)])


class FirmwareUpdate(QlimaEntity, UpdateEntity):
    """Shows the ESP's firmware and whether a newer one is released.

    Installing goes through the ESP's own update entity: the prebuilt image's
    online update, or the ESPHome dashboard (rebuilds the device's own yaml).
    """

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_release_url = RELEASES_URL
    _attr_title = "Qlima Control firmware"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, "firmware")
        self._latest: str | None = hass.data.get(DOMAIN, {}).get(LATEST_FIRMWARE)

    @property
    def _device_id(self) -> str | None:
        return self.unit.config.get(CONF_DEVICE)

    def _esp_update(self) -> str | None:
        """The ESP device's own update entity – its online update first."""
        if not self._device_id:
            return None
        found = [
            e
            for e in er.async_entries_for_device(er.async_get(self.hass), self._device_id)
            if e.domain == "update"
        ]
        found.sort(key=lambda e: "/update/" not in str(e.unique_id))
        return found[0].entity_id if found else None

    def watched(self) -> list[str]:
        esp = self._esp_update()
        return [esp] if esp else []

    @property
    def installed_version(self) -> str | None:
        device = dr.async_get(self.hass).async_get(self._device_id) if self._device_id else None
        return firmware_version(device.sw_version if device else None)

    @property
    def latest_version(self) -> str | None:
        installed = self.installed_version
        if self._latest is None or installed is None:
            return installed
        # A local build newer than the release counts as up to date.
        return self._latest if newer(self._latest, installed) else installed

    @property
    def supported_features(self) -> UpdateEntityFeature:
        return UpdateEntityFeature.INSTALL if self._esp_update() else UpdateEntityFeature(0)

    @property
    def in_progress(self) -> bool:
        esp = self._esp_update()
        state = self.hass.states.get(esp) if esp else None
        return bool(state and state.attributes.get("in_progress"))

    @property
    def release_summary(self) -> str | None:
        if self._latest:
            return f"Qlima Control firmware {self._latest}"
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"newest_release": self._latest, "esp_update_entity": self._esp_update()}

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        esp = self._esp_update()
        if esp:
            await self.hass.services.async_call("update", "install", {"entity_id": esp}, blocking=True)

    async def _check(self, _now=None) -> None:
        try:
            async with async_get_clientsession(self.hass).get(MANIFEST_URL, timeout=20) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
            version = firmware_version(str(data.get("version", "")))
        except Exception as err:  # noqa: BLE001 - offline or GitHub down: keep the last answer
            _LOGGER.debug("Firmware check failed: %s", err)
            return
        if version and version != self._latest:
            self._latest = version
            self.hass.data.setdefault(DOMAIN, {})[LATEST_FIRMWARE] = version
            self.async_write_ha_state()
            async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _device_changed(event: Event) -> None:
            if event.data.get("device_id") == self._device_id:
                self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, _device_changed)
        )
        self.async_on_remove(async_track_time_interval(self.hass, self._check, _CHECK_EVERY))
        self.hass.async_create_task(self._check())
