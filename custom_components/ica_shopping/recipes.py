"""Dinner planning from ICA recipes, ported from kanylbullen/ica-mcp's plan_dinners."""
from __future__ import annotations

import logging
import random
import re
from typing import Iterable

from .ica_api import ICAApi, IcaApiError, IcaError, list_matches

_LOGGER = logging.getLogger(__name__)

SOURCE_RANDOM = "random"
SOURCE_FAVORITES = "favorites"
RECIPE_WEB_BASE = "https://www.ica.se"
_SLUG_CHARS = str.maketrans({"å": "a", "ä": "a", "ö": "o", "é": "e", "è": "e", "ü": "u"})


class NoRecipesError(IcaError):
    """ICA returned no recipes (or the account has no favorites)."""


def norm(text) -> str:
    return (text or "").strip().lower()


def _ingredients(recipe: dict) -> Iterable[dict]:
    for grp in recipe.get("ingredientGroups") or []:
        for ing in grp.get("ingredients") or []:
            if isinstance(ing, dict):
                yield ing


def aggregate_ingredients(recipes: list[dict]) -> list[dict]:
    """Merge ingredients across recipes into [{name, text}], keeping first-seen order.

    Same ingredient + same unit is summed ('2 dl' + '3 dl mjölk' → '5 dl mjölk');
    otherwise each distinct original line is kept.
    """
    groups: dict[str, list[dict]] = {}
    for recipe in recipes:
        for ing in _ingredients(recipe):
            name = (ing.get("ingredient") or ing.get("text") or "").strip()
            if name:
                groups.setdefault(name.lower(), []).append(ing)

    out: list[dict] = []
    for ings in groups.values():
        name = (ings[0].get("ingredient") or ings[0].get("text") or "").strip()
        units = {ing.get("unit") or "" for ing in ings}
        qtys = [ing.get("quantity") for ing in ings]
        if len(units) == 1 and all(isinstance(q, (int, float)) for q in qtys) and any(qtys):
            unit = ings[0].get("unit")
            text = f"{sum(qtys):g}{(' ' + unit) if unit else ''} {name}".strip()
            out.append({"name": name, "text": text})
        else:
            for text in dict.fromkeys((ing.get("text") or "").strip() for ing in ings):
                if text:
                    out.append({"name": name, "text": text})
    return out


def recipe_url(recipe: dict) -> str | None:
    for key in ("url", "absoluteUrl", "shareUrl", "webUrl"):
        value = recipe.get(key)
        if isinstance(value, str) and value:
            return value if value.startswith("http") else f"{RECIPE_WEB_BASE}/{value.lstrip('/')}"
    recipe_id, title = recipe.get("id"), recipe.get("title")
    if not recipe_id or not title:
        return None
    # ica.se recipe pages live at /recept/<slugified-title>-<id>/.
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower().translate(_SLUG_CHARS)).strip("-")
    return f"{RECIPE_WEB_BASE}/recept/{slug}-{recipe_id}/"


def dinner_summary(recipe: dict) -> dict:
    return {
        "title": recipe.get("title"),
        "cooking_time": recipe.get("cookingTime"),
        "url": recipe_url(recipe),
    }


async def async_fetch_recipes(api: ICAApi, count: int, source: str) -> list[dict]:
    if source == SOURCE_FAVORITES:
        ids = list(dict.fromkeys(await api.async_get_favorite_recipe_ids()))
        picked = random.sample(ids, min(count, len(ids)))
        return [r for r in [await api.async_get_recipe(i) for i in picked] if r]

    recipes: dict = {}
    # Random picks can repeat, so allow a few extra calls to reach `count` distinct recipes.
    for _ in range(count * 2):
        if len(recipes) >= count:
            break
        recipe = await api.async_get_random_recipe()
        if recipe and recipe.get("id") not in recipes:
            recipes[recipe.get("id")] = recipe
    return list(recipes.values())


async def async_resolve_or_create_list(api: ICAApi, ref: str | None, default_id: str) -> dict:
    lists = await api.async_get_lists()
    if not ref or not ref.strip():
        found = next((L for L in lists if list_matches(L, default_id)), None)
        if found is None:
            raise IcaApiError(f"Configured list {default_id} not found")
        return found
    wanted = norm(ref)
    found = next((L for L in lists if norm(L.get("name")) == wanted or list_matches(L, ref)), None)
    if found is not None:
        return found
    _LOGGER.info("📝 Skapar ICA-lista '%s'", ref.strip())
    return await api.async_create_list(ref.strip())


async def async_plan_dinners(
    api: ICAApi,
    *,
    count: int,
    source: str,
    list_ref: str | None,
    default_list_id: str,
    never_add: Iterable[str],
) -> tuple[dict, dict]:
    """Return (target list, service response)."""
    recipes = await async_fetch_recipes(api, count, source)
    if not recipes:
        raise NoRecipesError(f"No {source} recipes returned")
    target = await async_resolve_or_create_list(api, list_ref, default_list_id)
    # Re-fetch: the overview may omit rows, and active rows decide what to skip.
    target = await api.async_get_list(target["id"]) or target

    skip_names = {norm(n) for n in never_add}
    present = {norm(r["text"]) for r in target.get("rows", []) if not r.get("isStriked")}
    added: list[str] = []
    for ing in aggregate_ingredients(recipes):
        name_key, text_key = norm(ing["name"]), norm(ing["text"])
        if name_key in skip_names or name_key in present or text_key in present:
            continue
        if await api.add_to_list(target["id"], ing["text"]):
            added.append(ing["text"])

    return target, {
        "list": target.get("name"),
        "dinners": [dinner_summary(r) for r in recipes],
        "added": added,
    }
