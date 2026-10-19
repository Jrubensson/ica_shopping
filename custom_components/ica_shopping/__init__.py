import asyncio
import datetime as dt
import logging
import time
from typing import Iterable

import voluptuous as vol

from homeassistant.components.calendar import CalendarEntityFeature
from homeassistant.const import (
    ATTR_SUPPORTED_FEATURES,
    CONF_PASSWORD,
    CONF_USERNAME,
    STATE_UNAVAILABLE,
)
from homeassistant.core import ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util
from homeassistant.helpers import entity_registry
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_AUTH,
    CONF_CALENDAR,
    CONF_LIST_ID,
    CONF_NEVER_ADD,
    CONF_REMOVE_STRIKED,
    CONF_STORE_ID,
    CONF_TODO_ENTITY,
    DOMAIN,
    DATA_ICA,
    DEFAULT_NEVER_ADD,
    MAX_ICA_ITEMS,
)
from .ica_api import ICAApi, IcaAuthError, IcaError, format_offer, list_matches, parse_bonus
from .memory import DayTakenError, DinnerMemory
from .recipes import (
    SOURCE_FAVORITES,
    SOURCE_RANDOM,
    NoRecipesError,
    async_plan_dinners,
    event_description,
    format_description,
    norm as _norm,
    search_summary,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = ["sensor", "calendar"]

# get_offers is called on each turn of a conversation; the store's offers only change daily,
# so a short cache keeps a back-and-forth from hammering ICA's API. The sensor polls every 6 h.
OFFERS_CACHE_SECONDS = 15 * 60

def _record_sync_result(hass, success: bool):
    """Store the outcome of a sync run so the Last Sync sensor can report it.

    Writes into hass.data[DOMAIN] (the same shared dict the sensors read from)
    and fires a lightweight event so the sensor updates immediately without
    triggering extra ICA API calls.
    """
    now = dt_util.utcnow()
    data = hass.data.setdefault(DOMAIN, {})
    data["last_run_time"] = now
    data["last_run_status"] = "success" if success else "failed"
    if success:
        data["last_success_time"] = now
    hass.bus.async_fire("ica_shopping_sync_recorded")


STORAGE_VERSION = 1
STORAGE_KEY = "ica_shopping_sync_state"
DEBOUNCE_SECONDS = 1


async def _load_snapshot(hass) -> dict:
    """Return {key: done} for items present on both sides after the last sync.

    done is None for entries saved by older versions, which stored a plain list.
    """
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load()
    if not data or not isinstance(data, dict):
        return {}
    synced = data.get("synced")
    if isinstance(synced, list):
        return {key: None for key in synced}
    if isinstance(synced, dict):
        return dict(synced)
    return {}


async def _save_snapshot(hass, items: dict) -> None:
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    await store.async_save({"synced": dict(sorted(items.items()))})


def _ica_done(group: dict) -> bool:
    return all(r["isStriked"] for r in group["rows"])


def _ha_done(group: dict) -> bool:
    return all(it.get("status") == "completed" for it in group["items"])


def _drop_ica_rows(ica: dict, row_ids: list) -> None:
    for key in list(ica):
        ica[key]["rows"] = [r for r in ica[key]["rows"] if r["id"] not in row_ids]
        if not ica[key]["rows"]:
            del ica[key]


def _ha_ref(item: dict) -> str:
    return item.get("uid") or item["summary"]


def _is_own_calendar(hass, entity_id: str) -> bool:
    entry = entity_registry.async_get(hass).async_get(entity_id)
    return entry is not None and entry.platform == DOMAIN


def _pick_calendar(hass, requested: str | None, options: dict) -> str | None:
    """The external calendar to write the plan to, checked before the ICA list is touched."""
    calendar = requested or options.get(CONF_CALENDAR)
    if not calendar or _is_own_calendar(hass, calendar):
        # The dinner calendar already shows everything in the memory.
        return None
    state = hass.states.get(calendar)
    available = state is not None and state.state != STATE_UNAVAILABLE
    writable = available and state.attributes.get(ATTR_SUPPORTED_FEATURES, 0) & CalendarEntityFeature.CREATE_EVENT
    if writable:
        return calendar
    if not requested:
        # A bad Default calendar option must not block planning; the dinner calendar still gets the plan.
        _LOGGER.warning("⚠️ Standardkalendern %s går inte att skriva till – hoppar över den", calendar)
        return None
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="calendar_not_writable" if available else "calendar_unavailable",
        translation_placeholders={"entity_id": calendar},
    )


