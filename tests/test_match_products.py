import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ica_shopping import async_setup
from custom_components.ica_shopping.const import (
    CONF_STORE_ID,
    DATA_ICA,
    DOMAIN,
    GLOBAL_SEARCH_URL,
    PUBLIC_TOKEN_URL,
    STORE_SEARCH_URL,
)
from custom_components.ica_shopping.ica_api import ICAApi, IcaApiError, IcaRateLimitedError
from custom_components.ica_shopping.products import (
    async_match_products,
    choose_package,
    choose_product,
    package_pieces,
    package_size,
    search_query,
    small_measure,
    to_base,
)
from custom_components.ica_shopping.recipes import async_plan_dinners


def _p(name, category="", group="", **extra):
    return {
        "displayName": name,
        "ecomCategoryNameLevel2": [group] if group else None,
        "ecomCategoryNameLevel3": [category] if category else None,
        **extra,
    }


# Real names and ICA's order from a search for "crème fraiche" at a Maxi.
CREME_FRAICHE = [
    _p("Crème Fraiche 32% Laktosfri 2dl Arla®", "Laktosfri crème fraiche"),
    _p("Lätt Crème fraiche 13% Laktosfri 200g Arla®", "Laktosfri crème fraiche"),
    _p("Crème fraiche 32% 5dl Arla Köket®", "Crème fraiche naturell"),
    _p("Lätt Crème Fraiche Sötstark Mango 11% 200ml Arla Köket®", "Crème fraiche smaksatt"),
    _p("Crème fraiche 32% 5dl ICA", "Crème fraiche naturell"),
    _p("Crème fraiche 32% 2dl ICA", "Crème fraiche naturell"),
]


@pytest.mark.parametrize(
    ("name", "size"),
    [
        ("Vispgrädde 36% 5dl Skånemejerier", ("volume", 500)),
        ("Matlagningsgrädde 13% 2,5dl ICA", ("volume", 250)),
        ("Mjölk 3% 1l ICA", ("volume", 1000)),
        ("Gul lök 1kg Klass 1 ICA", ("weight", 1000)),
        ("Krossade tomater 4x400g ICA", ("weight", 1600)),
        ("Lök gul ca 180g Klass 1 ICA", None),
        ("Krossade Tomater", None),
    ],
)
def test_package_size(name, size):
    assert package_size(name) == size


def test_spoons_and_pieces_have_no_package_amount():
    assert to_base(6, "msk") is None
    assert to_base(2, None) is None
    assert to_base(1, "förp") is None
    assert to_base(4.5, "dl") == ("volume", 450)


def test_search_query_drops_alternatives_and_notes():
    assert search_query("nötfärs eller hushållsfärs (ärt- och nötfärs)") == "nötfärs"
    assert search_query("krossade tomater (à 390 g)") == "krossade tomater"


def test_plain_products_beat_variants():
    product, count = choose_product("crème fraiche", "volume", 200, CREME_FRAICHE)
    assert (product["displayName"], count) == ("Crème fraiche 32% 2dl ICA", 1)


def test_a_variant_wins_when_the_recipe_asks_for_it():
    product, _ = choose_product("laktosfri crème fraiche", "volume", 200, CREME_FRAICHE)
    assert product["displayName"] == "Crème Fraiche 32% Laktosfri 2dl Arla®"


def test_a_variant_is_not_a_fallback():
    products = [_p("Grädde Syrad 30% 3dl Arla Ko®", "Mellangrädde")]
    assert choose_product("grädde", "volume", 300, products) is None


# Real names from searches at a Maxi: the ingredient word inside another product's name.
@pytest.mark.parametrize(
    ("query", "dimension", "needed", "products", "expected"),
    [
        ("mjölk", "volume", 400, [_p("Mjölk 3% 1l ICA", group="Mjölk"), _p("Kokosmjölk 400ml ICA Asia", group="Asien")], "Mjölk 3% 1l ICA"),
        ("smör", "weight", 50, [_p("Mikropopcorn Med Smör 240g Maze", group="Popcorn"), _p("Smör 82% 500g ICA", group="Smör & Margarin")], "Smör 82% 500g ICA"),
        ("pecorino", "weight", 30, [_p("Ostkex Pecorino & Truffle 75g", group="Kex"), _p("Pecorino Romano 150g ICA", group="Ost")], "Pecorino Romano 150g ICA"),
        ("olja", "volume", 100, [_p("Tonfisk i olja 170g ICA", group="Fiskkonserver"), _p("MCT-olja 500ml", group="Olja")], None),
        ("crème fraiche", "volume", 200, [_p("Crème fraiche 32% 2dl ICA", group="Crème fraiche")], "Crème fraiche 32% 2dl ICA"),
    ],
)
def test_other_kinds_of_product_never_fit(query, dimension, needed, products, expected):
    chosen = choose_product(query, dimension, needed, products)
    assert (chosen[0]["displayName"] if chosen else None) == expected


