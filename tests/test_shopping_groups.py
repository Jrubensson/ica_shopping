"""Groceries recipes name differently that the household buys as one thing."""
from unittest.mock import AsyncMock, patch

from custom_components.ica_shopping.const import DATA_ICA, DOMAIN
from custom_components.ica_shopping.matching import shopping_name
from custom_components.ica_shopping.recipes import shopping_line

from .list_helpers import FakeList, _call, _ing, _recipe, iso

TACOS = _recipe(
    "5", "Tacos",
    _ing("tacoost", "2 dl riven tacoost", 2, "dl"),
    _ing("mellanmjölk", "1 dl mellanmjölk", 1, "dl"),
)
GRATANG = _recipe(
    "6", "Gratäng",
    _ing("riven ost", "1 1/2 dl riven ost", 1.5, "dl"),
    _ing("mjölk", "3 dl mjölk (gärna 1,5 %)", 3, "dl"),
    _ing("ost", "200 g ost", 200, "g"),
)


async def _setup(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)
    hass.data[DOMAIN][DATA_ICA].async_get_recipe = AsyncMock(
        side_effect=lambda rid: {"5": TACOS, "6": GRATANG}[rid]
    )
    return the_list, memory


async def _plan(hass, recipes, start, **data):
    with patch("custom_components.ica_shopping._pick_calendar", return_value=None):
        return await _call(hass, "plan_dinners", recipes=recipes, start_date=start, **data)


def test_names_for_shopping():
    assert shopping_name("Tacoost") == "riven ost"
    assert shopping_name("riven pizzaost") == "riven ost"
    assert shopping_name("finriven gratängost") == "riven ost"
    assert shopping_name("riven ost") == "riven ost"
    assert shopping_name("mellanmjölk") == "mjölk"
    assert shopping_name("mellangrädde") == "matlagningsgrädde"
    assert shopping_name("creme fraiche") == "crème fraiche"
    # A block of cheese, a grated cheese of its own and whipping cream are what they are
    assert shopping_name("ost") == "ost"
    assert shopping_name("riven parmesan") == "riven parmesan"
    assert shopping_name("vispgrädde") == "vispgrädde"
    assert shopping_name("crème fraiche vitlök/parmesan") == "crème fraiche vitlök/parmesan"
    assert shopping_line("2 dl riven tacoost") == {"name": "riven ost", "text": "2 dl riven ost"}


async def test_one_plan_buys_grouped_groceries_as_one(hass, setup):
    the_list, _ = await _setup(hass, setup)

    response = await _plan(hass, "5,6", iso(1), preview=True)

    groups = {g["name"]: g for g in response["ingredient_groups"]}
    assert groups["riven ost"]["text"] == "3.5 dl riven ost"
    assert groups["riven ost"]["dinners"] == ["Tacos", "Gratäng"]
    assert groups["mjölk"]["text"] == "4 dl mjölk"
    assert groups["ost"]["text"] == "200 g ost"

    await _plan(hass, "5,6", iso(1))
    assert the_list.texts() == ["3.5 dl riven ost", "4 dl mjölk", "200 g ost"]


async def test_grouped_groceries_add_up_across_plans(hass, setup):
    the_list, _ = await _setup(hass, setup)
    await _plan(hass, "5", iso(1))

    await _plan(hass, "6", iso(2))

    assert the_list.texts() == ["3.5 dl riven ost", "4 dl mjölk", "200 g ost"]

    response = await _call(hass, "remove_dinner", date=iso(1))
    assert response["list_changed"] == [
        {"row": "3.5 dl riven ost", "now": "1.5 dl riven ost"},
        {"row": "4 dl mjölk", "now": "3 dl mjölk"},
    ]