async def _trigger_sensor_update(hass, list_id):
    registry = entity_registry.async_get(hass)
    target_unique_id = f"shoppinglist_{list_id}"
    sensor_entity = None

    for entity in registry.entities.values():
        if entity.unique_id == target_unique_id:
            sensor_entity = entity.entity_id
            break

    if not sensor_entity:
        _LOGGER.debug("ℹ️ Kunde inte hitta sensor med unique_id %s", target_unique_id)
        return

    _LOGGER.debug("🔁 Triggar update för %s", sensor_entity)
    await hass.services.async_call(
        "homeassistant", "update_entity",
        {"entity_id": sensor_entity},
        blocking=True,
    )


async def _perform_sync(hass, api: ICAApi, entry, clear_checked: bool = False) -> None:
    """Reconcile HA todo entity with ICA list using a persisted snapshot.

    Presence diff (per normalized item name, any completion state):
      in_ha  in_ica  in_snap   → action
      T      T       *         → sync completion state (below)
      T      F       T         → user deleted from ICA → remove from HA
      T      F       F         → user added in HA → push to ICA
      F      T       T         → user deleted from HA → remove from ICA
      F      T       F         → user added in ICA → push to HA
      F      F       *         → no-op (gone on both sides)

    Completion (ICA struck over ↔ HA completed), when the sides disagree:
    the side that differs from the snapshot's stored state changed and wins.
    Without a stored state, done wins.

    With clear_checked (or the remove_striked option), struck rows and
    completed items are then deleted on both sides.
    """
    lock = hass.data[DOMAIN].setdefault("sync_lock", asyncio.Lock())
    async with lock:
        await _perform_sync_locked(hass, api, entry, clear_checked)


