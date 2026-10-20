"""Dinner planning from ICA recipes, ported from kanylbullen/ica-mcp's plan_dinners."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
from typing import Iterable

from .const import MAX_ICA_ITEMS, RECIPE_IMAGE_BASE
from .ica_api import ICAApi, IcaApiError, IcaError, list_matches
from .matching import covers
from .memory import LEFT_OFF_KEY, is_custom_id, is_own_id
from .products import async_match_products
from .rules import LEVEL_NEVER, adjust, fold, is_theme, mentions, touched_rules

_LOGGER = logging.getLogger(__name__)

SOURCE_RANDOM = "random"
SOURCE_FAVORITES = "favorites"
SOURCE_OWN = "own"
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


def _summed_amount(ings: list[dict]) -> tuple[float, str | None] | None:
    units = {ing.get("unit") or "" for ing in ings}
    qtys = [ing.get("quantity") for ing in ings]
    if len(units) == 1 and all(isinstance(q, (int, float)) for q in qtys) and any(qtys):
        return sum(qtys), ings[0].get("unit")
    return None


def _summed_text(name: str, ings: list[dict]) -> str | None:
    if (amount := _summed_amount(ings)) is None:
        return None
    quantity, unit = amount
    return f"{quantity:g}{(' ' + unit) if unit else ''} {name}".strip()


def summed_ingredients(recipes: list[dict], skip_names: Iterable[str] = ()) -> list[dict]:
    """[{name, text, quantity, unit}] for each ingredient whose lines add up to one amount."""
    skip = {norm(n) for n in skip_names}
    out = []
    for key, pairs in _group_by_name(recipes).items():
        ings = [ing for ing, _ in pairs]
        if key in skip or (amount := _summed_amount(ings)) is None:
            continue
        name = _ingredient_name(ings[0])
        out.append({"name": name, "text": _summed_text(name, ings), "quantity": amount[0], "unit": amount[1]})
    return out


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
    if is_own_id(recipe.get("id")):
        return recipe.get("url") or None
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


_UNITS = (
    "st|g|kg|hg|dl|cl|ml|l|msk|tsk|krm|förp|paket|burkar|burk|påsar|påse|knippen|knippe|krukor|kruka"
    "|klyftor|klyfta|skivor|skiva|portioner|port"
)
_AMOUNT = re.compile(
    rf"^(?P<qty>\d+(?:[.,]\d+)?|\d+/\d+|[½¼¾])\s*(?:(?P<unit>{_UNITS})\b\.?)?\s+(?P<name>\S.*)$",
    re.IGNORECASE,
)
_FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75}


def own_ingredient(line: str) -> dict:
    """An own recipe's line in the shape ICA's ingredients have, so equal amounts add up with
    other recipes': "2 dl grädde" → 2, "dl", "grädde". A line without an amount stays whole."""
    line = line.strip()
    if not (match := _AMOUNT.match(line)):
        return {"ingredient": line, "text": line}
    qty = match["qty"]
    if qty in _FRACTIONS:
        quantity = _FRACTIONS[qty]
    elif "/" in qty:
        top, bottom = qty.split("/")
        quantity = int(top) / int(bottom) if int(bottom) else None
    else:
        quantity = float(qty.replace(",", "."))
    unit = (match["unit"] or "").lower() or None
    return {"ingredient": match["name"].strip(), "text": line, "quantity": quantity, "unit": unit}


def own_recipe(recipe: dict) -> dict:
    """One of the household's own recipes in the shape ICA's API gives, for planning."""
    return {
        "id": recipe["id"],
        "title": recipe.get("title"),
        "cookingTime": recipe.get("cooking_time"),
        "url": recipe.get("url"),
        "ingredientGroups": [
            {"ingredients": [own_ingredient(line) for line in recipe.get("ingredients") or [] if line.strip()]}
        ],
    }


def entry_description(entry: dict, rules: Iterable[dict] = (), variants: dict | None = None) -> str:
    """A remembered dinner's calendar description, with what the food rules and the household's
    own version of the recipe leave out and add. Worked out from the rules as they are now, so a
    new rule shows up on dinners that are already planned."""
    ingredients = entry.get("ingredients") or []
    variant = (variants or {}).get(str(entry.get("id")))
    omitted, extra = adjust(ingredients, rules, variant, entry.get("keep") or ())
    return format_description(
        entry.get("cooking_time"),
        entry.get("url"),
        ingredients,
        note=entry.get("note"),
        omitted=omitted,
        extra=extra,
        variant=variant,
        own=entry.get("id") if is_own_id(entry.get("id")) else None,
    )