def test_only_the_shared_category_counts():
    products = [
        _p("Smör 82% 500g Arla", group="Smör & Margarin"),
        _p("Smördeg på smör 280g ICA", group="Pizza, Paj & Deg"),
        _p("Smör Normalsaltat 82% 500g ICA", group="Smör & Margarin"),
    ]
    assert choose_product("smör", "weight", 50, products)[0]["displayName"] == "Smör 82% 500g Arla"


def test_fresh_beats_canned_but_canned_still_fits_alone():
    # Live at a Maxi: seven tins of champinjoner and one fresh pack.
    tins = [_p(f"Champinjoner Skivade {g}g ICA", group="Grönsakskonserver") for g in (290, 180, 400)]
    fresh = _p("Champinjoner 400g Klass 1 ICA", group="Svamp")
    assert choose_product("champinjoner", "weight", 250, [*tins, fresh])[0] is fresh
    crushed = _p("Tomater krossade 500g ICA Basic", group="Tomatkonserver")
    assert choose_product("krossade tomater", "weight", 400, [crushed])[0] is crushed


def test_huge_packages_and_uncategorised_products_never_fit():
    products = [_p("Lök 5kg"), _p("Lök gul 5kg", group="Grönsaker")]
    assert choose_product("lök", "weight", 300, products) is None


def test_a_good_fit_beats_a_smaller_leftover_from_a_speciality_pack():
    products = [_p("Olivolja 500ml Zeta", group="Olja"), _p("Olivolja Extra 250ml Kalas", group="Olja")]
    assert choose_product("olivolja", "volume", 100, products)[0]["displayName"] == "Olivolja 500ml Zeta"
    sizes = [_p("Nötfärs Färsk 12% 1kg ICA", group="Kött"), _p("Nötfärs Färsk 12% 500g ICA", group="Kött")]
    assert choose_product("nötfärs", "weight", 500, sizes)[0]["displayName"] == "Nötfärs Färsk 12% 500g ICA"


def test_fewest_packages_then_a_good_fit():
    products = [_p("Vispgrädde 36% 2,5dl ICA"), _p("Vispgrädde 36% 5dl ICA"), _p("Vispgrädde 36% 1l ICA")]
    assert choose_product("vispgrädde", "volume", 450, products) == (products[1], 1)
    assert choose_product("vispgrädde", "volume", 1800, products) == (products[2], 2)


def test_other_unit_kinds_and_other_words_never_fit():
    products = [_p("Paprika i Grädde 500g Poljorad"), _p("Mjölk 3% 1l ICA")]
    assert choose_product("grädde", "volume", 300, products) is None


def test_more_than_six_packages_is_no_fit():
    assert choose_product("vispgrädde", "volume", 2000, [_p("Vispgrädde 36% 2,5dl ICA")]) is None


async def test_ingredients_on_one_product_are_counted_together():
    api = MagicMock()
    api.async_search_products = AsyncMock(return_value=[_p("Gul lök 500g Klass 1 ICA", consumerItemId="7")])
    wanted = [
        {"name": "gul lök", "text": "400 g gul lök", "quantity": 400, "unit": "g"},
        {"name": "lök gul", "text": "300 g lök gul", "quantity": 300, "unit": "g"},
    ]
    results = await async_match_products(api, wanted, None)
    assert [(r["ingredient"], r["text"]) for r in results] == [
        ("gul lök", "2 × Gul lök 500g Klass 1 ICA (400 g gul lök + 300 g lök gul)"),
        ("lök gul", "2 × Gul lök 500g Klass 1 ICA (400 g gul lök + 300 g lök gul)"),
    ]


async def test_a_rate_limited_search_is_retried():
    api = MagicMock()
    api.async_search_products = AsyncMock(
        side_effect=[IcaRateLimitedError("HTTP 429"), [_p("Mjölk 3% 1l ICA")]]
    )
    wanted = [{"name": "mjölk", "text": "10 dl mjölk", "quantity": 10, "unit": "dl"}]
    with patch("custom_components.ica_shopping.products.RATE_LIMIT_BACKOFF_SECONDS", 0):
        results = await async_match_products(api, wanted, "1003380")
    assert [r["text"] for r in results] == ["Mjölk 3% 1l ICA (10 dl mjölk)"]


