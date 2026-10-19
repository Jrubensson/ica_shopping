"""Turn a recipe amount like '4.5 dl vispgrädde' into ICA products and a package count."""
from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import Counter

from .ica_api import ICAApi, IcaError

_LOGGER = logging.getLogger(__name__)

# No spoon measures: a few msk of butter or flour is a pantry amount, not a package to buy.
VOLUME_ML = {"ml": 1, "cl": 10, "dl": 100, "l": 1000}
WEIGHT_G = {"g": 1, "hg": 100, "kg": 1000}
MAX_PACKAGES = 6
# More than 3× the amount and over 500 g/ml left: a 5 kg sack is not what 300 g of onion means.
MAX_LEFTOVER_RATIO = 3
MAX_LEFTOVER = 500
# Packages up to 1.5× the amount count as a good fit; beyond that ICA's ranking only breaks ties.
GOOD_FIT_RATIO = 1.5
SEARCH_LIMIT = 10
# ICA answers 429 to bursts of searches.
PARALLEL_SEARCHES = 2
MATCH_TIMEOUT_SECONDS = 30

# "5dl", "2,5 dl", "4x125g"; the last size in a product name is its package size.
_SIZE = re.compile(r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(ml|cl|dl|l|g|hg|kg)\b", re.I)
# "Lök gul ca 180g" is a loose item weighed at the till, not a package.
_APPROXIMATE = re.compile(r"\bca\.?\s*\d", re.I)
# Variants (in the name or ICA category) are only picked when the recipe asks for them.
VARIANT_WORDS = (
    "laktosfri", "låglaktos", "syrad", "ekologisk", "krav", "eco", "vegan", "glutenfri",
    "smaksatt", "lätt", "tryffel", "hållbarhet", "fryst", "rökt", "alkoholfri",
    "mousserande", "gravad", "i lake", "marinerad", "chili", "vitlök", "citron", "örter",
)


def to_base(quantity: float, unit: str | None) -> tuple[str, float] | None:
    """(dimension, amount in ml or g), or None for units without a fixed size."""
    unit = (unit or "").strip().lower().rstrip(".")
    if unit in VOLUME_ML:
        return "volume", quantity * VOLUME_ML[unit]
    if unit in WEIGHT_G:
        return "weight", quantity * WEIGHT_G[unit]
    return None


def package_size(name: str) -> tuple[str, float] | None:
    if _APPROXIMATE.search(name):
        return None
    matches = list(_SIZE.finditer(name))
    if not matches:
        return None
    match = matches[-1]
    count = int(match.group(1)) if match.group(1) else 1
    return to_base(float(match.group(2).replace(",", ".")) * count, match.group(3))


def search_query(ingredient: str) -> str:
    """'nötfärs eller hushållsfärs (ärt- och nötfärs)' → 'nötfärs'."""
    name = re.sub(r"\(.*?\)", " ", ingredient)
    name = re.split(r"\beller\b|,", name)[0]
    return " ".join(name.split())


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[\W_]+", text.lower()) if len(w) >= 3]


def _relevant(name: str, words: list[str]) -> bool:
    """ICA names start with what the product is, which rules out 'Kokosmjölk' for mjölk."""
    tokens = name.lower().split()
    return bool(tokens) and any(tokens[0].startswith(w) for w in words) and all(
        any(t.startswith(w) for t in tokens) for w in words
    )


def _category(product: dict) -> str | None:
    level2 = product.get("ecomCategoryNameLevel2") or []
    return level2[0] if level2 else None


def _described(product: dict) -> str:
    return " ".join([
        product.get("displayName") or "",
        *(product.get("ecomCategoryNameLevel2") or []),
        *(product.get("ecomCategoryNameLevel3") or []),
    ]).lower()


def _is_variant(product: dict, query: str) -> bool:
    described = _described(product)
    return any(re.search(rf"\b{v}", described) and v not in query for v in VARIANT_WORDS)


def _is_canned(product: dict) -> bool:
    return "konserv" in _described(product)


def choose_product(
    query: str, dimension: str, needed: float, products: list[dict]
) -> tuple[dict, int] | None:
    """Fewest packages, then a good fit, then ICA's ranking, among relevant non-variant
    products in the category most of them share."""
    words = _words(query)
    if not words or needed <= 0:
        return None
    query = query.lower()
    # Variants go before the vote, or seven tins of champinjoner outvote the fresh ones.
    relevant = [
        (rank, p) for rank, p in enumerate(products)
        if _relevant(p.get("displayName") or "", words) and not _is_variant(p, query)
    ]
    # Tins only count when nothing fresh fits, since krossade tomater only come in tins.
    if "konserv" not in query and any(not _is_canned(p) for _, p in relevant):
        relevant = [(rank, p) for rank, p in relevant if not _is_canned(p)]
    categories = Counter(c for _, p in relevant if (c := _category(p)))
    if categories:
        top = max(categories.values())
        majority = next(_category(p) for _, p in relevant if categories.get(_category(p)) == top)
        relevant = [(rank, p) for rank, p in relevant if _category(p) == majority]
    best = None
    for rank, product in relevant:
        size = package_size(product.get("displayName") or "")
        if size is None or size[0] != dimension or size[1] <= 0:
            continue
        count = max(1, math.ceil(round(needed / size[1], 6)))
        leftover = count * size[1] - needed
        if count > MAX_PACKAGES or (leftover > MAX_LEFTOVER and count * size[1] > MAX_LEFTOVER_RATIO * needed):
            continue
        # Ranking beats an exact fit, or small speciality packs win on leftover alone.
        key = (count, count * size[1] > GOOD_FIT_RATIO * needed, rank)
        if best is None or key < best[0]:
            best = (key, product, count)
    return (best[1], best[2]) if best else None


def product_text(name: str, count: int) -> str:
    return name if count == 1 else f"{count} × {name}"


async def async_match_products(
    api: ICAApi, wanted: list[dict], account_number: str | None
) -> list[dict]:
    """Match each {name, text, quantity, unit} to a product; unmatched ones are left out."""
    semaphore = asyncio.Semaphore(PARALLEL_SEARCHES)

    async def match(item: dict) -> dict | None:
        base = to_base(item["quantity"], item.get("unit"))
        query = search_query(item["name"])
        if base is None or not query:
            return None
        async with semaphore:
            try:
                products = await api.async_search_products(query, account_number, SEARCH_LIMIT)
            except IcaError as err:
                _LOGGER.debug("Produktsökning för '%s' misslyckades: %s", query, err)
                return None
        chosen = choose_product(query, base[0], base[1], products)
        if chosen is None:
            return None
        product, count = chosen
        return {
            "ingredient": item["name"],
            "needed": item["text"],
            "product": product.get("displayName"),
            "count": count,
            "text": product_text(product.get("displayName"), count),
            "product_id": product.get("consumerItemId"),
            "url": product.get("friendlyUrl"),
            "image": product.get("image"),
            "_needed": base[1],
        }

    tasks = [asyncio.create_task(match(item)) for item in wanted]
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=MATCH_TIMEOUT_SECONDS)
    if pending:
        # Keep what was found; only the slow ingredients stay as text.
        _LOGGER.warning("⚠️ ICA:s produktsökning svarade inte i tid för %d ingredienser", len(pending))
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    results = [r for task in tasks if task in done and (r := task.result())]
    # '400 g gul lök' and '300 g lök' can land on the same bag; count packages for both together.
    by_product: dict[str, list[dict]] = {}
    for result in results:
        by_product.setdefault(result["product_id"] or result["product"], []).append(result)
    for shared in by_product.values():
        if len(shared) > 1 and (size := package_size(shared[0]["product"])):
            count = max(1, math.ceil(round(sum(r["_needed"] for r in shared) / size[1], 6)))
            for result in shared:
                result["count"] = count
                result["text"] = product_text(result["product"], count)
    for result in results:
        del result["_needed"]
    return results