def cooked_lines(entry: dict, rules: Iterable[dict] = (), variants: dict | None = None) -> list[str]:
    """A remembered dinner's ingredient lines as it will be cooked: what the food rules and the
    household's own version leave out taken away, and what that version adds put in."""
    ingredients = entry.get("ingredients") or []
    variant = (variants or {}).get(str(entry.get("id")))
    omitted, extra = adjust(ingredients, rules, variant, entry.get("keep") or ())
    left_out = {o["line"] for o in omitted}
    return [line for line in ingredients if line not in left_out] + list(extra)


def format_description(
    cooking_time, url, ingredients, note=None, omitted=(), extra=(), variant=None, own=None
) -> str:
    """"Eget recept (own-…)" for one of the household's own recipes, so a dashboard can fetch
    its steps; the note, cooking time and link; the ingredients as "• 2 dl grädde"; then one line per
    change: "Utan: 1 gul lök (lök)" (the rule in brackets, none for the household's own
    version), "Lägg till: 2 morötter", and "Vår version[: note]" when there is one."""
    parts = [str(p) for p in (own and f"Eget recept ({own})", note, cooking_time, url) if p]
    lines = [f"• {line}" for line in ingredients or [] if line]
    changes = [
        f"Utan: {o['line']} ({o['rule']})" if o.get("rule") else f"Utan: {o['line']}" for o in omitted
    ]
    changes += [f"Lägg till: {line}" for line in extra]
    if variant:
        changes.append(f"Vår version: {variant['note']}" if variant.get("note") else "Vår version")
    return "\n\n".join(p for p in ("\n".join(parts), "\n".join(lines), "\n".join(changes)) if p)


def recipe_ingredients(recipe: dict) -> list[str]:
    """The recipe's ingredient lines, as written on ica.se."""
    return [
        text
        for ing in _ingredients(recipe)
        if (text := (ing.get("text") or "").strip())
    ]


def dinner_summary(recipe: dict, adjusted: dict | None = None) -> dict:
    """The dinner as the response and the memory see it. `adjusted` adds what the food rules
    leave out ("omitted" lines), what the household's own version adds ("extra"), and the rules
    the user overrode by asking for the dish ("keep")."""
    summary = {
        "id": recipe.get("id"),
        "title": recipe.get("title"),
        "cooking_time": recipe.get("cookingTime"),
        "url": recipe_url(recipe),
        "ingredients": recipe_ingredients(recipe),
    }
    if adjusted and adjusted["omitted"]:
        summary["omitted"] = [o["line"] for o in adjusted["omitted"]]
    if adjusted and adjusted["extra"]:
        summary["extra"] = adjusted["extra"]
    if adjusted and adjusted["keep"]:
        summary["keep"] = adjusted["keep"]
    return summary


def _without(recipe: dict, omitted: list[dict], extra: list[str]) -> dict:
    """The recipe as it will be cooked, for the shopping list: omitted lines out, extras in."""
    if not omitted and not extra:
        return recipe
    left_out = {fold(o["line"]) for o in omitted}
    groups = [
        {**grp, "ingredients": [
            ing for ing in grp.get("ingredients") or []
            if not isinstance(ing, dict) or fold(ing.get("text")) not in left_out
        ]}
        for grp in recipe.get("ingredientGroups") or []
    ]
    if extra:
        groups.append({"ingredients": [{"ingredient": line, "text": line} for line in extra]})
    return {**recipe, "ingredientGroups": groups}


async def _async_theme_rules(api: ICAApi, recipe: dict, rules: list[dict]) -> list[dict]:
    """Of `rules` (all in the dish), the ones it is built around. Only fetches ICA's tags when
    the title doesn't settle it."""
    themed = [r for r in rules if mentions(r, recipe.get("title"))]
    rest = [r for r in rules if r not in themed]
    if rest:
        tags = await api.async_get_recipe_categories(recipe)
        themed += [r for r in rest if is_theme(r, recipe.get("title"), tags)]
    return themed


