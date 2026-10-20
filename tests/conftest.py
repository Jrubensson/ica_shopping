from unittest.mock import AsyncMock

import pytest

from custom_components.ica_shopping import async_setup
from custom_components.ica_shopping.const import DATA_ICA, DOMAIN
from custom_components.ica_shopping.memory import DinnerMemory


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


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
