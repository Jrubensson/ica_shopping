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

Ingredients already on the list are skipped, as are pantry staples listed under **Never add from recipes** in the integration's options (default: salt, peppar, svartpeppar, vatten, olja, olivolja, smör).

The action returns the menu, so an automation can send it to your phone:

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
        response_variable: plan
      - action: notify.mobile_app_your_phone
        data:
          title: "This week's dinners"
          message: >
            {% for d in plan.dinners %}• {{ d.title }}{% if d.cooking_time %} ({{ d.cooking_time }}){% endif %}
            {% endfor %}
            {{ plan.added | count }} items added to {{ plan.list }}.
```

The response looks like `{"list": "Handla", "dinners": [{"title": …, "cooking_time": …, "url": …}], "added": ["5 dl mjölk", …]}`.

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
