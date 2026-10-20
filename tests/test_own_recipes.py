import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.ica_shopping import async_setup
from custom_components.ica_shopping.calendar import _to_event
from custom_components.ica_shopping.const import DATA_ICA, DOMAIN
from custom_components.ica_shopping.memory import DinnerMemory
from custom_components.ica_shopping.recipes import _own_ingredient


def iso(offset: int) -> str:
    return (dt_util.now().date() + dt.timedelta(days=offset)).isoformat()


PASTA = {
    "id": "1",
    "title": "Pasta",
    "ingredientGroups": [{"ingredients": [
        {"ingredient": "vispgrädde", "text": "3 dl vispgrädde", "quantity": 3, "unit": "dl"},
        {"ingredient": "pasta", "text": "400 g pasta", "quantity": 400, "unit": "g"},
    ]}],
}


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("2 dl vispgrädde", ("vispgrädde", 2, "dl")),
        ("2,5 dl mjölk", ("mjölk", 2.5, "dl")),
        ("½ tsk salt", ("salt", 0.5, "tsk")),
        ("1/2 citron", ("citron", 0.5, None)),
        ("1 gul lök", ("gul lök", 1, None)),
        ("1 lime", ("lime", 1, None)),
        ("2 förp potatis- och purjolökssoppa", ("potatis- och purjolökssoppa", 2, "förp")),
        ("salt och peppar", ("salt och peppar", None, None)),
    ],
)
def test_own_lines_get_an_amount_and_a_name(line, expected):
    ing = _own_ingredient(line)
    assert (ing["ingredient"], ing.get("quantity"), ing.get("unit")) == expected
    assert ing["text"] == line


class FakeList:
    def __init__(self):
        self.rows = []

    async def add(self, _list, text, striked=False):
        self.rows.append({"id": f"r{len(self.rows)}", "text": text, "isStriked": striked})
        return True


@pytest.fixture
async def setup(hass):
    the_list = FakeList()
    api = MagicMock()
    api.async_get_recipe = AsyncMock(side_effect=lambda rid: {"1": PASTA}[rid])
    api.async_get_recipe_categories = AsyncMock(return_value=[])
    api.async_search_recipes = AsyncMock(
        return_value=[{"id": "9", "title": "Potatis- och purjolökssoppa", "imageUrl": "/imagevaultfiles/id_1/soppa.jpg"}]
    )
    api.async_get_lists = AsyncMock(return_value=[{"id": "LIST", "name": "Handla"}])
    api.async_get_list = AsyncMock(side_effect=lambda _id: {"id": "LIST", "name": "Handla", "rows": list(the_list.rows)})
    api.add_to_list = AsyncMock(side_effect=the_list.add)
    memory = DinnerMemory(hass)
    hass.data[DOMAIN] = {
        "memory": memory,
        "sync": AsyncMock(),
        "current_list_id": "list",
        "current_options": {},
        DATA_ICA: api,
    }
    await async_setup(hass, {})
    return memory, api, the_list


async def _call(hass, service, **data):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=True)


async def _soup(hass, **extra):
    return await _call(
        hass,
        "save_recipe",
        title="Potatis- och purjolökssoppa",
        ingredients="2 förp potatis- och purjolökssoppa\n1 baguette\n1 dl vispgrädde",
        steps=["1. Värm soppan.", "2) Servera med bröd."],
        **extra,
    )


async def test_save_recipe_finds_a_picture_and_tidies_the_steps(hass, setup):
    memory, api, _ = setup
    soup = await _soup(hass)

    assert soup["id"].startswith("own-")
    assert soup["ingredients"] == ["2 förp potatis- och purjolökssoppa", "1 baguette", "1 dl vispgrädde"]
    assert soup["steps"] == ["Värm soppan.", "Servera med bröd."]
    assert soup["image"] == "https://assets.icanet.se/imagevaultfiles/id_1/soppa.jpg"
    api.async_search_recipes.assert_awaited_once_with("Potatis- och purjolökssoppa", limit=1)
    assert memory.as_dict()["own_recipes"] == [{"id": soup["id"], "title": soup["title"]}]

    listed = await _call(hass, "get_own_recipes")
    assert listed["recipes"] == [soup]


async def test_save_recipe_changes_only_what_is_given(hass, setup):
    _, api, _ = setup
    soup = await _soup(hass, note="Gott med ost")

    changed = await _call(hass, "save_recipe", recipe=soup["title"].upper(), steps="Värm soppan försiktigt.", note="")
    assert changed["id"] == soup["id"]
    assert changed["ingredients"] == soup["ingredients"]
    assert changed["steps"] == ["Värm soppan försiktigt."]
    assert "note" not in changed
    # It already had a picture
    api.async_search_recipes.assert_awaited_once()

    await _call(hass, "save_recipe", recipe=soup["id"], image_search="soppa")
    api.async_search_recipes.assert_awaited_with("soppa", limit=1)


async def test_save_recipe_refuses_unknown_and_untitled(hass, setup):
    with pytest.raises(HomeAssistantError):
        await _call(hass, "save_recipe", recipe="own-nope", steps="x")
    with pytest.raises(HomeAssistantError):
        await _call(hass, "save_recipe", ingredients="1 dl grädde")


async def test_own_recipe_plans_with_ica_recipes(hass, setup):
    memory, _, the_list = setup
    soup = await _soup(hass)

    with patch("custom_components.ica_shopping._pick_calendar", return_value=None):
        response = await _call(hass, "plan_dinners", recipes=f"1,{soup['id']}", start_date=iso(1))

    pasta, own = response["dinners"]
    assert own["id"] == soup["id"] and own["url"] is None
    # Equal amounts add up across ICA's recipe and our own
    assert [r["text"] for r in the_list.rows] == [
        "4 dl vispgrädde", "400 g pasta", "2 förp potatis- och purjolökssoppa", "1 baguette",
    ]
    assert own["on_list"] == ["4 dl vispgrädde", "2 förp potatis- och purjolökssoppa", "1 baguette"]

    entry = memory.on(iso(2))
    assert entry["id"] == soup["id"]
    description = _to_event(entry, memory).description
    assert description.startswith(f"Eget recept ({soup['id']})")
    assert "• 1 baguette" in description


async def test_source_own_picks_among_own_recipes(hass, setup):
    memory, _, _ = setup
    soup = await _soup(hass)
    with patch("custom_components.ica_shopping._pick_calendar", return_value=None):
        response = await _call(hass, "plan_dinners", source="own", count=3, preview=True)
    assert [d["id"] for d in response["dinners"]] == [soup["id"]]


async def test_changing_a_recipe_updates_coming_dinners(hass, setup):
    memory, _, _ = setup
    soup = await _soup(hass)
    with patch("custom_components.ica_shopping._pick_calendar", return_value=None):
        await _call(hass, "plan_dinners", recipes=soup["id"], start_date=iso(1), add_to_list=False)

    await _call(hass, "save_recipe", recipe=soup["id"], title="Soppa och bröd", ingredients="1 förp soppa")
    entry = memory.on(iso(1))
    assert entry["title"] == "Soppa och bröd"
    assert entry["ingredients"] == ["1 förp soppa"]

    removed = await _call(hass, "remove_recipe", recipe=soup["id"])
    assert removed["removed"]["title"] == "Soppa och bröd"
    assert memory.own_recipes == {}
    # The planned dinner stays
    assert memory.on(iso(1))["title"] == "Soppa och bröd"
    with pytest.raises(HomeAssistantError):
        await _call(hass, "remove_recipe", recipe=soup["id"])
