import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.ica_shopping import async_setup
from custom_components.ica_shopping.const import DATA_ICA, DOMAIN
from custom_components.ica_shopping.memory import DinnerMemory


def _ing(name, text, quantity=None, unit=None):
    return {"ingredient": name, "text": text, "quantity": quantity, "unit": unit}


def _recipe(rid, title, *ings):
    return {"id": rid, "title": title, "ingredientGroups": [{"ingredients": list(ings)}]}


PASTA = _recipe(
    "1", "Pasta",
    _ing("vispgrädde", "3 dl vispgrädde", 3, "dl"),
    _ing("pasta", "400 g pasta", 400, "g"),
    _ing("Lök", "1 gul lök", 1, None),
)
SOPPA = _recipe(
    "2", "Soppa",
    _ing("Vispgrädde", "1,5 dl vispgrädde", 1.5, "dl"),
    _ing("Lök", "2 rödlökar", 2, "st"),
)


def iso(offset: int) -> str:
    return (dt_util.now().date() + dt.timedelta(days=offset)).isoformat()


class FakeList:
    """An ICA list whose rows the API mock reads and changes."""

    def __init__(self, *texts):
        self.rows = [{"id": f"r{i}", "text": t, "isStriked": False} for i, t in enumerate(texts)]

    def api(self):
        api = MagicMock()
        api.async_get_recipe = AsyncMock(side_effect=lambda rid: {"1": PASTA, "2": SOPPA}[rid])
        api.async_get_lists = AsyncMock(return_value=[{"id": "LIST", "name": "Handla"}])
        api.async_get_list = AsyncMock(
            side_effect=lambda _id: {"id": "LIST", "name": "Handla", "rows": [dict(r) for r in self.rows]}
        )
        api.add_to_list = AsyncMock(side_effect=self._add)
        api.remove_items = AsyncMock(side_effect=self._remove)
        api.set_striked = AsyncMock(return_value=True)
        return api

    async def _add(self, _list, text, striked=False):
        # Never reuse an id, or removing one row would take another along
        self.added = getattr(self, "added", 0) + 1
        self.rows.append({"id": f"n{self.added}", "text": text, "isStriked": striked})
        return True

    async def _remove(self, _list, ids):
        self.rows = [r for r in self.rows if r["id"] not in ids]
        return True

    def texts(self):
        return [r["text"] for r in self.rows]


@pytest.fixture
async def setup(hass):
    async def _setup(the_list):
        memory = DinnerMemory(hass)
        hass.data[DOMAIN] = {
            "memory": memory,
            "sync": AsyncMock(),
            "current_list_id": "list",
            "current_options": {},
            DATA_ICA: the_list.api(),
        }
        await async_setup(hass, {})
        return memory
    return _setup


async def _call(hass, service, **data):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=True)


async def _plan(hass, start):
    with patch("custom_components.ica_shopping._pick_calendar", return_value=None):
        return await _call(hass, "plan_dinners", recipes="1,2", start_date=start)


async def test_plan_records_the_rows_each_dinner_needs(hass, setup):
    the_list = FakeList("1 gul lök")
    memory = await setup(the_list)

    response = await _plan(hass, iso(1))

    pasta, soppa = response["dinners"]
    # The summed row is both dinners'; a line of their own is only theirs
    assert pasta["on_list"] == ["4.5 dl vispgrädde", "400 g pasta", "1 gul lök"]
    assert soppa["on_list"] == ["4.5 dl vispgrädde", "2 rödlökar"]
    # The onion was on the list already, so removing Pasta must leave it
    assert pasta["put_on_list"] == ["4.5 dl vispgrädde", "400 g pasta"]
    assert memory.on(iso(1))["put_on_list"] == ["4.5 dl vispgrädde", "400 g pasta"]
    # Not for the LLM
    assert "on_list" not in memory.as_dict()["history"][0]

    listed = await _call(hass, "get_list_dinners")
    assert [(d["title"], d["date"], d["on_list"]) for d in listed["dinners"]] == [
        ("Pasta", iso(1), pasta["on_list"]),
        ("Soppa", iso(2), soppa["on_list"]),
    ]


async def test_remove_dinner_keeps_rows_another_dinner_needs(hass, setup):
    the_list = FakeList("1 gul lök")
    memory = await setup(the_list)
    await _plan(hass, iso(1))
    hass.data[DOMAIN]["sync"].reset_mock()

    response = await _call(hass, "remove_dinner", date=iso(1))

    assert response["removed"]["title"] == "Pasta"
    assert response["list_removed"] == ["400 g pasta"]
    assert the_list.texts() == ["1 gul lök", "4.5 dl vispgrädde", "2 rödlökar"]
    hass.data[DOMAIN]["sync"].assert_awaited_once()
    assert memory.on(iso(1)) is None

    # The cream Pasta put there is now Soppa's to take off
    response = await _call(hass, "remove_dinner", date=iso(2))
    assert response["list_removed"] == ["4.5 dl vispgrädde", "2 rödlökar"]
    assert the_list.texts() == ["1 gul lök"]