async def test_match_products_skips_failed_and_unmatched_ingredients():
    async def search(query, account, limit):
        if query == "mjölk":
            raise IcaApiError("HTTP 500")
        return [_p("Vispgrädde 36% 5dl ICA", consumerItemId="42", friendlyUrl="https://x/42")]

    api = MagicMock()
    api.async_search_products = AsyncMock(side_effect=search)
    wanted = [
        {"name": "vispgrädde", "text": "4.5 dl vispgrädde", "quantity": 4.5, "unit": "dl"},
        {"name": "mjölk", "text": "1 l mjölk", "quantity": 1, "unit": "l"},
        {"name": "smör", "text": "2 msk smör", "quantity": 2, "unit": "msk"},
        {"name": "pizzadeg", "text": "1 sats pizzadeg", "quantity": 1, "unit": "sats"},
    ]

    assert await async_match_products(api, wanted, "1003380") == [{
        "ingredient": "vispgrädde",
        "needed": "4.5 dl vispgrädde",
        "product": "Vispgrädde 36% 5dl ICA",
        "count": 1,
        "text": "Vispgrädde 36% 5dl ICA (4.5 dl vispgrädde)",
        "product_id": "42",
        "url": "https://x/42",
        "image": None,
        "pantry": False,
    }]
    assert [c.args[0] for c in api.async_search_products.await_args_list] == ["vispgrädde", "mjölk", "smör"]
    assert api.async_search_products.await_args_list[0].args[1] == "1003380"


def _recipe(rid, title, *ings):
    return {"id": rid, "title": title, "ingredientGroups": [{"ingredients": list(ings)}]}


RECIPES = {
    "1": _recipe("1", "Pasta", {"ingredient": "vispgrädde", "text": "3 dl vispgrädde", "quantity": 3, "unit": "dl"}),
    "2": _recipe(
        "2", "Soppa",
        {"ingredient": "vispgrädde", "text": "1,5 dl vispgrädde", "quantity": 1.5, "unit": "dl"},
        {"ingredient": "gula lökar", "text": "2 gula lökar", "quantity": 2},
    ),
}


def _api():
    api = MagicMock()
    api.async_get_recipe = AsyncMock(side_effect=lambda rid: RECIPES[rid])
    api.async_search_products = AsyncMock(return_value=[_p("Vispgrädde 36% 5dl Skånemejerier")])
    api.async_get_list = AsyncMock(return_value={"id": "list", "name": "Handla", "rows": []})
    api.add_to_list = AsyncMock(return_value=True)
    return api


async def _plan(api, **kwargs):
    return await async_plan_dinners(
        api, count=2, source="random", list_ref="list", default_list_id="list",
        never_add=[], recipe_ids=["1", "2"], **kwargs,
    )


async def test_preview_shows_the_swaps():
    api = _api()
    _, _, result = await _plan(api, preview=True, match_products=True, store_account="1003380")
    row = "Vispgrädde 36% 5dl Skånemejerier (4.5 dl vispgrädde)"
    assert result["ingredients"] == [row, "2 gula lökar"]
    assert [(p["needed"], p["text"]) for p in result["products"]] == [("4.5 dl vispgrädde", row)]
    assert "products_skipped" not in result
    api.add_to_list.assert_not_called()


async def test_commit_adds_the_product_rows():
    api = _api()
    with patch(
        "custom_components.ica_shopping.recipes.async_resolve_or_create_list",
        AsyncMock(return_value={"id": "list", "name": "Handla", "rows": []}),
    ):
        _, _, result = await _plan(api, match_products=True, store_account="1003380")
    assert result["added"] == ["Vispgrädde 36% 5dl Skånemejerier (4.5 dl vispgrädde)", "2 gula lökar"]
    assert [c.args[1] for c in api.add_to_list.await_args_list] == result["added"]


async def test_without_a_store_nothing_is_searched():
    api = _api()
    _, _, result = await _plan(api, preview=True, match_products=True, store_account=None)
    assert result["products"] == []
    assert result["products_skipped"] == "no_store"
    assert result["ingredients"] == ["4.5 dl vispgrädde", "2 gula lökar"]
    api.async_search_products.assert_not_called()


async def test_a_slow_search_only_leaves_its_own_ingredient_as_text():
    async def search(query, *args):
        if query == "mjölk":
            await asyncio.sleep(1)
        return [_p("Vispgrädde 36% 5dl ICA")]

    api = MagicMock()
    api.async_search_products = AsyncMock(side_effect=search)
    wanted = [
        {"name": "mjölk", "text": "1 l mjölk", "quantity": 1, "unit": "l"},
        {"name": "vispgrädde", "text": "4.5 dl vispgrädde", "quantity": 4.5, "unit": "dl"},
    ]
    with patch("custom_components.ica_shopping.products.MATCH_TIMEOUT_SECONDS", 0.05):
        results = await async_match_products(api, wanted, "1003380")
    assert [r["ingredient"] for r in results] == ["vispgrädde"]


