import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError

from custom_components.ica_shopping import async_setup
from custom_components.ica_shopping.const import API_BASE, DATA_ICA, DOMAIN, RECIPE_PATH
from custom_components.ica_shopping.ica_api import ICAApi, IcaApiError

FAVORITES_URL = f"{API_BASE}/{RECIPE_PATH}/favorites"


def _logged_in(hass) -> ICAApi:
    api = ICAApi(hass, "199001011234", "pw")
    api._state = {"token": {"access_token": "tok", "expires_at": time.time() + 3600}}
    return api


async def test_favorites_are_saved_and_removed_by_id(hass, aioclient_mock):
    # Checked against ICA: both answer 200 with an empty body
    aioclient_mock.put(f"{FAVORITES_URL}/719659", text="")
    aioclient_mock.delete(f"{FAVORITES_URL}/719659", text="")
    api = _logged_in(hass)

    await api.async_add_favorite_recipe("719659")
    await api.async_remove_favorite_recipe("719659")

    assert [(c[0], str(c[1])) for c in aioclient_mock.mock_calls] == [
        ("PUT", f"{FAVORITES_URL}/719659"),
        ("DELETE", f"{FAVORITES_URL}/719659"),
    ]
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer tok"


async def test_a_rejected_favorite_is_an_api_error(hass, aioclient_mock):
    aioclient_mock.put(f"{FAVORITES_URL}/1", status=404, text="not found")
    with pytest.raises(IcaApiError, match="404"):
        await _logged_in(hass).async_add_favorite_recipe("1")


async def _setup(hass, favorites=(719659, 722416)):
    api = MagicMock()
    api.async_get_favorite_recipe_ids = AsyncMock(return_value=list(favorites))
    api.async_add_favorite_recipe = AsyncMock()
    api.async_remove_favorite_recipe = AsyncMock()
    hass.data[DOMAIN] = {"memory": MagicMock(), "current_list_id": "list", DATA_ICA: api}
    await async_setup(hass, {})
    return api


async def _call(hass, service, data=None):
    return await hass.services.async_call(DOMAIN, service, data or {}, blocking=True, return_response=True)


async def test_get_favorites_returns_the_ids_as_strings(hass):
    await _setup(hass, favorites=(719659, 722416, 719659))
    assert await _call(hass, "get_favorites") == {"recipes": ["719659", "722416"]}


async def test_add_and_remove_return_the_new_list(hass):
    api = await _setup(hass)

    assert await _call(hass, "add_favorite", {"recipe": 713522}) == {"recipes": ["719659", "722416"]}
    api.async_add_favorite_recipe.assert_awaited_once_with("713522")

    await _call(hass, "remove_favorite", {"recipe": "719659"})
    api.async_remove_favorite_recipe.assert_awaited_once_with("719659")


async def test_own_dinners_cannot_be_favorites(hass):
    api = await _setup(hass)
    with pytest.raises(vol.Invalid):
        await _call(hass, "add_favorite", {"recipe": "custom-tacos-1"})
    api.async_add_favorite_recipe.assert_not_awaited()


async def test_ica_errors_are_reported(hass):
    api = await _setup(hass)
    api.async_add_favorite_recipe.side_effect = IcaApiError("HTTP 500")
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "add_favorite", {"recipe": "1"})
    assert err.value.translation_key == "favorites_failed"


async def test_favorites_need_a_login(hass):
    hass.data[DOMAIN] = {"memory": MagicMock(), "current_list_id": "list"}
    await async_setup(hass, {})
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "get_favorites")
    assert err.value.translation_key == "not_logged_in"
