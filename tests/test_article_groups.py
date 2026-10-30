"""Rows the integration adds get ICA's aisle category, looked up by the bare grocery name."""
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import SupportsResponse

from custom_components.ica_shopping import _perform_sync
from custom_components.ica_shopping.const import DOMAIN
from custom_components.ica_shopping.ica_api import ICAApi, IcaConnectionError
from custom_components.ica_shopping.matching import grocery_names
from custom_components.ica_shopping.recipes import async_add_to_list

ARTICLES = {"articles": [
    {"id": 11795, "name": "vispgrädde", "pluralName": "", "parentId": 10, "parentIdExtended": 10},
    {"id": 55192, "name": "gul lök", "pluralName": "gula lökar", "parentId": 4, "parentIdExtended": 4},
    {"id": 10000, "name": "5-minuterssill", "parentId": 6, "parentIdExtended": 15},
    {"id": 1, "name": "utan grupp"},
    {"id": 2, "name": "potatis", "pluralName": "potatisar", "parentId": 4, "parentIdExtended": 4},
    {"id": 3, "name": "ost", "pluralName": "", "parentId": 10, "parentIdExtended": 12},
    {"id": 4, "name": "sej", "pluralName": "", "parentId": 6, "parentIdExtended": 16},
]}


def _api(hass, articles=ARTICLES):
    api = ICAApi(hass, "", "")
    api._request = AsyncMock(return_value=articles)
    api._sync = AsyncMock(return_value={})
    return api


def _sent(api):
    return [c.args[1]["createdRows"][0] for c in api._sync.await_args_list]


async def test_row_gets_the_category_of_its_name(hass):
    api = _api(hass)
    assert await api.add_to_list("LIST", "2 dl vispgrädde", name="Vispgrädde")
    row = _sent(api)[0]
    assert row["productName"] == "2 dl vispgrädde"
    assert (row["articleGroupId"], row["articleGroupIdExtended"]) == (10, 10)


async def test_plural_and_extended_group(hass):
    api = _api(hass)
    await api.add_to_list("LIST", "2 gula lökar", name="gula lökar")
    await api.add_to_list("LIST", "1 burk sill", name="5-minuterssill")
    assert [(r["articleGroupId"], r["articleGroupIdExtended"]) for r in _sent(api)] == [(4, 4), (6, 15)]


async def test_unknown_name_or_no_name_sends_no_category(hass):
    api = _api(hass)
    await api.add_to_list("LIST", "2 msk okänt", name="okänt")
    await api.add_to_list("LIST", "Mjölk")
    await api.add_to_list("LIST", "x", name="utan grupp")
    assert all("articleGroupId" not in r for r in _sent(api))


async def test_table_is_read_once(hass):
    api = _api(hass)
    for _ in range(3):
        await api.add_to_list("LIST", "vispgrädde", name="vispgrädde")
    api._request.assert_awaited_once()


async def test_failed_lookup_still_adds_the_row(hass):
    api = _api(hass)
    api._request = AsyncMock(side_effect=IcaConnectionError("down"))
    assert await api.add_to_list("LIST", "2 dl vispgrädde", name="vispgrädde")
    assert "articleGroupId" not in _sent(api)[0]


async def test_unexpected_reply_still_adds_the_row(hass):
    api = _api(hass, articles=["not", "a", "dict"])
    assert await api.add_to_list("LIST", "2 dl vispgrädde", name="vispgrädde")
    assert "articleGroupId" not in _sent(api)[0]


async def test_planned_rows_are_categorised_and_tracked_by_their_text(hass):
    api = _api(hass)
    planned = [
        {"name": "vispgrädde", "text": "4.5 dl vispgrädde"},
        {"name": "gula lökar", "text": "2 gula lökar"},
    ]
    with patch(
        "custom_components.ica_shopping.recipes.async_resolve_or_create_list",
        AsyncMock(return_value={"id": "LIST", "name": "Handla", "rows": []}),
    ), patch.object(api, "async_get_list", AsyncMock(return_value=None)):
        _, added, _, on_list, _ = await async_add_to_list(api, planned, None, "LIST")
    assert [(r["productName"], r["articleGroupId"]) for r in _sent(api)] == [
        ("4.5 dl vispgrädde", 10), ("2 gula lökar", 4),
    ]
    assert added == ["4.5 dl vispgrädde", "2 gula lökar"]
    assert on_list == [("4.5 dl vispgrädde", True), ("2 gula lökar", True)]


def test_grocery_names_leave_the_amount_out():
    assert grocery_names("Potatis 7 st") == ["potatis 7 st", "potatis"]
    assert grocery_names("Potatis 7st")[1:] == ["potatis"]
    assert grocery_names("Riven ost 1,5 dl")[1:] == ["riven ost", "ost"]
    assert grocery_names("djupfryst sej eller torsk 400 g")[1:] == ["djupfryst sej", "sej"]
    assert grocery_names("2 morötter, rivna")[1:] == ["morötter"]
    assert grocery_names("Yoghurt med vanilj")[1:] == ["yoghurt"]
    assert grocery_names("4 port ris (gärna fullkorn) eller kokt potatis")[1:] == ["ris"]


async def test_a_row_with_an_amount_gets_the_category_of_its_grocery(hass):
    api = _api(hass)
    for text in ("Potatis 7 st", "7st potatisar", "Riven ost 1,5 dl", "Djupfryst sej eller torsk 400 g"):
        await api.add_to_list("LIST", text, name=text)
    assert [(r["productName"], r["articleGroupId"], r["articleGroupIdExtended"]) for r in _sent(api)] == [
        ("Potatis 7 st", 4, 4),
        ("7st potatisar", 4, 4),
        ("Riven ost 1,5 dl", 10, 12),
        ("Djupfryst sej eller torsk 400 g", 6, 16),
    ]


async def test_rows_from_the_todo_list_are_categorised_by_their_text(hass):
    """The voice assistant and the dashboard add to the to-do list; the sync carries the rows to ICA."""
    hass.data[DOMAIN] = {}

    async def get_items(call):
        return {"todo.handla": {"items": [
            {"uid": "1", "summary": "Potatis 7 st", "status": "needs_action"},
            {"uid": "2", "summary": "Mjölk", "status": "completed"},
        ]}}

    hass.services.async_register("todo", "get_items", get_items, supports_response=SupportsResponse.ONLY)
    api = MagicMock()
    api.get_list_by_id = AsyncMock(return_value={"id": "LIST", "rows": []})
    api.add_to_list = AsyncMock(return_value=True)
    entry = MagicMock(options={"todo_entity_id": "todo.handla", "ica_list_id": "LIST"}, data={})

    await _perform_sync(hass, api, entry)

    assert sorted((c.args[1], c.kwargs["striked"], c.kwargs["name"]) for c in api.add_to_list.await_args_list) == [
        ("Mjölk", True, "Mjölk"),
        ("Potatis 7 st", False, "Potatis 7 st"),
    ]