def test_variant_words_only_match_whole_word_starts():
    products = [_p("Pecorino Romano 150g ICA", group="Ost")]
    assert choose_product("pecorino", "weight", 30, products)[0] is products[0]
    butter = [_p("Smör Lättsaltat 82% 500g Arla", group="Smör & Margarin")]
    assert choose_product("smör", "weight", 250, butter)[0] is butter[0]


def test_smoked_compounds_are_variants():
    # Live at a Maxi for 'lax'.
    products = [
        _p("Lax Färsk Varmrökt i bit 600g", group="Fisk"),
        _p("Lax Kallrökt Skivad 150g Mowi", group="Fisk"),
        _p("Lax najad 150g Miljömärkt ICA", group="Fisk"),
    ]
    product, count = choose_product("lax", "weight", 400, products)
    assert (product["displayName"], count) == ("Lax najad 150g Miljömärkt ICA", 3)


async def test_a_second_plan_for_the_same_product_gets_its_own_row():
    api = _api()
    earlier = "Vispgrädde 36% 5dl Skånemejerier (3 dl vispgrädde)"
    with patch(
        "custom_components.ica_shopping.recipes.async_resolve_or_create_list",
        AsyncMock(return_value={"id": "list", "name": "Handla", "rows": []}),
    ):
        api.async_get_list = AsyncMock(return_value={
            "id": "list", "name": "Handla", "rows": [{"id": "r1", "text": earlier, "isStriked": False}],
        })
        _, _, result = await _plan(api, match_products=True, store_account="1003380")
    assert result["added"][0] == "Vispgrädde 36% 5dl Skånemejerier (4.5 dl vispgrädde)"


async def test_without_the_option_nothing_is_searched():
    api = _api()
    _, _, result = await _plan(api, preview=True)
    assert "products" not in result
    assert result["ingredients"] == ["4.5 dl vispgrädde", "2 gula lökar"]
    api.async_search_products.assert_not_called()


def _token():
    return {"publicAccessToken": "tok", "tokenExpires": "2099-01-01T00:00:00.0000000Z"}


async def test_product_search_request(hass, aioclient_mock):
    aioclient_mock.get(PUBLIC_TOKEN_URL, json=_token())
    aioclient_mock.post(
        f"{GLOBAL_SEARCH_URL}/search/quicksearch",
        json={"products": {"documents": [_p("Mjölk 3% 1l ICA"), {"displayName": None}]}},
    )
    api = ICAApi(hass, "199001011234", "pw")

    assert await api.async_search_products("mjölk", "1003380", 5) == [_p("Mjölk 3% 1l ICA")]
    call = next(c for c in aioclient_mock.mock_calls if c[0] == "POST")
    assert call[2] == {
        "queryString": "mjölk", "take": 5, "offset": 0,
        "accountNumber": "1003380", "searchDomain": "All",
    }


async def test_product_searches_are_reused_for_the_commit(hass, aioclient_mock):
    aioclient_mock.get(PUBLIC_TOKEN_URL, json=_token())
    aioclient_mock.post(
        f"{GLOBAL_SEARCH_URL}/search/quicksearch",
        json={"products": {"documents": [_p("Mjölk 3% 1l ICA")]}},
    )
    api = ICAApi(hass, "199001011234", "pw")
    await api.async_search_products("mjölk", "1003380")
    await api.async_search_products("Mjölk", "1003380")
    await api.async_search_products("mjölk", None)
    assert len([c for c in aioclient_mock.mock_calls if c[0] == "POST"]) == 2


async def test_store_account_lookup(hass, aioclient_mock):
    aioclient_mock.get(PUBLIC_TOKEN_URL, json=_token())
    aioclient_mock.get(f"{STORE_SEARCH_URL}/search", json=[{"id": 11920, "accountNumber": "1003380"}])
    api = ICAApi(hass, "199001011234", "pw")
    assert await api.async_get_store_account_number("11920") == "1003380"


async def _setup(hass, api, options):
    hass.data[DOMAIN] = {
        "memory": None,
        "sync": AsyncMock(),
        "current_list_id": "list",
        "current_options": options,
        DATA_ICA: api,
    }
    await async_setup(hass, {})


async def _commit_with_products(hass):
    return await hass.services.async_call(
        DOMAIN, "plan_dinners",
        {"recipes": "1", "preview": True, "match_products": True},
        blocking=True, return_response=True,
    )