async def test_remove_dinner_leaves_checked_rows_and_can_skip_the_list(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)
    await _plan(hass, iso(1))
    the_list.rows[1]["isStriked"] = True  # 400 g pasta, already in the basket

    response = await _call(hass, "remove_dinner", date=iso(2), update_list=False)
    assert response["list_removed"] == []
    assert len(the_list.rows) == 4

    response = await _call(hass, "remove_dinner", date=iso(1))
    assert response["list_removed"] == ["4.5 dl vispgrädde", "1 gul lök"]
    assert the_list.texts() == ["400 g pasta", "2 rödlökar"]
    assert memory.history == []


async def test_remove_dinner_on_an_empty_day(hass, setup):
    await setup(FakeList())
    with pytest.raises(HomeAssistantError):
        await _call(hass, "remove_dinner", date=iso(1))


async def test_move_dinner_swaps_with_one_already_there(hass, setup):
    memory = await setup(FakeList())
    await _plan(hass, iso(1))

    response = await _call(hass, "move_dinner", date=iso(1), to=iso(2))
    assert [(d["title"], d["date"]) for d in response["moved"]] == [("Pasta", iso(2)), ("Soppa", iso(1))]
    assert memory.on(iso(1))["title"] == "Soppa"

    response = await _call(hass, "move_dinner", date=iso(1), to=iso(5))
    assert [(d["title"], d["date"]) for d in response["moved"]] == [("Soppa", iso(5))]
    assert memory.on(iso(1)) is None


async def test_planning_again_adds_the_new_rows(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)
    await _plan(hass, iso(1))
    the_list.rows = [r for r in the_list.rows if r["text"] != "400 g pasta"]
    the_list.rows.append({"id": "x", "text": "Pasta", "isStriked": False})

    await _plan(hass, iso(1))
    assert memory.on(iso(1))["on_list"] == ["4.5 dl vispgrädde", "400 g pasta", "1 gul lök", "Pasta"]


TUNNBROD = _recipe(
    "3", "Tunnbrödsrulle",
    _ing("varmkorvar", "8 varmkorvar", 8, None),
    _ing("bitar mjukt tunnbröd", "4 bitar mjukt tunnbröd", 4, "bitar"),
    _ing("mjölk (gärna 1,5 %)", "2 dl mjölk (gärna 1,5 %)", 2, "dl"),
)


def _with_tunnbrod(api):
    api.async_get_recipe = AsyncMock(
        side_effect=lambda rid: {"1": PASTA, "2": SOPPA, "3": TUNNBROD}[rid]
    )
    return api


async def _plan_ids(hass, ids, start):
    with patch("custom_components.ica_shopping._pick_calendar", return_value=None):
        return await _call(hass, "plan_dinners", recipes=ids, start_date=start)


async def test_plan_uses_rows_worded_otherwise(hass, setup):
    the_list = FakeList("Varmkorv", "Mjukt tunnbröd", "Mjölig potatis")
    the_list.rows.append({"id": "s", "text": "Mjölk", "isStriked": True})
    memory = await setup(the_list)
    _with_tunnbrod(hass.data[DOMAIN][DATA_ICA])

    response = await _plan_ids(hass, "3", iso(1))

    # No "8 varmkorvar" next to "Varmkorv"; the checked-off milk is picked up again
    assert the_list.texts() == ["Varmkorv", "Mjukt tunnbröd", "Mjölig potatis", "Mjölk"]
    assert response["dinners"][0]["on_list"] == ["Varmkorv", "Mjukt tunnbröd", "Mjölk"]
    assert memory.on(iso(1))["put_on_list"] == ["Mjölk"]


async def test_list_dinners_finds_rows_typed_by_hand(hass, setup):
    await setup(FakeList())
    _with_tunnbrod(hass.data[DOMAIN][DATA_ICA])
    with patch("custom_components.ica_shopping._pick_calendar", return_value=None):
        await _call(hass, "plan_dinners", recipes="3", start_date=iso(1), add_to_list=False)

    async def get_items(call):
        return {"todo.shopping_list": {"items": [
            {"summary": "Varmkorv", "status": "needs_action"},
            {"summary": "Kaffe", "status": "needs_action"},
            {"summary": "Mjölk", "status": "completed"},
        ]}}

    hass.services.async_register("todo", "get_items", get_items, supports_response=SupportsResponse.ONLY)
    hass.data[DOMAIN]["todo_entity"] = "todo.shopping_list"

    listed = await _call(hass, "get_list_dinners")
    assert listed["dinners"] == [
        {"id": "3", "title": "Tunnbrödsrulle", "date": iso(1), "on_list": ["Varmkorv", "Mjölk"]}
    ]


