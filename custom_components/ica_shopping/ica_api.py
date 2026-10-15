"""Async client for ICA's mobile-app API (OAuth via ims.icagruppen.se).

Auth flow ported from kanylbullen/ica-mcp (originally LazyTarget/ha-ica-todo).
The gateway (apimgw-pub.ica.se) answers HTTP 451 to non-Swedish IPs.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import random
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)

from .const import (
    ACR,
    API_BASE,
    AUTHORIZE_ENDPOINT,
    DCR_CLIENT_ID,
    DCR_CLIENT_SECRET,
    DCR_SOFTWARE_ID,
    LOGIN_ENDPOINT,
    RECIPE_PATH,
    REDIRECT_URI,
    REGISTER_ENDPOINT,
    SL_PATH,
    TOKEN_ENDPOINT,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=30)
EARLY_REFRESH_SECONDS = 60


class IcaError(Exception):
    """Generic ICA error."""


class IcaConnectionError(IcaError):
    """Network problem or server-side error; worth retrying later."""


class IcaGeoBlockedError(IcaConnectionError):
    """Gateway refused a non-Swedish IP (HTTP 451)."""


class IcaAuthError(IcaError):
    """Credentials rejected; needs user action (reauth)."""


class IcaApiError(IcaError):
    """Unexpected API response."""


def normalize_personnummer(value: str | None) -> str:
    """Return 12 digits; the authenticate endpoint answers a bare 400 without the century."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 10:
        yy = int(digits[:2])
        century = "20" if yy <= dt.date.today().year % 100 else "19"
        return century + digits
    return digits


def list_matches(lst: dict, list_id: str | None) -> bool:
    """Match on offlineId (case-insensitive) or numeric id, so legacy web list ids keep working."""
    if not list_id:
        return False
    wanted = str(list_id).strip().upper()
    return wanted in (str(lst.get("id") or "").upper(), str(lst.get("numeric_id") or ""))


def _normalize_row(row: dict) -> dict:
    return {
        "id": row.get("offlineId"),
        "text": row.get("productName") or "",
        "isStriked": bool(row.get("isStrikedOver")),
        # changedRows takes the whole row back; sending only changed fields is unverified.
        "raw": row,
    }


def _normalize_list(raw: dict) -> dict:
    return {
        "id": raw.get("offlineId"),
        "numeric_id": raw.get("id"),
        "name": raw.get("title") or "",
        "rows": [_normalize_row(r) for r in raw.get("rows") or [] if isinstance(r, dict)],
    }


def _ts() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _query_param(location: str, key: str) -> str:
    vals = parse_qs(urlparse(location).query).get(key)
    if vals:
        return vals[0]
    m = re.search(rf"[?&]{re.escape(key)}=([^&\s]+)", location)
    if not m:
        raise IcaAuthError(f"'{key}' missing from redirect")
    return m.group(1)


