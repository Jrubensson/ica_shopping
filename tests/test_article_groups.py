"""Rows the integration adds get ICA's aisle category, looked up by the bare grocery name."""
from unittest.mock import AsyncMock, patch

from custom_components.ica_shopping.ica_api import ICAApi, IcaConnectionError
from custom_components.ica_shopping.recipes import async_add_to_list

ARTICLES = {"articles": [
    {"id": 11795, "name": "vispgrädde", "pluralName": "", "parentId": 10, "parentIdExtended": 10},
    {"id": 55192, "name": "gul lök", "pluralName": "gula lökar", "parentId": 4, "parentIdExtended": 4},
    {"id": 10000, "name": "5-minuterssill", "parentId": 6, "parentIdExtended": 15},
    {"id": 1, "name": "utan grupp"},
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
        _, added, _, on_list = await async_add_to_list(api, planned, None, "LIST")
    assert [(r["productName"], r["articleGroupId"]) for r in _sent(api)] == [
        ("4.5 dl vispgrädde", 10), ("2 gula lökar", 4),
    ]
    assert added == ["4.5 dl vispgrädde", "2 gula lökar"]
    assert on_list == [("4.5 dl vispgrädde", True), ("2 gula lökar", True)]
