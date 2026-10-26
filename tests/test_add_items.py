"""add_items: rows the household asks for, minding what is on the list already."""
from unittest.mock import MagicMock, patch

from homeassistant.core import SupportsResponse
from homeassistant.helpers.llm import ToolInput

from custom_components.ica_shopping.const import DATA_ICA, DOMAIN
from custom_components.ica_shopping.llm import TOOLS, AddToShoppingListTool
from custom_components.ica_shopping.matching import merge

from .list_helpers import FakeList, _call, iso


def test_the_same_thing_merges():
    assert merge("Mjölk", "Mjölk 1 l") == "Mjölk 1 l"
    assert merge("Potatis 7 st", "Potatis 3 st") == "Potatis 10 st"
    assert merge("Potatis 7st", "3 potatisar") == "Potatis 10st"
    assert merge("Riven ost 1,5 dl", "Riven ost 2 dl") == "Riven ost 3,5 dl"
    # Into the row's unit
    assert merge("Mjölk 5 dl", "Mjölk 1 l") == "Mjölk 15 dl"
    # Nothing to add, or the same amount said again
    assert merge("Mjölk 1 l", "Mjölk") == "Mjölk 1 l"
    assert merge("Mjölk 1 l", "1 l mjölk") == "Mjölk 1 l"


def test_different_things_and_amounts_that_dont_add_up_stay_apart():
    assert merge("Mjölk 3%", "Mjölk 1,5%") is None
    assert merge("Ost", "Riven ost") is None
    assert merge("Potatis 2 kg", "Potatis 7 st") is None
    assert merge("2 x 500 g köttfärs", "Köttfärs 500 g") is None
    assert merge("Vispgrädde 36% 5dl Skånemejerier (4.5 dl vispgrädde)", "Vispgrädde 2 dl") is None


async def test_skips_what_is_on_the_list_already(hass, setup):
    the_list = FakeList("Mjölk", "Lök", "Potatis 7 st")
    await setup(the_list)

    response = await _call(hass, "add_items", items=["mjölk", "Röd lök", "Potatis", "Ägg"])

    assert response["skipped"] == [
        {"item": "mjölk", "row": "Mjölk"},
        # Covered the way planning matches rows
        {"item": "Röd lök", "row": "Lök"},
        {"item": "Potatis", "row": "Potatis 7 st"},
    ]
    assert response["added"] == ["Ägg"]
    assert response["merged"] == []
    assert the_list.texts() == ["Mjölk", "Lök", "Potatis 7 st", "Ägg"]
    hass.data[DOMAIN]["sync"].assert_awaited_once()


async def test_merges_the_same_thing_into_its_row(hass, setup):
    the_list = FakeList("Mjölk", "Potatis 7 st", "Smör")
    await setup(the_list)

    response = await _call(hass, "add_items", items="Mjölk 1 l\nPotatis 3 st\nPotatis 2 kg")

    assert response["merged"] == [
        {"item": "Mjölk 1 l", "row": "Mjölk", "now": "Mjölk 1 l"},
        {"item": "Potatis 3 st", "row": "Potatis 7 st", "now": "Potatis 10 st"},
    ]
    # The same thing in another unit goes on, and is pointed out
    assert response["added"] == ["Potatis 2 kg"]
    assert response["similar"] == [{"item": "Potatis 2 kg", "row": "Potatis 10 st"}]
    assert sorted(the_list.texts()) == ["Mjölk 1 l", "Potatis 10 st", "Potatis 2 kg", "Smör"]


async def test_items_in_one_call_merge_with_each_other(hass, setup):
    the_list = FakeList()
    await setup(the_list)

    response = await _call(hass, "add_items", items=["Mjölk", "Mjölk 1 l", "mjölk 1 l"])

    assert response["added"] == ["Mjölk 1 l"]
    assert the_list.texts() == ["Mjölk 1 l"]


async def test_allow_similar_only_skips_rows_said_again(hass, setup):
    the_list = FakeList("Mjölk", "Lök")
    await setup(the_list)

    response = await _call(
        hass, "add_items", items=["Mjölk 1 l", "Röd lök", "lök"], allow_similar=True
    )

    assert response["added"] == ["Mjölk 1 l", "Röd lök"]
    assert response["skipped"] == [{"item": "lök", "row": "Lök"}]
    assert the_list.texts() == ["Mjölk", "Lök", "Mjölk 1 l", "Röd lök"]


