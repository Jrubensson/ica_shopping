from unittest.mock import MagicMock

from homeassistant.core import SupportsResponse
from homeassistant.helpers.llm import ToolInput

from custom_components.ica_shopping.const import DOMAIN
from custom_components.ica_shopping.llm import PlanDinnersTool


async def _forwarded(hass, args: dict) -> dict:
    calls = []

    async def plan_dinners(call):
        calls.append(dict(call.data))
        return {}

    hass.services.async_register(
        DOMAIN, "plan_dinners", plan_dinners, supports_response=SupportsResponse.ONLY
    )
    tool = PlanDinnersTool()
    await tool.async_call(
        hass, ToolInput(tool_name=tool.name, tool_args=tool.parameters(args)), MagicMock()
    )
    return calls[0]


async def test_add_to_list_is_forwarded(hass):
    data = await _forwarded(hass, {"preview": False, "recipes": "1,2", "add_to_list": False})
    assert data["add_to_list"] is False


async def test_add_to_list_is_left_to_the_service_default(hass):
    data = await _forwarded(hass, {"preview": False, "recipes": "1,2"})
    assert "add_to_list" not in data


async def test_match_products_is_forwarded(hass):
    data = await _forwarded(hass, {"preview": True, "recipes": "1,2", "match_products": True})
    assert data["match_products"] is True