async def test_service_searches_the_chosen_store_once(hass):
    api = MagicMock()
    api.async_get_favorite_stores = AsyncMock(return_value=[{"id": "1"}, {"id": "11920"}])
    api.async_get_store_account_number = AsyncMock(return_value="1003380")
    await _setup(hass, api, {CONF_STORE_ID: "11920"})
    plan = AsyncMock(return_value=(None, [], {"dinners": [], "added": [], "list": None}))

    with patch("custom_components.ica_shopping.async_plan_dinners", plan):
        await _commit_with_products(hass)
        await _commit_with_products(hass)

    api.async_get_store_account_number.assert_awaited_once_with("11920")
    assert plan.await_args.kwargs["match_products"] is True
    assert plan.await_args.kwargs["store_account"] == "1003380"


async def test_service_retries_a_store_lookup_that_found_nothing(hass):
    api = MagicMock()
    api.async_get_favorite_stores = AsyncMock(return_value=[{"id": "11920"}])
    api.async_get_store_account_number = AsyncMock(side_effect=[None, "1003380"])
    await _setup(hass, api, {})
    plan = AsyncMock(return_value=(None, [], {"dinners": [], "added": [], "list": None}))

    with patch("custom_components.ica_shopping.async_plan_dinners", plan):
        await _commit_with_products(hass)
        assert plan.await_args.kwargs["store_account"] is None
        await _commit_with_products(hass)

    assert plan.await_args.kwargs["store_account"] == "1003380"


async def test_service_falls_back_to_all_stores(hass):
    api = MagicMock()
    api.async_get_favorite_stores = AsyncMock(side_effect=IcaApiError("HTTP 500"))
    await _setup(hass, api, {})
    plan = AsyncMock(return_value=(None, [], {"dinners": [], "added": [], "list": None}))

    with patch("custom_components.ica_shopping.async_plan_dinners", plan):
        response = await _commit_with_products(hass)

    assert plan.await_args.kwargs["store_account"] is None
    assert response["products_skipped"] == "store_lookup_failed"


async def test_service_reports_a_missing_favourite_store(hass):
    api = MagicMock()
    api.async_get_favorite_stores = AsyncMock(return_value=[])
    await _setup(hass, api, {})
    plan = AsyncMock(return_value=(None, [], {"dinners": [], "added": [], "list": None}))

    with patch("custom_components.ica_shopping.async_plan_dinners", plan):
        response = await _commit_with_products(hass)

    assert response["products_skipped"] == "no_store"


async def test_a_rate_limited_store_detail_still_lists_the_favourite(hass):
    api = ICAApi(hass, "199001011234", "pw")
    api._request = AsyncMock(side_effect=[{"favoriteStores": [11920]}, IcaRateLimitedError("HTTP 429")])
    assert await api.async_get_favorite_stores() == [{"id": "11920", "name": "Butik 11920", "city": None}]


async def test_searches_run_outside_the_list_lock():
    lock = asyncio.Lock()
    held = {}
    api = _api()

    async def search(*args):
        held["search"] = lock.locked()
        return [_p("Vispgrädde 36% 5dl Skånemejerier")]

    async def add(*args):
        held["add"] = lock.locked()
        return True

    api.async_search_products = AsyncMock(side_effect=search)
    api.add_to_list = AsyncMock(side_effect=add)
    with patch(
        "custom_components.ica_shopping.recipes.async_resolve_or_create_list",
        AsyncMock(return_value={"id": "list", "name": "Handla", "rows": []}),
    ):
        await _plan(api, match_products=True, store_account="1003380", list_lock=lock)

    assert held == {"search": False, "add": True}


@pytest.mark.parametrize(
    ("quantity", "unit", "expected"),
    [
        (2, "msk", ("measure", 30)),
        (1, "tsk", ("measure", 5)),
        (4, "port", ("measure", 400)),
        (1, "nypa", ("pinch", 1)),
        (0.5, "tärning", ("piece", 0.5)),
        (2, None, ("piece", 2)),
        (2, "förp", ("package", 2)),
        (1, "sats", None),
    ],
)
def test_small_measures(quantity, unit, expected):
    assert small_measure(quantity, unit) == expected


@pytest.mark.parametrize(
    ("name", "pieces"),
    [("Kycklingbuljong 12-p Bong", 12), ("Rödlök 2-pack Klass 1", 2), ("Gurka 1-pack Klass 1 ICA", 1),
     ("Ägg M/L 20-p ICA", 20), ("Gul lök 500g Klass 1 ICA", None)],
)
def test_package_pieces(name, pieces):
    assert package_pieces(name) == pieces


