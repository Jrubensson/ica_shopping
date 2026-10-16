"""Assist/LLM tools for ICA Shopping.

Contributed through the ``llm`` integration's platform hook, so every user of the
integration gets the store's offers, the dinner memory and ``plan_dinners`` as tools in
Home Assistant Assist - without copying any scripts. The tools are thin wrappers around the
integration's own actions, so they behave exactly like calling the actions directly.

Home Assistant changed the tool schema library in 2026.10 (voluptuous -> probatio) and made
tools return ``ToolResult`` instead of a plain dict, so both are supported here.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.llm import (
    LLM_API_ASSIST,
    LLMContext,
    Tool,
    ToolInput,
)

from .const import DATA_ICA, DOMAIN

_LOGGER = logging.getLogger(__name__)

try:  # Home Assistant 2026.10 and newer
    import probatio

    _Schema = probatio.Schema
    _Optional = probatio.Optional
    _Required = probatio.Required
except ImportError:  # Home Assistant 2026.8 / 2026.9
    import voluptuous as vol

    probatio = None
    _Schema = vol.Schema
    _Optional = vol.Optional
    _Required = vol.Required

try:  # Home Assistant 2026.10 and newer
    from homeassistant.helpers.llm import ToolAnnotations
except ImportError:
    ToolAnnotations = None

try:  # Home Assistant 2026.10 and newer
    from homeassistant.helpers.llm import ToolResult
except ImportError:
    ToolResult = None


def _annotations(**kwargs: bool) -> Any:
    """Tool annotations where the running Home Assistant supports them."""
    return ToolAnnotations(**kwargs) if ToolAnnotations is not None else None


PROMPT = """ICA Shopping gives you the household's grocery list, the offers at the favourite ICA store, and a dinner memory.
- GetOffers: what is on offer this week (name, brand, deal) and the bonus balance. Treat it as a hint, not a shopping constraint - and remember many offers are not food (cleaning, flowers, drinks).
- GetMemory: the dinners eaten recently and the saved food preferences. Read it before suggesting meals.
- PlanDinners: returns real ICA recipes with their ingredients and the merged shopping list. Call it with preview=true to get suggestions; commit with preview=false and the recipe ids you agreed on. Recipes eaten within avoid_days are skipped automatically.
- SetPreference / RemovePreference: save or drop a preference such as "no fish".
- ForgetDinner / ClearMemory: correct the dinner memory.

When asked what to eat:
1. Read GetMemory and GetOffers first, and apply the saved preferences silently - pick dishes that respect them without listing the preferences or saying a dish is "utan lök", "utan räkor" or similar. Mention a preference only when the user asks about it, or when you could not avoid one.
2. Build the menu from the recipes PlanDinners returns. Present those dishes (shortening a title is fine); do not invent dishes of your own or rename them into something else.
3. Treat the offers as a bonus, not a requirement. A dish does not have to use an offer, and it is normal to serve it with whatever it is usually eaten with - kassler with rice or potatoes, pasta with a sauce - even when that side is not on offer. Mention an offer only when a recipe really uses it.
4. Never pad a dish or a plate with unrelated offers, and never stack several offers onto one meal. Plain, recognisable home cooking beats a clever combination of whatever happens to be cheap.
5. Never suggest something the memory says was eaten recently, and never add anything to the list until the user agrees.

When the user states a preference, save it with SetPreference."""


async def _call(hass: HomeAssistant, service: str, data: dict[str, Any]) -> dict[str, Any]:
    """Call one of the integration's own actions and return its response."""
    response = await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )
    return response or {}


class _ServiceTool(Tool):
    """A tool that forwards its arguments to one of the integration's actions."""

    integration = DOMAIN
    title = ""
    service: str = ""
    annotations = _annotations(
        read_only=True, destructive=False, idempotent=True, open_world=True
    )

    def _service_data(self, args: dict[str, Any]) -> dict[str, Any]:
        return dict(args)

    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> Any:
        data = await _call(hass, self.service, self._service_data(tool_input.tool_args))
        # 2026.10+ wants a ToolResult; older versions take the dict directly.
        return ToolResult(data=data) if ToolResult is not None else data


class GetOffersTool(_ServiceTool):
    name = "ica_shopping__GetOffers"
    title = "Get ICA offers"
    description = (
        "What is on offer this week at the favourite ICA store - name, brand and deal for each "
        "offer - plus the Stammis bonus balance. Use it as a hint for the menu; many offers are "
        "not food, and a dish does not have to use any of them."
    )
    service = "get_offers"


