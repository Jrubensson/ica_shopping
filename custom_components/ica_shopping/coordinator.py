"""Polling for the bonus and on-sale sensors, kept apart from list sync so failures stay local."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ica_api import ICAApi, IcaError, offers_on_list, parse_bonus, pick_store

_LOGGER = logging.getLogger(__name__)

BONUS_INTERVAL = timedelta(hours=6)
OFFERS_INTERVAL = timedelta(hours=1)
# The list is re-read every poll; store offers only change daily.
OFFERS_TTL_SECONDS = 6 * 3600


class IcaBonusCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass, entry, api: ICAApi) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name="ICA bonus", update_interval=BONUS_INTERVAL
        )
        self._api = api

    async def _async_update_data(self) -> dict:
        try:
            return parse_bonus(await self._api.async_get_bonus())
        except IcaError as err:
            raise UpdateFailed(f"ICA bonus: {err}") from err


class IcaOffersCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass, entry, api: ICAApi, list_id: str, store_id: str | None) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name="ICA offers", update_interval=OFFERS_INTERVAL
        )
        self._api = api
        self._list_id = list_id
        self._store_id = store_id
        self._store: dict | None = None
        self._offers: list[dict] = []
        self._offers_fetched = 0.0

    async def _refresh_offers(self) -> None:
        stores = await self._api.async_get_favorite_stores()
        if not stores:
            raise UpdateFailed("No favourite store set in the ICA app")
        store = pick_store(stores, self._store_id)
        self._offers = await self._api.async_get_store_offers(store["id"])
        self._store = store
        self._offers_fetched = time.monotonic()

    async def _async_update_data(self) -> dict:
        try:
            if self._store is None or time.monotonic() - self._offers_fetched > OFFERS_TTL_SECONDS:
                await self._refresh_offers()
            the_list = await self._api.async_get_list(self._list_id)
        except IcaError as err:
            raise UpdateFailed(f"ICA offers: {err}") from err
        if the_list is None:
            raise UpdateFailed(f"ICA list {self._list_id} not found")
        return {
            "store": self._store,
            "offer_count": len(self._offers),
            "matches": offers_on_list(the_list.get("rows", []), self._offers),
        }
