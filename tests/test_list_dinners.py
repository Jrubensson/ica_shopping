import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
        self.rows.append({"id": f"r{len(self.rows)}", "text": text, "isStriked": striked})
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