async def _async_blocked(api: ICAApi, recipe: dict, rules: list[dict]) -> bool:
    """Whether planning should pass the recipe by: it has a never-ingredient, or it is built
    around an ingredient the household would rather be without."""
    touched = touched_rules(rules, recipe.get("title"), recipe_ingredients(recipe))
    if any(r["level"] == LEVEL_NEVER for r in touched):
        return True
    return bool(touched) and bool(await _async_theme_rules(api, recipe, touched))


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
    rules: Iterable[dict] = (),
    own_recipes: dict | None = None,
) -> list[dict]:
    """The recipes to plan. Suggestions pass by recipes the food rules rule out; recipes the
    caller picked are always fetched, since the user asked for them. `own_recipes` are the
    household's own (memory.own_recipes), planned by their "own-" ids or with source "own"."""
    avoid = {str(i) for i in (avoid_ids or ())}
    rules = list(rules)
    own_recipes = own_recipes or {}
    if recipe_ids:
        # Committing a plan the caller already picked: fetch exactly those recipes.
        picked = [r for r in dict.fromkeys(str(r) for r in recipe_ids) if not is_custom_id(r)]
        return [
            r for r in [
                (own_recipe(own_recipes[i]) if i in own_recipes else None)
                if is_own_id(i) else await api.async_get_recipe(i)
                for i in picked
            ] if r
        ]

    if source == SOURCE_OWN:
        candidates = [own_recipe(r) for i, r in own_recipes.items() if i not in avoid]
        random.shuffle(candidates)
        picked = []
        for recipe in candidates:
            if len(picked) >= count:
                break
            if not await _async_blocked(api, recipe, rules):
                picked.append(recipe)
        return picked

    if source == SOURCE_FAVORITES:
        ids = [
            i
            for i in dict.fromkeys(await api.async_get_favorite_recipe_ids())
            if str(i) not in avoid
        ]
        random.shuffle(ids)
        picked: list[dict] = []
        for recipe_id in ids:
            if len(picked) >= count:
                break
            recipe = await api.async_get_recipe(recipe_id)
            if recipe and not await _async_blocked(api, recipe, rules):
                picked.append(recipe)
        return picked

    recipes: dict = {}
    # Random picks can repeat, and recently eaten or ruled-out ones are skipped, so allow extra calls.
    for _ in range(count * (4 if rules else 2) + len(avoid)):
        if len(recipes) >= count:
            break
        recipe = await api.async_get_random_recipe()
        if (
            recipe
            and recipe.get("id") not in recipes
            and str(recipe.get("id")) not in avoid
            and not await _async_blocked(api, recipe, rules)
        ):
            recipes[recipe.get("id")] = recipe
    return list(recipes.values())


async def _async_adjust(
    api: ICAApi, recipe: dict, rules: list[dict], variants: dict, picked: bool
) -> dict:
    """{omitted, extra, keep, conflicts} for one recipe. A picked recipe built around a rule's
    ingredient keeps it (räkor in the räkpasta the user asked for); the rest is left out."""
    lines = recipe_ingredients(recipe)
    keep: list[str] = []
    conflicts: list[dict] = []
    if picked and (touched := touched_rules(rules, recipe.get("title"), lines)):
        for rule in await _async_theme_rules(api, recipe, touched):
            keep.append(rule["ingredient"])
            conflicts.append({
                "id": recipe.get("id"),
                "title": recipe.get("title"),
                "ingredient": rule["ingredient"],
                "level": rule["level"],
            })
    omitted, extra = adjust(lines, rules, variants.get(str(recipe.get("id"))), keep)
    return {"omitted": omitted, "extra": extra, "keep": keep, "conflicts": conflicts}


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


def covering_row(rows: Iterable[str], ing: dict) -> str | None:
    """The list row that already has the planned ingredient: one reading as its name or line,
    else one worded otherwise that covers the line ("Varmkorv" for "8 varmkorvar")."""
    rows = list(rows)
    keys = {norm(ing["name"]), norm(ing["text"])}
    return next((r for r in rows if norm(r) in keys), None) or next(
        (r for r in rows if covers(r, ing["text"])), None
    )


async def _async_rows_now(api: ICAApi, list_ref: str | None, default_list_id: str) -> list[str] | None:
    """The unchecked rows of the list a plan would go on, without creating it; None when it
    can't be read."""
    try:
        lists = await api.async_get_lists()
        if list_ref and list_ref.strip():
            found = next((L for L in lists if norm(L.get("name")) == norm(list_ref) or list_matches(L, list_ref)), None)
        else:
            found = next((L for L in lists if list_matches(L, default_list_id)), None)
        if found is None:
            return [] if list_ref else None
        found = await api.async_get_list(found["id"]) or found
        return [r["text"] for r in found.get("rows", []) if r.get("text") and not r.get("isStriked")]
    except Exception as err:  # noqa: BLE001 - the preview stands without on_list
        _LOGGER.debug("🛒 Kunde inte läsa listan för förhandsvisningen: %s", err)
        return None