async def _perform_sync_locked(hass, api: ICAApi, entry, clear_checked: bool) -> None:
    todo_entity = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))
    list_id = entry.options.get("ica_list_id", entry.data.get("ica_list_id"))
    auto_clear = entry.options.get(CONF_REMOVE_STRIKED, entry.data.get(CONF_REMOVE_STRIKED, False))

    if not todo_entity or not list_id:
        _LOGGER.error("❌ Saknar todo_entity_id eller ica_list_id – avbryter sync")
        return

    async def todo_call(service, data):
        await hass.services.async_call(
            "todo", service, {"entity_id": todo_entity, **data}, blocking=True,
        )

    hass.data[DOMAIN]["sync_in_progress"] = True
    try:
        # --- Hämta ICA-listans rader -------------------------------------
        the_list = await api.get_list_by_id(list_id)
        if the_list is None:
            _LOGGER.warning("❌ Kunde inte hitta ICA-lista %s", list_id)
            _record_sync_result(hass, False)
            return
        # Writes go to the list's offlineId, which may differ in case/format from the stored id.
        list_offline_id = the_list["id"]

        ica: dict = {}  # norm -> {"text", "rows": [normalized rows]}
        for row in the_list.get("rows", []):
            if not isinstance(row, dict) or not row.get("id"):
                continue
            text = (row.get("text") or "").strip()
            if not text:
                continue
            ica.setdefault(_norm(text), {"text": text, "rows": []})["rows"].append(row)

        # --- Hämta items från HA todo-entitet ----------------------------
        try:
            result = await hass.services.async_call(
                "todo", "get_items",
                {"entity_id": todo_entity},
                blocking=True, return_response=True,
            )
        except Exception as e:
            _LOGGER.error("💥 Kunde inte hämta items från %s: %s", todo_entity, e)
            return

        ha: dict = {}  # norm -> {"text", "items": [todo items]}
        for it in result.get(todo_entity, {}).get("items", []):
            if not isinstance(it, dict):
                continue
            summary = (it.get("summary") or "").strip()
            if not summary:
                continue
            ha.setdefault(_norm(summary), {"text": summary, "items": []})["items"].append(it)

        # --- Ladda snapshot och beräkna diff -----------------------------
        snap = await _load_snapshot(hass)

        add_to_ica: list = []
        add_to_ha: list = []
        remove_from_ica: list = []
        remove_from_ha: list = []
        set_ica_done: list = []  # (key, done)
        set_ha_done: list = []   # (key, done)

        for key in set(ha) | set(ica) | set(snap):
            in_ha = key in ha
            in_ica = key in ica
            in_snap = key in snap

            if in_ha and in_ica:
                ha_done = _ha_done(ha[key])
                ica_done = _ica_done(ica[key])
                if ha_done == ica_done:
                    continue
                prev = snap.get(key)
                if prev is None:
                    target = True
                elif ha_done != prev:
                    target = ha_done
                else:
                    target = ica_done
                if ica_done != target:
                    set_ica_done.append((key, target))
                if ha_done != target:
                    set_ha_done.append((key, target))
            elif in_ha:
                (remove_from_ha if in_snap else add_to_ica).append(key)
            elif in_ica:
                (remove_from_ica if in_snap else add_to_ha).append(key)

        # --- Ta bort i ICA före tillägg, så att de ryms under taket -----
        if remove_from_ica:
            row_ids = [r["id"] for key in remove_from_ica for r in ica[key]["rows"]]
            if await api.remove_items(list_offline_id, row_ids):
                for key in remove_from_ica:
                    _LOGGER.info("❌ Tog bort '%s' från ICA (saknas i HA)", ica.pop(key)["text"])

        clearing = clear_checked or auto_clear
        if clearing:
            unstrike = {key for key, done in set_ica_done if not done}
            struck_ids = [
                r["id"] for key, g in ica.items() if key not in unstrike
                for r in g["rows"] if r["isStriked"] and r["id"]
            ]
            if struck_ids and await api.remove_items(list_offline_id, struck_ids):
                _LOGGER.info("🧹 Rensade %d avbockade ICA-rader", len(struck_ids))
                _drop_ica_rows(ica, struck_ids)
                add_to_ha = [key for key in add_to_ha if key in ica]

        # --- ICA-taket: begränsa add_to_ica om vi närmar oss MAX_ICA_ITEMS
        allowed = max(0, MAX_ICA_ITEMS - sum(len(g["rows"]) for g in ica.values()))
        skipped_full = add_to_ica[allowed:]
        if skipped_full:
            _LOGGER.warning(
                "⚠️ Tillägg överskrider ICA-tak (%d); begränsar från %d till %d",
                MAX_ICA_ITEMS, len(add_to_ica), allowed,
            )
            add_to_ica = add_to_ica[:allowed]
            ir.async_create_issue(
                hass, DOMAIN, "list_full",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="list_full",
                translation_placeholders={
                    "limit": str(MAX_ICA_ITEMS),
                    "skipped": ", ".join(ha[key]["text"] for key in skipped_full[:10]),
                    "count": str(len(skipped_full)),
                },
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, "list_full")

        # --- Applicera operationer ---------------------------------------
        for key in add_to_ica:
            text = ha[key]["text"]
            done = _ha_done(ha[key])
            if await api.add_to_list(list_offline_id, text, striked=done):
                _LOGGER.info("📥 Lade till '%s' i ICA", text)
                # No row id until the next fetch, so clearing skips it this run.
                ica[key] = {"text": text, "rows": [{"id": None, "isStriked": done, "raw": None}]}

        for key, done in set_ica_done:
            for row in ica[key]["rows"]:
                if row["isStriked"] != done and await api.set_striked(
                    list_offline_id, row["id"], done, row=row["raw"]
                ):
                    row["isStriked"] = done
            _LOGGER.info(
                "%s '%s' i ICA", "✔️ Bockade av" if done else "↩️ Ångrade avbockning av",
                ica[key]["text"],
            )

        for key in add_to_ha:
            text = ica[key]["text"]
            status = "completed" if _ica_done(ica[key]) else "needs_action"
            await todo_call("add_item", {"item": text})
            if status == "completed":
                await todo_call("update_item", {"item": text, "status": status})
            _LOGGER.info("✅ Lade till '%s' i HA-todo", text)
            ha[key] = {"text": text, "items": [{"summary": text, "status": status}]}

        for key in remove_from_ha:
            await todo_call("remove_item", {"item": [_ha_ref(it) for it in ha[key]["items"]]})
            _LOGGER.info("🗑️ Tog bort '%s' från HA-todo (saknas i ICA)", ha.pop(key)["text"])

        for key, done in set_ha_done:
            status = "completed" if done else "needs_action"
            for it in ha[key]["items"]:
                if it.get("status") != status:
                    await todo_call("update_item", {"item": _ha_ref(it), "status": status})
                    it["status"] = status
            _LOGGER.info("🔁 Satte '%s' till %s i HA-todo", ha[key]["text"], status)

        # --- Rensa avbockade/avklarade på båda sidor ---------------------
        if clearing:
            # Rows struck by this run; the rest went before the adds.
            struck_ids = [
                r["id"] for g in ica.values() for r in g["rows"] if r["isStriked"] and r["id"]
            ]
            if struck_ids and await api.remove_items(list_offline_id, struck_ids):
                _LOGGER.info("🧹 Rensade %d avbockade ICA-rader", len(struck_ids))
                _drop_ica_rows(ica, struck_ids)

            # Keep HA items whose ICA delete or strike failed, or the next sync would re-add them.
            still_struck = {
                k for k, g in ica.items()
                if any(r["isStriked"] for r in g["rows"]) or (k in ha and _ha_done(ha[k]))
            }
            completed = [
                _ha_ref(it) for key, g in ha.items() if key not in still_struck
                for it in g["items"] if it.get("status") == "completed"
            ]
            if completed:
                await todo_call("remove_item", {"item": completed})
                _LOGGER.info("🧹 Rensade %d avklarade HA-items", len(completed))
                for key in list(ha):
                    if key in still_struck:
                        continue
                    ha[key]["items"] = [
                        it for it in ha[key]["items"] if it.get("status") != "completed"
                    ]
                    if not ha[key]["items"]:
                        del ha[key]

        # --- Spara snapshot = items på BÅDA sidor med gemensam status ----
        # Keys whose completion write failed keep their old state, so the next run retries.
        new_snap = {}
        for key in ha.keys() & ica.keys():
            ha_done = _ha_done(ha[key])
            new_snap[key] = ha_done if ha_done == _ica_done(ica[key]) else snap.get(key)
        await _save_snapshot(hass, new_snap)
        _LOGGER.debug(
            "💾 Snapshot uppdaterad: %d items (ha=%d, ica=%d)",
            len(new_snap), len(ha), len(ica),
        )

        await _trigger_sensor_update(hass, list_id)

        _record_sync_result(hass, not skipped_full)
    except Exception as e:
        _LOGGER.exception("💥 Fel under _perform_sync: %s", e)
        _record_sync_result(hass, False)
    finally:
        hass.data[DOMAIN]["sync_in_progress"] = False


