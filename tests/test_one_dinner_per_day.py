import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.calendar import EVENT_END, EVENT_START
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.ica_shopping import async_setup
from custom_components.ica_shopping.calendar import DinnerCalendar
from custom_components.ica_shopping.const import DATA_ICA, DOMAIN
from custom_components.ica_shopping.memory import DayTakenError, DinnerMemory


def day(offset: int) -> dt.date:
    # Relative to today, since the memory trims dinners older than 120 days.
    return dt_util.now().date() + dt.timedelta(days=offset)


def iso(offset: int) -> str:
    return day(offset).isoformat()


async def _memory(hass, planned: list[tuple[str, int]]) -> DinnerMemory:
    memory = DinnerMemory(hass)
    await memory.async_record(
        [{"id": rid, "title": f"Dish {rid}", "date": iso(offset)} for rid, offset in planned]
    )
    return memory


async def _setup_services(hass, memory: DinnerMemory) -> None:
    hass.data[DOMAIN] = {
        "memory": memory,
        "sync": AsyncMock(),
        "current_list_id": "list",
        "current_options": {},
        DATA_ICA: MagicMock(),
    }
    await async_setup(hass, {})


def _plan(*recipe_ids: str):
    result = {
        "dinners": [{"id": rid, "title": f"Dish {rid}"} for rid in recipe_ids],
        "added": [],
        "list": "Handla",
    }
    return patch(
        "custom_components.ica_shopping.async_plan_dinners",
        AsyncMock(return_value=(None, [{} for _ in recipe_ids], result)),
    )


async def _commit(hass, recipes: str, start: int) -> dict:
    return await hass.services.async_call(
        DOMAIN,
        "plan_dinners",
        {"count": len(recipes.split(",")), "recipes": recipes, "start_date": iso(start)},
        blocking=True,
        return_response=True,
    )


async def test_free_days_skips_taken_days(hass):
    memory = await _memory(hass, [("1", 2), ("2", 4)])
    assert memory.free_days(day(1), 4) == [day(1), day(3), day(5), day(6)]


async def test_free_days_with_empty_memory_is_consecutive(hass):
    memory = DinnerMemory(hass)
    assert memory.free_days(day(1), 3) == [day(1), day(2), day(3)]


async def test_record_skips_a_day_that_already_has_a_dinner(hass):
    memory = await _memory(hass, [("1", 1)])
    assert await memory.async_record([{"id": "2", "date": iso(1)}]) == 0
    assert memory.find("2", iso(1)) is None


async def test_update_entry_rejects_a_taken_day(hass):
    memory = await _memory(hass, [("1", 1), ("2", 2)])
    with pytest.raises(DayTakenError):
        await memory.async_update_entry("1", iso(1), new_date=iso(2))
    assert memory.find("1", iso(1)) is not None


async def test_plan_dinners_shifts_past_planned_days(hass):
    memory = await _memory(hass, [("old", 1), ("old2", 2)])
    await _setup_services(hass, memory)

    with _plan("a", "b"):
        response = await _commit(hass, "a,b", 1)

    assert [d["date"] for d in response["dinners"]] == [iso(3), iso(4)]
    dates = [e["date"] for e in memory.history]
    assert len(dates) == len(set(dates))


async def test_repeated_commit_keeps_the_planned_days(hass):
    memory = DinnerMemory(hass)
    await _setup_services(hass, memory)

    with _plan("a", "b"):
        await _commit(hass, "a,b", 1)
        response = await _commit(hass, "a,b", 1)

    assert [d["date"] for d in response["dinners"]] == [iso(1), iso(2)]
    assert len(memory.history) == 2


async def test_external_calendar_gets_the_shifted_days(hass):
    memory = await _memory(hass, [("old", 1)])
    await _setup_services(hass, memory)
    calls = async_mock_service(hass, "calendar", "create_event")

    with _plan("a", "b"), patch(
        "custom_components.ica_shopping._pick_calendar", return_value="calendar.family"
    ):
        await _commit(hass, "a,b", 1)
        await _commit(hass, "a,b", 1)

    assert [c.data["start_date"] for c in calls] == [day(2), day(3)]


@pytest.fixture
def entry():
    entry = MagicMock()
    entry.entry_id = "entry"
    return entry


async def test_calendar_move_onto_taken_day_is_rejected(hass, entry):
    memory = await _memory(hass, [("1", 1), ("2", 2)])
    calendar = DinnerCalendar(memory, entry, "list")

    with pytest.raises(HomeAssistantError) as err:
        await calendar.async_update_event(
            f"1@{iso(1)}", {EVENT_START: day(2), EVENT_END: day(3)}
        )

    assert err.value.translation_key == "dinner_day_taken"
    assert memory.find("1", iso(1)) is not None
    assert memory.find("2", iso(2)) is not None


async def test_calendar_move_onto_free_day_works(hass, entry):
    memory = await _memory(hass, [("1", 1), ("2", 2)])
    calendar = DinnerCalendar(memory, entry, "list")

    await calendar.async_update_event(f"1@{iso(1)}", {EVENT_START: day(3), EVENT_END: day(4)})

    assert memory.find("1", iso(3)) is not None
    assert memory.find("1", iso(1)) is None


async def test_calendar_rename_on_same_day_is_allowed(hass, entry):
    memory = await _memory(hass, [("1", 1)])
    calendar = DinnerCalendar(memory, entry, "list")

    await calendar.async_update_event(
        f"1@{iso(1)}", {EVENT_START: day(1), EVENT_END: day(2), "summary": "Fredagstacos"}
    )

    assert memory.find("1", iso(1))["title"] == "Fredagstacos"
