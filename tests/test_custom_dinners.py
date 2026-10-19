import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.calendar import EVENT_DESCRIPTION, EVENT_END, EVENT_START
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.ica_shopping import async_setup
from custom_components.ica_shopping.calendar import DinnerCalendar, _to_event
from custom_components.ica_shopping.const import DATA_ICA, DOMAIN
from custom_components.ica_shopping.memory import DayTakenError, DinnerMemory
from custom_components.ica_shopping.recipes import async_fetch_recipes, async_plan_dinners


def day(offset: int) -> dt.date:
    return dt_util.now().date() + dt.timedelta(days=offset)


def iso(offset: int) -> str:
    return day(offset).isoformat()


@pytest.fixture
def entry():
    entry = MagicMock()
    entry.entry_id = "entry"
    return entry


async def _setup(hass, memory, api=None):
    hass.data[DOMAIN] = {
        "memory": memory,
        "sync": AsyncMock(),
        "current_list_id": "list",
        "current_options": {},
        DATA_ICA: api or MagicMock(),
    }
    await async_setup(hass, {})


async def _add_dinner(hass, **data):
    return await hass.services.async_call(
        DOMAIN, "add_dinner", data, blocking=True, return_response=True
    )


async def test_custom_dinner_gets_a_custom_id(hass):
    memory = DinnerMemory(hass)
    added = await memory.async_add_custom("Tacos", iso(1), note="Guacamole")
    assert added["id"].startswith("custom-") and "@" not in added["id"]
    assert memory.as_dict()["history"][0]["custom"] is True


async def test_custom_dinner_refuses_a_taken_day(hass):
    memory = DinnerMemory(hass)
    await memory.async_record([{"id": "1", "title": "Pasta", "date": iso(1)}])
    with pytest.raises(DayTakenError):
        await memory.async_add_custom("Tacos", iso(1))
    assert len(memory.history) == 1


async def test_custom_dinners_are_not_recent_recipes(hass):
    memory = DinnerMemory(hass)
    await memory.async_record([{"id": "1", "date": iso(-1)}])
    await memory.async_add_custom("Tacos", iso(-2))
    assert memory.recent_ids(14) == {"1"}


async def test_forget_dinner_by_custom_id(hass):
    memory = DinnerMemory(hass)
    added = await memory.async_add_custom("Tacos", iso(1))
    assert await memory.async_forget_dinner(added["id"]) == 1
    assert memory.history == []


async def test_calendar_shows_note_and_url(hass):
    memory = DinnerMemory(hass)
    added = await memory.async_add_custom("Tacos", iso(1), note="Guacamole", url="https://x.se/t")
    event = _to_event(added)
    assert event.uid == f"{added['id']}@{iso(1)}"
    assert event.description == "Guacamole\nhttps://x.se/t"


async def test_calendar_moves_and_deletes_custom_dinners(hass, entry):
    memory = DinnerMemory(hass)
    added = await memory.async_add_custom("Tacos", iso(1), note="Guacamole")
    calendar = DinnerCalendar(memory, entry, "list")

    # The edit dialog sends the unchanged description back, which must still pass the check.
    await calendar.async_update_event(
        f"{added['id']}@{iso(1)}",
        {EVENT_START: day(3), EVENT_END: day(4), EVENT_DESCRIPTION: "Guacamole"},
    )
    assert memory.find(added["id"], iso(3)) is not None

    await calendar.async_delete_event(f"{added['id']}@{iso(3)}")
    assert memory.history == []


async def test_add_dinner_service_records_and_mirrors(hass):
    memory = DinnerMemory(hass)
    await _setup(hass, memory)
    calls = async_mock_service(hass, "calendar", "create_event")

    with patch(
        "custom_components.ica_shopping._pick_calendar", return_value="calendar.family"
    ):
        response = await _add_dinner(
            hass, title="Tacos", date=iso(2), note="Guacamole", url="https://x.se/t"
        )

    assert response["id"].startswith("custom-")
    assert response | {"id": None} == {"id": None, "title": "Tacos", "date": iso(2)}
    assert memory.find(response["id"], iso(2))["note"] == "Guacamole"
    assert len(calls) == 1
    assert calls[0].data["summary"] == "Tacos"
    assert calls[0].data["description"] == "Guacamole\nhttps://x.se/t"
    assert calls[0].data["start_date"] == day(2)
    assert calls[0].data["end_date"] == day(3)


async def test_add_dinner_service_refuses_a_taken_day(hass):
    memory = DinnerMemory(hass)
    await memory.async_record([{"id": "1", "title": "Pasta", "date": iso(2)}])
    await _setup(hass, memory)
    calls = async_mock_service(hass, "calendar", "create_event")

    with patch(
        "custom_components.ica_shopping._pick_calendar", return_value="calendar.family"
    ), pytest.raises(HomeAssistantError) as err:
        await _add_dinner(hass, title="Tacos", date=iso(2))

    assert err.value.translation_key == "dinner_day_taken"
    assert calls == []
    assert len(memory.history) == 1


async def test_plan_dinners_skips_custom_ids():
    api = MagicMock()
    api.async_get_recipe = AsyncMock(return_value={"id": 679675, "title": "Lasagne"})
    recipes = await async_fetch_recipes(api, 2, "random", ["custom-1a2b3c4d", "679675"])
    assert recipes == [{"id": 679675, "title": "Lasagne"}]
    api.async_get_recipe.assert_awaited_once_with("679675")


async def test_custom_ids_stay_out_of_ingredient_groups():
    recipe = {
        "id": 679675,
        "title": "Lasagne",
        "ingredientGroups": [{"ingredients": [{"ingredient": "lök", "text": "1 lök"}]}],
    }
    api = MagicMock()
    api.async_get_recipe = AsyncMock(return_value=recipe)
    _, _, result = await async_plan_dinners(
        api, count=2, source="random", list_ref=None, default_list_id="list",
        never_add=[], preview=True, recipe_ids=["custom-1a2b3c4d", "679675"],
    )
    assert [d["id"] for d in result["dinners"]] == [679675]
    assert [g["dinners"] for g in result["ingredient_groups"]] == [["Lasagne"]]


async def test_add_dinner_refuses_a_day_the_memory_would_drop(hass):
    memory = DinnerMemory(hass)
    await _setup(hass, memory)
    calls = async_mock_service(hass, "calendar", "create_event")

    with patch(
        "custom_components.ica_shopping._pick_calendar", return_value="calendar.family"
    ), pytest.raises(HomeAssistantError) as err:
        await _add_dinner(hass, title="Kålpudding", date=iso(-130))

    assert err.value.translation_key == "dinner_too_old"
    assert calls == [] and memory.history == []


async def test_add_dinner_rejects_an_empty_title(hass):
    memory = DinnerMemory(hass)
    await _setup(hass, memory)
    with pytest.raises(vol.Invalid):
        await _add_dinner(hass, title="  ", date=iso(1))
    assert memory.history == []


async def test_plan_dinners_with_only_custom_ids_plans_nothing():
    api = MagicMock()
    api.async_get_recipe = AsyncMock()
    target, recipes, result = await async_plan_dinners(
        api, count=1, source="random", list_ref=None, default_list_id="list",
        never_add=[], recipe_ids=["custom-1a2b3c4d"],
    )
    assert target is None and recipes == []
    assert result["dinners"] == [] and result["added"] == []
    api.async_get_recipe.assert_not_called()
