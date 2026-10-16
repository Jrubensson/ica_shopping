import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.calendar import EVENT_END, EVENT_START
from homeassistant.exceptions import HomeAssistantError

from custom_components.ica_shopping import async_setup
from custom_components.ica_shopping.calendar import DinnerCalendar
from custom_components.ica_shopping.const import DATA_ICA, DOMAIN
from custom_components.ica_shopping.memory import DinnerMemory

D = dt.date


async def _memory(hass, planned: list[tuple[str, str]]) -> DinnerMemory:
    memory = DinnerMemory(hass)
    await memory.async_record([{"id": rid, "title": f"Dish {rid}", "date": day} for rid, day in planned])
    return memory


async def test_free_days_skips_taken_days(hass):
    memory = await _memory(hass, [("1", "2026-10-18"), ("2", "2026-10-20")])
    assert memory.free_days(D(2026, 10, 17), 4) == [
        D(2026, 10, 17),
        D(2026, 10, 19),
        D(2026, 10, 21),
        D(2026, 10, 22),
    ]


async def test_free_days_with_empty_memory_is_consecutive(hass):
    memory = DinnerMemory(hass)
    assert memory.free_days(D(2026, 10, 17), 3) == [D(2026, 10, 17), D(2026, 10, 18), D(2026, 10, 19)]


async def test_plan_dinners_shifts_past_planned_days(hass):
    memory = await _memory(hass, [("old", "2026-10-17"), ("old2", "2026-10-18")])
    hass.data[DOMAIN] = {
        "memory": memory,
        "sync": AsyncMock(),
        "current_list_id": "list",
        "current_options": {},
        DATA_ICA: MagicMock(),
    }
    await async_setup(hass, {})
    result = {
        "dinners": [{"id": "a", "title": "Tacos"}, {"id": "b", "title": "Curry"}],
        "added": [],
        "list": "Handla",
    }
    with patch(
        "custom_components.ica_shopping.async_plan_dinners",
        AsyncMock(return_value=(None, [{}, {}], result)),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            "plan_dinners",
            {"count": 2, "recipes": "a,b", "start_date": "2026-10-17"},
            blocking=True,
            return_response=True,
        )

    assert [d["date"] for d in response["dinners"]] == ["2026-10-19", "2026-10-20"]
    dates = [e["date"] for e in memory.history]
    assert len(dates) == len(set(dates))


@pytest.fixture
def entry():
    entry = MagicMock()
    entry.entry_id = "entry"
    return entry


async def test_calendar_move_onto_taken_day_is_rejected(hass, entry):
    memory = await _memory(hass, [("1", "2026-10-17"), ("2", "2026-10-18")])
    calendar = DinnerCalendar(memory, entry, "list")

    with pytest.raises(HomeAssistantError) as err:
        await calendar.async_update_event(
            "1@2026-10-17", {EVENT_START: D(2026, 10, 18), EVENT_END: D(2026, 10, 19)}
        )

    assert err.value.translation_key == "dinner_day_taken"
    assert memory.find("1", "2026-10-17") is not None
    assert memory.find("2", "2026-10-18") is not None


async def test_calendar_move_onto_free_day_works(hass, entry):
    memory = await _memory(hass, [("1", "2026-10-17"), ("2", "2026-10-18")])
    calendar = DinnerCalendar(memory, entry, "list")

    await calendar.async_update_event(
        "1@2026-10-17", {EVENT_START: D(2026, 10, 19), EVENT_END: D(2026, 10, 20)}
    )

    assert memory.find("1", "2026-10-19") is not None
    assert memory.find("1", "2026-10-17") is None


async def test_calendar_rename_on_same_day_is_allowed(hass, entry):
    memory = await _memory(hass, [("1", "2026-10-17")])
    calendar = DinnerCalendar(memory, entry, "list")

    await calendar.async_update_event(
        "1@2026-10-17",
        {EVENT_START: D(2026, 10, 17), EVENT_END: D(2026, 10, 18), "summary": "Fredagstacos"},
    )

    assert memory.find("1", "2026-10-17")["title"] == "Fredagstacos"