def test_search_query_folds_accents_and_cloves():
    assert search_query("mâchesallad") == "machesallad"
    assert search_query("crème fraiche") == "creme fraiche"
    assert search_query("vitlöksklyftor") == "vitlök"
    assert search_query("gräddfil") == "gräddfil"


def test_mache_is_found_despite_accent_spelling_and_adjective():
    # The only hit at a Maxi for 'mâchesallad'.
    products = [_p("Finbladig Machesallat Sköljd 65g ICA", "Sallad", "Grönsaker")]
    assert choose_product(search_query("mâchesallad"), "weight", 65, products) == (products[0], 1)


def test_a_plural_matches_its_singular_but_not_a_longer_word():
    assert choose_package("tomater", "piece", 4, [_p("Tomatpuré 200g ICA", group="Kryddor")]) is None
    onions = [_p("Gul lök 1kg Klass 1 ICA", group="Lök"), _p("Gul lök 500g Klass 1 ICA", group="Lök")]
    assert choose_package("gula lökar", "piece", 2, onions) == (onions[1], 1)


# Live at a Maxi.
OREGANO = [
    _p("Oregano 15g ICA Basic", "Kryddor", "Kryddor & smaksättare"),
    _p("Oregano Torkad 7g ICA", "Kryddor", "Kryddor & smaksättare"),
    _p("Oregano 30g Santa Maria", "Kryddor", "Kryddor & smaksättare"),
    _p("Oregano 5g Santa Maria", "Kryddor", "Kryddor & smaksättare"),
    _p("Oregano ICA I love eco  Klass 1", "Oregano", "Färska kryddor"),
    _p("Oregano Torkad Eko 3g Culinar Sverige", "Kryddor", "Kryddor & smaksättare"),
]
TOMATPURE = [
    _p("Tomatpuré 200g ICA", "Ketchup & senap", "Kryddor & smaksättare"),
    _p("Tomatpuré 800g Durra"),
    _p("Tomatpuré Ekologisk 200g ICA", "Ketchup & senap", "Kryddor & smaksättare"),
    _p("Tomatpuré Triplo 185g Mutti", "Ketchup & senap", "Kryddor & smaksättare"),
    _p("Tomatpuré vitlök 130g Mutti", "Ketchup & senap", "Kryddor & smaksättare"),
]
OLIVOLJA = [
    _p("Olivolja 500ml Zeta", "Olja", "Kryddor & smaksättare"),
    _p("Olivolja 2l ICA Basic", "Olja", "Kryddor & smaksättare"),
    _p("Olivolja Spray 200ml Zeta", "Olja", "Kryddor & smaksättare"),
]


@pytest.mark.parametrize(
    ("query", "kind", "amount", "products", "expected"),
    [
        ("oregano", "measure", 5, OREGANO, "Oregano 5g Santa Maria"),
        ("tomatpuré", "measure", 30, TOMATPURE, "Tomatpuré Triplo 185g Mutti"),
        ("olivolja", "measure", 30, OLIVOLJA, "Olivolja 500ml Zeta"),
        ("oregano", "pinch", 1, OREGANO, "Oregano 5g Santa Maria"),
    ],
)
def test_spoons_get_the_smallest_plain_package(query, kind, amount, products, expected):
    product, count = choose_package(query, kind, amount, products)
    assert (product["displayName"], count) == (expected, 1)


BULJONG = [
    _p("Kycklingbuljong 12-p Bong", "Buljong & fond", "Kryddor & smaksättare"),
    _p("Kycklingbuljong tärning 120g ICA", "Buljong & fond", "Kryddor & smaksättare"),
    _p("Kycklingbuljong Ekologisk 6-p Kung Markatta", "Buljong & fond", "Kryddor & smaksättare"),
    _p("Buljong Kyckling 80g Maggi", "Polen", "Smaker från världen"),
]
HONSBULJONG = [
    _p("Hönsbuljong Tärningar 12-p 6l Knorr", "Buljong & fond", "Kryddor & smaksättare"),
    _p("Hönsbuljong Tärningar 6-p 3l Knorr", "Buljong & fond", "Kryddor & smaksättare"),
]


@pytest.mark.parametrize(
    ("query", "cubes", "products", "expected"),
    [
        ("kycklingbuljong", 0.5, BULJONG, ("Kycklingbuljong 12-p Bong", 1)),
        ("hönsbuljong", 3, HONSBULJONG, ("Hönsbuljong Tärningar 6-p 3l Knorr", 1)),
        ("hönsbuljong", 8, HONSBULJONG, ("Hönsbuljong Tärningar 12-p 6l Knorr", 1)),
        ("hönsbuljong", 8, HONSBULJONG[1:], ("Hönsbuljong Tärningar 6-p 3l Knorr", 2)),
    ],
)
def test_cubes_are_counted_against_the_pack(query, cubes, products, expected):
    product, count = choose_package(query, "piece", cubes, products)
    assert (product["displayName"], count) == expected


