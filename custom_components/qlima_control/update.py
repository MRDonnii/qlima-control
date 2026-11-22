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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_DEVICE, DOMAIN, SIGNAL_UPDATE
from .esp import (
    LATEST_FIRMWARE,
    firmware_channel_entity,
    firmware_version,
    newer,
    runs_prebuilt_image,
)
from .entity import QlimaEntity

_LOGGER = logging.getLogger(__name__)

MANIFEST_URL = "https://mrdonnii.github.io/qlima-control/firmware/qlima-control-esp32c3.manifest.json"
RELEASES_URL = "https://github.com/MRDonnii/qlima-control/releases"
_CHECK_EVERY = timedelta(hours=6)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    async_add_entities([FirmwareUpdate(hass, entry)])


class FirmwareUpdate(QlimaEntity, UpdateEntity):
    """Shows the ESP's firmware and whether a newer one is released.

    Two install paths, never mixed:
    - factory image (explicit firmware-channel marker): the ESP's own online update,
      which installs the prebuilt image (Wi-Fi is kept in the ESP's own storage);
    - adopted in ESPHome: Home Assistant's ESPHome Builder update for the device,
      which builds the user's own yaml (with its Wi-Fi and key) and uploads it –
      exactly what "Install" in ESPHome Builder does. The prebuilt image is never
      put on an adopted ESP.
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
        """The update entity that installs safely on this ESP.

        Factory image: the ESP's own online update. Anything else: only the
        ESPHome Builder update, which builds the device's own yaml. A legacy
        online updater on an adopted ESP is never used.
        """
        if not self._device_id:
            return None
        if runs_prebuilt_image(self.hass, self._device_id):
            found = [
                e
                for e in er.async_entries_for_device(er.async_get(self.hass), self._device_id)
                if e.platform == "esphome"
                and e.domain == "update"
                and str(e.unique_id).endswith("/update/Firmware")
            ]
            return found[0].entity_id if found else None
        builder = self._builder_entry()
        return builder.entity_id if builder is not None and builder.disabled_by is None else None

    def _builder_entry(self) -> er.RegistryEntry | None:
        """Home Assistant's ESPHome Builder update for this device (its unique id is the MAC)."""
        device = dr.async_get(self.hass).async_get(self._device_id) if self._device_id else None
        if device is None:
            return None
        macs = {str(value).lower() for kind, value in device.connections if kind == dr.CONNECTION_NETWORK_MAC}
        for entry in er.async_entries_for_device(
            er.async_get(self.hass), self._device_id, include_disabled_entities=True
        ):
            if entry.platform == "esphome" and entry.domain == "update" and str(entry.unique_id).lower() in macs:
                return entry
        return None

    @property
    def install_path(self) -> str | None:
        if not self._esp_update():
            return None
        return "online_image" if runs_prebuilt_image(self.hass, self._device_id) else "esphome_builder"

    def watched(self) -> list[str]:
        marker = firmware_channel_entity(self.hass, self._device_id)
        esp = self._esp_update()
        builder = self._builder_entry()
        return [entity for entity in (marker, esp, builder.entity_id if builder else None) if entity]

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
        return {
            "newest_release": self._latest,
            "esp_update_entity": self._esp_update(),
            "prebuilt_image": runs_prebuilt_image(self.hass, self._device_id),
            "install_path": self.install_path,
        }

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        esp = self._esp_update()
        if not esp:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="install_own_yaml")
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
        self._enable_builder_update()

    @callback
    def _enable_builder_update(self) -> None:
        """An adopted ESP updates through ESPHome Builder; HA hides that entity by default."""
        if runs_prebuilt_image(self.hass, self._device_id):
            return
        builder = self._builder_entry()
        if builder is not None and builder.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
            _LOGGER.info("%s: enabling %s so the firmware can be installed from ESPHome Builder", self.entry.title, builder.entity_id)
            er.async_get(self.hass).async_update_entity(builder.entity_id, disabled_by=None)
