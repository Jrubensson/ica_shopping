from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.ica_shopping import async_setup
from custom_components.ica_shopping.const import CONF_PREFER_OFFERS, DATA_ICA, DOMAIN
from custom_components.ica_shopping.memory import DinnerMemory
from custom_components.ica_shopping.products import (
    async_match_products,
    choose_package,
    choose_product,
    offer_for,
    offers_by_ean,
)
from custom_components.ica_shopping.recipes import async_plan_dinners
from custom_components.ica_shopping.rules import make_rule


def _p(name, gtin, **extra):
    return {"displayName": name, "gtin": gtin, **extra}


# Shaped like offerservice's offersdiscounts: EANs without the search's leading zero.
GRADDE_OFFER = {
    "name": "Vispgrädde",
    "brand": "Arla Köket",
    "parsedMechanics": {"value1": "2 för", "value2": "40", "unitSign": "kr"},
    "validTo": "2026-10-26T00:00:00",
    "eans": [{"id": "7310865061935", "articleDescription": "Vispgrädde 36% 5dl Arla Köket"}],
}
SOAP_OFFER = {"name": "Diskmedel", "eans": [{"id": "7310000000001"}]}

# ICA's order from a search for vispgrädde at a Maxi.
GRADDE = [
    _p("Vispgrädde Färsk 40% 5dl ICA", "07318690105732"),
    _p("Vispgrädde 36% 2,5dl Arla Köket®", "07310865026392"),
    _p("Vispgrädde 36% 5dl Arla Köket®", "07310865061935"),
]


def test_offers_are_keyed_by_ean_without_leading_zeros():
    found = offers_by_ean([GRADDE_OFFER, {"name": "Utan EAN", "eans": [{"id": ""}]}])
    assert list(found) == ["7310865061935"]
    assert found["7310865061935"]["deal"] == "2 för 40kr"


def test_the_product_on_offer_wins_over_icas_ranking():
    offered = offers_by_ean([GRADDE_OFFER])
    assert choose_product("vispgrädde", "volume", 450, GRADDE)[0] is GRADDE[0]
    assert choose_product("vispgrädde", "volume", 450, GRADDE, offered)[0] is GRADDE[2]


def test_an_offer_never_beats_a_better_fit():
    offered = offers_by_ean([GRADDE_OFFER])
    # 2 dl fits the 2,5 dl pack; the 5 dl on offer is more than 1.5× that.
    small = [GRADDE[2], GRADDE[1]]
    assert choose_product("vispgrädde", "volume", 200, small, offered)[0] is GRADDE[1]


def test_of_two_small_packages_the_same_size_the_one_on_offer_wins():
    paprika = [_p("Paprika Malen 20g Santa Maria", "0001"), _p("Paprika Malen 20g ICA", "0002")]
    offer = [{"name": "Paprika", "eans": [{"id": "2"}]}]
    assert choose_package("paprika", "measure", 5, paprika)[0] is paprika[0]
    assert choose_package("paprika", "measure", 5, paprika, offers_by_ean(offer))[0] is paprika[1]


async def test_a_matched_product_on_offer_carries_the_deal():
    api = MagicMock()
    api.async_search_products = AsyncMock(return_value=GRADDE)
    wanted = [{"name": "vispgrädde", "text": "4.5 dl vispgrädde", "quantity": 4.5, "unit": "dl"}]
    [result] = await async_match_products(api, wanted, "1051012", [GRADDE_OFFER])
    assert result["product"] == "Vispgrädde 36% 5dl Arla Köket®"
    assert result["offer"]["deal"] == "2 för 40kr"
    [plain] = await async_match_products(api, wanted, "1051012")
    assert "offer" not in plain


def test_an_ingredient_finds_an_offer_by_whole_words_only():
    assert offer_for("vispgrädde", [SOAP_OFFER, GRADDE_OFFER])["name"] == "Vispgrädde"
    assert offer_for("grädde", [GRADDE_OFFER]) is None
    assert offer_for("smör", [{"name": "Smörgåsgurka", "eans": []}]) is None
    # By one of the offer's products when its name is a heading.
    chicken = {"name": "Fågel", "eans": [{"id": "1", "articleDescription": "Kycklingfilé Fryst 1kg"}]}
    assert offer_for("kycklingfilé", [chicken])["name"] == "Fågel"
    assert offer_for("gula lökar", [{"name": "Lök gul 1kg", "eans": []}])["name"] == "Lök gul 1kg"


def test_a_flavour_or_scent_is_not_an_offer_on_the_ingredient():
    chips = {"name": "Chips", "eans": [{"id": "1", "articleDescription": "Chips Gräddfil & Lök 175g"}]}
    soap = {"name": "Diskmedel Citron", "eans": []}
    assert offer_for("lök", [chips]) is None
    assert offer_for("citron", [soap]) is None
    # A describing word may come first, as in ICA's product names.
    assert offer_for("sallad", [{"name": "Finbladig Sallad", "eans": []}])["name"] == "Finbladig Sallad"


