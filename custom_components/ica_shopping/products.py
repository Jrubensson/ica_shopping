"""Turn a recipe amount like '4.5 dl vispgrädde' into ICA products and a package count."""
from __future__ import annotations

import asyncio
import logging
import math
import re

from .ica_api import ICAApi, IcaError

_LOGGER = logging.getLogger(__name__)

# No spoon measures: a few msk of butter or flour is a pantry amount, not a package to buy.
VOLUME_ML = {"ml": 1, "cl": 10, "dl": 100, "l": 1000}
WEIGHT_G = {"g": 1, "hg": 100, "kg": 1000}
MAX_PACKAGES = 6
SEARCH_LIMIT = 10
PARALLEL_SEARCHES = 4

# "5dl", "2,5 dl", "4x125g"; the last size in a product name is its package size.
_SIZE = re.compile(r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(ml|cl|dl|l|g|hg|kg)\b", re.I)
# "Lök gul ca 180g" is a loose item weighed at the till, not a package.
_APPROXIMATE = re.compile(r"\bca\.?\s*\d", re.I)
# ICA ranks these variants high; they only win when the recipe asks for them or nothing else fits.
VARIANT_WORDS = (
    "laktosfri", "låglaktos", "syrad", "ekologisk", "krav", "vegan", "glutenfri", "smaksatt", "lätt",
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
    return [w for w in re.split(r"[^0-9a-zåäöéü]+", text.lower()) if len(w) >= 3]


def choose_product(
    query: str, dimension: str, needed: float, products: list[dict]
) -> tuple[dict, int] | None:
    """The product and count that cover `needed`: plain products first, then the fewest
    packages, then the least left over, then ICA's ranking.

    Only products whose name holds every word of the query and whose size is in the same
    dimension count.
    """
    words = _words(query)
    query_lower = query.lower()
    if not words or needed <= 0:
        return None
    best = None
    for rank, product in enumerate(products):
        name = product.get("displayName") or ""
        if not all(w in name.lower() for w in words):
            continue
        size = package_size(name)
        if size is None or size[0] != dimension or size[1] <= 0:
            continue
        count = max(1, math.ceil(round(needed / size[1], 6)))
        if count > MAX_PACKAGES:
            continue
        described = " ".join([name, *(product.get("ecomCategoryNameLevel3") or [])]).lower()
        variant = any(v in described and v not in query_lower for v in VARIANT_WORDS)
        key = (variant, count, count * size[1] - needed, rank)
        if best is None or key < best[0]:
            best = (key, product, count)
    return (best[1], best[2]) if best else None


def product_text(name: str, count: int) -> str:
    return name if count == 1 else f"{count} × {name}"


async def async_match_products(
    api: ICAApi, wanted: list[dict], account_number: str | None
) -> list[dict]:
    """Match each {name, text, quantity, unit} to a product; unmatched ones are left out.

    A failed search only leaves that ingredient as plain text.
    """
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
        }

    results = await asyncio.gather(*(match(item) for item in wanted))
    return [r for r in results if r]
