"""Turn a recipe amount like '4.5 dl vispgrädde' or '2 msk tomatpuré' into ICA products and a package count."""
from __future__ import annotations

import asyncio
import logging
import math
import re
import unicodedata
from collections import Counter
from typing import Collection

from .ica_api import ICAApi, IcaError, IcaRateLimitedError, format_offer

_LOGGER = logging.getLogger(__name__)

VOLUME_ML = {"ml": 1, "cl": 10, "dl": 100, "l": 1000}
WEIGHT_G = {"g": 1, "hg": 100, "kg": 1000}
# Amounts the shop doesn't sell by: the smallest normal package covers them.
SPOON_ML = {"msk": 15, "tsk": 5, "krm": 1}
PORTION_G = 100  # A 500 g pack of pasta is 5–6 portions.
PINCH_UNITS = {
    "nypa", "nypor", "klyfta", "klyftor", "kvist", "kvistar", "blad", "näve", "nävar",
    "droppe", "droppar", "cm",
}
PIECE_UNITS = {
    "", "st", "tärning", "tärningar", "huvud", "huvuden", "stjälk", "stjälkar",
    "bit", "bitar", "skiva", "skivor",
}
# A pot or bunch of herbs is fresh, never a jar of dried.
FRESH_UNITS = {"kruka", "krukor", "knippe", "knippen", "bunt", "kvist", "kvistar"}
PACKAGE_UNITS = {
    "förp", "förpackning", "förpackningar", "paket", "pkt", "burk", "burkar", "flaska", "flaskor",
    "påse", "påsar", "tub", "tuber", "ask", "askar", "kruka", "krukor", "knippe", "knippen", "bunt",
}
# Bigger than this is a sack or a catering pack, not something bought for a pinch.
MAX_SMALL_PACKAGE = 2000
# ICA's level-3 categories for what most kitchens already have; a UI can leave these unticked.
PANTRY_CATEGORIES = {
    "kryddor", "buljong & fond", "olja", "salt", "mjöl", "sötning", "ketchup & senap",
    "övriga smaksättare", "vinäger & ättika",
}
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
# "Kycklingbuljong 12-p", "Rödlök 2-pack", "Ägg 6 st".
_PIECES = re.compile(r"\b(\d+)\s*-?\s*(?:p|pk|pack|st)\b", re.I)
# "Finbladig Machesallat": a describing word before what the product is.
_ADJECTIVE = re.compile(r"\w{3,}(?:ig|ad|ade)$")
_PLURAL_ENDINGS = ("arna", "orna", "erna", "ar", "or", "er", "na", "a")
_CLOVES = re.compile(r"s?klyft(?:a|or)$")
# Variants (in the name or ICA category) are only picked when the recipe asks for them.
VARIANT_WORDS = (
    "laktosfri", "låglaktos", "syrad", "eko", "krav", "eco", "vegan", "glutenfri",
    "smaksatt", "tryffel", "hållbarhet", "fryst", "alkoholfri", "mousserande", "gravad",
    "i lake", "marinerad", "chili", "vitlök", "citron", "örter", "spray",
)
_VARIANT_PATTERNS = [
    # At a word start, so 'eco' isn't found in 'Pecorino'.
    *((w, re.compile(rf"\b{w}")) for w in VARIANT_WORDS),
    ("rökt", re.compile("rökt")),  # Also inside 'Varmrökt'.
    ("lätt", re.compile(r"\blätt\b")),  # Not 'Lättsaltat', which is plain smör.
]
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_BACKOFF_SECONDS = 2


def _unit(unit: str | None) -> str:
    return (unit or "").strip().lower().rstrip(".")


def to_base(quantity: float, unit: str | None) -> tuple[str, float] | None:
    """(dimension, amount in ml or g), or None for units without a fixed size."""
    unit = _unit(unit)
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


def small_measure(quantity: float, unit: str | None) -> tuple[str, float] | None:
    """(kind, amount) for units bought as a whole package, or None when the unit is unknown.

    'measure' is ml or g, 'pinch' is a nypa or clove, 'piece' counts pieces or cubes and
    'package' counts packages.
    """
    unit = _unit(unit)
    if unit in SPOON_ML:
        return "measure", quantity * SPOON_ML[unit]
    if unit in ("port", "portion", "portioner"):
        return "measure", quantity * PORTION_G
    if unit in PINCH_UNITS:
        return "pinch", quantity
    if unit in PIECE_UNITS:
        return "piece", quantity
    if unit in PACKAGE_UNITS:
        return "package", quantity
    return None