async def test_checked_off_rows_dont_count(hass, setup):
    the_list = FakeList("Mjölk", "Potatis 7 st")
    for row in the_list.rows:
        row["isStriked"] = True
    await setup(the_list)
    api = hass.data[DOMAIN][DATA_ICA]

    response = await _call(hass, "add_items", items=["mjölk", "Potatis 3 st"])

    # The row reading as the item is checked on again rather than added next to it
    api.set_striked.assert_awaited_once()
    assert api.set_striked.await_args.args[1:3] == ("r0", False)
    assert response["added"] == ["Mjölk", "Potatis 3 st"]
    assert response["merged"] == response["skipped"] == []


async def test_rows_are_sorted_by_their_grocery(hass, setup):
    the_list = FakeList("Mjölk")
    await setup(the_list)
    api = hass.data[DOMAIN][DATA_ICA]

    await _call(hass, "add_items", items=["Potatis 7 st", "Mjölk 1 l"])

    assert [(c.args[1], c.kwargs["name"]) for c in api.add_to_list.await_args_list] == [
        ("Mjölk 1 l", "Mjölk 1 l"), ("Potatis 7 st", "Potatis 7 st"),
    ]


async def test_a_full_list_skips_new_rows(hass, setup):
    the_list = FakeList(*[f"Vara {i}" for i in range(250)])
    await setup(the_list)

    response = await _call(hass, "add_items", items=["Ägg", "vara 1"])

    assert response["skipped_list_full"] == ["Ägg"]
    assert response["skipped"] == [{"item": "vara 1", "row": "Vara 1"}]
    assert response["added"] == []
    hass.data[DOMAIN]["sync"].assert_not_awaited()


async def test_a_failed_merge_leaves_the_old_row(hass, setup):
    the_list = FakeList("Potatis 7 st")
    await setup(the_list)
    api = hass.data[DOMAIN][DATA_ICA]
    api.add_to_list.side_effect = None
    api.add_to_list.return_value = False

    response = await _call(hass, "add_items", items=["Potatis 3 st"])

    assert response["merged"] == []
    assert response["failed"] == ["Potatis 10 st"]
    assert the_list.texts() == ["Potatis 7 st"]


async def _plan(hass, start):
    with patch("custom_components.ica_shopping._pick_calendar", return_value=None):
        return await _call(hass, "plan_dinners", recipes="1,2", start_date=start)


async def test_merged_rows_stay_tied_to_their_dinners(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)
    await _plan(hass, iso(1))

    response = await _call(hass, "add_items", items=["Vispgrädde 3 dl"])

    assert response["merged"] == [
        {"item": "Vispgrädde 3 dl", "row": "4.5 dl vispgrädde", "now": "7.5 dl vispgrädde"}
    ]
    listed = await _call(hass, "get_list_dinners")
    assert [d["on_list"] for d in listed["dinners"]] == [
        ["7.5 dl vispgrädde", "400 g pasta", "1 gul lök"],
        ["7.5 dl vispgrädde", "2 rödlökar"],
    ]
    # The household asked for the cream too, so removing the dinners leaves it
    assert "7.5 dl vispgrädde" not in memory.on(iso(1))["put_on_list"]
    await _call(hass, "remove_dinner", date=iso(1))
    await _call(hass, "remove_dinner", date=iso(2))
    assert the_list.texts() == ["7.5 dl vispgrädde"]


async def test_a_row_asked_for_again_stays_when_its_dinner_goes(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)
    await _plan(hass, iso(1))

    response = await _call(hass, "add_items", items=["Pasta"])

    assert response["skipped"] == [{"item": "Pasta", "row": "400 g pasta"}]
    assert memory.on(iso(1))["on_list"] == ["4.5 dl vispgrädde", "400 g pasta", "1 gul lök"]
    response = await _call(hass, "remove_dinner", date=iso(1))
    assert response["list_removed"] == ["1 gul lök"]
    assert "400 g pasta" in the_list.texts()


async def test_the_tool_forwards_to_add_items(hass):
    calls = []

    async def add_items(call):
        calls.append(dict(call.data))
        return {"added": ["Ägg"]}

    hass.services.async_register(DOMAIN, "add_items", add_items, supports_response=SupportsResponse.ONLY)
    tool = AddToShoppingListTool()
    await tool.async_call(
        hass,
        ToolInput(tool_name=tool.name, tool_args=tool.parameters({"items": ["Ägg"], "allow_similar": True})),
        MagicMock(),
    )
    assert calls == [{"items": ["Ägg"], "allow_similar": True}]
    assert any(isinstance(t, AddToShoppingListTool) for t in TOOLS)
