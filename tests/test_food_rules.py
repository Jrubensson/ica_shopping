import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol
from homeassistant.core import SupportsResponse
from homeassistant.helpers.llm import ToolInput
from homeassistant.util import dt as dt_util

from custom_components.ica_shopping import async_setup
from custom_components.ica_shopping.calendar import _to_event
from custom_components.ica_shopping.const import DATA_ICA, DOMAIN, RECIPE_WEB_URL, USER_AGENT
from custom_components.ica_shopping.ica_api import ICAApi
from custom_components.ica_shopping.llm import SetFoodRuleTool, SetRecipeVariantTool
from custom_components.ica_shopping.memory import DinnerMemory
from custom_components.ica_shopping.recipes import (
    async_fetch_recipes,
    async_plan_dinners,
    entry_description,
)
from custom_components.ica_shopping.rules import adjust, is_theme, make_rule, mentions

LOK = make_rule("lök", "omit", ["lök"], ["vitlök", "gräslök", "purjolök"])
RAKOR = make_rule("räkor", "omit")
MOROT = make_rule("morot", "omit", ["morot", "morött"])
NOTTER = make_rule("nötter", "never", ["nöt", "cashew"])
VEG = "Grönsaker, frukt, frön & nötter"


def iso(offset: int) -> str:
    return (dt_util.now().date() + dt.timedelta(days=offset)).isoformat()


def recipe(rid, title, lines, categories=None):
    r = {
        "id": rid,
        "title": title,
        "cookingTime": "Under 45 minuter",
        "ingredientGroups": [{"ingredients": [{"ingredient": t.split(" ", 1)[-1], "text": t} for t in lines]}],
    }
    if categories is not None:
        r["categories"] = categories
    return r


KOTTFARSSAS = recipe(
    720707, "Köttfärssås med spaghetti",
    ["500 g köttfärs", "1 gul lök", "1 vitlöksklyfta", "2 morötter", "400 g spaghetti"],
    [{"name": "Styckdel", "value": "Färs"}, {"name": "Typ av recept", "value": "Köttfärssås"}],
)
LOKSAS = recipe(
    740434, "Stekt fläsk med löksås", ["500 g rimmat sidfläsk", "3 gula lökar"],
    [{"name": "Protein", "value": "Fläsk"}, {"name": VEG, "value": "Lök"}],
)
PANNBIFF = recipe(
    727247, "Pannbiff med löksås", ["500 g nötfärs", "2 dl hackad lök"],
    [{"name": "Styckdel", "value": "Färs"}, {"name": VEG, "value": "Lök"}],
)
LASAGNE = recipe(
    679675, "Klassisk lasagne", ["500 g nötfärs", "2 gula lökar", "2 vitlöksklyftor"],
    [{"name": VEG, "value": "Grönsaker"}, {"name": VEG, "value": "Lök"}, {"name": "Typ av recept", "value": "Lasagne"}],
)
SKAGEN = recipe(
    735000, "Langos med skagenröra", ["300 g räkor i lake", "1 dl majonnäs"],
    [{"name": "Protein", "value": "Skaldjur"}, {"name": "Protein", "value": "Räkor"}],
)
RAKPASTA = recipe(730093, "Räkpasta med saffran", ["300 g räkor", "1 gul lök", "250 g pasta"], [])
WOK = recipe(
    731000, "Kycklingwok", ["600 g kycklingfilé", "1 dl cashewnötter"], [{"name": "Protein", "value": "Kyckling"}]
)


# --- Matching -------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("1 gul lök", True),
        ("3 gula lökar", True),
        ("1 rödlök", True),
        ("2 dl hackad fryst eller färsk lök", True),
        ("1 vitlöksklyfta", False),
        ("1/2 förp färsk gräslök (à 20 g)", False),
        ("1 purjolök", False),
        ("2 gula lökar och 1 vitlöksklyfta", True),
    ],
)
def test_lok_is_not_vitlok(text, hit):
    assert mentions(LOK, text) is hit


def test_terms_default_to_the_stem():
    assert RAKOR["terms"] == ["räk"]
    assert mentions(RAKOR, "Räkpasta med saffran")
    assert mentions(MOROT, "2 morötter") and mentions(MOROT, "1 morot")


