# 🛒 ICA Shopping – Home Assistant Integration

Sync your ICA shopping lists with Home Assistant and your shopping/todo list in HA.

The integration performs **bi-directional sync** between your configured HA todo entity and your ICA list:

- Items added on either side are added to the other.
- Items removed on either side are removed from the other.
- Checking an item off in the ICA app marks it completed in HA, and completing it in HA checks it off in ICA. Un-checking works the same way.

Checked items stay on both lists until you clear them, with the `ica_shopping.clear_checked` action or the **Clear checked items after every sync** option.

## Limitations at the Moment

**ICA API:**  
Changes made to your ICA shopping list (e.g. via the ICA app or website) will **not** appear immediately in Home Assistant.  
The integration does **not** support real-time updates, so you’ll need to manually trigger a refresh to fetch the latest version of the list.


## Installation via HACS

Add Custom Repository:

Open HACS in Home Assistant.
Click on the ... in the top right corner and select Custom repositories.
Add the URL https://github.com/jrubensson/ica_shopping/
Set the category to Integration and click Add.
Download the Integration and restart Home Assistant.

Configure Integration:

1. Go to **Settings → Devices & Services**.
2. Click **Add Integration** and select **ICA Shopping**.
3. Log in with your **personnummer** and the **password** you use in the ICA app.
4. Pick the ICA shopping list to sync from the dropdown.
5. Link a `todo` entity to sync with (any HA todo entity — the built-in shopping list, a custom todo integration, etc.).

## Authentication

The integration logs in the same way the ICA mobile app does (OAuth with personnummer + password) and keeps a refresh token in the config entry. Access tokens are renewed automatically, so there is nothing to paste or update by hand.