def _hidden_field(html: str, name: str) -> str:
    for pat in (
        rf'name="{name}"[^>]*value="([^"]*)"',
        rf'value="([^"]*)"[^>]*name="{name}"',
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    raise IcaAuthError("Login rejected (wrong personnummer/password, or a BankID-only account)")


def _pkce() -> tuple[str, str]:
    verifier = re.sub("[^a-zA-Z0-9]+", "", base64.urlsafe_b64encode(os.urandom(40)).decode())
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().replace("=", "")
    return challenge, verifier


def _stamp(token: dict) -> dict:
    token = dict(token)
    token["expires_at"] = time.time() + float(token.get("expires_in", 900))
    return token


async def _check(resp: aiohttp.ClientResponse, step: str) -> None:
    if resp.status < 400:
        return
    body = (await resp.text())[:300]
    if resp.status == 451:
        raise IcaGeoBlockedError("ICA gateway geo-blocks non-Swedish IPs (HTTP 451)")
    if resp.status >= 500:
        raise IcaConnectionError(f"{step}: HTTP {resp.status}: {body}")
    raise IcaAuthError(f"{step}: HTTP {resp.status}: {body}")


class ICAApi:
    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        auth_state: dict | None = None,
        on_auth_update: Callable[[dict], None] | None = None,
        on_auth_failed: Callable[[], None] | None = None,
    ) -> None:
        self.hass = hass
        self.username = normalize_personnummer(username)
        self.password = password or ""
        self._state: dict = dict(auth_state or {})
        self._on_auth_update = on_auth_update
        self._on_auth_failed = on_auth_failed
        self._lock = asyncio.Lock()

    @property
    def auth_state(self) -> dict:
        """Registered client + tokens, JSON-serializable for config entry storage."""
        return dict(self._state)

    @property
    def token_expires_at(self) -> float | None:
        return (self._state.get("token") or {}).get("expires_at")

    # ------------------------------------------------------------------ auth
    def _save_state(self) -> None:
        if self._on_auth_update:
            self._on_auth_update(self.auth_state)

    async def _full_login(self) -> None:
        if not self.username or not self.password:
            raise IcaAuthError("Personnummer and password are required")
        _LOGGER.debug("Performing full ICA login")

        # Private cookie jar: Curity's login session cookies must not leak into HA's shared session.
        session = async_create_clientsession(
            self.hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar()
        )
        # HA freezes a created session's default headers, so the UA goes on each request.
        ua = {"User-Agent": USER_AGENT}
        try:
            async with session.post(TOKEN_ENDPOINT, data={
                "client_id": DCR_CLIENT_ID, "client_secret": DCR_CLIENT_SECRET,
                "grant_type": "client_credentials", "scope": "dcr", "response_type": "token",
            }, headers=ua, timeout=TIMEOUT) as resp:
                await _check(resp, "bootstrap token")
                bootstrap = (await resp.json())["access_token"]

            async with session.post(
                REGISTER_ENDPOINT, json={"software_id": DCR_SOFTWARE_ID},
                headers={**ua, "Authorization": f"Bearer {bootstrap}"}, timeout=TIMEOUT,
            ) as resp:
                await _check(resp, "client registration")
                client = await resp.json()

            challenge, verifier = _pkce()
            async with session.get(AUTHORIZE_ENDPOINT, params={
                "client_id": client["client_id"], "scope": client["scope"],
                "redirect_uri": REDIRECT_URI, "response_type": "code",
                "code_challenge": challenge, "code_challenge_method": "S256",
                "prompt": "login", "acr": ACR,
            }, allow_redirects=False, headers=ua, timeout=TIMEOUT) as resp:
                await _check(resp, "authorize")
                location = urljoin(AUTHORIZE_ENDPOINT, resp.headers["Location"])
            state = _query_param(location, "state")

            async with session.get(location, headers=ua, timeout=TIMEOUT) as resp:
                await _check(resp, "login page")

            async with session.post(LOGIN_ENDPOINT, data={
                "userName": self.username, "password": self.password,
            }, headers=ua, timeout=TIMEOUT) as resp:
                await _check(resp, "authenticate")
                sso_token = _hidden_field(await resp.text(), "token")

            async with session.post(
                AUTHORIZE_ENDPOINT,
                params={"client_id": client["client_id"], "forceAuthN": "true", "acr": ACR},
                data={"token": sso_token, "state": state},
                allow_redirects=False, headers=ua, timeout=TIMEOUT,
            ) as resp:
                await _check(resp, "authorize code")
                code = _query_param(resp.headers.get("Location", ""), "code")

            async with session.post(TOKEN_ENDPOINT, data={
                "code": code, "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "grant_type": "authorization_code", "scope": client["scope"],
                "response_type": "token", "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            }, headers=ua, timeout=TIMEOUT) as resp:
                await _check(resp, "token exchange")
                token = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise IcaConnectionError(f"ICA login failed: {err}") from err
        except KeyError as err:
            raise IcaAuthError(f"Unexpected ICA login response, missing {err}") from err
        finally:
            # close() would shut HA's shared connector; detach() is the documented release.
            session.detach()

        self._state = {"client": client, "token": _stamp(token)}
        self._save_state()

    async def _refresh(self) -> None:
        client = self._state["client"]
        refresh_token = self._state["token"]["refresh_token"]
        basic = base64.b64encode(
            f"{client['client_id']}:{client['client_secret']}".encode()
        ).decode()
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                TOKEN_ENDPOINT,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                headers={"Authorization": f"Basic {basic}", "User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            ) as resp:
                await _check(resp, "refresh")
                fresh = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise IcaConnectionError(f"ICA token refresh failed: {err}") from err
        self._state["token"] = _stamp({**self._state["token"], **fresh})
        self._save_state()

    def _token_valid(self) -> bool:
        token = self._state.get("token") or {}
        expires_at = token.get("expires_at")
        return bool(token.get("access_token")) and bool(expires_at) and (
            float(expires_at) > time.time() + EARLY_REFRESH_SECONDS
        )

    async def async_get_access_token(self, force_refresh: bool = False) -> str:
        """Return a valid access token, refreshing or re-logging in as needed."""
        async with self._lock:
            if not force_refresh and self._token_valid():
                return self._state["token"]["access_token"]
            token = self._state.get("token") or {}
            if token.get("refresh_token") and self._state.get("client"):
                try:
                    await self._refresh()
                    return self._state["token"]["access_token"]
                except IcaAuthError as err:
                    _LOGGER.info("ICA refresh rejected (%s); doing full login", err)
            await self._full_login()
            return self._state["token"]["access_token"]

    async def async_login(self) -> None:
        """Force a full login; used by the config flow to validate credentials."""
        async with self._lock:
            await self._full_login()

    # ------------------------------------------------------------------ HTTP
    async def _request(self, method: str, path: str, json_body: Any = None, _retry: bool = True) -> Any:
        token = await self.async_get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        session = async_get_clientsession(self.hass)
        try:
            async with session.request(
                method, f"{API_BASE}/{path}", headers=headers, json=json_body, timeout=TIMEOUT,
            ) as resp:
                if resp.status == 401 and _retry:
                    await self.async_get_access_token(force_refresh=True)
                    return await self._request(method, path, json_body, _retry=False)
                if resp.status == 451:
                    raise IcaGeoBlockedError("ICA gateway geo-blocks non-Swedish IPs (HTTP 451)")
                if resp.status >= 400:
                    body = (await resp.text())[:300]
                    if resp.status >= 500:
                        raise IcaConnectionError(f"{method} {path}: HTTP {resp.status}: {body}")
                    raise IcaApiError(f"{method} {path}: HTTP {resp.status}: {body}")
                body = await resp.text()
                return json.loads(body) if body.strip() else None
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise IcaConnectionError(f"{method} {path}: {err}") from err

    def _handle_error(self, action: str, err: IcaError) -> None:
        _LOGGER.error("❗ ICA %s misslyckades: %s", action, err)
        if isinstance(err, IcaAuthError) and self._on_auth_failed:
            self._on_auth_failed()

    # ------------------------------------------------------- raising API
    async def async_get_lists(self) -> list[dict]:
        data = await self._request("GET", SL_PATH) or {}
        return [_normalize_list(L) for L in data.get("shoppingLists", []) if isinstance(L, dict)]

    async def async_get_list(self, list_id: str) -> dict | None:
        match = next((L for L in await self.async_get_lists() if list_matches(L, list_id)), None)
        if match is None:
            return None
        raw = await self._request("GET", f"{SL_PATH}/{match['id']}")
        return _normalize_list(raw) if isinstance(raw, dict) else match

    async def _sync(self, list_offline_id: str, payload: dict) -> Any:
        return await self._request("POST", f"{SL_PATH}/{list_offline_id}/sync", payload)

    async def async_create_list(self, title: str) -> dict:
        offline_id = str(uuid.uuid4()).upper()
        await self._request("POST", SL_PATH, {
            "offlineId": offline_id, "title": title, "commentText": "",
            "sortingStore": 0, "rows": [], "latestChange": _ts(),
        })
        return await self.async_get_list(offline_id) or {"id": offline_id, "name": title, "rows": []}

    async def async_get_random_recipe(self) -> dict | None:
        # numberofrecipes is ignored; the endpoint always returns one recipe.
        data = await self._request("GET", f"{RECIPE_PATH}/recipes/random?numberofrecipes=1")
        if isinstance(data, list):
            return data[0] if data and isinstance(data[0], dict) else None
        return data if isinstance(data, dict) and data.get("id") else None

    async def async_get_favorite_recipe_ids(self) -> list:
        data = await self._request("GET", f"{RECIPE_PATH}/favorites") or {}
        return [f["recipeId"] for f in data.get("favorites", []) if isinstance(f, dict) and f.get("recipeId")]

    async def async_get_recipe(self, recipe_id) -> dict:
        return await self._request("GET", f"{RECIPE_PATH}/recipes/{recipe_id}?api-version=2.0") or {}

    # --------------------------------------- non-raising (used by sync/sensors)
    async def fetch_lists(self) -> list[dict]:
        try:
            return await self.async_get_lists()
        except IcaError as err:
            self._handle_error("fetch_lists", err)
            return []

    async def get_list_by_id(self, list_id: str) -> dict | None:
        try:
            return await self.async_get_list(list_id)
        except IcaError as err:
            self._handle_error("get_list", err)
            return None

    async def get_list_name(self, list_id: str) -> str:
        for lst in await self.fetch_lists():
            if list_matches(lst, list_id):
                return lst.get("name") or f"Lista {list_id}"
        return f"Lista {list_id}"

    async def add_to_list(self, list_offline_id: str, text: str, striked: bool = False) -> bool:
        row = {
            "offlineId": str(uuid.uuid4()).upper(),
            "productName": text,
            # Negative sourceId marks a free-text row (not linked to a product).
            "sourceId": -random.randint(10**6, 10**9),
            "isStrikedOver": striked,
            "recipes": [],
            "latestChange": _ts(),
        }
        try:
            await self._sync(list_offline_id, {"createdRows": [row]})
            return True
        except IcaError as err:
            self._handle_error(f"add '{text}'", err)
            return False

    async def set_striked(
        self, list_offline_id: str, row_offline_id: str, striked: bool, row: dict | None = None
    ) -> bool:
        """Strike or unstrike a row. Pass the raw row if you have it to skip a re-fetch."""
        try:
            if row is None:
                raw = await self._request("GET", f"{SL_PATH}/{list_offline_id}") or {}
                row = next(
                    (r for r in raw.get("rows") or [] if r.get("offlineId") == row_offline_id),
                    None,
                )
                if row is None:
                    raise IcaApiError(f"row {row_offline_id} not found")
            changed = {**row, "isStrikedOver": striked, "latestChange": _ts()}
            await self._sync(list_offline_id, {"changedRows": [changed]})
            return True
        except IcaError as err:
            self._handle_error(f"strike row {row_offline_id}", err)
            return False

    async def remove_item(self, list_offline_id: str, row_offline_id: str) -> bool:
        return await self.remove_items(list_offline_id, [row_offline_id])

    async def remove_items(self, list_offline_id: str, row_offline_ids: list[str]) -> bool:
        if not row_offline_ids:
            return True
        try:
            await self._sync(list_offline_id, {"deletedRows": list(row_offline_ids)})
            return True
        except IcaError as err:
            self._handle_error(f"remove {len(row_offline_ids)} row(s)", err)
            return False
