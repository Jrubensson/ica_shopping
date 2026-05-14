import logging
from typing import Iterable

from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers import entity_registry

from .const import DOMAIN, DATA_ICA
from .ica_api import ICAApi

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "ica_shopping_sync_state"
MAX_ICA_ITEMS = 250
DEBOUNCE_SECONDS = 1


def _norm(text):
    return (text or "").strip().lower()


async def _load_snapshot(hass) -> set:
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load()
    if not data or not isinstance(data, dict):
        return set()
    return set(data.get("synced", []))


async def _save_snapshot(hass, items: Iterable[str]) -> None:
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    await store.async_save({"synced": sorted(set(items))})


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


async def _perform_sync(hass, api: ICAApi, entry) -> None:
    """Reconcile HA todo entity with ICA list using a persisted snapshot.

    Diff algorithm (per normalized item name):
      in_ha  in_ica  in_snap   → action
      T      T       *         → no-op (converged)
      T      F       T         → user deleted from ICA → remove from HA
      T      F       F         → user added in HA → push to ICA
      F      T       T         → user deleted from HA → remove from ICA
      F      T       F         → user added in ICA → push to HA
      F      F       *         → no-op (gone on both sides)

    Striked (ICA) / completed (HA) items are pre-processed: when
    `remove_striked` is enabled, they are treated as an explicit "done"
    signal and removed from both sides outside the diff. When disabled,
    they are treated as still present on their own side.
    """
    todo_entity = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))
    list_id = entry.options.get("ica_list_id", entry.data.get("ica_list_id"))
    remove_striked = entry.options.get("remove_striked", True)

    if not todo_entity or not list_id:
        _LOGGER.error("❌ Saknar todo_entity_id eller ica_list_id – avbryter sync")
        return

    hass.data[DOMAIN]["sync_in_progress"] = True
    try:
        # --- Hämta ICA-listans rader -------------------------------------
        lists = await api.fetch_lists()
        the_list = next((l for l in lists if l.get("id") == list_id), None)
        if the_list is None:
            _LOGGER.warning("❌ Kunde inte hitta ICA-lista %s", list_id)
            return

        ica_rows = the_list.get("rows", [])
        ica_active: dict = {}   # norm -> (text, row_id)
        ica_striked: dict = {}  # norm -> (text, row_id)
        for row in ica_rows:
            if not isinstance(row, dict):
                continue
            text = (row.get("text") or "").strip()
            if not text:
                continue
            key = _norm(text)
            row_id = row.get("id")
            if row.get("isStriked") is True:
                ica_striked[key] = (text, row_id)
            else:
                ica_active[key] = (text, row_id)

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

        todo_items = result.get(todo_entity, {}).get("items", [])
        todo_active: dict = {}     # norm -> summary
        todo_completed: dict = {}  # norm -> summary
        for it in todo_items:
            if not isinstance(it, dict):
                continue
            summary = (it.get("summary") or "").strip()
            if not summary:
                continue
            key = _norm(summary)
            if it.get("status") == "completed":
                todo_completed[key] = summary
            else:
                todo_active[key] = summary

        # --- Explicita "klar"-händelser ----------------------------------
        # När remove_striked är aktivt: behandla striked (ICA) och completed (HA)
        # som "användaren vill ha bort det överallt". Bypass-diff för dessa nycklar.
        explicit_removes: set = set()
        if remove_striked:
            for key, (text, row_id) in ica_striked.items():
                if row_id and await api.remove_item(row_id):
                    _LOGGER.info("🧹 Rensade avbockad ICA-rad '%s'", text)
                explicit_removes.add(key)

            for key, summary in todo_completed.items():
                await hass.services.async_call(
                    "todo", "remove_item",
                    {"entity_id": todo_entity, "item": summary},
                )
                _LOGGER.info("🧹 Rensade avklarad HA-item '%s'", summary)
                explicit_removes.add(key)

            # För items som är explicit klara på ena sidan men aktiva på den andra:
            # ta även bort dem från den andra sidan (det är hela poängen med "done").
            for key in list(explicit_removes):
                if key in ica_active:
                    text, row_id = ica_active[key]
                    if row_id and await api.remove_item(row_id):
                        _LOGGER.info("❌ Tog bort '%s' från ICA (klar i HA)", text)
                    ica_active.pop(key, None)
                if key in todo_active:
                    summary = todo_active[key]
                    await hass.services.async_call(
                        "todo", "remove_item",
                        {"entity_id": todo_entity, "item": summary},
                    )
                    _LOGGER.info("🗑️ Tog bort '%s' från HA (avbockad i ICA)", summary)
                    todo_active.pop(key, None)
        else:
            # Striked/completed räknas som närvarande i sin egen källa
            for key, (text, row_id) in ica_striked.items():
                ica_active.setdefault(key, (text, row_id))
            for key, summary in todo_completed.items():
                todo_active.setdefault(key, summary)

        cur_ha: set = set(todo_active.keys())
        cur_ica: set = set(ica_active.keys())

        # --- Ladda snapshot och beräkna diff -----------------------------
        snap = await _load_snapshot(hass)
        snap -= explicit_removes  # explicit removes räknas inte längre som synkade

        add_to_ica: list = []
        add_to_ha: list = []
        remove_from_ica: list = []
        remove_from_ha: list = []

        for key in (cur_ha | cur_ica | snap) - explicit_removes:
            in_ha = key in cur_ha
            in_ica = key in cur_ica
            in_snap = key in snap

            if in_ha and in_ica:
                continue
            if in_ha and not in_ica:
                if in_snap:
                    remove_from_ha.append((key, todo_active[key]))
                else:
                    add_to_ica.append((key, todo_active[key]))
            elif in_ica and not in_ha:
                text, row_id = ica_active[key]
                if in_snap:
                    if row_id:
                        remove_from_ica.append((key, text, row_id))
                else:
                    add_to_ha.append((key, text))

        # --- ICA-taket: begränsa add_to_ica om vi närmar oss MAX_ICA_ITEMS
        projected_ica_count = (
            len(cur_ica) + len(add_to_ica) - len(remove_from_ica)
        )
        if projected_ica_count > MAX_ICA_ITEMS:
            allowed = MAX_ICA_ITEMS - (len(cur_ica) - len(remove_from_ica))
            allowed = max(0, allowed)
            if allowed < len(add_to_ica):
                _LOGGER.warning(
                    "⚠️ Tillägg överskrider ICA-tak (%d); begränsar från %d till %d",
                    MAX_ICA_ITEMS, len(add_to_ica), allowed,
                )
                add_to_ica = add_to_ica[:allowed]

        # --- Applicera operationer ---------------------------------------
        for key, text in add_to_ica:
            if await api.add_to_list(list_id, text):
                _LOGGER.info("📥 Lade till '%s' i ICA", text)
                cur_ica.add(key)

        for key, text, row_id in remove_from_ica:
            if await api.remove_item(row_id):
                _LOGGER.info("❌ Tog bort '%s' från ICA (saknas i HA)", text)
                cur_ica.discard(key)

        for key, text in add_to_ha:
            await hass.services.async_call(
                "todo", "add_item",
                {"entity_id": todo_entity, "item": text},
            )
            _LOGGER.info("✅ Lade till '%s' i HA-todo", text)
            cur_ha.add(key)

        for key, summary in remove_from_ha:
            await hass.services.async_call(
                "todo", "remove_item",
                {"entity_id": todo_entity, "item": summary},
            )
            _LOGGER.info("🗑️ Tog bort '%s' från HA-todo (saknas i ICA)", summary)
            cur_ha.discard(key)

        # --- Spara snapshot = items som finns på BÅDA sidor efter ops ---
        new_snap = cur_ha & cur_ica
        await _save_snapshot(hass, new_snap)
        _LOGGER.debug(
            "💾 Snapshot uppdaterad: %d items (ha=%d, ica=%d)",
            len(new_snap), len(cur_ha), len(cur_ica),
        )

        await _trigger_sensor_update(hass, list_id)
    except Exception as e:
        _LOGGER.exception("💥 Fel under _perform_sync: %s", e)
    finally:
        hass.data[DOMAIN]["sync_in_progress"] = False