def package_pieces(name: str) -> int | None:
    """How many pieces a package holds when its name says so, e.g. 'Kycklingbuljong 12-p'."""
    match = _PIECES.search(name)
    return int(match.group(1)) or None if match else None


def fold(text: str) -> str:
    """Lower case without accents, but keeps å, ä and ö, which are letters of their own."""
    return "".join(
        c if c in "åäö" else "".join(d for d in unicodedata.normalize("NFKD", c) if not unicodedata.combining(d))
        for c in text.lower()
    )


def search_query(ingredient: str) -> str:
    """'nötfärs eller hushållsfärs (ärt- och nötfärs)' → 'nötfärs'; 'vitlöksklyftor' → 'vitlök'."""
    name = re.sub(r"\(.*?\)", " ", ingredient)
    name = re.split(r"\beller\b|,", name)[0]
    name = " ".join(fold(name).split())
    return _CLOVES.sub("", name) if len(name) > 8 else name


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[\W_]+", fold(text)) if len(w) >= 3]


def _stem(word: str) -> str:
    """'lökar' → 'lök', 'gula' → 'gul'."""
    for ending in _PLURAL_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


def _fits(token: str, word: str) -> bool:
    if token.startswith(word):
        return True
    # A plural only matches its singular, not a longer word: 'tomater' never picks 'Tomatpuré'.
    stem = _stem(word)
    if stem != word and token.startswith(stem) and len(token) <= len(word):
        return True
    # 'sallad' and 'sallat' are both spelled; same length, so 'grädde' never fits 'gräddfil'.
    return len(word) >= 6 and len(token) == len(word) and token[:-1] == word[:-1]


def _relevance(name: str, words: list[str]) -> int:
    """2 when the name starts with the ingredient, 1 after a describing word, else 0.

    ICA names start with what the product is, which rules out 'Kokosmjölk' for mjölk.
    """
    tokens = fold(name).split()
    if not tokens or not all(any(_fits(t, w) for t in tokens) for w in words):
        return 0
    if any(_fits(tokens[0], w) for w in words):
        return 2
    return int(len(tokens) > 1 and bool(_ADJECTIVE.fullmatch(tokens[0])) and any(_fits(tokens[1], w) for w in words))


def _ean(code) -> str:
    """ICA's search pads EANs to 14 digits, its offers don't."""
    return str(code or "").strip().lstrip("0")


def offers_by_ean(offers: list[dict]) -> dict[str, dict]:
    """Each product an offer covers, by EAN, with the offer as format_offer gives it."""
    found = {}
    for offer in offers:
        for item in offer.get("eans") or []:
            if isinstance(item, dict) and (ean := _ean(item.get("id"))):
                found.setdefault(ean, format_offer(offer))
    return found


def offer_for(ingredient: str, offers: list[dict]) -> dict | None:
    """The offer on what the ingredient names, by the offer's name or one of its products.

    Every word must be a whole word there, so 'smör' doesn't find 'Smörgåsgurka', and the name
    must start with it, so 'citron' doesn't find 'Diskmedel Citron'.
    """
    words = {_stem(w) for w in _words(search_query(ingredient))}
    if not words:
        return None
    for offer in offers:
        names = [offer.get("name") or "", *(
            e.get("articleDescription") or "" for e in offer.get("eans") or [] if isinstance(e, dict)
        )]
        if any(_names(name, words) for name in names):
            return format_offer(offer)
    return None


def _names(name: str, words: set[str]) -> bool:
    """Whether the name is of what the stemmed words say, read like _relevance reads it."""
    tokens = _words(name)
    stems = [_stem(t) for t in tokens]
    if not stems or not words <= set(stems):
        return False
    return stems[0] in words or (len(stems) > 1 and bool(_ADJECTIVE.fullmatch(tokens[0])) and stems[1] in words)


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
    return any(pattern.search(described) and word not in query for word, pattern in _VARIANT_PATTERNS)


