"""A calendar of the planned and eaten dinners, backed by the dinner memory.

Moving or deleting an event edits the memory, so `avoid_days` and GetMemory follow what the
household actually did instead of what was first planned.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from homeassistant.components.calendar import (
    EVENT_DESCRIPTION,
    EVENT_END,
    EVENT_LOCATION,
    EVENT_RRULE,
    EVENT_START,
    EVENT_SUMMARY,
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import CONF_LIST_ID, DOMAIN
from .memory import DayTakenError, DinnerMemory, parse_date
from .recipes import format_description
from .sensor import device_info


async def async_setup_entry(hass, entry, async_add_entities):
    list_id = entry.options.get(CONF_LIST_ID, entry.data[CONF_LIST_ID])
    async_add_entities([DinnerCalendar(hass.data[DOMAIN]["memory"], entry, list_id)])


def _uid(recipe_id: str, date: str) -> str:
    return f"{recipe_id}@{date}"


def _not_found(uid: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="dinner_not_found",
        translation_placeholders={"uid": uid},
    )


def _split_uid(uid: str) -> tuple[str, str]:
    recipe_id, sep, date = uid.rpartition("@")
    if not sep:
        raise _not_found(uid)
    return recipe_id, date


def _to_event(entry: dict) -> CalendarEvent | None:
    day = parse_date(entry.get("date"))
    if day is None:
        return None
    description = format_description(
        entry.get("cooking_time"), entry.get("url"), entry.get("ingredients"), note=entry.get("note")
    )
    return CalendarEvent(
        start=day,
        end=day + dt.timedelta(days=1),
        summary=entry.get("title") or "Middag",
        description=description or None,
        uid=_uid(str(entry["id"]), entry["date"]),
    )


class DinnerCalendar(CalendarEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "dinners"
    _attr_supported_features = (
        CalendarEntityFeature.UPDATE_EVENT | CalendarEntityFeature.DELETE_EVENT
    )

    def __init__(self, memory: DinnerMemory, entry: ConfigEntry, list_id: str) -> None:
        self._memory = memory
        # The memory is shared by every list, so the id must survive switching lists.
        self._attr_unique_id = f"ica_dinners_{entry.entry_id}"
        # The sensors rename the device to the live list name; this platform usually registers it first.
        self._attr_device_info = device_info(list_id, entry.title)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._memory.async_add_listener(self.async_write_ha_state))

    def _events(self) -> list[CalendarEvent]:
        events = [e for entry in self._memory.history if (e := _to_event(entry))]
        return sorted(events, key=lambda e: e.start)

    @property
    def event(self) -> CalendarEvent | None:
        today = dt_util.now().date()
        return next((e for e in self._events() if e.start >= today), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: dt.datetime, end_date: dt.datetime
    ) -> list[CalendarEvent]:
        return [
            e
            for e in self._events()
            if e.start_datetime_local < end_date and e.end_datetime_local > start_date
        ]

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        if not await self._memory.async_remove_entry(*_split_uid(uid)):
            raise _not_found(uid)

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        recipe_id, date = _split_uid(uid)
        entry = self._memory.find(recipe_id, date)
        if entry is None:
            raise _not_found(uid)
        start, end = event.get(EVENT_START), event.get(EVENT_END)
        current = _to_event(entry)
        description = event.get(EVENT_DESCRIPTION)
        if (
            isinstance(start, dt.datetime)
            or (isinstance(end, dt.date) and end - start > dt.timedelta(days=1))
            or event.get(EVENT_RRULE)
            or event.get(EVENT_LOCATION)
            or (description is not None and description.strip() != (current.description or "").strip())
        ):
            # Anything else would be accepted and then lost, since events are rebuilt from the memory.
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="dinner_edit_unsupported"
            )
        try:
            await self._memory.async_update_entry(
                recipe_id, date, new_date=start.isoformat(), title=event.get(EVENT_SUMMARY)
            )
        except DayTakenError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="dinner_day_taken",
                translation_placeholders={"date": str(err)},
            ) from err