async def async_setup(hass, config):
    return True


async def async_setup_entry(hass, entry):
    _LOGGER.debug("⚙️ ICA Shopping initieras via UI config entry")
    session_id = entry.options.get("session_id", entry.data["session_id"])
    list_id = entry.options.get("ica_list_id", entry.data["ica_list_id"])
    api = ICAApi(hass, session_id=session_id)
    hass.data.setdefault(DOMAIN, {})[DATA_ICA] = api
    hass.data[DOMAIN]["current_list_id"] = list_id
    hass.data[DOMAIN]["sync_in_progress"] = False

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

    # --- refresh-tjänst ----------------------------------------------
    async def handle_refresh(call):
        _LOGGER.debug("🔄 ICA refresh triggered via service")
        await _perform_sync(hass, api, entry)

    hass.services.async_register(DOMAIN, "refresh", handle_refresh)

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
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

    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])

    if hass.services.has_service(DOMAIN, "refresh"):
        hass.services.async_remove(DOMAIN, "refresh")

    if DOMAIN in hass.data:
        hass.data[DOMAIN].pop("unsub_listener", None)
        hass.data[DOMAIN].pop("debounce_unsub_getter", None)
        hass.data[DOMAIN].pop("sync_in_progress", None)

    return unload_ok


async def _options_update_listener(hass, entry):
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
        await _save_snapshot(hass, set())

    hass.data[DOMAIN]["current_list_id"] = new_list_id
    await hass.config_entries.async_reload(entry.entry_id)
