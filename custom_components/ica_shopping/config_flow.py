import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AUTH,
    CONF_LIST_ID,
    CONF_NEVER_ADD,
    CONF_REMOVE_STRIKED,
    CONF_TODO_ENTITY,
    DATA_ICA,
    DEFAULT_NEVER_ADD,
    DOMAIN,
)
from .ica_api import (
    ICAApi,
    IcaAuthError,
    IcaError,
    IcaGeoBlockedError,
    list_matches,
    normalize_personnummer,
)

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
TODO_SELECTOR = EntitySelector(EntitySelectorConfig(domain="todo"))
NEVER_ADD_SELECTOR = TextSelector(TextSelectorConfig(multiple=True))


def _credentials_schema(username: str = "") -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_USERNAME, default=username): str,
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    })


def _list_selector(lists: list[dict], current_id: str | None = None) -> SelectSelector:
    options = [
        SelectOptionDict(
            # Keep the stored id when it matches so sensor unique_ids don't change.
            value=current_id if list_matches(lst, current_id) else lst["id"],
            label=lst["name"] or lst["id"],
        )
        for lst in lists
        if lst.get("id")
    ]
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))


async def _async_validate(hass, username: str, password: str) -> tuple[ICAApi, list[dict], dict]:
    """Log in and fetch lists. Returns (api, lists, errors)."""
    api = ICAApi(hass, username, password)
    errors: dict[str, str] = {}
    lists: list[dict] = []
    try:
        await api.async_login()
        lists = await api.async_get_lists()
        if not lists:
            errors["base"] = "no_lists"
    except IcaAuthError as err:
        _LOGGER.warning("ICA login rejected: %s", err)
        errors["base"] = "invalid_auth"
    except IcaGeoBlockedError:
        errors["base"] = "geo_blocked"
    except IcaError as err:
        _LOGGER.warning("ICA unreachable: %s", err)
        errors["base"] = "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error validating ICA credentials")
        errors["base"] = "unknown"
    return api, lists, errors


class ICAConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 3

    def __init__(self) -> None:
        self._api: ICAApi | None = None
        self._lists: list[dict] = []
        self._credentials: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ICAOptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = normalize_personnummer(user_input[CONF_USERNAME])
            self._api, self._lists, errors = await _async_validate(
                self.hass, username, user_input[CONF_PASSWORD]
            )
            if not errors:
                self._credentials = {CONF_USERNAME: username, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                return await self.async_step_list()

        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema((user_input or {}).get(CONF_USERNAME, "")),
            errors=errors,
        )

    async def async_step_list(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            list_id = user_input[CONF_LIST_ID]
            name = next((L["name"] for L in self._lists if list_matches(L, list_id)), list_id)
            return self.async_create_entry(
                title=name,
                data={
                    **self._credentials,
                    CONF_AUTH: self._api.auth_state,
                    **user_input,
                },
            )

        return self.async_show_form(
            step_id="list",
            data_schema=vol.Schema({
                vol.Required(CONF_LIST_ID, default=self._lists[0]["id"]): _list_selector(self._lists),
                vol.Required(CONF_TODO_ENTITY): TODO_SELECTOR,
                vol.Optional(CONF_REMOVE_STRIKED, default=False): BooleanSelector(),
            }),
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            username = normalize_personnummer(user_input[CONF_USERNAME])
            self._api, self._lists, errors = await _async_validate(
                self.hass, username, user_input[CONF_PASSWORD]
            )
            if not errors:
                self._credentials = {CONF_USERNAME: username, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                current = entry.options.get(CONF_LIST_ID, entry.data.get(CONF_LIST_ID))
                if any(list_matches(L, current) for L in self._lists):
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={**self._credentials, CONF_AUTH: self._api.auth_state},
                    )
                # The stored list id isn't visible to this account (e.g. a legacy web-API id).
                return await self.async_step_reauth_list()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_credentials_schema(
                (user_input or {}).get(CONF_USERNAME) or entry.data.get(CONF_USERNAME, "")
            ),
            errors=errors,
        )

    async def async_step_reauth_list(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        if user_input is not None:
            list_id = user_input[CONF_LIST_ID]
            from . import _save_snapshot

            # Snapshot belongs to the previous list id; an empty one only causes a merge, never deletes.
            await _save_snapshot(self.hass, set())
            return self.async_update_reload_and_abort(
                entry,
                data_updates={**self._credentials, CONF_AUTH: self._api.auth_state, CONF_LIST_ID: list_id},
                options={**entry.options, CONF_LIST_ID: list_id},
            )

        return self.async_show_form(
            step_id="reauth_list",
            data_schema=vol.Schema({
                vol.Required(CONF_LIST_ID, default=self._lists[0]["id"]): _list_selector(self._lists),
            }),
        )


class ICAOptionsFlowHandler(OptionsFlow):
    # NB: do NOT define __init__ to set self.config_entry – modern Home Assistant
    # provides it as a read-only property on the base class, and assigning to it
    # raises AttributeError, which surfaces as a 500 when loading the options flow.

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        current_list_id = entry.options.get(CONF_LIST_ID, entry.data.get(CONF_LIST_ID, ""))

        if user_input is not None:
            new_list_id = user_input.get(CONF_LIST_ID, current_list_id)
            if new_list_id != current_list_id:
                _LOGGER.warning("⚠️ ICA list changed from %s to %s.", current_list_id, new_list_id)
            return self.async_create_entry(title="", data=user_input)

        lists: list[dict] = []
        api = self.hass.data.get(DOMAIN, {}).get(DATA_ICA)
        if api is not None:
            try:
                lists = await api.async_get_lists()
            except IcaError as err:
                _LOGGER.warning("Could not fetch ICA lists for options: %s", err)

        list_field = _list_selector(lists, current_list_id) if lists else str
        if lists and not any(list_matches(L, current_list_id) for L in lists):
            current_list_id = lists[0]["id"]

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_LIST_ID, default=current_list_id): list_field,
                vol.Optional(
                    CONF_TODO_ENTITY,
                    default=entry.options.get(CONF_TODO_ENTITY, entry.data.get(CONF_TODO_ENTITY, "")),
                ): TODO_SELECTOR,
                vol.Optional(
                    CONF_REMOVE_STRIKED,
                    default=entry.options.get(CONF_REMOVE_STRIKED, entry.data.get(CONF_REMOVE_STRIKED, False)),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_NEVER_ADD,
                    default=entry.options.get(CONF_NEVER_ADD, DEFAULT_NEVER_ADD),
                ): NEVER_ADD_SELECTOR,
            }),
        )