def _mark_on_list(groups: list[dict], planned: list[dict], rows: list[str]) -> None:
    """Give each ingredient group the row that already covers it ("on_list"), when one row
    covers everything the plan would add for it."""
    for group in groups:
        mine = [ing for ing in planned if norm(ing["name"]) == norm(group["name"])]
        found = [covering_row(rows, ing) for ing in mine]
        if found and all(found):
            group["on_list"] = found[0]


async def async_add_to_list(
    api: ICAApi, planned: list[dict], list_ref: str | None, default_list_id: str
) -> tuple[dict, list[str], list[str], list[tuple[str, bool] | None]]:
    """Add the planned ingredients to the list.

    Returns (list, added, skipped_list_full, on_list): on_list has, for each planned
    ingredient, the row that covers it now and whether this plan put it there (added or
    picked up again rather than already on the list), or None when it isn't on the list.
    """
    target = await async_resolve_or_create_list(api, list_ref, default_list_id)
    # Re-fetch: the overview may omit rows, and active rows decide what to skip.
    target = await api.async_get_list(target["id"]) or target

    rows = target.get("rows", [])
    # Key -> the row's text; None for an ingredient left off a full list
    present: dict[str, str | None] = {norm(r["text"]): r["text"] for r in rows if not r.get("isStriked")}
    struck = {norm(r["text"]): r for r in rows if r.get("isStriked") and r.get("id")}
    room = MAX_ICA_ITEMS - len(rows)
    added: list[str] = []
    skipped: list[str] = []
    on_list: list[tuple[str, bool] | None] = []
    for ing in planned:
        name_key, text_key = norm(ing["name"]), norm(ing["text"])
        # Shares a row with an ingredient left off a full list
        if any(k in present and present[k] is None for k in (name_key, text_key)):
            on_list.append(None)
            continue
        if (row := covering_row((t for t in present.values() if t), ing)) is not None:
            on_list.append((row, False))
            continue
        # Reuse a checked-off row instead of adding a duplicate next to it.
        old = (
            struck.pop(text_key, None)
            or struck.pop(name_key, None)
            or struck.pop(next((k for k, r in struck.items() if covers(r["text"], ing["text"])), ""), None)
        )
        if old is not None:
            if await api.set_striked(target["id"], old["id"], False, row=old.get("raw")):
                added.append(old["text"])
                present[norm(old["text"])] = old["text"]
                on_list.append((old["text"], True))
            else:
                on_list.append(None)
            continue
        if room <= 0:
            skipped.append(ing["text"])
            # Ingredients sharing one product share a row; report it once.
            present[text_key] = None
            on_list.append(None)
            continue
        if await api.add_to_list(target["id"], ing["text"]):
            added.append(ing["text"])
            present[text_key] = ing["text"]
            on_list.append((ing["text"], True))
            room -= 1
        else:
            on_list.append(None)
    if skipped:
        _LOGGER.warning(
            "⚠️ ICA-listan är full (%d rader); hoppade över %d ingredienser", MAX_ICA_ITEMS, len(skipped)
        )
    return target, added, skipped, on_list


