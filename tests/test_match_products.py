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
from custom_components.ica_shopping.ica_api import ICAApi, IcaApiError
from custom_components.ica_shopping.products import (
    async_match_products,
    choose_product,
    package_size,
    search_query,
    to_base,
)
from custom_components.ica_shopping.recipes import async_plan_dinners


def _p(name, category="", **extra):
    return {"displayName": name, "ecomCategoryNameLevel3": [category] if category else None, **extra}


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


def test_fewest_packages_then_least_left_over():
    products = [_p("Vispgrädde 36% 2,5dl ICA"), _p("Vispgrädde 36% 5dl ICA"), _p("Vispgrädde 36% 1l ICA")]
    assert choose_product("vispgrädde", "volume", 450, products) == (products[1], 1)
    assert choose_product("vispgrädde", "volume", 1800, products) == (products[2], 2)


def test_other_unit_kinds_and_other_words_never_fit():
    products = [_p("Paprika i Grädde 500g Poljorad"), _p("Mjölk 3% 1l ICA")]
    assert choose_product("grädde", "volume", 300, products) is None


def test_more_than_six_packages_is_no_fit():
    assert choose_product("vispgrädde", "volume", 2000, [_p("Vispgrädde 36% 2,5dl ICA")]) is None


async def test_match_products_skips_failed_and_unmeasured_ingredients():
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
    ]

    assert await async_match_products(api, wanted, "1003380") == [{
        "ingredient": "vispgrädde",
        "needed": "4.5 dl vispgrädde",
        "product": "Vispgrädde 36% 5dl ICA",
        "count": 1,
        "text": "Vispgrädde 36% 5dl ICA",
        "product_id": "42",
        "url": "https://x/42",
        "image": None,
    }]
    assert [c.args[0] for c in api.async_search_products.await_args_list] == ["vispgrädde", "mjölk"]
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
    assert result["ingredients"] == ["Vispgrädde 36% 5dl Skånemejerier", "2 gula lökar"]
    assert [(p["needed"], p["text"]) for p in result["products"]] == [
        ("4.5 dl vispgrädde", "Vispgrädde 36% 5dl Skånemejerier")
    ]
    api.add_to_list.assert_not_called()


async def test_commit_adds_the_product_rows():
    api = _api()
    with patch(
        "custom_components.ica_shopping.recipes.async_resolve_or_create_list",
        AsyncMock(return_value={"id": "list", "name": "Handla", "rows": []}),
    ):
        _, _, result = await _plan(api, match_products=True)
    assert result["added"] == ["Vispgrädde 36% 5dl Skånemejerier", "2 gula lökar"]
    assert [c.args[1] for c in api.add_to_list.await_args_list] == result["added"]


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
    api.async_get_store_account_number = AsyncMock(return_value="1003380")
    await _setup(hass, api, {CONF_STORE_ID: "11920"})
    plan = AsyncMock(return_value=(None, [], {"dinners": [], "added": [], "list": None}))

    with patch("custom_components.ica_shopping.async_plan_dinners", plan):
        await _commit_with_products(hass)
        await _commit_with_products(hass)

    api.async_get_store_account_number.assert_awaited_once_with("11920")
    assert plan.await_args.kwargs["match_products"] is True
    assert plan.await_args.kwargs["store_account"] == "1003380"


async def test_service_falls_back_to_all_stores(hass):
    api = MagicMock()
    api.async_get_favorite_stores = AsyncMock(side_effect=IcaApiError("HTTP 500"))
    await _setup(hass, api, {})
    plan = AsyncMock(return_value=(None, [], {"dinners": [], "added": [], "list": None}))

    with patch("custom_components.ica_shopping.async_plan_dinners", plan):
        await _commit_with_products(hass)

    assert plan.await_args.kwargs["store_account"] is None