def _is_canned(product: dict) -> bool:
    return "konserv" in _described(product)


def _candidates(query: str, products: list[dict]) -> list[tuple[int, dict]]:
    """(ICA rank, product) for plain products of what the query names, in one category."""
    words = _words(query)
    if not words:
        return []
    query = fold(query)
    # Variants go before the vote, or seven tins of champinjoner outvote the fresh ones.
    scored = [
        (_relevance(p.get("displayName") or "", words), rank, p) for rank, p in enumerate(products)
        if not _is_variant(p, query)
    ]
    # 'Kryddig Potatis' is only potatis when nothing is plainly called that.
    top = max((score for score, _, _ in scored), default=0)
    relevant = [(rank, p) for score, rank, p in scored if score and score == top]
    # Tins only count when nothing fresh fits, since krossade tomater only come in tins.
    if "konserv" not in query and any(not _is_canned(p) for _, p in relevant):
        relevant = [(rank, p) for rank, p in relevant if not _is_canned(p)]
    categories = Counter(c for _, p in relevant if (c := _category(p)))
    if categories:
        top = max(categories.values())
        majority = next(_category(p) for _, p in relevant if categories.get(_category(p)) == top)
        relevant = [(rank, p) for rank, p in relevant if _category(p) == majority]
    return relevant


def choose_product(
    query: str, dimension: str, needed: float, products: list[dict], offered: Collection[str] = ()
) -> tuple[dict, int] | None:
    """The product and package count to buy, or None when nothing fits.

    A product whose EAN is in `offered` wins over ICA's ranking, but not over a better fit.
    """
    if needed <= 0:
        return None
    best = None
    for rank, product in _candidates(query, products):
        size = package_size(product.get("displayName") or "")
        if size is None or size[0] != dimension or size[1] <= 0:
            continue
        count = max(1, math.ceil(round(needed / size[1], 6)))
        leftover = count * size[1] - needed
        if count > MAX_PACKAGES or (leftover > MAX_LEFTOVER and count * size[1] > MAX_LEFTOVER_RATIO * needed):
            continue
        # Ranking beats an exact fit, or small speciality packs win on leftover alone.
        key = (count, count * size[1] > GOOD_FIT_RATIO * needed, _ean(product.get("gtin")) not in offered, rank)
        if best is None or key < best[0]:
            best = (key, product, count)
    return (best[1], best[2]) if best else None


def _small_fit(kind: str, amount: float, name: str) -> tuple[int, int, float] | None:
    """(preference, count, size) of one package for an amount the shop doesn't sell by."""
    size = package_size(name)
    pieces = package_pieces(name)
    # Loose, or sold by the item like 'Basilika i kruka Klass 1'.
    loose = bool(_APPROXIMATE.search(name)) or not (size or pieces)
    if size and size[1] > MAX_SMALL_PACKAGE:
        size = None
    if kind == "measure":
        # A gram of oregano and a ml of it are close enough for a spoonful.
        if size:
            return 0, max(1, math.ceil(round(amount / size[1], 6))), size[1]
        return (1, 1, pieces or 0) if pieces or loose else None
    if kind == "piece":
        # Only a pack that says how many it holds can be counted against the pieces.
        if pieces:
            return 0, max(1, math.ceil(round(amount / pieces, 6))), pieces
        if loose:
            return 1, 1, 0
        return (2, 1, size[1]) if size else None
    if kind == "package":
        count = max(1, math.ceil(round(amount, 6)))
    else:
        count = 1
    if size:
        return 0, count, size[1]
    return (1, count, pieces or 0) if pieces or loose else None


def choose_package(
    query: str, kind: str, amount: float, products: list[dict], offered: Collection[str] = ()
) -> tuple[dict, int] | None:
    """The smallest normal package for spoons, pinches, pieces, cubes, portions or packages;
    of two the same size, the one whose EAN is in `offered`."""
    if amount <= 0:
        return None
    best = None
    for rank, product in _candidates(query, products):
        fit = _small_fit(kind, amount, product.get("displayName") or "")
        if fit is None or fit[1] > MAX_PACKAGES:
            continue
        key = (*fit, _ean(product.get("gtin")) not in offered, rank)
        if best is None or key < best[0]:
            best = (key, product, fit[1])
    return (best[1], best[2]) if best else None