def _rows_per_dinner(
    cooked: list[dict], planned: list[dict], on_list: list[tuple[str, bool] | None]
) -> list[dict]:
    """For each recipe, the list rows its ingredients are on ("on_list") and the ones of
    those this plan put there ("put_on_list").

    A row with one of the recipe's own lines is that recipe's; a summed or product row
    ("4.5 dl vispgrädde") belongs to every recipe with the ingredient.
    """
    index = {id(recipe): i for i, recipe in enumerate(cooked)}
    groups = _group_by_name(cooked)
    rows: list[dict] = [{"on_list": [], "put_on_list": []} for _ in cooked]
    for ing, covered in zip(planned, on_list):
        if covered is None:
            continue
        row, put = covered
        pairs = groups.get(norm(ing["name"]), [])
        own = [r for i, r in pairs if (i.get("text") or "").strip() == ing["text"]]
        for recipe in own or [r for _, r in pairs]:
            mine = rows[index[id(recipe)]]
            for key, wanted in (("on_list", True), ("put_on_list", put)):
                if wanted and row not in mine[key]:
                    mine[key].append(row)
    return rows


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
    match_products: bool = False,
    store_account: str | None = None,
    list_lock: asyncio.Lock | None = None,
    rules: Iterable[dict] = (),
    variants: dict | None = None,
    own_recipes: dict | None = None,
    items: Iterable[str] | None = None,
) -> tuple[dict | None, list[dict], dict]:
    """Return (target list, recipes, service response).

    With preview=True the list is left untouched and the response carries the menu and the
    merged ingredients, so a caller can show a suggestion before committing it.
    add_to_list=False also leaves the list untouched, for callers that shop elsewhere.
    match_products=True swaps recipe amounts for an ICA product and package count
    where one fits at the store with `store_account` (none without one), listed in "products".
    Only the list update holds `list_lock`, so slow searches don't block the to-do sync.
    `rules` (food rules) pass suggestions by and leave ingredients out; `variants` (the
    household's own versions, by recipe id) leave out and add lines too. `own_recipes` are the
    household's own recipes, planned by id or with source "own". Each dinner then has
    "omitted" and "extra", and "rule_conflicts" lists picked dishes built around a rule's ingredient.
    Once the list is updated, each dinner has the rows it is on in "on_list", and the ones this
    plan put there in "put_on_list", so removing the dinner can take them off again.
    `items` (ingredient group names, any case) limits what goes on the list to those; the rest
    are left off this time only, and each dinner names its own in "left_off". A preview's
    ingredient groups then still has them all, and each group says which row already covers it
    in "on_list".
    """
    rules = list(rules)
    recipes = await async_fetch_recipes(api, count, source, recipe_ids, avoid_ids, rules, own_recipes)
    skip_names = {norm(n) for n in never_add}
    only_custom = bool(recipe_ids) and all(is_custom_id(r) for r in recipe_ids)
    if not recipes and not only_custom:
        raise NoRecipesError(f"No {source} recipes returned")
    adjusted = [await _async_adjust(api, r, rules, variants or {}, bool(recipe_ids)) for r in recipes]
    dinners = [dinner_summary(r, a) for r, a in zip(recipes, adjusted)]
    conflicts = [c for a in adjusted for c in a["conflicts"]]
    if conflicts:
        _LOGGER.debug("🍽️ Valda rätter som bygger på en undvikt ingrediens: %s", conflicts)
    # The recipes as they will be cooked, so left-out ingredients stay off the list
    cooked = [_without(r, a["omitted"], a["extra"]) for r, a in zip(recipes, adjusted)]
    # Every ingredient the plan could add; `planned` is what it will add
    everything = [ing for ing in aggregate_ingredients(cooked) if norm(ing["name"]) not in skip_names]
    extra: dict = {}
    left_off: set[str] = set()
    if items is not None:
        chosen = {norm(i) for i in items}
        names = {norm(ing["name"]) for ing in everything}
        left_off = names - chosen
        if unknown := [i for i in items if norm(i) not in names]:
            extra["items_not_found"] = unknown
    if match_products:
        # Without the store, ICA's all-store search is too noisy to pick products from.
        products = [] if not store_account else await async_match_products(
            api, summed_ingredients(cooked, skip_names | left_off), store_account
        )
        if not store_account:
            extra["products_skipped"] = "no_store"
        swaps = {norm(p["ingredient"]): p["text"] for p in products}
        everything = [{**ing, "text": swaps.get(norm(ing["name"]), ing["text"])} for ing in everything]
        extra["products"] = products
    planned = [ing for ing in everything if norm(ing["name"]) not in left_off]

    if conflicts:
        extra["rule_conflicts"] = conflicts

    if preview or not add_to_list or not recipes:
        groups = group_ingredients(cooked, skip_names)
        if preview and (rows := await _async_rows_now(api, list_ref, default_list_id)) is not None:
            # Every group, chosen or not, so a screen can tick what isn't on the list yet
            _mark_on_list(groups, everything, rows)
        return None, recipes, {
            "list": None,
            "preview": preview,
            "dinners": dinners,
            "ingredients": [ing["text"] for ing in planned],
            "ingredient_groups": groups,
            "added": [],
            "skipped_list_full": [],
            **extra,
        }

    async with list_lock or contextlib.nullcontext():
        target, added, skipped, on_list = await async_add_to_list(api, planned, list_ref, default_list_id)
    for dinner, rows in zip(dinners, _rows_per_dinner(cooked, planned, on_list)):
        dinner.update({key: value for key, value in rows.items() if value})
    if left_off:
        # So a later recipe change doesn't put back what the household said it has
        for dinner, recipe in zip(dinners, cooked):
            names = [_ingredient_name(ing) for ing in _ingredients(recipe) if norm(_ingredient_name(ing)) in left_off]
            if names := list(dict.fromkeys(names)):
                dinner[LEFT_OFF_KEY] = names

    return target, recipes, {
        "list": target.get("name"),
        "preview": False,
        "dinners": dinners,
        "ingredients": [ing["text"] for ing in planned],
        "added": added,
        "skipped_list_full": skipped,
        **extra,
    }