def test_a_dish_is_built_around_what_its_title_or_ica_tags_name():
    assert is_theme(LOK, LOKSAS["title"])
    assert is_theme(LOK, PANNBIFF["title"], PANNBIFF["categories"])
    assert is_theme(RAKOR, SKAGEN["title"], SKAGEN["categories"])
    assert not is_theme(LOK, KOTTFARSSAS["title"], KOTTFARSSAS["categories"])


def test_vegetable_tags_only_list_ingredients():
    # Richly tagged recipes are tagged with nearly every vegetable in them
    assert not is_theme(LOK, LASAGNE["title"], LASAGNE["categories"])
    # Spices and the like say nothing about what the dish is built around
    assert not is_theme(make_rule("senap"), "Kycklinggryta", [{"name": "Kryddhyllan & smaksättare", "value": "Senap"}])


def test_adjust_leaves_out_rule_lines_and_applies_the_own_version():
    lines = ["500 g köttfärs", "1 gul lök", "1 vitlöksklyfta", "1 dl grädde"]
    omitted, extra = adjust(lines, [LOK], {"omit": ["1 dl grädde"], "add": ["1 tsk chili"]})
    assert omitted == [{"line": "1 gul lök", "rule": "lök"}, {"line": "1 dl grädde", "rule": None}]
    assert extra == ["1 tsk chili"]


def test_adjust_skips_rules_the_user_overrode():
    omitted, _ = adjust(["300 g räkor", "1 gul lök"], [RAKOR, LOK], keep=["räkor"])
    assert omitted == [{"line": "1 gul lök", "rule": "lök"}]


# --- Planning -------------------------------------------------------------------------------

def _api(*random_recipes, by_id=None):
    api = MagicMock()
    api.async_get_random_recipe = AsyncMock(side_effect=list(random_recipes) + [None] * 20)
    api.async_get_recipe = AsyncMock(side_effect=lambda rid: (by_id or {})[str(rid)])
    api.async_get_recipe_categories = AsyncMock(side_effect=lambda r: r.get("categories") or [])
    api.async_get_favorite_recipe_ids = AsyncMock(return_value=[])
    return api


async def test_suggestions_pass_by_dishes_built_around_an_avoided_ingredient():
    api = _api(LOKSAS, RAKPASTA, PANNBIFF, KOTTFARSSAS)
    picked = await async_fetch_recipes(api, 1, "random", rules=[LOK, RAKOR])
    assert [r["title"] for r in picked] == ["Köttfärssås med spaghetti"]


async def test_never_passes_by_any_recipe_with_the_ingredient():
    api = _api(WOK, KOTTFARSSAS)
    picked = await async_fetch_recipes(api, 1, "random", rules=[NOTTER])
    assert [r["title"] for r in picked] == ["Köttfärssås med spaghetti"]
    # Settled by the ingredients alone; no need for ICA's tags
    api.async_get_recipe_categories.assert_not_awaited()


async def test_tags_are_only_fetched_when_the_title_does_not_settle_it():
    api = _api(LOKSAS, SKAGEN, KOTTFARSSAS)
    picked = await async_fetch_recipes(api, 1, "random", rules=[LOK, RAKOR])
    assert [r["title"] for r in picked] == ["Köttfärssås med spaghetti"]
    # Löksås settles it by its title; the skagenröra and the köttfärssås need the tags
    assert [c.args[0]["title"] for c in api.async_get_recipe_categories.await_args_list] == [
        "Langos med skagenröra", "Köttfärssås med spaghetti",
    ]


async def test_favorites_are_filtered_too():
    api = _api(by_id={"740434": LOKSAS, "720707": KOTTFARSSAS})
    api.async_get_favorite_recipe_ids = AsyncMock(return_value=[740434, 720707])
    picked = await async_fetch_recipes(api, 2, "favorites", rules=[LOK])
    assert [r["title"] for r in picked] == ["Köttfärssås med spaghetti"]


async def _plan(api, **kwargs):
    return await async_plan_dinners(
        api, count=1, source="random", list_ref=None, default_list_id="list", never_add=[],
        preview=True, **kwargs,
    )


