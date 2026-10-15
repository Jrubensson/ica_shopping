"""Run the integration's ICA login against the real ICA servers, outside Home Assistant.

Uses Home Assistant's real aiohttp helpers, so session quirks behave as in HA.

    uv run --python 3.13 --with homeassistant scripts/test_ica_login.py
    uv run --python 3.13 --with homeassistant scripts/test_ica_login.py --dry-run

--dry-run stops before credentials are sent. --write-test adds, strikes, unstrikes and removes a probe item.
--recipes fetches 2 random recipes and your favorites and prints the merged ingredients, without writing anything.
--extras prints the bonus and the list items on offer at your favourite store.
Credentials are prompted for and never stored.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import json
import logging
import sys
import tempfile
import types
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from homeassistant.core import HomeAssistant

INTEGRATION_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "ica_shopping"


PKG = "ica_shopping_under_test"


def load_ica_api():
    # Import ica_api without running the integration's __init__ (which needs a full HA setup).
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(INTEGRATION_DIR)]
    sys.modules[pkg.__name__] = pkg
    return importlib.import_module(f"{pkg.__name__}.ica_api")


async def recipe_test(api) -> None:
    recipes_mod = importlib.import_module(f"{PKG}.recipes")
    const = importlib.import_module(f"{PKG}.const")

    print("Fetching 2 random recipes …")
    recipes = await recipes_mod.async_fetch_recipes(api, 2, recipes_mod.SOURCE_RANDOM)
    assert recipes, "no random recipes returned"
    for r in recipes:
        d = recipes_mod.dinner_summary(r)
        print(f"  • {d['title']}  (cookingTime={d['cooking_time']!r})")
        print(f"    url: {d['url']}")
    print(f"  recipe fields: {sorted(recipes[0])}")
    url_fields = [k for k in ("url", "absoluteUrl", "shareUrl", "webUrl") if recipes[0].get(k)]
    print(f"  url source: {url_fields[0] if url_fields else 'built from title (open it to check)'}")

    skip = {recipes_mod.norm(n) for n in const.DEFAULT_NEVER_ADD}
    merged = recipes_mod.aggregate_ingredients(recipes)
    print(f"Merged ingredients ({len(merged)}):")
    for ing in merged:
        mark = "  (never add)" if recipes_mod.norm(ing["name"]) in skip else ""
        print(f"  - {ing['text']}{mark}")
    assert merged, "recipes had no ingredients"
    print("✓ Random recipes work.")

    print("Fetching favorite recipes …")
    ids = await api.async_get_favorite_recipe_ids()
    print(f"  {len(ids)} favorite(s).")
    if ids:
        fav = await api.async_get_recipe(ids[0])
        print(f"  • {fav.get('title')}  ({len(list(recipes_mod._ingredients(fav)))} ingredients)")
        assert fav.get("title"), "favorite recipe detail had no title"
        print("✓ Favorites and recipe detail work.")
    else:
        print("  (save a recipe in the ICA app to test source: favorites)")


def trace_config() -> aiohttp.TraceConfig:
    tc = aiohttp.TraceConfig()

    async def on_end(session, ctx, params):
        url = urlsplit(str(params.url))
        location = params.response.headers.get("Location")
        hop = f" -> {urlsplit(location).path}" if location else ""
        print(f"  {params.method:6} {url.netloc}{url.path}  {params.response.status}{hop}")

    async def on_redirect(session, ctx, params):
        url = urlsplit(str(params.url))
        print(f"  {params.method:6} {url.netloc}{url.path}  {params.response.status} (followed)")

    tc.on_request_end.append(on_end)
    tc.on_request_redirect.append(on_redirect)
    return tc


async def main(dry_run: bool, write_test: bool, recipes: bool, extras: bool) -> int:
    ica_api = load_ica_api()

    import homeassistant.helpers.aiohttp_client as ha_client

    # HA's resolver needs the network/zeroconf components; everything else about the session stays real.
    class _Resolver(aiohttp.ThreadedResolver):
        async def real_close(self) -> None:
            await self.close()

    ha_client._async_make_resolver = lambda hass: _Resolver()

    tracing = trace_config()
    real_create = ha_client.async_create_clientsession

    def traced_create(hass, *args, **kwargs):
        kwargs.setdefault("trace_configs", [tracing])
        return real_create(hass, *args, **kwargs)

    ica_api.async_create_clientsession = traced_create
    shared: dict = {}

    def shared_session(hass):
        # Mirrors HA's single shared session instead of opening one per request.
        if "session" not in shared:
            shared["session"] = traced_create(hass, auto_cleanup=False)
        return shared["session"]

    ica_api.async_get_clientsession = shared_session

    if dry_run:
        username, password = "000000000000", ""
    else:
        username = input("Personnummer (YYYYMMDDXXXX): ").strip()
        password = getpass.getpass("ICA password (hidden): ")

    with tempfile.TemporaryDirectory() as config_dir:
        hass = HomeAssistant(config_dir)
        api = ica_api.ICAApi(hass, username, password)

        if dry_run:
            orig_post = aiohttp.ClientSession.post

            def guarded_post(self, url, *a, **kw):
                if str(url) == ica_api.LOGIN_ENDPOINT:
                    print("\n✓ Dry run OK: reached the credential step without errors.")
                    raise SystemExit(0)
                return orig_post(self, url, *a, **kw)

            aiohttp.ClientSession.post = guarded_post
            api.password = "dry-run"

        print("Logging in to ICA …")
        try:
            await api.async_login()
        except ica_api.IcaGeoBlockedError as err:
            print(f"\n✗ Geo-blocked: {err}")
            return 1
        except ica_api.IcaAuthError as err:
            print(f"\n✗ ICA rejected the login: {err}")
            return 1
        except ica_api.IcaConnectionError as err:
            print(f"\n✗ Network/server error: {err}")
            return 1

        expires = api.token_expires_at
        print(f"\n✓ Logged in. Access token valid for ~{int(expires - __import__('time').time())} s.")

        print("Testing token refresh …")
        await api.async_get_access_token(force_refresh=True)
        print("✓ Refresh works.")

        print("Fetching shopping lists …")
        lists = await api.async_get_lists()
        for lst in lists:
            print(f"  • {lst['name']}  ({len(lst['rows'])} rows)  id={lst['id']}")
        print(f"✓ {len(lists)} list(s) found.")

        if write_test and lists:
            target = lists[0]
            probe = f"ica-shopping test {uuid.uuid4().hex[:6]}"
            print(f"Adding '{probe}' to {target['name']} …")
            assert await api.add_to_list(target["id"], probe), "add failed"
            after_add = await api.async_get_list(target["id"])
            row = next((r for r in after_add["rows"] if r["text"] == probe), None)
            assert row, "added row not found on re-fetch"
            print(f"✓ Added (row id {row['id']}).")

            async def probe_row():
                fresh = await api.async_get_list(target["id"])
                return next((r for r in fresh["rows"] if r["id"] == row["id"]), None)

            try:
                print("Striking it over …")
                assert await api.set_striked(target["id"], row["id"], True, row=row["raw"]), "strike failed"
                struck = await probe_row()
                assert struck and struck["isStriked"], "row not struck over on re-fetch"
                assert struck["text"] == probe, f"strike changed the text to {struck['text']!r}"
                print("✓ Struck over.")
                print("Un-striking it (row looked up by id) …")
                assert await api.set_striked(target["id"], row["id"], False), "unstrike failed"
                unstruck = await probe_row()
                assert unstruck and not unstruck["isStriked"], "row still struck over on re-fetch"
                print("✓ Un-struck.")
            except AssertionError:
                await api.remove_item(target["id"], row["id"])
                print("(probe row removed after the failure)")
                raise
            print("Removing it again …")
            assert await api.remove_item(target["id"], row["id"]), "remove failed"
            after_remove = await api.async_get_list(target["id"])
            assert not any(r["text"] == probe for r in after_remove["rows"]), "row still present"
            print("✓ Removed. Add, strike, unstrike and remove all work.")

        if recipes:
            await recipe_test(api)

        if extras:
            await print_extras(ica_api, api, lists)
        if "session" in shared:
            shared["session"].detach()
        await hass.async_stop(force=True)
    return 0


async def print_extras(ica_api, api, lists) -> None:
    print("Fetching bonus …")
    try:
        raw = await api.async_get_bonus()
        print(f"  response keys: {sorted(raw)}")
        print(f"  accountBalance: {json.dumps(raw.get('accountBalance'), ensure_ascii=False)}")
        print(f"  discountSummary: {json.dumps(raw.get('discountSummary'), ensure_ascii=False)}")
        bonus = ica_api.parse_bonus(raw)
        print(f"✓ Bonus sensor state: {bonus['total_voucher_value']} SEK")
        for key, value in bonus.items():
            print(f"    {key}: {value}")
    except ica_api.IcaError as err:
        print(f"✗ Bonus failed: {err}")

    print("Fetching favourite stores …")
    try:
        stores = await api.async_get_favorite_stores()
    except ica_api.IcaError as err:
        print(f"✗ Stores failed: {err}")
        return
    for store in stores:
        print(f"  • {store['name']} ({store['city']})  id={store['id']}")
    if not stores:
        print("✗ No favourite stores; add one in the ICA app.")
        return

    store = stores[0]
    print(f"Fetching offers at {store['name']} …")
    try:
        offers = await api.async_get_store_offers(store["id"])
    except ica_api.IcaError as err:
        print(f"✗ Offers failed: {err}")
        return
    print(f"✓ {len(offers)} offer(s).")
    for offer in offers[:3]:
        print(f"    e.g. {ica_api.format_offer(offer)}")

    for lst in lists:
        matches = ica_api.offers_on_list(lst["rows"], offers)
        print(f"On-sale sensor for '{lst['name']}': {len(matches)} item(s)")
        for match in matches:
            deals = "; ".join(
                f"{o['name']} ({o['brand'] or '-'}) {o['deal'] or ''}".strip() for o in match["offers"]
            )
            print(f"  • {match['item']}: {deals}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="stop before sending credentials")
    parser.add_argument("--write-test", action="store_true",
                        help="add a probe item to your first list, strike and unstrike it, then remove it")
    parser.add_argument("--recipes", action="store_true",
                        help="fetch 2 random recipes and print the merged ingredients (read-only)")
    parser.add_argument("--extras", action="store_true",
                        help="print the bonus and the list items on offer at your first favourite store")
    parser.add_argument("-v", "--verbose", action="store_true", help="show the integration's debug logs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    sys.exit(asyncio.run(main(args.dry_run, args.write_test, args.recipes, args.extras)))
