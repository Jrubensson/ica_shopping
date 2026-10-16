"""Persistent dinner memory: what the household has eaten and what it prefers.

Stored in Home Assistant's `.storage` next to the sync state, so it survives restarts and is
included in backups. The conversation agent is stateless between sessions, so this is what
lets `plan_dinners` avoid repeats and lets the assistant answer "what did we eat last week?".
"""
from __future__ import annotations

import datetime as dt
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_memory"
# Keep the store small: at most this many dinners, and nothing older than this.
HISTORY_MAX = 300
HISTORY_MAX_AGE_DAYS = 120


class DinnerMemory:
    """Dinner history and free-text preferences, persisted between runs."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.history: list[dict] = []
        self.preferences: list[str] = []

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

    def recent_ids(self, days: int) -> set[str]:
        """Recipe ids eaten within the last `days` days (0 = nothing)."""
        if days <= 0:
            return set()
        cutoff = dt_util.now().date() - dt.timedelta(days=days)
        ids: set[str] = set()
        for entry in self.history:
            parsed = _parse_date(entry.get("date"))
            if parsed is not None and parsed >= cutoff:
                ids.add(str(entry["id"]))
        return ids

    async def async_record(
        self, dinners: list[dict], list_name: str | None = None
    ) -> int:
        """Add dinners to the history. Entries may carry their own `date`."""
        default_date = dt_util.now().date().isoformat()
        known = {(str(e.get("id")), e.get("date")) for e in self.history}
        added = 0
        for dinner in dinners:
            recipe_id = dinner.get("id")
            if recipe_id is None:
                continue
            date = dinner.get("date") or default_date
            key = (str(recipe_id), date)
            if key in known:
                continue
            self.history.append(
                {
                    "id": str(recipe_id),
                    "title": dinner.get("title"),
                    "date": date,
                    "list": list_name,
                }
            )
            known.add(key)
            added += 1
        if added:
            self._trim()
            await self._async_save()
            _LOGGER.debug("🍽️ %d middagar sparade i minnet", added)
        return added

    def _trim(self) -> None:
        cutoff = dt_util.now().date() - dt.timedelta(days=HISTORY_MAX_AGE_DAYS)
        kept = [
            e for e in self.history if (parsed := _parse_date(e.get("date"))) and parsed >= cutoff
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

    def as_dict(self) -> dict:
        return {
            "history": sorted(self.history, key=lambda e: e.get("date") or "", reverse=True),
            "preferences": list(self.preferences),
        }


def _parse_date(value) -> dt.date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None
