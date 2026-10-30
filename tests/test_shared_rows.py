"""Dinners planned one at a time share one row for the same thing, and take their amount off
it again when they go."""
from unittest.mock import AsyncMock, patch

from custom_components.ica_shopping.const import DATA_ICA, DOMAIN
from custom_components.ica_shopping.matching import add_up, merge
from custom_components.ica_shopping.recipes import add_to_row, plain_line

from .list_helpers import FakeList, _call, _ing, _recipe, iso

GRYTA = _recipe(
    "3", "Gryta",
    _ing("vispgrädde", "3 dl vispgrädde", 3, "dl"),
    _ing("purjolök", "2 purjolökar", 2, None),
)
FISKSOPPA = _recipe(
    "4", "Fisksoppa",
    _ing("vispgrädde", "1,5 dl vispgrädde", 1.5, "dl"),
    _ing("purjolök", "2 purjolökar", 2, None),
    _ing("torsk", "400 g torsk", 400, "g"),
)


async def _setup(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)
    hass.data[DOMAIN][DATA_ICA].async_get_recipe = AsyncMock(
        side_effect=lambda rid: {"3": GRYTA, "4": FISKSOPPA}[rid]
    )
    return the_list, memory


async def _plan(hass, recipes, start):
    with patch("custom_components.ica_shopping._pick_calendar", return_value=None):
        return await _call(hass, "plan_dinners", recipes=recipes, start_date=start)


def test_plans_add_up_even_the_same_amount():
    assert add_up("3 dl vispgrädde", "4.5 dl vispgrädde") == "7.5 dl vispgrädde"
    # Two dinners need two each; merge() takes it as the row said again
    assert add_up("2 purjolökar", "2 purjolökar") == "4 purjolökar"
    assert merge("2 purjolökar", "2 purjolökar") == "2 purjolökar"
    assert add_up("Mjölk", "2 dl mjölk") is None
    assert add_up("2 dl mjölk", "1 msk mjölk") is None
    assert add_up("7.5 dl vispgrädde", "3 dl vispgrädde", less=True) == "4.5 dl vispgrädde"
    assert add_up("3 dl vispgrädde", "3 dl vispgrädde", less=True) == ""


def test_product_rows_count_packages_for_all_their_dinners():
    row = "Matlagningsgrädde 13% 5dl ICA (3.5 dl matlagningsgrädde)"
    both = "2 × Matlagningsgrädde 13% 5dl ICA (6 dl matlagningsgrädde)"
    assert add_to_row(row, "Matlagningsgrädde 13% 5dl ICA (2.5 dl matlagningsgrädde)") == both
    # A line of what it is bought for adds too
    assert add_to_row(row, "1 dl matlagningsgrädde") == "Matlagningsgrädde 13% 5dl ICA (4.5 dl matlagningsgrädde)"
    assert add_to_row(both, "2.5 dl matlagningsgrädde", less=True) == row
    # Amounts that don't add up in the package stay two rows
    assert add_to_row(
        "Fast potatis 1,2kg Klass 1 ICA (900 g fast potatis)",
        "Fast potatis 1,2kg Klass 1 ICA (8 fast potatisar)",
    ) is None
    # A spoonful adds to a carton's amounts
    assert add_to_row("Vispgrädde 36% 5dl ICA (3 dl vispgrädde)", "Vispgrädde 36% 5dl ICA (2 msk vispgrädde)") == (
        "Vispgrädde 36% 5dl ICA (3 dl vispgrädde + 2 msk vispgrädde)"
    )
    # Another product, or something else, is another row
    assert add_to_row(row, "Matlagningsgrädde 15% 5dl Arla (2 dl matlagningsgrädde)") is None
    assert add_to_row(row, "2 dl mjölk") is None


def test_recipe_lines_read_as_plans_write_them():
    assert plain_line("2 1/2 dl mellangrädde (27%)") == "2.5 dl matlagningsgrädde"
    assert plain_line("1/2 kycklingbuljongtärning") == "0.5 kycklingbuljongtärning"
    assert plain_line("1,5 dl vispgrädde") == "1.5 dl vispgrädde"
    assert plain_line("ca 400 g halloumi eller grillost") == "400 g halloumi eller grillost"
    assert plain_line("ca 400 g halloumi eller grillost", first=True) == "400 g halloumi"
    assert plain_line("1 l fisk- eller grönsaksbuljong", first=True) == "1 l fisk- eller grönsaksbuljong"
    assert plain_line("Persilja") == "Persilja"


async def test_dinners_planned_one_at_a_time_share_a_row(hass, setup):
    the_list, memory = await _setup(hass, setup)
    await _plan(hass, "3", iso(1))

    response = await _plan(hass, "4", iso(2))

    assert response["merged"] == [
        {"item": "1.5 dl vispgrädde", "row": "3 dl vispgrädde", "now": "4.5 dl vispgrädde"},
        {"item": "2 purjolök", "row": "2 purjolök", "now": "4 purjolök"},
    ]
    assert the_list.texts() == ["4.5 dl vispgrädde", "4 purjolök", "400 g torsk"]
    assert memory.on(iso(1))["put_on_list"] == ["4.5 dl vispgrädde", "4 purjolök"]
    assert memory.on(iso(2))["put_on_list"] == ["4.5 dl vispgrädde", "4 purjolök", "400 g torsk"]
    hass.data[DOMAIN]["sync"].assert_awaited()

    response = await _call(hass, "remove_dinner", date=iso(2))

    assert response["list_removed"] == ["400 g torsk"]
    assert response["list_changed"] == [
        {"row": "4.5 dl vispgrädde", "now": "3 dl vispgrädde"},
        {"row": "4 purjolök", "now": "2 purjolök"},
    ]
    assert the_list.texts() == ["3 dl vispgrädde", "2 purjolök"]
    assert memory.on(iso(1))["put_on_list"] == ["3 dl vispgrädde", "2 purjolök"]


async def test_planning_a_dinner_again_does_not_add_to_its_own_row(hass, setup):
    the_list, _ = await _setup(hass, setup)
    await _plan(hass, "3", iso(1))

    response = await _plan(hass, "3", iso(1))

    assert "merged" not in response
    assert the_list.texts() == ["3 dl vispgrädde", "2 purjolök"]


async def test_a_changed_recipe_moves_its_amount_on_a_shared_row(hass, setup):
    the_list, _ = await _setup(hass, setup)
    await _plan(hass, "3", iso(1))
    await _plan(hass, "4", iso(2))

    response = await _call(
        hass, "set_recipe_variant", recipe="3", omit=["3 dl vispgrädde"], add=["2 dl vispgrädde"],
        update_list=True,
    )

    assert response["list_changed"] == [
        {"row": "4.5 dl vispgrädde", "now": "1.5 dl vispgrädde"},
        {"row": "1.5 dl vispgrädde", "now": "3.5 dl vispgrädde"},
    ]
    assert the_list.texts() == ["4 purjolök", "400 g torsk", "3.5 dl vispgrädde"]
