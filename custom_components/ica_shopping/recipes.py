"""Dinner planning from ICA recipes, ported from kanylbullen/ica-mcp's plan_dinners."""
from __future__ import annotations

import logging
import random
import re
from typing import Iterable

from .const import MAX_ICA_ITEMS, RECIPE_IMAGE_BASE
from .ica_api import ICAApi, IcaApiError, IcaError, list_matches
from .memory import is_custom_id

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


def _ingredient_name(ing: dict) -> str:
    return (ing.get("ingredient") or ing.get("text") or "").strip()


def _group_by_name(recipes: list[dict]) -> dict[str, list[tuple[dict, dict]]]:
    """(ingredient, recipe) pairs keyed by lower-cased ingredient name, in first-seen order."""
    groups: dict[str, list[tuple[dict, dict]]] = {}
    for recipe in recipes:
        for ing in _ingredients(recipe):
            if name := _ingredient_name(ing):
                groups.setdefault(name.lower(), []).append((ing, recipe))
    return groups


def _summed_text(name: str, ings: list[dict]) -> str | None:
    units = {ing.get("unit") or "" for ing in ings}
    qtys = [ing.get("quantity") for ing in ings]
    if len(units) == 1 and all(isinstance(q, (int, float)) for q in qtys) and any(qtys):
        unit = ings[0].get("unit")
        return f"{sum(qtys):g}{(' ' + unit) if unit else ''} {name}".strip()
    return None


def _distinct_lines(ings: list[dict]) -> list[str]:
    return [t for t in dict.fromkeys((ing.get("text") or "").strip() for ing in ings) if t]


def aggregate_ingredients(recipes: list[dict]) -> list[dict]:
    """Merge ingredients across recipes into [{name, text}], keeping first-seen order.

    Same ingredient + same unit is summed ('2 dl' + '3 dl mjölk' → '5 dl mjölk');
    otherwise each distinct original line is kept.
    """
    out: list[dict] = []
    for pairs in _group_by_name(recipes).values():
        ings = [ing for ing, _ in pairs]
        name = _ingredient_name(ings[0])
        if (text := _summed_text(name, ings)) is not None:
            out.append({"name": name, "text": text})
        else:
            out.extend({"name": name, "text": line} for line in _distinct_lines(ings))
    return out


def group_ingredients(recipes: list[dict], skip_names: Iterable[str] = ()) -> list[dict]:
    """One [{name, text, lines, dinners}] entry per ingredient name across the recipes.

    text is the summed amount when aggregate_ingredients would sum it, otherwise the
    distinct lines joined with ' + '. Names in skip_names (lower-cased) are left out.
    """
    skip = {norm(n) for n in skip_names}
    out: list[dict] = []
    for key, pairs in _group_by_name(recipes).items():
        if key in skip:
            continue
        ings = [ing for ing, _ in pairs]
        name = _ingredient_name(ings[0])
        lines = _distinct_lines(ings)
        out.append({
            "name": name,
            "text": _summed_text(name, ings) or " + ".join(lines),
            "lines": lines,
            "dinners": [t for t in dict.fromkeys(r.get("title") for _, r in pairs) if t],
        })
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


def event_description(recipe: dict) -> str:
    return format_description(recipe.get("cookingTime"), recipe_url(recipe), recipe_ingredients(recipe))


def format_description(cooking_time, url, ingredients, note=None) -> str:
    parts = [str(p) for p in (note, cooking_time, url) if p]
    lines = [f"• {line}" for line in ingredients or [] if line]
    return "\n\n".join(p for p in ("\n".join(parts), "\n".join(lines)) if p)


def recipe_ingredients(recipe: dict) -> list[str]:
    """The recipe's ingredient lines, as written on ica.se."""
    return [
        text
        for ing in _ingredients(recipe)
        if (text := (ing.get("text") or "").strip())
    ]


def dinner_summary(recipe: dict) -> dict:
    return {
        "id": recipe.get("id"),
        "title": recipe.get("title"),
        "cooking_time": recipe.get("cookingTime"),
        "url": recipe_url(recipe),
        "ingredients": recipe_ingredients(recipe),
    }