def _is_dried(product: dict) -> bool:
    return "kryddor" in (c.lower() for c in product.get("ecomCategoryNameLevel3") or [])


def is_pantry(product: dict) -> bool:
    """Spices, stock, oil, flour, sugar and the like, which most kitchens already have."""
    return any(c.lower() in PANTRY_CATEGORIES for c in product.get("ecomCategoryNameLevel3") or [])


def product_text(name: str, count: int, needed: str) -> str:
    """'2 × Mjölk 3% 1l ICA (14 dl mjölk)'; the amount keeps two plans' rows for one product apart."""
    return f"{name if count == 1 else f'{count} × {name}'} ({needed})"


async def async_match_products(
    api: ICAApi, wanted: list[dict], account_number: str | None, offers: list[dict] | None = None
) -> list[dict]:
    """Match each {name, text, quantity, unit} to a product; unmatched ones are left out.

    With the store's `offers`, a product on offer is picked when it fits as well, and its
    result has the deal in "offer".
    """
    offered = offers_by_ean(offers or [])
    semaphore = asyncio.Semaphore(PARALLEL_SEARCHES)

    async def match(item: dict) -> dict | None:
        base = to_base(item["quantity"], item.get("unit"))
        small = None if base else small_measure(item["quantity"], item.get("unit"))
        if _CLOVES.search(fold(item["name"])) and small and small[0] == "piece":
            small = ("pinch", small[1])
        query = search_query(item["name"])
        if (base is None and small is None) or not query:
            return None
        products = None
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            async with semaphore:
                try:
                    products = await api.async_search_products(query, account_number, SEARCH_LIMIT)
                    break
                except IcaRateLimitedError:
                    pass
                except IcaError as err:
                    _LOGGER.debug("Produktsökning för '%s' misslyckades: %s", query, err)
                    return None
            if attempt < RATE_LIMIT_RETRIES:
                await asyncio.sleep(RATE_LIMIT_BACKOFF_SECONDS * (attempt + 1))
        if products is None:
            _LOGGER.debug("Produktsökning för '%s' begränsades av ICA (429)", query)
            return None
        if base:
            chosen = choose_product(query, base[0], base[1], products, offered)
        else:
            if _unit(item.get("unit")) in FRESH_UNITS:
                products = [p for p in products if not _is_dried(p)]
            chosen = choose_package(query, small[0], small[1], products, offered)
        if chosen is None:
            return None
        product, count = chosen
        result = {
            "ingredient": item["name"],
            "needed": item["text"],
            "product": product.get("displayName"),
            "count": count,
            "text": product_text(product.get("displayName"), count, item["text"]),
            "product_id": product.get("consumerItemId"),
            "url": product.get("friendlyUrl"),
            "image": product.get("image"),
            "pantry": is_pantry(product),
            # Spoons and portions are already in ml or g, so they add up with weighed amounts.
            "_needed": base[1] if base else small[1] if small[0] == "measure" else None,
            "_pieces": small[1] if small and small[0] == "piece" else None,
            "_packages": count if small and small[0] == "package" else None,
        }
        if offer := offered.get(_ean(product.get("gtin"))):
            result["offer"] = offer
        return result

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
        if len(shared) < 2:
            continue
        size = package_size(shared[0]["product"])
        pieces = package_pieces(shared[0]["product"])
        if size and all(r["_needed"] is not None for r in shared):
            count = max(1, math.ceil(round(sum(r["_needed"] for r in shared) / size[1], 6)))
        elif pieces and all(r["_pieces"] is not None for r in shared):
            count = max(1, math.ceil(round(sum(r["_pieces"] for r in shared) / pieces, 6)))
        elif all(r["_packages"] is not None for r in shared):
            count = sum(r["_packages"] for r in shared)
        else:
            count = max(r["count"] for r in shared)
        needed = " + ".join(r["needed"] for r in shared)
        for result in shared:
            result["count"] = count
            result["text"] = product_text(result["product"], count, needed)
    for result in results:
        del result["_needed"], result["_pieces"], result["_packages"]
    return results