class GetMemoryTool(_ServiceTool):
    name = "ica_shopping__GetMemory"
    title = "Get dinner memory"
    description = (
        "The dinners the household has eaten recently and the saved food preferences. Read it "
        "before suggesting meals, and never suggest a dinner it lists as recently eaten."
    )
    service = "get_memory"


class PlanDinnersTool(_ServiceTool):
    name = "ica_shopping__PlanDinners"
    title = "Plan dinners"
    description = (
        "Returns real ICA recipes with their ingredients, and merges them into a shopping list. "
        "With preview=true it returns a menu without changing the list - use that to suggest and "
        "refine. Once the user agrees, call it again with preview=false and recipes set to the "
        "ids you agreed on, which adds exactly that menu. Present the recipes it returns as the "
        "suggestions; do not invent dishes. Dinners eaten within avoid_days are skipped."
    )
    service = "plan_dinners"
    annotations = _annotations(
        read_only=False, destructive=False, idempotent=False, open_world=True
    )
    parameters = _Schema(
        {
            _Optional(
                "count", description="How many dinner suggestions to fetch (1-10)."
            ): int,
            _Optional(
                "source",
                description="'random' ICA recipes, or 'favorites' saved in the ICA app.",
            ): str,
            _Optional(
                "preview",
                description=(
                    "true returns the menu without changing the list (default). false commits "
                    "the menu, together with 'recipes'."
                ),
            ): bool,
            _Optional(
                "recipes",
                description=(
                    "Comma-separated recipe ids to commit exactly, taken from an earlier "
                    "preview. Overrides count and source."
                ),
            ): str,
            _Optional(
                "avoid_days",
                description=(
                    "Skip recipes eaten within this many days. Defaults to 14 when not given."
                ),
            ): int,
        }
    )

    def _service_data(self, args: dict[str, Any]) -> dict[str, Any]:
        data = dict(args)
        data.setdefault("avoid_days", 14)
        return data


class SetPreferenceTool(_ServiceTool):
    name = "ica_shopping__SetPreference"
    title = "Remember a food preference"
    description = (
        "Remember a food preference for the household, for example 'no fish' or 'vegetarian on "
        "weekdays'. Use it when the user says what they like or dislike."
    )
    service = "set_preference"
    annotations = _annotations(
        read_only=False, destructive=False, idempotent=True, open_world=False
    )
    parameters = _Schema(
        {_Required("text", description="The preference to remember."): str}
    )


class RemovePreferenceTool(_ServiceTool):
    name = "ica_shopping__RemovePreference"
    title = "Forget a food preference"
    description = "Remove a saved food preference, matched on the text."
    service = "remove_preference"
    annotations = _annotations(
        read_only=False, destructive=True, idempotent=True, open_world=False
    )
    parameters = _Schema(
        {_Required("text", description="The preference to remove."): str}
    )


class ForgetDinnerTool(_ServiceTool):
    name = "ica_shopping__ForgetDinner"
    title = "Forget a dinner"
    description = (
        "Remove a dinner from the memory history, for example one that was planned but never "
        "cooked. Match on a recipe id or part of the title, and optionally on a date."
    )
    service = "forget_dinner"
    annotations = _annotations(
        read_only=False, destructive=True, idempotent=True, open_world=False
    )
    parameters = _Schema(
        {
            _Optional(
                "recipe", description="Recipe id, or part of the dinner title."
            ): str,
            _Optional(
                "date", description="Only remove meals eaten on this date (YYYY-MM-DD)."
            ): str,
        }
    )


class ClearMemoryTool(_ServiceTool):
    name = "ica_shopping__ClearMemory"
    title = "Clear the dinner memory"
    description = (
        "Empty the dinner memory: the history and/or the preferences. Ask the user to confirm "
        "first, since it cannot be undone."
    )
    service = "clear_memory"
    annotations = _annotations(
        read_only=False, destructive=True, idempotent=True, open_world=False
    )
    parameters = _Schema(
        {
            _Optional(
                "history", description="Clear the dinner history (default true)."
            ): bool,
            _Optional(
                "preferences", description="Clear the saved preferences (default true)."
            ): bool,
        }
    )


TOOLS: list[Tool] = [
    GetOffersTool(),
    GetMemoryTool(),
    PlanDinnersTool(),
    SetPreferenceTool(),
    RemovePreferenceTool(),
    ForgetDinnerTool(),
    ClearMemoryTool(),
]


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: LLMContext, api_id: str
) -> LLMTools | None:
    """Return the ICA Shopping tools for the Assist API."""
    if api_id != LLM_API_ASSIST:
        return None
    if not hass.data.get(DOMAIN, {}).get(DATA_ICA):
        # Not set up (or not logged in): nothing to contribute.
        return None
    return LLMTools(tools=TOOLS, prompt=PROMPT)