def search_summary(result: dict) -> dict:
    """A recipe search hit, with the field names dinner_summary uses."""
    image = result.get("imageUrl")
    if isinstance(image, str) and image and not image.startswith("http"):
        image = f"{RECIPE_IMAGE_BASE}/{image.lstrip('/')}"
    return {
        "id": result.get("id"),
        "title": result.get("title"),
        "cooking_time": result.get("cookingTime"),
        "url": recipe_url(result),
        "image": image or None,
        "ingredient_count": result.get("numberOfIngredients"),
    }


async def async_fetch_recipes(
    api: ICAApi,
    count: int,
    source: str,
    recipe_ids: list | None = None,
    avoid_ids: Iterable | None = None,
) -> list[dict]:
    avoid = {str(i) for i in (avoid_ids or ())}
    if recipe_ids:
        # Committing a plan the caller already picked: fetch exactly those recipes.
        # Custom dinners have no ICA recipe to fetch.
        picked = [r for r in dict.fromkeys(str(r) for r in recipe_ids) if not is_custom_id(r)]
        return [r for r in [await api.async_get_recipe(i) for i in picked] if r]

    if source == SOURCE_FAVORITES:
        ids = [
            i
            for i in dict.fromkeys(await api.async_get_favorite_recipe_ids())
            if str(i) not in avoid
        ]
        picked = random.sample(ids, min(count, len(ids)))
        return [r for r in [await api.async_get_recipe(i) for i in picked] if r]

    recipes: dict = {}
    # Random picks can repeat, and recently eaten ones are skipped, so allow extra calls.
    for _ in range(count * 2 + len(avoid)):
        if len(recipes) >= count:
            break
        recipe = await api.async_get_random_recipe()
        if recipe and recipe.get("id") not in recipes and str(recipe.get("id")) not in avoid:
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
    preview: bool = False,
    add_to_list: bool = True,
    recipe_ids: list | None = None,
    avoid_ids: Iterable | None = None,
) -> tuple[dict | None, list[dict], dict]:
    """Return (target list, recipes, service response).

    With preview=True the list is left untouched and the response carries the menu and the
    merged ingredients, so a caller can show a suggestion before committing it.
    add_to_list=False also leaves the list untouched, for callers that shop elsewhere.
    """
    recipes = await async_fetch_recipes(api, count, source, recipe_ids, avoid_ids)
    if not recipes:
        raise NoRecipesError(f"No {source} recipes returned")

    skip_names = {norm(n) for n in never_add}
    planned = [ing for ing in aggregate_ingredients(recipes) if norm(ing["name"]) not in skip_names]

    if preview or not add_to_list:
        return None, recipes, {
            "list": None,
            "preview": preview,
            "dinners": [dinner_summary(r) for r in recipes],
            "ingredients": [ing["text"] for ing in planned],
            "ingredient_groups": group_ingredients(recipes, skip_names),
            "added": [],
            "skipped_list_full": [],
        }

    target = await async_resolve_or_create_list(api, list_ref, default_list_id)
    # Re-fetch: the overview may omit rows, and active rows decide what to skip.
    target = await api.async_get_list(target["id"]) or target

    rows = target.get("rows", [])
    present = {norm(r["text"]) for r in rows if not r.get("isStriked")}
    struck = {norm(r["text"]): r for r in rows if r.get("isStriked") and r.get("id")}
    room = MAX_ICA_ITEMS - len(rows)
    added: list[str] = []
    skipped: list[str] = []
    for ing in planned:
        name_key, text_key = norm(ing["name"]), norm(ing["text"])
        if name_key in present or text_key in present:
            continue
        # Reuse a checked-off row instead of adding a duplicate next to it.
        old = struck.pop(text_key, None) or struck.pop(name_key, None)
        if old is not None:
            if await api.set_striked(target["id"], old["id"], False, row=old.get("raw")):
                added.append(old["text"])
                present.add(norm(old["text"]))
            continue
        if room <= 0:
            skipped.append(ing["text"])
            continue
        if await api.add_to_list(target["id"], ing["text"]):
            added.append(ing["text"])
            present.add(text_key)
            room -= 1
    if skipped:
        _LOGGER.warning(
            "⚠️ ICA-listan är full (%d rader); hoppade över %d ingredienser", MAX_ICA_ITEMS, len(skipped)
        )

    return target, recipes, {
        "list": target.get("name"),
        "preview": False,
        "dinners": [dinner_summary(r) for r in recipes],
        "ingredients": [ing["text"] for ing in planned],
        "added": added,
        "skipped_list_full": skipped,
    }