async def async_setup(hass, config):
    # Registered here, not per entry, so automations keep a valid action while login is broken.
    def _recipe_ids(value) -> list[str] | None:
        """Normalise a list of recipe ids, or a comma-separated string, to a list."""
        if not value:
            return None
        parts = value.replace(" ", "").split(",") if isinstance(value, str) else value
        ids = [str(p).strip() for p in parts if str(p).strip()]
        return ids or None

    def _sync_or_raise():
        sync = hass.data.get(DOMAIN, {}).get("sync")
        if sync is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_logged_in"
            )
        return sync

    async def handle_refresh(call):
        sync = _sync_or_raise()
        _LOGGER.debug("🔄 ICA refresh triggered via service")
        await sync()

    async def handle_clear_checked(call):
        sync = _sync_or_raise()
        _LOGGER.debug("🧹 ICA clear_checked triggered via service")
        await sync(clear_checked=True)

    hass.services.async_register(DOMAIN, "refresh", handle_refresh)
    hass.services.async_register(DOMAIN, "clear_checked", handle_clear_checked)

    async def handle_get_offers(call: ServiceCall) -> ServiceResponse:
        """The favourite store's current offers and the bonus balance."""
        data = hass.data.get(DOMAIN, {})
        api = data.get(DATA_ICA)
        if api is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_logged_in"
            )
        cached = data.get("offers_cache")
        if cached and time.monotonic() - cached[0] < OFFERS_CACHE_SECONDS:
            return cached[1]
        options = data.get("current_options") or {}
        try:
            stores = await api.async_get_favorite_stores()
            if not stores:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="no_favorite_store"
                )
            store = next(
                (s for s in stores if s["id"] == options.get(CONF_STORE_ID)), stores[0]
            )
            offers = await api.async_get_store_offers(store["id"])
            bonus = parse_bonus(await api.async_get_bonus())
        except IcaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="offers_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        result = {
            "store": store.get("name"),
            "offer_count": len(offers),
            "offers": [format_offer(o) for o in offers],
            "bonus": bonus,
        }
        data["offers_cache"] = (time.monotonic(), result)
        return result

    hass.services.async_register(
        DOMAIN, "get_offers", handle_get_offers, supports_response=SupportsResponse.OPTIONAL
    )

    async def handle_search_recipes(call: ServiceCall) -> ServiceResponse:
        api = hass.data.get(DOMAIN, {}).get(DATA_ICA)
        if api is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_logged_in"
            )
        try:
            results = await api.async_search_recipes(call.data["query"], call.data["limit"])
        except IcaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="search_recipes_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        return {"recipes": [search_summary(r) for r in results]}

    hass.services.async_register(
        DOMAIN,
        "search_recipes",
        handle_search_recipes,
        schema=vol.Schema({
            vol.Required("query"): cv.string,
            vol.Optional("limit", default=10): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
        }),
        supports_response=SupportsResponse.ONLY,
    )

    async def handle_plan_dinners(call: ServiceCall) -> ServiceResponse:
        sync = _sync_or_raise()
        data = hass.data[DOMAIN]
        default_list_id = data["current_list_id"]
        options = data.get("current_options") or {}
        preview = bool(call.data.get("preview", False))
        recipe_ids = _recipe_ids(call.data.get("recipes"))
        memory = data.get("memory")
        avoid_days = call.data.get("avoid_days", 0) or 0
        avoid_ids = memory.recent_ids(avoid_days) if memory is not None else None
        calendar = None if preview else _pick_calendar(hass, call.data.get("calendar"), options)
        try:
            # Not held across sync() below, which takes the same lock.
            async with data.setdefault("sync_lock", asyncio.Lock()):
                target, recipes, result = await async_plan_dinners(
                    data[DATA_ICA],
                    count=call.data["count"],
                    source=call.data["source"],
                    list_ref=call.data.get("list"),
                    default_list_id=default_list_id,
                    never_add=options.get(CONF_NEVER_ADD, DEFAULT_NEVER_ADD),
                    preview=preview,
                    add_to_list=call.data.get("add_to_list", True),
                    recipe_ids=recipe_ids,
                    avoid_ids=avoid_ids,
                )
        except NoRecipesError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_recipes",
                translation_placeholders={"source": call.data["source"]},
            ) from err
        except IcaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="plan_dinners_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        if not preview and result["added"] and list_matches(target, default_list_id):
            # Push the new rows to the to-do entity now instead of on the next refresh.
            await sync()

        if not preview:
            start = call.data.get("start_date") or dt_util.now().date() + dt.timedelta(days=1)
            count = len(result["dinners"])
            if memory is not None:
                # A repeated commit keeps a recipe's planned day instead of planning it twice.
                planned = [memory.planned_day(str(d.get("id")), start) for d in result["dinners"]]
                free = iter(memory.free_days(start, planned.count(None)))
                days = [day or next(free) for day in planned]
            else:
                planned = [None] * count
                days = [start + dt.timedelta(days=offset) for offset in range(count)]
            for day, dinner in zip(days, result["dinners"]):
                dinner["date"] = day.isoformat()
            if memory is not None:
                # Before the external calendar, so a failure there still leaves the plan remembered.
                await memory.async_record(result["dinners"], result.get("list"))
        if calendar and not preview:
            created = 0
            new_events = [
                (day, recipe, dinner)
                for day, recipe, dinner, was_planned in zip(days, recipes, result["dinners"], planned)
                if was_planned is None
            ]
            try:
                for day, recipe, dinner in new_events:
                    await hass.services.async_call("calendar", "create_event", {
                        "entity_id": calendar,
                        "summary": dinner["title"] or "Middag",
                        "description": event_description(recipe),
                        "start_date": day,
                        "end_date": day + dt.timedelta(days=1),
                    }, blocking=True)
                    created += 1
            except Exception as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="calendar_event_failed",
                    translation_placeholders={
                        "error": str(err),
                        "created": str(created),
                        "total": str(len(new_events)),
                        "added": str(len(result["added"])),
                        "list": result["list"] or "",
                    },
                ) from err
        return result

    # --- Dinner memory: history and preferences ---------------------------------
    def _memory_or_raise() -> DinnerMemory:
        memory = hass.data.get(DOMAIN, {}).get("memory")
        if memory is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_loaded"
            )
        return memory

    async def handle_get_memory(call: ServiceCall) -> ServiceResponse:
        return _memory_or_raise().as_dict()

    async def handle_set_preference(call: ServiceCall) -> ServiceResponse:
        memory = _memory_or_raise()
        added = await memory.async_add_preference(call.data["text"])
        return {"added": added, "preferences": memory.preferences}

    async def handle_remove_preference(call: ServiceCall) -> ServiceResponse:
        memory = _memory_or_raise()
        removed = await memory.async_remove_preference(call.data["text"])
        return {"removed": removed, "preferences": memory.preferences}

    async def handle_forget_dinner(call: ServiceCall) -> ServiceResponse:
        memory = _memory_or_raise()
        recipe = call.data.get("recipe")
        date = call.data.get("date")
        if not recipe and not date:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="forget_needs_target"
            )
        removed = await memory.async_forget_dinner(recipe, str(date) if date else None)
        return {"removed": removed, "history": memory.as_dict()["history"]}

    async def handle_add_dinner(call: ServiceCall) -> ServiceResponse:
        memory = _memory_or_raise()
        options = hass.data[DOMAIN].get("current_options") or {}
        title, day = call.data["title"], call.data["date"]
        note, url = call.data.get("note"), call.data.get("url")
        calendar = _pick_calendar(hass, None, options)
        try:
            entry = await memory.async_add_custom(title, day.isoformat(), note=note, url=url)
        except DayTakenError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="dinner_day_taken",
                translation_placeholders={"date": str(err)},
            ) from err
        if calendar:
            try:
                await hass.services.async_call("calendar", "create_event", {
                    "entity_id": calendar,
                    "summary": title,
                    "description": format_description(None, url, None, note=note),
                    "start_date": day,
                    "end_date": day + dt.timedelta(days=1),
                }, blocking=True)
            except Exception as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="add_dinner_calendar_failed",
                    translation_placeholders={
                        "error": str(err), "title": title, "entity_id": calendar,
                    },
                ) from err
        return {"id": entry["id"], "title": entry["title"], "date": entry["date"]}

    async def handle_clear_memory(call: ServiceCall) -> ServiceResponse:
        memory = _memory_or_raise()
        removed = await memory.async_clear(
            history=bool(call.data.get("history", True)),
            preferences=bool(call.data.get("preferences", True)),
        )
        return {
            "history_removed": removed["history"],
            "preferences_removed": removed["preferences"],
        }

    hass.services.async_register(
        DOMAIN, "get_memory", handle_get_memory, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN,
        "set_preference",
        handle_set_preference,
        schema=vol.Schema({vol.Required("text"): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "remove_preference",
        handle_remove_preference,
        schema=vol.Schema({vol.Required("text"): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "forget_dinner",
        handle_forget_dinner,
        schema=vol.Schema({
            vol.Optional("recipe"): cv.string,
            vol.Optional("date"): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "add_dinner",
        handle_add_dinner,
        schema=vol.Schema({
            vol.Required("title"): cv.string,
            vol.Required("date"): cv.date,
            vol.Optional("note"): cv.string,
            vol.Optional("url"): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "clear_memory",
        handle_clear_memory,
        schema=vol.Schema({
            vol.Optional("history", default=True): cv.boolean,
            vol.Optional("preferences", default=True): cv.boolean,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        "plan_dinners",
        handle_plan_dinners,
        schema=vol.Schema({
            vol.Optional("count", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
            vol.Optional("source", default=SOURCE_RANDOM): vol.In([SOURCE_RANDOM, SOURCE_FAVORITES]),
            vol.Optional("list"): cv.string,
            vol.Optional("calendar"): cv.entity_domain("calendar"),
            vol.Optional("start_date"): cv.date,
            vol.Optional("preview", default=False): cv.boolean,
            vol.Optional("add_to_list", default=True): cv.boolean,
            # Recipe ids to commit exactly what was previewed; "1,2,3" is accepted too.
            vol.Optional("recipes"): vol.Any(cv.string, [vol.Coerce(str)]),
            # Skip recipes eaten within this many days (0 = don't skip).
            vol.Optional("avoid_days", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=365)),
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_migrate_entry(hass, entry):
    if entry.version == 1:
        # v1 authenticated with a pasted thSessionId cookie; v2 needs personnummer + password.
        data = {
            k: entry.options.get(k, entry.data.get(k))
            for k in (CONF_LIST_ID, CONF_TODO_ENTITY, CONF_REMOVE_STRIKED)
            if entry.options.get(k, entry.data.get(k)) is not None
        }
        options = {k: v for k, v in entry.options.items() if k != "session_id"}
        hass.config_entries.async_update_entry(entry, data=data, options=options, version=2)
        ir.async_delete_issue(hass, DOMAIN, "invalid_session_id")
        _LOGGER.info("ICA Shopping migrerad till v2 – logga in med personnummer och lösenord")
    if entry.version == 2:
        # remove_striked used to delete items when checked; it now means "clear checked items
        # after every sync", which would undo strike syncing, so existing entries start off.
        data = {**entry.data, CONF_REMOVE_STRIKED: False}
        options = dict(entry.options)
        if CONF_REMOVE_STRIKED in options:
            options[CONF_REMOVE_STRIKED] = False
        hass.config_entries.async_update_entry(entry, data=data, options=options, version=3)
        _LOGGER.info("ICA Shopping migrerad till v3 – avbockning synkas istället för att ta bort")
    return True


async def async_setup_entry(hass, entry):
    _LOGGER.debug("⚙️ ICA Shopping initieras via UI config entry")
    if not entry.data.get(CONF_USERNAME) or not entry.data.get(CONF_PASSWORD):
        raise ConfigEntryAuthFailed("Personnummer och lösenord saknas")

    list_id = entry.options.get(CONF_LIST_ID, entry.data[CONF_LIST_ID])

    def _persist_auth(state):
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_AUTH: state})

    api = ICAApi(
        hass,
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        auth_state=entry.data.get(CONF_AUTH),
        on_auth_update=_persist_auth,
        on_auth_failed=lambda: entry.async_start_reauth(hass),
    )
    try:
        await api.async_get_access_token()
    except IcaAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except IcaError as err:
        raise ConfigEntryNotReady(str(err)) from err

    hass.data.setdefault(DOMAIN, {})[DATA_ICA] = api
    hass.data[DOMAIN]["current_list_id"] = list_id
    hass.data[DOMAIN]["current_options"] = dict(entry.options)
    hass.data[DOMAIN]["sync_in_progress"] = False
    hass.data[DOMAIN]["memory"] = await DinnerMemory(hass).async_load()

    todo_entity = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))
    if not todo_entity:
        _LOGGER.error("❌ Ingen todo-entity vald – integrationen kan inte synka.")
        return False

    # --- Debouncad sync, triggad av call_service-events --------------
    debounce_unsub = None

    async def _debounced_sync(_now=None):
        nonlocal debounce_unsub
        debounce_unsub = None
        await _perform_sync(hass, api, entry)

    def _trigger_debounced_sync():
        nonlocal debounce_unsub
        if debounce_unsub:
            debounce_unsub()
        debounce_unsub = async_call_later(hass, DEBOUNCE_SECONDS, _debounced_sync)

    def call_service_listener(event):
        # Hoppa över när vi själva opererar
        if hass.data[DOMAIN].get("sync_in_progress", False):
            return

        # Filtrera endast todo-domänen
        if event.data.get("domain") != "todo":
            return

        data = event.data.get("service_data") or {}
        # entity_id kan ligga i service_data, i target, eller direkt i event.data
        # beroende på hur tjänsten anropades (UI använder ofta target:)
        entity_ids = (
            data.get("entity_id")
            or (event.data.get("target") or {}).get("entity_id")
            or event.data.get("entity_id")
            or []
        )
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        configured = entry.options.get(
            "todo_entity_id", entry.data.get("todo_entity_id")
        )
        if configured not in entity_ids:
            return

        service = event.data.get("service")
        if service in ("add_item", "remove_item", "update_item"):
            _LOGGER.debug("🔔 todo.%s på %s – schemalägger sync", service, configured)
            _trigger_debounced_sync()

    unsub_listener = hass.bus.async_listen("call_service", call_service_listener)
    hass.data[DOMAIN]["unsub_listener"] = unsub_listener
    hass.data[DOMAIN]["debounce_unsub_getter"] = lambda: debounce_unsub

    hass.data[DOMAIN]["sync"] = lambda clear_checked=False: _perform_sync(
        hass, api, entry, clear_checked
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_update_listener))

    return True


async def async_unload_entry(hass, entry):
    _LOGGER.debug("🔄 Unloading ICA Shopping integration")

    if "debounce_unsub_getter" in hass.data.get(DOMAIN, {}):
        debounce_unsub = hass.data[DOMAIN]["debounce_unsub_getter"]()
        if debounce_unsub:
            debounce_unsub()

    if "unsub_listener" in hass.data.get(DOMAIN, {}):
        hass.data[DOMAIN]["unsub_listener"]()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    ir.async_delete_issue(hass, DOMAIN, "list_full")

    if DOMAIN in hass.data:
        hass.data[DOMAIN].pop("unsub_listener", None)
        hass.data[DOMAIN].pop("debounce_unsub_getter", None)
        hass.data[DOMAIN].pop("sync_in_progress", None)
        hass.data[DOMAIN].pop("current_options", None)
        hass.data[DOMAIN].pop("sync", None)
        hass.data[DOMAIN].pop("memory", None)

    return unload_ok


async def _options_update_listener(hass, entry):
    # Token refreshes rewrite entry.data every ~15 min; only option changes warrant a reload.
    if dict(entry.options) == hass.data[DOMAIN].get("current_options"):
        return
    _LOGGER.debug("♻️ Optioner har ändrats, laddar om entry")

    prev_list_id = hass.data[DOMAIN].get("current_list_id")
    new_list_id = entry.options.get("ica_list_id", entry.data.get("ica_list_id"))

    if prev_list_id and prev_list_id != new_list_id:
        _LOGGER.warning(
            "⚠️ List ID changed from %s to %s – snapshot återställs vid omladdning.",
            prev_list_id, new_list_id,
        )
        # Snapshot tillhör föregående lista – nollställ för att undvika
        # falska "deletes" mot den nya listan.
        await _save_snapshot(hass, {})

    hass.data[DOMAIN]["current_list_id"] = new_list_id
    await hass.config_entries.async_reload(entry.entry_id)
