"""Persistent dinner memory: what the household has eaten and what it prefers.

Stored in Home Assistant's `.storage` next to the sync state, so it survives restarts and is
included in backups. The conversation agent is stateless between sessions, so this is what
lets `plan_dinners` avoid repeats and lets the assistant answer "what did we eat last week?".
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_memory"
# Keep the store small: at most this many dinners, and nothing older than this.
HISTORY_MAX = 300
HISTORY_MAX_AGE_DAYS = 120


class DayTakenError(Exception):
    """The day already has a dinner; each day holds at most one."""


class DinnerMemory:
    """Dinner history and free-text preferences, persisted between runs."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.history: list[dict] = []
        self.preferences: list[str] = []
        self._listeners: list[Callable[[], None]] = []

    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> CALLBACK_TYPE:
        """Call `update_callback` whenever the memory changes."""
        self._listeners.append(update_callback)
        return lambda: self._listeners.remove(update_callback)

    async def async_load(self) -> "DinnerMemory":
        data = await self._store.async_load() or {}
        self.history = [
            e
            for e in data.get("history", [])
            if isinstance(e, dict) and e.get("id") is not None
        ]
        self.preferences = [
            p.strip()
            for p in data.get("preferences", [])
            if isinstance(p, str) and p.strip()
        ]
        _LOGGER.debug(
            "🍽️ Middagsminne laddat: %d middagar, %d preferenser",
            len(self.history),
            len(self.preferences),
        )
        return self

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"history": self.history, "preferences": self.preferences}
        )
        for update_callback in list(self._listeners):
            update_callback()

    def recent_ids(self, days: int) -> set[str]:
        """Recipe ids eaten within the last `days` days (0 = nothing)."""
        if days <= 0:
            return set()
        cutoff = dt_util.now().date() - dt.timedelta(days=days)
        ids: set[str] = set()
        for entry in self.history:
            parsed = parse_date(entry.get("date"))
            if parsed is not None and parsed >= cutoff:
                ids.add(str(entry["id"]))
        return ids

    async def async_record(
        self, dinners: list[dict], list_name: str | None = None
    ) -> int:
        """Add dinners to the history. Entries may carry their own `date`.

        A dinner on a day that already has a different dinner is skipped.
        """
        default_date = dt_util.now().date().isoformat()
        known = {(str(e.get("id")), e.get("date")) for e in self.history}
        taken = {e.get("date") for e in self.history}
        added = 0
        for dinner in dinners:
            recipe_id = dinner.get("id")
            if recipe_id is None:
                continue
            date = dinner.get("date") or default_date
            key = (str(recipe_id), date)
            if key in known:
                continue
            if date in taken:
                _LOGGER.debug("🍽️ %s har redan en middag – hoppar över %s", date, recipe_id)
                continue
            self.history.append(
                {
                    "id": str(recipe_id),
                    "title": dinner.get("title"),
                    "date": date,
                    "list": list_name,
                    "url": dinner.get("url"),
                    "cooking_time": dinner.get("cooking_time"),
                    "ingredients": dinner.get("ingredients") or [],
                }
            )
            known.add(key)
            taken.add(date)
            added += 1
        if added:
            self._trim()
            await self._async_save()
            _LOGGER.debug("🍽️ %d middagar sparade i minnet", added)
        return added

    def _trim(self) -> None:
        cutoff = dt_util.now().date() - dt.timedelta(days=HISTORY_MAX_AGE_DAYS)
        kept = [
            e for e in self.history if (parsed := parse_date(e.get("date"))) and parsed >= cutoff
        ]
        self.history = kept[-HISTORY_MAX:]

    async def async_add_preference(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        if text.lower() in {p.lower() for p in self.preferences}:
            return False
        self.preferences.append(text)
        await self._async_save()
        return True

    async def async_remove_preference(self, text: str) -> int:
        wanted = (text or "").strip().lower()
        if not wanted:
            return 0
        before = len(self.preferences)
        # Exact match first, otherwise any preference containing the text.
        self.preferences = [p for p in self.preferences if p.lower() != wanted and wanted not in p.lower()]
        removed = before - len(self.preferences)
        if removed:
            await self._async_save()
        return removed

    async def async_forget_dinner(
        self, recipe: str | None = None, date: str | None = None
    ) -> int:
        """Remove history entries matching a recipe id/title and/or a date.

        `recipe` matches the recipe id exactly or is a substring of the title; `date` is an
        ISO date. With only one of them set, the other is ignored.
        """
        wanted_recipe = (recipe or "").strip().lower()
        wanted_date = (date or "").strip()
        if not wanted_recipe and not wanted_date:
            return 0

        def matches(entry: dict) -> bool:
            if wanted_date and (entry.get("date") or "") != wanted_date:
                return False
            if not wanted_recipe:
                return True
            recipe_id = str(entry.get("id", "")).lower()
            title = (entry.get("title") or "").lower()
            return wanted_recipe == recipe_id or wanted_recipe in title

        before = len(self.history)
        self.history = [e for e in self.history if not matches(e)]
        removed = before - len(self.history)
        if removed:
            await self._async_save()
            _LOGGER.debug("🍽️ %d middagar borttagna ur minnet", removed)
        return removed

    def planned_day(self, recipe_id: str, start: dt.date) -> dt.date | None:
        """The first day on or after `start` this recipe is already planned for."""
        days = [
            parsed
            for e in self.history
            if str(e.get("id")) == recipe_id
            and (parsed := parse_date(e.get("date"))) is not None
            and parsed >= start
        ]
        return min(days, default=None)

    def free_days(self, start: dt.date, count: int) -> list[dt.date]:
        """The first `count` days from `start` that have no dinner yet."""
        taken = {e.get("date") for e in self.history}
        days: list[dt.date] = []
        day = start
        while len(days) < count:
            if day.isoformat() not in taken:
                days.append(day)
            day += dt.timedelta(days=1)
        return days

    def find(self, recipe_id: str, date: str) -> dict | None:
        return next(
            (e for e in self.history if str(e.get("id")) == recipe_id and e.get("date") == date),
            None,
        )

    async def async_remove_entry(self, recipe_id: str, date: str) -> bool:
        entry = self.find(recipe_id, date)
        if entry is None:
            return False
        self.history.remove(entry)
        await self._async_save()
        return True

    async def async_update_entry(
        self, recipe_id: str, date: str, *, new_date: str | None = None, title: str | None = None
    ) -> bool:
        """Move a dinner to another day and/or rename it.

        Raises DayTakenError when `new_date` already has a dinner.
        """
        entry = self.find(recipe_id, date)
        if entry is None:
            return False
        if new_date and new_date != date:
            if any(e.get("date") == new_date for e in self.history):
                raise DayTakenError(new_date)
            entry["date"] = new_date
        if title:
            entry["title"] = title
        await self._async_save()
        return True

    async def async_clear(
        self, history: bool = True, preferences: bool = True
    ) -> dict:
        """Empty the history and/or the preferences; returns what was removed."""
        removed = {"history": 0, "preferences": 0}
        if history:
            removed["history"] = len(self.history)
            self.history = []
        if preferences:
            removed["preferences"] = len(self.preferences)
            self.preferences = []
        if history or preferences:
            await self._async_save()
        return removed

    def as_dict(self) -> dict:
        # Ingredients are for the calendar; leaving them out keeps GetMemory small for the LLM.
        history = [{k: v for k, v in e.items() if k != "ingredients"} for e in self.history]
        return {
            "history": sorted(history, key=lambda e: e.get("date") or "", reverse=True),
            "preferences": list(self.preferences),
        }


def parse_date(value) -> dt.date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None