Requirements:
- **A password on your ICA account.** Accounts that only log in with BankID are not supported — set a password at [ica.se](https://www.ica.se) first.
- **A Swedish IP address.** ICA's API gateway rejects requests from outside Sweden (HTTP 451).

If ICA ever rejects the stored login (e.g. you changed your password), Home Assistant shows a **Reconfigure / re-authenticate** prompt under Settings → Devices & Services; enter your personnummer and password again.

### Upgrading from 3.x (session ID)

Version 4 drops the pasted `thSessionId` cookie. After upgrading, the integration asks you to re-authenticate with personnummer and password. If your configured list can't be found on the account, you'll be asked to pick it from a dropdown.

## Clearing Checked Items

`ica_shopping.clear_checked` syncs first, then deletes every checked-off row from the ICA list and every completed item from the to-do list. Run it from a dashboard button or after you leave the store:

```yaml
actions:
  - action: ica_shopping.clear_checked
```

To clear automatically, turn on **Clear checked items after every sync** under Settings → Devices & Services → ICA Shopping → Configure. Checked items are then removed on the next sync, which is how older versions behaved.

### Upgrading from earlier versions

The old **Remove checked and completed items** option deleted items as soon as they were checked. It is now **Clear checked items after every sync**, and upgrading turns it off so checked items sync as checked. Turn it back on if you prefer the old behavior.

## Example Automation (ICA Refresh)

Since ICA does not push updates, you can refresh the list every X minutes:

```yaml
automation:
  - alias: "Sync ICA List Every 10 Minutes"
    trigger:
      - platform: time_pattern
        minutes: "/10"
    action:
      - service: ica_shopping.refresh
```

You can also create an automation that refreshes the list when you enter or leave a store.
This only works for ICA stores you've added as zones in Home Assistant.
Purchases made in other locations won't trigger any update.

```yaml
alias: Ica shopping update
description: ""
triggers:
  - trigger: zone
    entity_id: device_tracker.max
    zone: zone.ica
    event: leave
  - trigger: zone
    entity_id: device_tracker.max
    zone: zone.ica
    event: enter
conditions: []
actions:
  - action: ica_shopping.refresh
    data: {}
mode: single

```

## Planning Dinners from ICA Recipes

`ica_shopping.plan_dinners` picks ICA recipes, merges their ingredients (`2 dl` + `3 dl mjölk` → `5 dl mjölk`) and adds them to a shopping list.

| Field | Default | |
|---|---|---|
| `count` | `5` | Number of dinners, 1–10 |
| `source` | `random` | `random` ICA recipes, or `favorites` saved in the ICA app |
| `list` | the synced list | ICA list name; a name that doesn't exist is created |
| `calendar` | the **Default calendar** option | Another calendar entity; also adds one all-day event per dinner there |
| `start_date` | tomorrow | Earliest day for the first dinner; each dinner goes on the next day that doesn't already have one |
| `preview` | `false` | Return the menu and merged ingredients without changing the list |
| `add_to_list` | `true` | Add the ingredients to the shopping list. Set `false` to only plan: the dinners still go in the dinner memory and calendar |
| `match_products` | `false` | Swap volume and weight amounts for an ICA product and package count, e.g. `4.5 dl vispgrädde` → `Vispgrädde 36% 5dl Skånemejerier`. See [Matching products](#matching-products) |
| `recipes` | – | Recipe ids to use, e.g. `"123,456"`. Overrides `count` and `source`, so a previewed menu can be committed exactly |
| `items` | – | Only put these ingredients on the list: names from a preview's `ingredient_groups`, as a list or one per line. See [Adding only some ingredients](#adding-only-some-ingredients) |
| `avoid_days` | `0` | Skip recipes eaten within this many days (uses the dinner memory) |

Ingredients already on the list are skipped, as are pantry staples listed under **Never add from recipes** in the integration's options (default: salt, peppar, svartpeppar, vatten, olja, olivolja, smör).

Committed dinners always appear in the integration's own **Dinners** calendar (see [Dinner Calendar](#dinner-calendar)). To have them in another calendar too, set `calendar` or pick a **Default calendar** in the integration's options; the Assist tool uses that option. Each event there has the recipe title as its name and the cooking time, recipe link and ingredients in its description. A `calendar` you pass must be available and support creating events (Local Calendar, Google Calendar and CalDAV do); otherwise the action fails before anything is added to the ICA list. The **Default calendar** option only offers calendars that can create events, and if it is unavailable or no longer exists when you plan, it is skipped with a warning in the log, so planning still works.

This automation plans Monday–Friday every Sunday morning, puts the dinners in a calendar and sends the menu to your phone:

```yaml
automation:
  - alias: "Weekly dinner plan"
    trigger:
      - trigger: time
        at: "09:00:00"
    condition:
      - condition: time
        weekday: sun
    action:
      - action: ica_shopping.plan_dinners
        data:
          count: 5
          source: favorites
          calendar: calendar.dinners
        response_variable: plan
      - action: notify.mobile_app_your_phone
        data:
          title: "This week's dinners"
          message: >
            {% for d in plan.dinners %}• {{ as_datetime(d.date).strftime('%a') }}: {{ d.title }}{% if d.cooking_time %} ({{ d.cooking_time }}){% endif %}
            {% endfor %}
            {{ plan.added | count }} items added to {{ plan.list }}.
```

The response looks like `{"list": "Handla", "preview": false, "dinners": [{"id": 719920, "title": …, "cooking_time": …, "url": …, "ingredients": […], "date": "2026-10-19"}], "ingredients": ["5 dl mjölk", …], "added": ["5 dl mjölk", …], "skipped_list_full": []}`; `date` is left out and `added` is empty when `preview` is true.

When `preview` is true or `add_to_list` is false, `list` is `null`, `added` is empty and the response also has `ingredient_groups`: one entry per ingredient across the menu, e.g. `{"name": "vispgrädde", "text": "4.5 dl vispgrädde", "lines": ["3 dl vispgrädde", "1,5 dl vispgrädde"], "dinners": ["Pasta", "Soppa"]}`. `text` is the summed amount when every line shares a unit, otherwise the lines joined with ` + `. Items on the never-add list are left out. In a preview, a group that a row on the list already covers says which, e.g. `"on_list": "Mjölk"`, using the same word matching as planning (see [Dinners and the shopping list](#dinners-and-the-shopping-list)); it is left out when no single row covers everything the plan would add for it.

### Adding only some ingredients

Pass `items` with the `name`s of the `ingredient_groups` to put on the list, e.g. after a screen has let you untick what you already have. Only those go on the list; the rest are left off this time (they are not added to the never-add list). With `match_products`, a chosen ingredient goes on as its product, and only the chosen ones are looked up, so a product two ingredients share is counted for the chosen ones only. Each dinner's `on_list` and `put_on_list` note the rows it actually got, so `remove_dinner`, `get_list_dinners` and `update_list` work as for a full plan. Names that match no group are returned in `items_not_found`. Leave `items` out to add everything; an empty list adds nothing.

It works for dinners that are already planned: commit them first with `add_to_list: false`, and shop later with the same `recipes`, `add_to_list: true` and `items`. Coming dinners already in the memory — from today on, or from `start_date` when it is given — keep their day and get no second calendar event; they just get the rows noted. Only send the dinners still to come: a recipe whose day has passed is planned again on the next free day. Each dinner remembers the ingredients left off it, so a later recipe change with `update_list` doesn't put them back on the list, and `remove_dinner` doesn't keep a row for a dinner that has that ingredient at home.

```yaml
- action: ica_shopping.plan_dinners
  data:
    recipes: "719920,722011"
    match_products: true
    items: ["vispgrädde", "pasta"]
  response_variable: plan
# plan.added: ["Vispgrädde 36% 5dl (4.5 dl vispgrädde)", …]
# plan.dinners[0].put_on_list: the rows that dinner got, which remove_dinner takes off again
```

### Matching products

With `match_products: true`, each ingredient with an amount is looked up in ICA's product search at your favourite store (the one picked in the options, otherwise the first in the ICA app). The list then gets the product and how many packages to buy, with the recipe amount after it:

| Recipe amount | On the list |
|---|---|
| `4.5 dl vispgrädde` (3 dl + 1,5 dl from two dinners) | `Vispgrädde 36% 5dl Skånemejerier (4.5 dl vispgrädde)` |
| `10 dl mjölk` | `Mjölk 3% 1l ICA (10 dl mjölk)` |
| `250 g champinjoner` | `Champinjoner 400g Klass 1 ICA (250 g champinjoner)` |
| `400 g krossade tomater` | `Tomater krossade 500g ICA Basic (400 g krossade tomater)` |
| `65 g mâchesallad` | `Finbladig Machesallat Sköljd 65g ICA (65 g mâchesallad)` |
| `2 msk tomatpuré` | `Tomatpuré Triplo 185g Mutti (2 msk tomatpuré)` |
| `1 tsk oregano` | `Oregano 5g Santa Maria (1 tsk oregano)` |
| `0.5 tärning kycklingbuljong` | `Kycklingbuljong 12-p Bong (0.5 tärning kycklingbuljong)` |
| `4 port spaghetti` | `Spaghetti 500g Zeta (4 port spaghetti)` |
| `1 gurka` | `Gurka 1-pack Klass 1 ICA (1 gurka)` |
| `3 vitlöksklyftor` | `Vitlök färsk 80g Klass 1 ICA (3 vitlöksklyftor)` |

The amount shows why the product is there, and keeps a second plan's row apart from the first when both pick the same product, so neither gets lost.

How a product is picked:

- Its name starts with the ingredient, so `Kokosmjölk` never stands in for mjölk, nor `Mikropopcorn med smör` for smör, and it is in the ICA category most of the matching products share. Accents don't count (`mâche` finds `Mache`), a plural finds its singular (`gula lökar` finds `Gul lök`), and a describing word in front (`Finbladig Machesallat`) is only accepted when no product is plainly named after the ingredient.
- Lactose-free, soured, flavoured, light, organic, frozen, smoked, cured, spray and long-life variants are only picked when the recipe asks for them. Tinned products only count when nothing fresh fits.
- Its size is in the same unit kind, and it isn't far bigger than needed: no 5 kg sack for 300 g.
- Then the fewest packages, then a package close to the amount, then ICA's own ranking. When one package isn't enough the row says how many, e.g. `2 × Kycklingfilé Färsk Minutstrimlor 550g Kronfågel (600 g kycklingfilé)`. Ingredients that land on the same product are counted together.

Amounts the shop doesn't sell by get the smallest normal package of that product, never a catering pack over 2 kg:

- Spoons, pinches and cloves (`msk`, `tsk`, `krm`, `nypa`, `klyfta`, `kvist`, `blad`, …): one package, or more only when the amount needs it.
- Portions (`port`) count as 100 g each, so `4 port spaghetti` is one 500 g pack and `8 port` one 1 kg pack, or two 500 g packs when that's all there is.
- Pieces, slices and cubes (`1 gurka`, `4 bitar laxfilé`, `3 tärningar`): a pack that states how many it holds is preferred and counted against it (`3 tärningar` from a 6-pack is one pack, `4 rödlökar` two 2-packs); otherwise a loose item or the smallest bag, once, with the recipe amount in the text.
- Packages (`2 förp`, `1 burk`): that many of the smallest package. A pot or bunch of herbs (`1 kruka basilika`) is never matched to a dried jar.

Ingredients with an amount the matcher doesn't know (`1 sats`), those with no fitting product, those whose search is slow or keeps being rate-limited by ICA, and everything when the favourite store can't be found keep their recipe text. In that last case the response says why: `"products_skipped": "no_store"` when no favourite store is set in the ICA app, or `"store_lookup_failed"` when ICA couldn't be reached for it.

The response gets a `products` list with each swap — `ingredient`, `needed` (the recipe amount), `product`, `count`, `text` (what goes on the list), `product_id`, `url`, `image` and `pantry` — so a preview shows what will be added. `pantry` is `true` for spices, dried herbs, stock, tomato purée, oil, flour, sugar and salt, which most kitchens already have, so a UI can leave them unticked; they are still added to the list unless you leave them out of `items` when you commit, or put staples you always have in the never-add list. Pass `match_products: true` again when you commit; search results are kept for 30 minutes, so the commit picks the same products as the preview. `ingredient_groups` keeps the recipe amounts. The search uses ica.se's anonymous access, like `search_recipes`, and doesn't hold up the shopping-list sync while it runs.

### Preview, then commit what you picked

Set `preview: true` to get a menu and its merged ingredients **without touching the list**. Each dinner in the response includes its `id` and its own `ingredients`, so you can show the suggestion, swap dishes, and only commit when it looks right. Pass the chosen ids back in `recipes` — `count` and `source` are then ignored, and exactly that menu is added:

```yaml
- alias: "Suggest a menu"
  action: ica_shopping.plan_dinners
  data:
    preview: true
    count: 8
    source: random
  response_variable: preview

# … pick the dinners you like from preview.dinners[].id …

- alias: "Add the two we agreed on"
  action: ica_shopping.plan_dinners
  data:
    recipes: "719920,719907"
```

An ingredient whose only row on the list is checked off is unchecked instead of added again. `skipped_list_full` lists ingredients left out because the ICA list reached its 250-row limit (checked rows count too); run `ica_shopping.clear_checked` to make room.

## Dinner Memory

The integration remembers what you have eaten and what you prefer, so `plan_dinners` can avoid repeats and an assistant can answer questions about past dinners. It is kept in Home Assistant's `.storage`, is included in backups, and survives restarts.

- **History** — every dinner `plan_dinners` adds is recorded automatically on the day it is planned for (recipe id, title, date, list, cooking time, link), trimmed to the last 300 dinners / 120 days. Planned dinners count as eaten for `avoid_days`.
- **Preferences** — free-text rules such as `Vardagar: vegetariskt`, for the assistant to follow.
- **Food rules** — ingredients the household avoids, which `plan_dinners` applies by itself. See [Food rules](#food-rules).
- **Your own versions** — how the household changes an ICA recipe. See [Your own version of a recipe](#your-own-version-of-a-recipe).
- **`avoid_days`** — set it on `plan_dinners` to skip recipes eaten recently; `14` is a good default for a weekly plan. It works for both `random` and `favorites` (`favorites` is filtered on the known recipe ids).

| Action | |
|---|---|
| `ica_shopping.get_memory` | Returns `{history: […], preferences: […], food_rules: […], variants: {…}}` |
| `ica_shopping.set_preference` | Remember a rule (`text`) |
| `ica_shopping.remove_preference` | Forget a rule (matched on the text) |
| `ica_shopping.add_dinner` | Save a dinner of your own (`title`, `date`, optional `note` and `url`) |
| `ica_shopping.forget_dinner` | Remove a dinner by recipe id/title (`recipe`) and/or `date` |
| `ica_shopping.save_recipe` | Save an own recipe (`title`, `ingredients`, `steps`, optional `note`, `cooking_time`, `portions`, `url`, `image`, `image_search`), or change one (`recipe`) |
| `ica_shopping.remove_recipe` | Remove an own recipe by id or title (`recipe`) |
| `ica_shopping.get_own_recipes` | Returns `{recipes: […]}`: the own recipes with their ingredients and steps |
| `ica_shopping.remove_dinner` | Remove the dinner on `date`, and the list rows only it needed (`update_list`, default true) |
| `ica_shopping.move_dinner` | Move the dinner on `date` to `to`; a dinner already there swaps places with it |
| `ica_shopping.get_list_dinners` | Returns `{dinners: [{id, title, date, on_list: […]}]}`: the coming dinners and the list rows each needs |
| `ica_shopping.clear_memory` | Empty the history and/or preferences (`history`, `preferences` booleans); `preferences` also clears the food rules and your own versions |

Use `forget_dinner` to correct a wrong entry (say you cooked something else) and `clear_memory` to start over. `forget_dinner` matches the recipe id exactly or as part of the title, and can be narrowed to one date.

```yaml
- action: ica_shopping.plan_dinners
  data:
    count: 5
    avoid_days: 14
  response_variable: plan
```

Only committed plans are remembered — `preview: true` never touches the memory.

### Food rules

A food rule is an ingredient the household avoids, at one of two levels:

| Level | `plan_dinners` suggestions | In the dinners that are planned |
|---|---|---|
| `omit` (Hellre utan) | Passes by dishes **built around** it: Löksås och fläsk for `lök`, a räkpasta or skagenröra for `räkor` | Left out, and kept off the shopping list: a köttfärssås is planned without its onion |
| `never` (Aldrig) | Passes by **every** recipe that has it | Left out, should it be in a dish you asked for |

A dish is built around an ingredient when its title names it, or when ICA tags the recipe with it as its protein or kind of dish (`"Protein": "Räkor"`). ICA's vegetable tags don't count, since richly tagged recipes list nearly every vegetable in them. The tags come from the recipe's ica.se page, and are only read when the title doesn't settle it.

**A dish you ask for is always planned.** When `recipes` names it, the rules never pass it by. A rule the dish is built around is kept for that dinner (the räkor stay in the räkpasta you asked for, listed in `keep`), and the rest are left out as usual. The response lists those dishes in `rule_conflicts` (`id`, `title`, `ingredient`, `level`).

Swedish compound words need care, so a rule has `terms`, the word stems that count, and `except`, words that contain one but are something else:

```yaml
- action: ica_shopping.set_food_rule
  data:
    ingredient: lök
    level: omit
    terms: lök            # gul lök, rödlök, gula lökar, hackad lök …
    except: vitlök, gräslök, purjolök
```

`terms` default to the ingredient without a plural ending (`räkor` → `räk`, which also finds räkpasta). Give them when the plural changes the word: `morot` needs `morot, morött`. `remove_food_rule` (`ingredient`) forgets a rule.

Ingredients that hold a dish together are better as a free-text preference. Egg in pancake batter can't be left out the way onion in a sauce can, so `Frun: gillar inte ägg` lets the assistant judge each dish instead.

Each planned dinner in the response carries `omitted` (lines left out) and `extra` (lines your own version adds). The full recipe is kept, so the Dinners calendar shows the changes under the ingredients, worked out from the rules as they are now. A rule added later shows on dinners already planned:

```text
• 500 g köttfärs
• 1 gul lök
• 2 morötter

Utan: 1 gul lök (lök)
Utan: 2 morötter (morot)
Lägg till: 1 tsk chili
Vår version
```

### Your own version of a recipe

`ica_shopping.set_recipe_variant` saves how the household cooks an ICA recipe: `omit`, ingredient lines to leave out exactly as the recipe writes them, and `add`, lines to add (lists, or one line per line). There's also an optional `note`. It replaces the earlier version, and is applied whenever the recipe is planned: the left-out lines stay off the shopping list, and the added ones go on it. `remove_recipe_variant` (`recipe`) goes back to ICA's recipe.

```yaml
- action: ica_shopping.set_recipe_variant
  data:
    recipe: "720707"
    add:
      - 1 tsk chiliflakes
```

### Your own dinners

`ica_shopping.add_dinner` saves a dinner that isn't an ICA recipe — leftovers, takeaway, mormors kålpudding. It goes on the given `date` in the Dinners calendar and the **Default calendar**, with the `note` and `url` in the event description, and responds with `{"id": "custom-1a2b3c4d", "title": …, "date": …}`. A day that already has a dinner is refused, as with `plan_dinners`. So is a day more than 120 days back, since the memory doesn't keep dinners that old.

Your own dinners have ids starting with `custom-` and are marked `"custom": true` in `get_memory`. They don't count for `avoid_days`, are skipped if passed to `plan_dinners`' `recipes`, and can be moved, renamed or deleted in the Dinners calendar or with `forget_dinner` like any other dinner.

```yaml
- action: ica_shopping.add_dinner
  data:
    title: Fredagstacos
    date: "2026-10-23"
    note: Glöm inte guacamolen
```

### Searching recipes

`ica_shopping.search_recipes` searches ica.se's recipes (`query`, optional `limit` 1–30, default 10) and responds with `{"recipes": [{"id", "title", "cooking_time", "url", "image", "ingredient_count"}]}`. Pass a result's `id` to `plan_dinners` as `recipes` to plan it — with `preview`, `add_to_list` and `start_date` as usual:

```yaml
- action: ica_shopping.search_recipes
  data:
    query: lasagne
    limit: 5
  response_variable: found
- action: ica_shopping.plan_dinners
  data:
    recipes: "{{ found.recipes[0].id }}"
    start_date: "2026-10-23"
```

The search uses the same anonymous access as ica.se, so it doesn't count against your ICA login.

### Favorite recipes

The `favorites` source of `plan_dinners` picks among the recipes saved with the heart in the ICA app. These actions read and change that same list, so a dashboard or an automation can save a dinner you liked:

| Action | |
|---|---|
| `ica_shopping.get_favorites` | Responds with `{"recipes": ["719659", …]}`, the favorite recipe ids |
| `ica_shopping.add_favorite` | Save a recipe (`recipe`: ICA recipe id) as a favorite |
| `ica_shopping.remove_favorite` | Remove a recipe (`recipe`) from the favorites |

`add_favorite` and `remove_favorite` respond with the updated list, in the same shape as `get_favorites`. It is the same list the ICA app reads its favorites from. Recipe ids come from `search_recipes`, `plan_dinners` and `get_memory`, and from the end of each dinner's recipe link (`…/recept/fransk-kycklinggryta-719659/`). Your own dinners (`custom-…` ids) aren't ICA recipes, so they can't be favorites.

```yaml
- action: ica_shopping.add_favorite
  data:
    recipe: "719659"
```

### Dinner Calendar

The integration adds a calendar entity, **Dinners** (e.g. `calendar.ica_handlingslista_dinners`), that shows the dinner history as all-day events — past dinners and the ones planned ahead, with the cooking time, recipe link and ingredients in each description. It reads straight from the memory, so it needs no other calendar integration, and like the memory it keeps the last 120 days.

Editing it edits the memory: move a dinner to another day, rename it, or delete it if you never cooked it. `avoid_days`, `get_memory` and the Assist tools follow along. Other changes — the description, a longer or timed event, a repeat or a location — are refused with an error rather than silently lost. New events can't be created here; dinners come from `plan_dinners` and `add_dinner`. Picking this calendar as the `calendar` of `plan_dinners` is harmless — it already shows the plan, so nothing extra is added.

If an external calendar fails part-way, the dinners are still remembered and shown here; add the missing events to the other calendar by hand rather than planning them again.

Dinners remembered before version 1.8.0 were all stored on the day they were planned, so an older weekly plan shows up as several dinners on one day. Move or delete them if they bother you; new plans get one dinner per day.

#### Own recipes

Dishes ICA has no recipe for — the soup on offer that you only heat up, a family recipe, or one the assistant writes from what you have — can be saved as the household's own recipes with `save_recipe` and planned again and again. Their ids start with `own-`. `plan_dinners` takes them in `recipes` like ICA ids, mixed with them if you like, and `source: own` picks among them. Their ingredients go on the list like any recipe's: amounts are read from lines like "2 dl grädde", so equal amounts add up with ICA's recipes, and the food rules, `never_add` and the list tracking apply as usual.

Without an `image`, `save_recipe` takes the picture of ICA's closest recipe to the title (or to `image_search`) — close enough, since a bowl of soup looks the same whoever made it. Changing a recipe also changes the dinners planned with it from today on; the shopping list stays as it is. Their calendar events start with "Eget recept (own-…)", so a dashboard can fetch the steps. `get_memory` lists the own recipes' titles and ids; `clear_memory` keeps the recipes. `add_dinner` stays for one-off dinners that aren't cooked from a recipe.

#### Dinners and the shopping list

When `plan_dinners` puts a dinner's ingredients on the list, the memory notes which rows the dinner needs, and which of those the plan put there rather than finding them on the list already. A summed row ("4.5 dl vispgrädde") belongs to every dinner it was summed from. Rows are matched by their words rather than letter for letter, so "Varmkorv" covers "8 varmkorvar" and "Mjölk" covers "2 dl mjölk (gärna 1,5 %)": planning ties the dinner to that row instead of adding the recipe's line next to it. A row has to name nothing the line doesn't ("Mjölig potatis" doesn't cover "900 g fast potatis"), a row with an amount only covers the same amount, and a plain row doesn't cover a processed kind ("Tomater" and "krossade tomater"). `get_list_dinners` returns, for each coming dinner, the rows planning noted and any other row on the to-do list that has one of its ingredients, so a dashboard can show what each row is for, also for rows typed in by hand.

`remove_dinner` uses it to clean up: the rows the plan put there come off the list again, except those another coming dinner needs, noted or by its words (they become that dinner's to remove), and those already checked off. Pass `update_list: false` to leave the list as it is. A summed row stays as it is rather than shrinking. `move_dinner` swaps two days in one go. Both change the memory, so the Dinners calendar follows; the **Default calendar** does not.

Changing a recipe can bring the list along: pass `update_list: true` to `save_recipe`, `set_recipe_variant` or `remove_recipe_variant`, and for the coming dinners of the recipe that were planned onto a list, new lines go on the list like planning puts them, and the rows a dinner put there for lines it no longer has come off, unless it or another coming dinner still needs them. A row with an amount keeps it: changing "2 dl grädde" to "3 dl grädde" leaves the row as it is. `save_recipe` with `dinner: <date>` also puts the recipe on that day's dinner in place of what is there, keeping its rows, which is how an ICA dinner becomes an own recipe to change.

## Assist tools

On Home Assistant 2026.8 and newer the integration contributes its own tools to Assist through the `llm` platform, so any voice assistant or LLM conversation agent can use them directly - no scripts, blueprints or YAML to copy. Ask *"what is on offer?"* or *"what should we eat this week?"* and the agent can answer.

| Tool | |
|---|---|
| `ica_shopping__GetOffers` | This week's offers at the favourite store, and the bonus balance |
| `ica_shopping__GetMemory` | Recently eaten dinners and the saved preferences |
| `ica_shopping__PlanDinners` | Suggest a menu from ICA recipes. `preview: true` returns it without changing the list; commit with `recipes` set to the ids you agreed on, optionally from a `start_date`. Committed dinners go in the Dinners calendar and the **Default calendar** |
| `ica_shopping__SearchRecipes` | Search ICA's recipes, e.g. when the user asks for a specific dish |
| `ica_shopping__AddDinner` | Save a dinner of the user's own, without an ICA recipe |
| `ica_shopping__SaveRecipe` | Save or change one of the household's own recipes — a ready meal from the offers, a family recipe, or one it writes |
| `ica_shopping__GetOwnRecipes` | The own recipes with ingredients and steps |
| `ica_shopping__RemoveRecipe` | Remove an own recipe |
| `ica_shopping__SetFoodRule` / `RemoveFoodRule` | Remember or drop an ingredient to avoid |
| `ica_shopping__SetRecipeVariant` / `RemoveRecipeVariant` | Remember or drop the household's own version of a recipe |
| `ica_shopping__SetPreference` / `RemovePreference` | Remember or drop a food preference that isn't about one ingredient |
| `ica_shopping__ForgetDinner` / `ClearMemory` | Correct the dinner memory |

They wrap the actions above, so they follow the same rules, and they ship with a short prompt fragment that tells the model to read the offers and the memory before suggesting meals, to plan a dish the user names ("vi äter köttfärssås ikväll") without letting the food rules stop it or commenting on them, and never to add anything before the user agrees. The tools are only offered when an assistant requests the built-in Assist API; on Home Assistant older than 2026.8 they are simply not registered and the actions keep working.

## Bonus and Offers Sensors

Two more sensors appear on the same device:

- **Bonus** — the value (SEK) of the Stammis bonus vouchers currently on your account. Attributes: `next_voucher_value`, `remaining_days`, `active_vouchers`, `total_discount`, `number_of_purchases`. Polled every 6 hours.
- **On Sale** — how many unchecked items on your list are on offer at your favourite ICA store. Attributes: `store`, `items` (the matching list items) and `matches` (each item with the offers it matched: name, brand, deal, valid to). Offers are fetched every 6 hours; the list is re-checked hourly and after every sync.

The store is taken from your favourite stores in the ICA app (the first one by default). To use another favourite, pick it under **Settings → Devices & Services → ICA Shopping → Configure**.

Items are matched on words of three or more letters, so "mjölk" matches any offer whose name contains "mjölk". Expect the odd false positive.

If ICA's bonus or offer services fail, only these two sensors become unavailable; list sync is unaffected.

### Getting the offers with `ica_shopping.get_offers`

The On Sale sensor only tells you *how many* offers exist. To get the offers themselves — for an automation or a voice assistant — call `ica_shopping.get_offers`. It returns the favourite store's current offers and the bonus balance:

```yaml
- action: ica_shopping.get_offers
  response_variable: offers
```

```json
{
  "store": "ICA Supermarket Edsbyn",
  "offer_count": 57,
  "offers": [
    { "name": "Kassler", "brand": "ICA", "deal": "69 /kg", "valid_to": "2026-10-18T23:59:59" }
  ],
  "bonus": { "total_voucher_value": 50.0, "next_voucher_value": 75.0, "remaining_days": 17 }
}
```

Offers are the store's whole assortment, not only food. The result is cached for 15 minutes, so calling it repeatedly — for example while a voice assistant refines a menu — does not hit ICA every time. The first call fetches live; the On Sale sensor keeps its own 6-hour cache.

```yaml
automation:
  - alias: "Tell me what's on sale"
    trigger:
      - platform: numeric_state
        entity_id: sensor.ica_handlingslista_on_sale
        above: 0
    action:
      - service: notify.notify
        data:
          message: >
            On sale at {{ state_attr('sensor.ica_handlingslista_on_sale', 'store') }}:
            {{ state_attr('sensor.ica_handlingslista_on_sale', 'items') | join(', ') }}
```

## Detecting Sync Failures

The integration exposes a **Sync Status** sensor whose state is `success` or `failed`, so you can be notified when a sync fails (for example if ICA is unreachable or the login needs to be renewed).

The entity id depends on your list name — check **Settings → Devices & Services → ICA Shopping → the device** to find it (e.g. `sensor.ica_handlingslista_sync_status`).

```yaml
automation:
  - alias: "Notify when ICA sync fails"
    trigger:
      - platform: state
        entity_id: sensor.ica_handlingslista_sync_status
        to: "failed"
    action:
      - service: notify.notify
        data:
          title: "ICA Shopping sync failed"
          message: >
            The ICA sync failed. Check Settings → Devices & Services
            for a re-authentication prompt.
```

> **Tip:** trigger on `to: "failed"` rather than "not success", so the automation doesn't fire on the brief `unknown` state after a Home Assistant restart.

Two related diagnostic sensors are also available on the same device:
- **Last Sync** — timestamp of the last run, with `status` and `last_successful_sync` attributes.
- **Access Token Expiry** — when the current access token expires (it is refreshed automatically before then).