SPAGHETTI = [
    _p("Spaghetti 1kg ICA", "Torkad pasta", "Pasta, ris & mos"),
    _p("Spaghetti 2kg ICA", "Torkad pasta", "Pasta, ris & mos"),
    _p("Spaghetti 500g Zeta", "Torkad pasta", "Pasta, ris & mos"),
    _p("Spaghetti Ekologisk 500g ICA", "Torkad pasta", "Pasta, ris & mos"),
]


@pytest.mark.parametrize(
    ("portions", "products", "expected"),
    [
        (4, SPAGHETTI, ("Spaghetti 500g Zeta", 1)),
        (8, SPAGHETTI, ("Spaghetti 1kg ICA", 1)),
        (8, SPAGHETTI[2:], ("Spaghetti 500g Zeta", 2)),
    ],
)
def test_portions_count_packages_only_when_clearly_needed(portions, products, expected):
    product, count = choose_package("spaghetti", "measure", portions * 100, products)
    assert (product["displayName"], count) == expected


def test_pieces_of_produce():
    cucumbers = [
        _p("Gurka 1-pack Klass 1 ICA", "Gurka", "Grönsaker"),
        _p("Gurka saltgurka 400g Kinda Klass 1 ICA", "Gurka", "Grönsaker"),
        _p("Hackad Gurka 575g ICA", "Gurka & rödbetor", "Grönsakskonserver"),
    ]
    assert choose_package("gurka", "piece", 1, cucumbers) == (cucumbers[0], 1)
    red = [
        _p("Picklad Rödlök 96g ICA", "Övriga grönsaker", "Grönsakskonserver"),
        _p("Rödlök 2-pack Klass 1", "Rödlök", "Lök"),
        _p("Rödlök 500g Klass 1 ICA", "Rödlök", "Lök"),
    ]
    assert choose_package("rödlök", "piece", 1, red) == (red[1], 1)
    # A pack that says how many it holds beats a bag, even at two packs.
    assert choose_package("rödlökar", "piece", 4, red) == (red[1], 2)
    loose = [_p("Gul lök 1kg Klass 1 ICA", group="Lök"), _p("Lök gul ca 180g Klass 1 ICA", group="Lök")]
    assert choose_package("lök", "piece", 3, loose) == (loose[1], 1)


def test_packages_are_counted_as_asked():
    tins = [_p("Krossade tomater 400g ICA", group="Tomatkonserver"), _p("Krossade tomater 4x400g ICA", group="Tomatkonserver")]
    assert choose_package("krossade tomater", "package", 2, tins) == (tins[0], 2)


async def test_small_amounts_are_matched_and_pantry_staples_marked():
    garlic = [_p("Vitlök färsk 80g Klass 1 ICA", "Vitlök", "Lök"), _p("Vitlök 250g Klass 1 ICA", "Vitlök", "Lök")]

    async def search(query, *args):
        return {"oregano": OREGANO, "vitlök": garlic, "spaghetti": SPAGHETTI}[query]

    api = MagicMock()
    api.async_search_products = AsyncMock(side_effect=search)
    wanted = [
        {"name": "oregano", "text": "1 tsk oregano", "quantity": 1, "unit": "tsk"},
        {"name": "vitlöksklyftor", "text": "3 vitlöksklyftor", "quantity": 3, "unit": None},
        {"name": "spaghetti", "text": "4 port spaghetti", "quantity": 4, "unit": "port"},
    ]
    results = await async_match_products(api, wanted, "1003380")
    assert [(r["text"], r["count"], r["pantry"]) for r in results] == [
        ("Oregano 5g Santa Maria (1 tsk oregano)", 1, True),
        ("Vitlök färsk 80g Klass 1 ICA (3 vitlöksklyftor)", 1, False),
        ("Spaghetti 500g Zeta (4 port spaghetti)", 1, False),
    ]


async def test_pieces_and_weight_on_one_product_keep_the_larger_count():
    api = MagicMock()
    api.async_search_products = AsyncMock(return_value=[_p("Gul lök 500g Klass 1 ICA", consumerItemId="7")])
    wanted = [
        {"name": "gul lök", "text": "2 gul lök", "quantity": 2, "unit": None},
        {"name": "lök gul", "text": "800 g lök gul", "quantity": 800, "unit": "g"},
    ]
    results = await async_match_products(api, wanted, None)
    assert {r["text"] for r in results} == {"2 × Gul lök 500g Klass 1 ICA (2 gul lök + 800 g lök gul)"}