async def test_left_out_ingredients_stay_off_the_list_and_extras_go_on():
    api = _api(KOTTFARSSAS)
    _, _, result = await _plan(
        api,
        rules=[LOK, MOROT],
        variants={"720707": {"omit": [], "add": ["1 burk krossade tomater"]}},
    )
    dinner = result["dinners"][0]
    assert dinner["omitted"] == ["1 gul lök", "2 morötter"]
    assert dinner["extra"] == ["1 burk krossade tomater"]
    # The full recipe is kept for the calendar
    assert "1 gul lök" in dinner["ingredients"]
    assert result["ingredients"] == [
        "500 g köttfärs", "1 vitlöksklyfta", "400 g spaghetti", "1 burk krossade tomater",
    ]


async def test_a_named_dish_is_planned_and_keeps_what_it_is_built_around():
    api = _api(by_id={"730093": RAKPASTA})
    _, _, result = await _plan(api, recipe_ids=["730093"], rules=[RAKOR, LOK])
    dinner = result["dinners"][0]
    assert dinner["title"] == "Räkpasta med saffran"
    assert dinner["keep"] == ["räkor"]
    assert dinner["omitted"] == ["1 gul lök"]
    assert result["rule_conflicts"] == [
        {"id": 730093, "title": "Räkpasta med saffran", "ingredient": "räkor", "level": "omit"}
    ]
    assert "300 g räkor" in result["ingredients"]


async def test_nothing_changes_without_rules():
    api = _api(KOTTFARSSAS)
    _, _, result = await _plan(api)
    assert "omitted" not in result["dinners"][0] and "rule_conflicts" not in result


# --- Calendar ---------------------------------------------------------------------------------

def test_description_lists_what_is_left_out_and_added():
    entry = {
        "id": "720707",
        "cooking_time": "Under 45 minuter",
        "url": "https://www.ica.se/recept/kottfarssas-med-spaghetti-720707/",
        "ingredients": ["500 g köttfärs", "1 gul lök", "1 dl grädde"],
    }
    variants = {"720707": {"omit": ["1 dl grädde"], "add": ["2 morötter, rivna"], "note": None}}
    assert entry_description(entry, [LOK], variants) == (
        "Under 45 minuter\nhttps://www.ica.se/recept/kottfarssas-med-spaghetti-720707/\n\n"
        "• 500 g köttfärs\n• 1 gul lök\n• 1 dl grädde\n\n"
        "Utan: 1 gul lök (lök)\nUtan: 1 dl grädde\nLägg till: 2 morötter, rivna\nVår version"
    )


async def test_calendar_follows_rules_added_after_planning(hass):
    memory = DinnerMemory(hass)
    await memory.async_record([{**_dinner(KOTTFARSSAS), "date": iso(1)}])
    assert "Utan" not in (_to_event(memory.history[0], memory).description or "")

    await memory.async_set_food_rule("lök", "omit", ["lök"], ["vitlök"])
    assert "Utan: 1 gul lök (lök)" in _to_event(memory.history[0], memory).description


def _dinner(r):
    return {
        "id": r["id"],
        "title": r["title"],
        "ingredients": [i["text"] for i in r["ingredientGroups"][0]["ingredients"]],
    }


# --- Memory and actions -----------------------------------------------------------------------

async def test_rules_and_variants_survive_a_restart(hass, hass_storage):
    memory = DinnerMemory(hass)
    await memory.async_set_food_rule("lök", "omit", ["lök"], ["vitlök"])
    await memory.async_set_food_rule("lök", "never")
    await memory.async_set_variant("720707", omit=["1 gul lök"], add=["2 morötter"])

    loaded = await DinnerMemory(hass).async_load()
    assert loaded.food_rules == [{"ingredient": "lök", "level": "never", "terms": ["lök"], "except": []}]
    assert loaded.variants["720707"]["add"] == ["2 morötter"]


async def test_an_empty_variant_removes_it(hass):
    memory = DinnerMemory(hass)
    await memory.async_set_variant("720707", add=["2 morötter"])
    assert await memory.async_set_variant("720707") is None
    assert memory.variants == {}


async def test_clearing_preferences_clears_rules_and_variants(hass):
    memory = DinnerMemory(hass)
    await memory.async_add_preference("Vardagar: vegetariskt")
    await memory.async_set_food_rule("lök")
    await memory.async_set_variant("720707", add=["2 morötter"])
    assert (await memory.async_clear(history=False))["preferences"] == 3
    assert memory.food_rules == [] and memory.variants == {} and memory.preferences == []