def _recipe(rid, *names):
    return {
        "id": rid,
        "title": f"Rätt {rid}",
        "ingredientGroups": [{"ingredients": [{"ingredient": n, "text": f"1 dl {n}"} for n in names]}],
    }


def _random_api(*recipes):
    api = MagicMock()
    api.async_get_random_recipe = AsyncMock(side_effect=list(recipes))
    return api


async def _suggest(api, count, **kwargs):
    _, _, result = await async_plan_dinners(
        api, count=count, source="random", list_ref=None, default_list_id="list",
        never_add=["smör"], preview=True, **kwargs,
    )
    return result


async def test_without_offers_suggestions_are_icas_random_order():
    api = _random_api(_recipe("1", "pasta"), _recipe("2", "vispgrädde"))
    result = await _suggest(api, 1)
    assert [d["id"] for d in result["dinners"]] == ["1"]
    assert api.async_get_random_recipe.await_count == 1


async def test_offers_tip_the_suggestions_towards_recipes_that_use_them():
    api = _random_api(
        _recipe("1", "pasta"), _recipe("2", "vispgrädde", "lax"),
        _recipe("3", "potatis"), _recipe("4", "ris"),
    )
    result = await _suggest(api, 2, offers=[GRADDE_OFFER])
    # Twice as many drawn; the one using the offer goes first, the rest keep their order.
    assert api.async_get_random_recipe.await_count == 4
    assert [d["id"] for d in result["dinners"]] == ["2", "1"]
    assert result["dinners"][0]["offers"] == [{
        "ingredient": "vispgrädde", "name": "Vispgrädde", "brand": "Arla Köket",
        "deal": "2 för 40kr", "valid_to": "2026-10-26T00:00:00",
    }]
    assert "offers" not in result["dinners"][1]


async def test_a_never_add_staple_on_offer_does_not_count():
    api = _random_api(_recipe("1", "pasta"), _recipe("2", "smör"))
    result = await _suggest(api, 1, offers=[{"name": "Smör", "eans": []}])
    assert [d["id"] for d in result["dinners"]] == ["1"]


async def test_an_offer_on_an_ingredient_a_food_rule_leaves_out_does_not_count():
    api = _random_api(_recipe("1", "pasta"), _recipe("2", "vispgrädde", "lax"))
    # No tags, so the dish isn't built around vispgrädde and is suggested without it.
    api.async_get_recipe_categories = AsyncMock(return_value=[])
    result = await _suggest(api, 1, offers=[GRADDE_OFFER], rules=[make_rule("vispgrädde")])
    assert [d["id"] for d in result["dinners"]] == ["1"]


async def test_picked_recipes_are_not_reordered_but_show_their_offers():
    recipes = {"1": _recipe("1", "pasta"), "2": _recipe("2", "vispgrädde")}
    api = MagicMock()
    api.async_get_recipe = AsyncMock(side_effect=lambda rid: recipes[rid])
    result = await _suggest(api, 2, recipe_ids=["1", "2"], offers=[GRADDE_OFFER])
    assert [d["id"] for d in result["dinners"]] == ["1", "2"]
    assert result["dinners"][1]["offers"][0]["ingredient"] == "vispgrädde"


async def _call_plan(hass, options):
    api = MagicMock()
    api.async_get_favorite_stores = AsyncMock(return_value=[{"id": "17687", "name": "Maxi"}])
    api.async_get_store_offers = AsyncMock(return_value=[GRADDE_OFFER])
    hass.data[DOMAIN] = {
        "memory": DinnerMemory(hass),
        "sync": AsyncMock(),
        "current_list_id": "list",
        "current_options": options,
        DATA_ICA: api,
    }
    await async_setup(hass, {})
    plan = AsyncMock(return_value=(None, [], {"dinners": [], "added": [], "list": None}))
    with patch("custom_components.ica_shopping.async_plan_dinners", plan):
        await hass.services.async_call(
            DOMAIN, "plan_dinners", {"count": 2, "source": "random", "preview": True},
            blocking=True, return_response=True,
        )
    return api, plan.await_args.kwargs["offers"]


async def test_offers_are_left_out_unless_the_household_opted_in(hass):
    api, offers = await _call_plan(hass, {})
    assert offers is None
    api.async_get_store_offers.assert_not_called()


async def test_opting_in_passes_the_store_offers_to_planning(hass):
    api, offers = await _call_plan(hass, {CONF_PREFER_OFFERS: True})
    assert offers == [GRADDE_OFFER]
    api.async_get_store_offers.assert_awaited_once_with("17687")