async def test_remove_dinner_keeps_a_row_another_dinner_needs_by_its_words(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)
    _with_tunnbrod(hass.data[DOMAIN][DATA_ICA])
    # Tunnbrödsrulle planned before rows were noted; its milk is the soup's row now
    await _plan_ids(hass, "3", iso(3))
    tunnbrod = memory.on(iso(3))
    del tunnbrod["on_list"], tunnbrod["put_on_list"]
    the_list.rows = [{"id": "m", "text": "Mjölk", "isStriked": False}]
    await _plan_ids(hass, "2", iso(1))
    entry = memory.on(iso(1))
    entry["put_on_list"].append("Mjölk")
    entry["on_list"].append("Mjölk")

    response = await _call(hass, "remove_dinner", date=iso(1))
    assert "Mjölk" not in response["list_removed"]
    assert "Mjölk" in the_list.texts()
    assert tunnbrod["put_on_list"] == ["Mjölk"]


async def test_our_version_brings_the_list_along(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)
    await _plan_ids(hass, "1", iso(1))
    assert the_list.texts() == ["3 dl vispgrädde", "400 g pasta", "1 Lök"]

    response = await _call(
        hass, "set_recipe_variant", recipe="1", omit=["400 g pasta"], add=["2 morötter"], update_list=True
    )
    assert response["list_removed"] == ["400 g pasta"]
    assert response["list_added"] == ["2 morötter"]
    assert the_list.texts() == ["3 dl vispgrädde", "1 Lök", "2 morötter"]
    entry = memory.on(iso(1))
    assert entry["on_list"] == ["3 dl vispgrädde", "1 Lök", "2 morötter"]
    assert entry["put_on_list"] == ["3 dl vispgrädde", "1 Lök", "2 morötter"]

    # Back to ICA's recipe: the pasta returns, the carrots go
    response = await _call(hass, "remove_recipe_variant", recipe="1", update_list=True)
    assert response["list_added"] == ["400 g pasta"] and response["list_removed"] == ["2 morötter"]
    assert sorted(the_list.texts()) == ["1 Lök", "3 dl vispgrädde", "400 g pasta"]


async def test_our_version_keeps_rows_others_need_and_leaves_lists_alone_unasked(hass, setup):
    the_list = FakeList()
    await setup(the_list)
    await _plan(hass, iso(1))  # Pasta and Soppa share the cream
    rows = the_list.texts()

    await _call(hass, "set_recipe_variant", recipe="1", omit=["3 dl vispgrädde"])
    assert the_list.texts() == rows

    response = await _call(hass, "set_recipe_variant", recipe="1", omit=["3 dl vispgrädde", "1 gul lök"], update_list=True)
    # The summed cream is Soppa's too; the onion only Pasta's
    assert response["list_removed"] == ["1 gul lök"]
    assert "4.5 dl vispgrädde" in the_list.texts()


async def test_a_dinner_planned_without_the_list_stays_off_it(hass, setup):
    the_list = FakeList()
    await setup(the_list)
    with patch("custom_components.ica_shopping._pick_calendar", return_value=None):
        await _call(hass, "plan_dinners", recipes="1", start_date=iso(1), add_to_list=False)

    response = await _call(hass, "set_recipe_variant", recipe="1", add=["2 morötter"], update_list=True)
    assert response["list_added"] == [] and the_list.texts() == []


async def test_changing_an_own_recipe_brings_the_list_along(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)
    hass.data[DOMAIN][DATA_ICA].async_search_recipes = AsyncMock(return_value=[])
    soup = await _call(hass, "save_recipe", title="Soppa", ingredients="2 förp soppa\n1 baguette")
    with patch("custom_components.ica_shopping._pick_calendar", return_value=None):
        await _call(hass, "plan_dinners", recipes=soup["id"], start_date=iso(1))

    response = await _call(
        hass, "save_recipe", recipe=soup["id"], ingredients="2 förp soppa\n1 dl riven ost", update_list=True
    )
    assert response["list_removed"] == ["1 baguette"] and response["list_added"] == ["1 dl riven ost"]
    assert the_list.texts() == ["2 förp soppa", "1 dl riven ost"]
    assert memory.on(iso(1))["ingredients"] == ["2 förp soppa", "1 dl riven ost"]


async def test_an_ica_dinner_becomes_an_own_recipe_in_place(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)
    hass.data[DOMAIN][DATA_ICA].async_search_recipes = AsyncMock(return_value=[])
    await _plan_ids(hass, "1", iso(1))
    rows = list(the_list.texts())

    copy = await _call(
        hass, "save_recipe", title="Pasta", dinner=iso(1), update_list=True,
        ingredients=["3 dl vispgrädde", "400 g pasta", "1 gul lök"], steps="Koka pastan.",
    )
    entry = memory.on(iso(1))
    assert entry["id"] == copy["id"] and entry["title"] == "Pasta"
    # The same lines: the list and the dinner's rows stay as they were
    assert copy["list_added"] == [] and copy["list_removed"] == []
    assert the_list.texts() == rows
    assert entry["put_on_list"] == rows

    with pytest.raises(HomeAssistantError):
        await _call(hass, "save_recipe", title="Ingen", dinner=iso(9))
