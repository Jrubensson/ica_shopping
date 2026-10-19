import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.ica_shopping import async_setup
from custom_components.ica_shopping.const import DATA_ICA, DOMAIN
from custom_components.ica_shopping.memory import DinnerMemory
from custom_components.ica_shopping.recipes import (
    aggregate_ingredients,
    async_plan_dinners,
    group_ingredients,
)


def _ing(name, text, quantity=None, unit=None):
    return {"ingredient": name, "text": text, "quantity": quantity, "unit": unit}


def _recipe(rid, title, *ings):
    return {"id": rid, "title": title, "ingredientGroups": [{"ingredients": list(ings)}]}


PASTA = _recipe(
    "1", "Pasta",
    _ing("vispgrädde", "3 dl vispgrädde", 3, "dl"),
    _ing("salt", "1 tsk salt", 1, "tsk"),
    _ing("Lök", "1 gul lök", 1, None),
)
SOPPA = _recipe(
    "2", "Soppa",
    _ing("Vispgrädde", "1,5 dl vispgrädde", 1.5, "dl"),
    _ing("Lök", "2 rödlökar", 2, "st"),
)
RECIPES = [PASTA, SOPPA]


def _api():
    api = MagicMock()
    api.async_get_recipe = AsyncMock(side_effect=lambda rid: {"1": PASTA, "2": SOPPA}[rid])
    api.async_get_list = AsyncMock()
    api.async_create_list = AsyncMock()
    api.add_to_list = AsyncMock()
    api.set_striked = AsyncMock()
    return api


def test_group_sums_same_unit_across_recipes():
    groups = {g["name"]: g for g in group_ingredients(RECIPES, ())}
    assert groups["vispgrädde"] == {
        "name": "vispgrädde",
        "text": "4.5 dl vispgrädde",
        "lines": ["3 dl vispgrädde", "1,5 dl vispgrädde"],
        "dinners": ["Pasta", "Soppa"],
    }


def test_group_joins_lines_when_units_differ():
    groups = {g["name"]: g for g in group_ingredients(RECIPES, ())}
    assert groups["Lök"]["text"] == "1 gul lök + 2 rödlökar"
    assert groups["Lök"]["lines"] == ["1 gul lök", "2 rödlökar"]


def test_group_skips_never_add_names():
    names = [g["name"] for g in group_ingredients(RECIPES, {"salt"})]
    assert names == ["vispgrädde", "Lök"]


def test_aggregate_is_unchanged():
    assert aggregate_ingredients(RECIPES) == [
        {"name": "vispgrädde", "text": "4.5 dl vispgrädde"},
        {"name": "salt", "text": "1 tsk salt"},
        {"name": "Lök", "text": "1 gul lök"},
        {"name": "Lök", "text": "2 rödlökar"},
    ]


async def test_add_to_list_false_leaves_list_untouched():
    api = _api()
    target, recipes, result = await async_plan_dinners(
        api, count=2, source="random", list_ref=None, default_list_id="list",
        never_add=["Salt"], add_to_list=False, recipe_ids=["1", "2"],
    )
    assert target is None and recipes == RECIPES
    assert result["list"] is None and result["added"] == [] and result["preview"] is False
    assert [g["name"] for g in result["ingredient_groups"]] == ["vispgrädde", "Lök"]
    api.async_get_list.assert_not_called()
    api.add_to_list.assert_not_called()


async def test_preview_response_carries_groups():
    _, _, result = await async_plan_dinners(
        _api(), count=2, source="random", list_ref=None, default_list_id="list",
        never_add=[], preview=True, recipe_ids=["1", "2"],
    )
    assert result["preview"] is True
    assert len(result["ingredient_groups"]) == 3


async def test_service_without_list_still_records_dinners(hass):
    api = _api()
    memory = DinnerMemory(hass)
    hass.data[DOMAIN] = {
        "memory": memory,
        "sync": AsyncMock(),
        "current_list_id": "list",
        "current_options": {},
        DATA_ICA: api,
    }
    await async_setup(hass, {})
    calls = async_mock_service(hass, "calendar", "create_event")
    start = dt_util.now().date() + dt.timedelta(days=1)

    with patch(
        "custom_components.ica_shopping._pick_calendar", return_value="calendar.family"
    ):
        response = await hass.services.async_call(
            DOMAIN,
            "plan_dinners",
            {"recipes": "1,2", "start_date": start.isoformat(), "add_to_list": False},
            blocking=True,
            return_response=True,
        )

    assert response["added"] == [] and response["ingredient_groups"]
    api.async_get_list.assert_not_called()
    api.add_to_list.assert_not_called()
    hass.data[DOMAIN]["sync"].assert_not_called()
    assert [e["id"] for e in memory.history] == ["1", "2"]
    assert [c.data["start_date"] for c in calls] == [start, start + dt.timedelta(days=1)]
