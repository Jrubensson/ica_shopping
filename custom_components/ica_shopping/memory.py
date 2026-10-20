"""Persistent dinner memory: what the household has eaten and what it prefers.

Stored in Home Assistant's `.storage` next to the sync state, so it survives restarts and is
included in backups. The conversation agent is stateless between sessions, so this is what
lets `plan_dinners` avoid repeats and lets the assistant answer "what did we eat last week?".
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .rules import LEVEL_OMIT, fold, make_rule

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_memory"
# Keep the store small: at most this many dinners, and nothing older than this.
HISTORY_MAX = 300
HISTORY_MAX_AGE_DAYS = 120
# A dinner's shopping list rows: all it needs, and the ones planning it put there
LIST_KEYS = ("on_list", "put_on_list")
# The ingredients the household chose not to shop for when the dinner went on a list
LEFT_OFF_KEY = "left_off"
# Ids of dinners without an ICA recipe; never an "@", since calendar uids are "<id>@<date>".
CUSTOM_ID_PREFIX = "custom-"


def is_custom_id(recipe_id) -> bool:
    return str(recipe_id).startswith(CUSTOM_ID_PREFIX)


# The household's own recipes, kept to plan again and again
OWN_ID_PREFIX = "own-"


def is_own_id(recipe_id) -> bool:
    return str(recipe_id).startswith(OWN_ID_PREFIX)


class DayTakenError(Exception):
    """The day already has a dinner; each day holds at most one."""


class DinnerTooOldError(Exception):
    """The day is older than the memory keeps, so the dinner would be dropped at once."""


class DinnerMemory:
    """Dinner history, preferences and food rules, persisted between runs."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.history: list[dict] = []
        self.preferences: list[str] = []
        # Ingredients the household avoids (see rules.py)
        self.food_rules: list[dict] = []
        # The household's own versions of ICA recipes, by recipe id: {title, omit, add, note}
        self.variants: dict[str, dict] = {}
        # The household's own recipes, by id: {id, title, ingredients, steps, note, cooking_time,
        # portions, image}. Planned like ICA recipes.
        self.own_recipes: dict[str, dict] = {}
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
        self.food_rules = [
            make_rule(r["ingredient"], r.get("level", LEVEL_OMIT), r.get("terms"), r.get("except"))
            for r in data.get("food_rules", [])
            if isinstance(r, dict) and r.get("ingredient")
        ]
        self.variants = {
            str(k): v for k, v in (data.get("variants") or {}).items() if isinstance(v, dict)
        }
        self.own_recipes = {
            str(k): v for k, v in (data.get("own_recipes") or {}).items() if isinstance(v, dict)
        }
        _LOGGER.debug(
            "🍽️ Middagsminne laddat: %d middagar, %d preferenser",
            len(self.history),
            len(self.preferences),
        )
        return self

    async def _async_save(self) -> None:
        await self._store.async_save({
            "history": self.history,
            "preferences": self.preferences,
            "food_rules": self.food_rules,
            "variants": self.variants,
            "own_recipes": self.own_recipes,
        })
        for update_callback in list(self._listeners):
            update_callback()

    def recent_ids(self, days: int) -> set[str]:
        """Recipe ids eaten within the last `days` days (0 = nothing)."""
        if days <= 0:
            return set()
        cutoff = dt_util.now().date() - dt.timedelta(days=days)
        ids: set[str] = set()
        for entry in self.history:
            if is_custom_id(entry["id"]):
                continue
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
        changed = False
        for dinner in dinners:
            recipe_id = dinner.get("id")
            if recipe_id is None:
                continue
            date = dinner.get("date") or default_date
            key = (str(recipe_id), date)
            if key in known:
                # Planned again: its rows on the list may have changed
                entry = self.find(str(recipe_id), date)
                changed |= _merge_rows(entry, dinner)
                if entry is not None and list_name:
                    # Saved without the list before, so removing it would look on the wrong one
                    if entry.get("list") is None:
                        entry["list"] = list_name
                        changed = True
                    if entry.get(LEFT_OFF_KEY) != dinner.get(LEFT_OFF_KEY):
                        if dinner.get(LEFT_OFF_KEY):
                            entry[LEFT_OFF_KEY] = list(dinner[LEFT_OFF_KEY])
                        else:
                            entry.pop(LEFT_OFF_KEY, None)
                        changed = True
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
                    # Rules the user overrode by asking for this dish (räkor in a räkpasta)
                    **({"keep": dinner["keep"]} if dinner.get("keep") else {}),
                    # The shopping list rows it needs, and the ones planning it put there
                    **{k: list(dinner[k]) for k in (*LIST_KEYS, LEFT_OFF_KEY) if dinner.get(k)},
                }
            )
            known.add(key)
            taken.add(date)
            added += 1
        if added:
            self._trim()
        if added or changed:
            await self._async_save()
            _LOGGER.debug("🍽️ %d middagar sparade i minnet", added)
        return added

    async def async_add_custom(
        self, title: str, date: str, note: str | None = None, url: str | None = None
    ) -> dict:
        """Save a dinner that has no ICA recipe.

        Raises DayTakenError when `date` has a dinner, DinnerTooOldError when it is past the
        HISTORY_MAX_AGE_DAYS the memory keeps.
        """
        cutoff = dt_util.now().date() - dt.timedelta(days=HISTORY_MAX_AGE_DAYS)
        if (parsed := parse_date(date)) is None or parsed < cutoff:
            raise DinnerTooOldError(date)
        if any(e.get("date") == date for e in self.history):
            raise DayTakenError(date)
        entry = {
            "id": f"{CUSTOM_ID_PREFIX}{uuid.uuid4().hex[:8]}",
            "title": title,
            "date": date,
            "list": None,
            "url": url or None,
            "cooking_time": None,
            "ingredients": [],
            "note": note or None,
            "custom": True,
        }
        self.history.append(entry)
        self._trim()
        await self._async_save()
        return entry

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

    async def async_set_food_rule(
        self,
        ingredient: str,
        level: str = LEVEL_OMIT,
        terms: list[str] | None = None,
        excepts: list[str] | None = None,
    ) -> dict:
        """Add a rule, or replace the one for the same ingredient."""
        rule = make_rule(ingredient, level, terms, excepts)
        key = fold(rule["ingredient"])
        self.food_rules = [r for r in self.food_rules if fold(r["ingredient"]) != key] + [rule]
        await self._async_save()
        return rule

    async def async_remove_food_rule(self, ingredient: str) -> int:
        key = fold(ingredient)
        before = len(self.food_rules)
        self.food_rules = [r for r in self.food_rules if fold(r["ingredient"]) != key]
        removed = before - len(self.food_rules)
        if removed:
            await self._async_save()
        return removed

    def variant(self, recipe_id) -> dict | None:
        return self.variants.get(str(recipe_id))

    async def async_set_variant(
        self,
        recipe_id: str,
        *,
        omit: list[str] | None = None,
        add: list[str] | None = None,
        note: str | None = None,
        title: str | None = None,
    ) -> dict | None:
        """Save the household's own version of an ICA recipe; an empty one removes it."""
        recipe_id = str(recipe_id)
        omit = [line.strip() for line in omit or [] if line and line.strip()]
        add = [line.strip() for line in add or [] if line and line.strip()]
        note = (note or "").strip() or None
        if not omit and not add and not note:
            await self.async_remove_variant(recipe_id)
            return None
        known = next((e.get("title") for e in self.history if str(e.get("id")) == recipe_id), None)
        variant = {"title": title or known, "omit": omit, "add": add, "note": note}
        self.variants[recipe_id] = variant
        await self._async_save()
        return variant

    async def async_remove_variant(self, recipe_id: str) -> bool:
        if self.variants.pop(str(recipe_id), None) is None:
            return False
        await self._async_save()
        return True

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

    def on(self, date: str) -> dict | None:
        """The dinner planned for `date`, if any."""
        return next((e for e in self.history if e.get("date") == date), None)

    async def async_remove_on(self, date: str) -> dict | None:
        """Remove the dinner planned for `date` and return it."""
        entry = self.on(date)
        if entry is not None:
            self.history.remove(entry)
            await self._async_save()
        return entry

    async def async_hand_over(self, rows: dict[str, list[dict]]) -> None:
        """Make the dinners that still need a removed dinner's rows the ones that put them
        there, so removing them later takes the rows off."""
        for row, entries in rows.items():
            for entry in entries:
                for key in LIST_KEYS:
                    if row not in entry.setdefault(key, []):
                        entry[key].append(row)
        if rows:
            await self._async_save()

    async def async_move(self, date: str, new_date: str) -> list[dict]:
        """Move the dinner on `date` to `new_date`; one already there takes its place.

        Returns the dinners that moved. Raises DinnerTooOldError when `new_date` is past
        what the memory keeps.
        """
        cutoff = dt_util.now().date() - dt.timedelta(days=HISTORY_MAX_AGE_DAYS)
        if (parsed := parse_date(new_date)) is None or parsed < cutoff:
            raise DinnerTooOldError(new_date)
        moving, other = self.on(date), self.on(new_date)
        if moving is None or date == new_date:
            return []
        moving["date"] = new_date
        if other is not None:
            other["date"] = date
        await self._async_save()
        return [e for e in (moving, other) if e is not None]

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

    def find_recipe(self, ref: str) -> dict | None:
        """An own recipe by id, or by title (any case)."""
        ref = (ref or "").strip()
        if ref in self.own_recipes:
            return self.own_recipes[ref]
        wanted = ref.lower()
        return next((r for r in self.own_recipes.values() if (r.get("title") or "").lower() == wanted), None)

    async def async_save_recipe(
        self, recipe: dict, recipe_id: str | None = None, on: str | None = None
    ) -> dict:
        """Save an own recipe, or replace the one with `recipe_id`. Coming dinners of it show
        the new version. With `on` (a date), the recipe also takes the place of that day's
        dinner, which keeps its shopping list rows. Raises KeyError for an unknown `recipe_id`."""
        if recipe_id is not None and recipe_id not in self.own_recipes:
            raise KeyError(recipe_id)
        recipe_id = recipe_id or f"{OWN_ID_PREFIX}{uuid.uuid4().hex[:8]}"
        saved = {"id": recipe_id, **{k: v for k, v in recipe.items() if v not in (None, "", [])}}
        self.own_recipes[recipe_id] = saved
        today = dt_util.now().date().isoformat()
        replaced = self.on(on) if on else None
        for entry in self.history:
            if entry is replaced or (entry.get("id") == recipe_id and (entry.get("date") or "") >= today):
                entry.update(
                    id=recipe_id,
                    title=saved["title"],
                    ingredients=list(saved.get("ingredients") or []),
                    cooking_time=saved.get("cooking_time"),
                    url=saved.get("url"),
                )
        await self._async_save()
        return saved

    async def async_note_rows(
        self, entry: dict, on_list: list[str] = (), put: list[str] = (), drop: list[str] = ()
    ) -> None:
        """Change the list rows a dinner needs (on_list) and the ones it put there."""
        for key, new in (("on_list", on_list), ("put_on_list", put)):
            rows = [row for row in entry.get(key) or [] if row not in drop]
            rows += [row for row in new if row not in rows]
            if rows:
                entry[key] = rows
            else:
                entry.pop(key, None)
        await self._async_save()

    async def async_remove_recipe(self, ref: str) -> dict | None:
        """Remove an own recipe; dinners already planned with it stay."""
        recipe = self.find_recipe(ref)
        if recipe is not None:
            del self.own_recipes[recipe["id"]]
            await self._async_save()
        return recipe

    async def async_clear(
        self, history: bool = True, preferences: bool = True
    ) -> dict:
        """Empty the history and/or the preferences (with the food rules and the household's
        own recipe versions); returns what was removed."""
        removed = {"history": 0, "preferences": 0}
        if history:
            removed["history"] = len(self.history)
            self.history = []
        if preferences:
            removed["preferences"] = len(self.preferences) + len(self.food_rules) + len(self.variants)
            self.preferences = []
            self.food_rules = []
            self.variants = {}
        if history or preferences:
            await self._async_save()
        return removed

    def as_dict(self) -> dict:
        # Ingredients are for the calendar and list rows for the dashboard; leaving them out
        # keeps GetMemory small for the LLM.
        hidden = {"ingredients", *LIST_KEYS, LEFT_OFF_KEY}
        history = [{k: v for k, v in e.items() if k not in hidden} for e in self.history]
        return {
            "history": sorted(history, key=lambda e: e.get("date") or "", reverse=True),
            "preferences": list(self.preferences),
            "food_rules": [dict(r) for r in self.food_rules],
            "variants": {k: dict(v) for k, v in self.variants.items()},
            # Titles only; the recipes themselves are in get_own_recipes
            "own_recipes": [
                {"id": r["id"], "title": r.get("title")} for r in self.own_recipes.values()
            ],
        }


def _merge_rows(entry: dict | None, dinner: dict) -> bool:
    """Add a re-planned dinner's list rows to its entry; True when that changed it."""
    changed = False
    for key in LIST_KEYS:
        new = [row for row in dinner.get(key) or [] if row not in (entry or {}).get(key, [])]
        if entry is not None and new:
            entry.setdefault(key, []).extend(new)
            changed = True
    return changed


def parse_date(value) -> dt.date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None
