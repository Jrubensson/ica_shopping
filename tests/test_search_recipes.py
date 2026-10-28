import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.llm import ToolInput

from custom_components.ica_shopping import async_setup
from custom_components.ica_shopping.const import (
    DATA_ICA,
    DOMAIN,
    PUBLIC_TOKEN_URL,
    RECIPE_SEARCH_URL,
    USER_AGENT,
)
from custom_components.ica_shopping.ica_api import ICAApi, IcaApiError
from custom_components.ica_shopping.llm import AddDinnerTool, SearchRecipesTool

SEARCH_URL = f"{RECIPE_SEARCH_URL}/page/recipes"

HIT = {
    "id": 679675,
    "title": "Klassisk lasagne",
    "absoluteUrl": "https://www.ica.se/recept/klassisk-lasagne-679675/",
    "url": "/recept/klassisk-lasagne-679675/",
    "imageUrl": "imagevaultfiles/id_223426/cf_259/klassisk_lasagne.jpg",
    "cookingTime": "Över 60 minuter",
    "numberOfIngredients": 17,
}


def _token(value="tok"):
    return {"publicAccessToken": value, "tokenExpires": "2099-01-01T00:00:00.0000000Z"}


async def test_search_uses_the_public_token(hass, aioclient_mock):
    aioclient_mock.get(PUBLIC_TOKEN_URL, json=_token())
    aioclient_mock.get(SEARCH_URL, json=[HIT, {"title": "no id"}])
    api = ICAApi(hass, "199001011234", "pw")

    assert await api.async_search_recipes("lasagne", 5) == [HIT]
    assert await api.async_search_recipes("lasagne", 5) == [HIT]

    token_calls = [c for c in aioclient_mock.mock_calls if str(c[1]) == PUBLIC_TOKEN_URL]
    search_calls = [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(SEARCH_URL)]
    assert len(token_calls) == 1
    assert search_calls[0][1].query == {"url": "/", "query": "lasagne", "skip": "0", "take": "5"}
    assert search_calls[0][3]["Authorization"] == "Bearer tok"


async def test_token_request_does_not_pose_as_a_browser(hass, aioclient_mock):
    aioclient_mock.get(PUBLIC_TOKEN_URL, json=_token())
    aioclient_mock.get(SEARCH_URL, json=[HIT])
    api = ICAApi(hass, "199001011234", "pw")

    await api.async_search_recipes("lasagne")

    token_call = next(c for c in aioclient_mock.mock_calls if str(c[1]) == PUBLIC_TOKEN_URL)
    assert USER_AGENT not in (token_call[3] or {}).values()


async def test_search_refreshes_a_rejected_token(hass, aioclient_mock):
    aioclient_mock.get(PUBLIC_TOKEN_URL, json=_token("fresh"))
    aioclient_mock.get(SEARCH_URL, status=401)
    api = ICAApi(hass, "199001011234", "pw")
    api._public_token = ("stale", time.time() + 3600)

    with pytest.raises(IcaApiError):
        await api.async_search_recipes("lasagne")

    search_calls = [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(SEARCH_URL)]
    assert [c[3]["Authorization"] for c in search_calls] == ["Bearer stale", "Bearer fresh"]


async def test_search_rejects_a_token_page_that_is_not_json(hass, aioclient_mock):
    aioclient_mock.get(PUBLIC_TOKEN_URL, text="<html>", status=200)
    api = ICAApi(hass, "199001011234", "pw")

    with pytest.raises(IcaApiError, match="not JSON"):
        await api.async_search_recipes("lasagne")


async def _setup(hass, api):
    hass.data[DOMAIN] = {"memory": MagicMock(), "current_list_id": "list", DATA_ICA: api}
    await async_setup(hass, {})


async def test_search_service_response(hass):
    api = MagicMock()
    api.async_search_recipes = AsyncMock(return_value=[HIT])
    await _setup(hass, api)

    response = await hass.services.async_call(
        DOMAIN, "search_recipes", {"query": "lasagne"}, blocking=True, return_response=True
    )

    api.async_search_recipes.assert_awaited_once_with("lasagne", 10)
    assert response == {"recipes": [{
        "id": 679675,
        "title": "Klassisk lasagne",
        "cooking_time": "Över 60 minuter",
        "url": "https://www.ica.se/recept/klassisk-lasagne-679675/",
        "image": "https://assets.icanet.se/imagevaultfiles/id_223426/cf_259/klassisk_lasagne.jpg",
        "ingredient_count": 17,
    }]}


async def test_search_service_reports_ica_errors(hass):
    api = MagicMock()
    api.async_search_recipes = AsyncMock(side_effect=IcaApiError("HTTP 400"))
    await _setup(hass, api)

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, "search_recipes", {"query": "x"}, blocking=True, return_response=True
        )
    assert err.value.translation_key == "search_recipes_failed"


async def _forwarded(hass, tool, service, args):
    calls = []

    async def handler(call):
        calls.append(dict(call.data))
        return {}

    hass.services.async_register(DOMAIN, service, handler, supports_response=SupportsResponse.ONLY)
    await tool.async_call(
        hass, ToolInput(tool_name=tool.name, tool_args=tool.parameters(args)), MagicMock()
    )
    return calls[0]


async def test_llm_tools_forward_their_arguments(hass):
    assert await _forwarded(
        hass, SearchRecipesTool(), "search_recipes", {"query": "lasagne", "limit": 3}
    ) == {"query": "lasagne", "limit": 3}
    assert await _forwarded(
        hass, AddDinnerTool(), "add_dinner", {"title": "Tacos", "date": "2026-10-23"}
    ) == {"title": "Tacos", "date": "2026-10-23"}


async def test_public_token_rejection_is_not_a_login_error(hass, aioclient_mock):
    aioclient_mock.get(PUBLIC_TOKEN_URL, status=403)
    api = ICAApi(hass, "199001011234", "pw")
    with pytest.raises(IcaApiError):
        await api.async_search_recipes("lasagne")
