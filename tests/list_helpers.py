"""The ICA list and recipes the week-menu tests plan against."""
import datetime as dt
from unittest.mock import AsyncMock, MagicMock

from homeassistant.util import dt as dt_util

from custom_components.ica_shopping.const import DOMAIN


def _ing(name, text, quantity=None, unit=None):
    return {"ingredient": name, "text": text, "quantity": quantity, "unit": unit}


def _recipe(rid, title, *ings):
    return {"id": rid, "title": title, "ingredientGroups": [{"ingredients": list(ings)}]}


PASTA = _recipe(
    "1", "Pasta",
    _ing("vispgrädde", "3 dl vispgrädde", 3, "dl"),
    _ing("pasta", "400 g pasta", 400, "g"),
    _ing("Lök", "1 gul lök", 1, None),
)
SOPPA = _recipe(
    "2", "Soppa",
    _ing("Vispgrädde", "1,5 dl vispgrädde", 1.5, "dl"),
    _ing("Lök", "2 rödlökar", 2, "st"),
)


def iso(offset: int) -> str:
    return (dt_util.now().date() + dt.timedelta(days=offset)).isoformat()


class FakeList:
    """An ICA list whose rows the API mock reads and changes."""

    def __init__(self, *texts):
        self.rows = [{"id": f"r{i}", "text": t, "isStriked": False} for i, t in enumerate(texts)]

    def api(self):
        api = MagicMock()
        api.async_get_recipe = AsyncMock(side_effect=lambda rid: {"1": PASTA, "2": SOPPA}[rid])
        api.async_get_lists = AsyncMock(return_value=[{"id": "LIST", "name": "Handla"}])
        api.async_get_list = AsyncMock(
            side_effect=lambda _id: {"id": "LIST", "name": "Handla", "rows": [dict(r) for r in self.rows]}
        )
        api.add_to_list = AsyncMock(side_effect=self._add)
        api.remove_items = AsyncMock(side_effect=self._remove)
        api.set_striked = AsyncMock(return_value=True)
        return api

    async def _add(self, _list, text, striked=False, name=None):
        # Never reuse an id, or removing one row would take another along
        self.added = getattr(self, "added", 0) + 1
        self.rows.append({"id": f"n{self.added}", "text": text, "isStriked": striked})
        return True

    async def _remove(self, _list, ids):
        self.rows = [r for r in self.rows if r["id"] not in ids]
        return True

    def texts(self):
        return [r["text"] for r in self.rows]


async def _call(hass, service, **data):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=True)