def test_a_describing_word_only_counts_when_nothing_is_plainly_named():
    # Live at a Maxi for 'potatisar'.
    products = [
        _p("Kryddig Potatis 500g Peka", "Potatissallad", "Gratäng & röror"),
        _p("Potatis Fast 1,2kg Klass 1", "Fast potatis", "Potatis"),
    ]
    assert choose_package("potatisar", "piece", 8, products) == (products[1], 1)
    assert choose_package("potatisar", "piece", 8, products[:1]) == (products[0], 1)


async def test_a_pot_of_herbs_is_never_dried():
    api = MagicMock()
    api.async_search_products = AsyncMock(return_value=[
        _p("Basilika Torkad 10g ICA", "Kryddor", "Kryddor & smaksättare"),
        _p("Basilika i kruka Klass 1 ICA", "Basilika", "Färska kryddor"),
    ])
    wanted = [{"name": "basilika", "text": "1 kruka basilika", "quantity": 1, "unit": "kruka"}]
    results = await async_match_products(api, wanted, "1003380")
    assert [(r["product"], r["pantry"]) for r in results] == [("Basilika i kruka Klass 1 ICA", False)]


def test_a_fuzzy_last_letter_never_reaches_a_longer_word():
    products = [
        _p("Gräddfil 3% 3dl Arla", group="Gräddfil"),
        _p("Gräddfil 3% 5dl ICA", group="Gräddfil"),
        _p("Matlagningsgrädde 15% 5dl ICA", group="Grädde"),
    ]
    assert choose_product("grädde", "volume", 300, products) is None


def test_slices_and_pieces_count_against_a_stated_pack():
    salmon = [_p("Laxfilé 2-p 250g ICA", group="Fisk"), _p("Laxfilé 4-p 500g ICA", group="Fisk")]
    assert small_measure(4, "bitar") == ("piece", 4)
    assert choose_package("laxfilé", "piece", 4, salmon) == (salmon[1], 1)
    chicken = [_p("Kycklingfilé 4-p 600g", group="Kyckling"), _p("Kycklingfilé 1kg", group="Kyckling")]
    assert choose_package("kycklingfilé", "piece", 8, chicken) == (chicken[0], 2)


async def test_pieces_on_one_stated_pack_are_added_up():
    api = MagicMock()
    api.async_search_products = AsyncMock(return_value=[_p("Rödlök 2-pack Klass 1", "Rödlök", "Lök", consumerItemId="9")])
    wanted = [
        {"name": "rödlök", "text": "1 rödlök", "quantity": 1, "unit": None},
        {"name": "rödlökar", "text": "2 rödlökar", "quantity": 2, "unit": None},
    ]
    results = await async_match_products(api, wanted, None)
    assert {r["text"] for r in results} == {"2 × Rödlök 2-pack Klass 1 (1 rödlök + 2 rödlökar)"}


def test_fold_keeps_swedish_letters_and_splits_ligatures():
    assert search_query("Crème fraîche") == "creme fraiche"
    assert search_query("ﬁkon") == "fikon"
    assert search_query("Ärtor") == "ärtor"


@pytest.mark.parametrize(
    ("wanted", "products", "text"),
    [
        (
            [{"name": "krossade tomater", "text": "1 burk krossade tomater", "quantity": 1, "unit": "burk"},
             {"name": "krossade tomater eller passerade tomater", "text": "1 burk krossade tomater eller passerade tomater", "quantity": 1, "unit": "burk"}],
            [_p("Krossade tomater 400g ICA", group="Tomatkonserver", consumerItemId="1")],
            "2 × Krossade tomater 400g ICA (1 burk krossade tomater + 1 burk krossade tomater eller passerade tomater)",
        ),
        (
            [{"name": "spaghetti", "text": "400 g spaghetti", "quantity": 400, "unit": "g"},
             {"name": "spaghetti eller linguine", "text": "4 port spaghetti eller linguine", "quantity": 4, "unit": "port"}],
            [_p("Spaghetti 500g Zeta", group="Pasta, ris & mos", consumerItemId="2")],
            "2 × Spaghetti 500g Zeta (400 g spaghetti + 4 port spaghetti eller linguine)",
        ),
    ],
)
async def test_packages_and_portions_on_one_product_are_added_up(wanted, products, text):
    api = MagicMock()
    api.async_search_products = AsyncMock(return_value=products)
    results = await async_match_products(api, wanted, None)
    assert {r["text"] for r in results} == {text}