async def _setup(hass):
    memory = DinnerMemory(hass)
    hass.data[DOMAIN] = {"memory": memory, "current_list_id": "list", DATA_ICA: MagicMock()}
    await async_setup(hass, {})
    return memory


async def _call(hass, service, data):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=True)


async def test_food_rule_actions(hass):
    memory = await _setup(hass)
    response = await _call(
        hass, "set_food_rule", {"ingredient": "lök", "terms": "lök", "except": "vitlök, gräslök"}
    )
    assert response["rule"] == {"ingredient": "lök", "level": "omit", "terms": ["lök"], "except": ["vitlök", "gräslök"]}
    assert (await _call(hass, "remove_food_rule", {"ingredient": "Lök"}))["removed"] == 1
    assert memory.food_rules == []
    with pytest.raises(vol.Invalid):
        await _call(hass, "set_food_rule", {"ingredient": "lök", "level": "sometimes"})


async def test_recipe_variant_actions(hass):
    memory = await _setup(hass)
    await _call(hass, "set_recipe_variant", {
        "recipe": 720707, "omit": "1 gul lök", "add": "2 morötter, rivna\n1 tsk chili",
    })
    assert memory.variants["720707"]["add"] == ["2 morötter, rivna", "1 tsk chili"]
    assert (await _call(hass, "remove_recipe_variant", {"recipe": "720707"}))["removed"] is True
    with pytest.raises(vol.Invalid):
        await _call(hass, "set_recipe_variant", {"recipe": "custom-ab12cd34", "add": "x"})


async def test_get_memory_includes_rules_and_variants(hass):
    memory = await _setup(hass)
    await memory.async_set_food_rule("räkor")
    response = await _call(hass, "get_memory", {})
    assert response["food_rules"][0]["ingredient"] == "räkor"
    assert response["variants"] == {}


async def test_llm_tools_forward_lists(hass):
    calls = []

    async def handler(call):
        calls.append(dict(call.data))
        return {}

    for tool, service in ((SetFoodRuleTool(), "set_food_rule"), (SetRecipeVariantTool(), "set_recipe_variant")):
        hass.services.async_register(DOMAIN, service, handler, supports_response=SupportsResponse.ONLY)
    for tool, args in (
        (SetFoodRuleTool(), {"ingredient": "lök", "except": ["vitlök"]}),
        (SetRecipeVariantTool(), {"recipe": "720707", "add": ["2 morötter"]}),
    ):
        await tool.async_call(hass, ToolInput(tool_name=tool.name, tool_args=tool.parameters(args)), MagicMock())
    assert calls == [
        {"ingredient": "lök", "except": ["vitlök"]},
        {"recipe": "720707", "add": ["2 morötter"]},
    ]


# --- ICA's tags -------------------------------------------------------------------------------

async def test_tags_come_from_the_recipe_page_and_are_cached(hass, aioclient_mock):
    page = '<script>{"defaultPortions":"4","categories":[{"name":"Protein","value":"Fläsk"},{"name":"%s","value":"Lök"}],"x":1}</script>' % VEG
    aioclient_mock.get(f"{RECIPE_WEB_URL}/740434/", text=page)
    api = ICAApi(hass, "199001011234", "pw")

    tags = await api.async_get_recipe_categories({"id": 740434})
    assert tags == [{"name": "Protein", "value": "Fläsk"}, {"name": VEG, "value": "Lök"}]
    await api.async_get_recipe_categories({"id": 740434})
    assert aioclient_mock.call_count == 1
    assert USER_AGENT not in (aioclient_mock.mock_calls[0][3] or {}).values()


async def test_tags_on_the_recipe_are_used_as_they_are(hass, aioclient_mock):
    api = ICAApi(hass, "199001011234", "pw")
    assert await api.async_get_recipe_categories(PANNBIFF) == PANNBIFF["categories"]
    assert aioclient_mock.call_count == 0


async def test_a_page_that_fails_gives_no_tags_and_is_tried_again(hass, aioclient_mock):
    aioclient_mock.get(f"{RECIPE_WEB_URL}/1/", status=500)
    api = ICAApi(hass, "199001011234", "pw")
    assert await api.async_get_recipe_categories({"id": 1}) == []
    assert await api.async_get_recipe_categories({"id": 1}) == []
    assert aioclient_mock.call_count == 2
