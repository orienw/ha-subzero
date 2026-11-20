# Reference

Detailed behavior for entities, controls, actions, events, availability, and diagnostics. For installation, supported models, and day-to-day setup, see the [README](../README.md).

## Contents

- [Entities, temperatures, and timestamps](#entities-temperatures-and-timestamps)
- [Refrigerators and wine storage](#refrigerators-and-wine-storage)
- [Dedicated ice makers](#dedicated-ice-makers)
- [Ovens](#ovens)
- [Hoods](#hoods)
- [Dishwashers](#dishwashers)
- [Events and Firebase alerts](#events-and-firebase-alerts)
- [Actions](#actions)
- [Updates, availability, and controls](#updates-availability-and-controls)
- [Diagnostics](#diagnostics)

## Entities, temperatures, and timestamps

Entities are created automatically from recognized properties reported by each appliance, both during setup and whenever new properties appear in later updates. There is no model allowlist. Properties that do not apply to an appliance's reported type are ignored along with any entities that depend on them (for example, an unused second-cavity probe does not create probe temperature entities).

Installing, restarting, or reconnecting the integration never changes appliance settings on its own. Settings change only when a control or automation requests it.

When an appliance later reports a second oven cavity, wine zone, or kitchen timer, entity display names update to distinguish them while existing entity IDs remain stable:

- Double wall ovens use **Upper oven** and **Lower oven**.
- Ranges with two cavities use **Right oven** and **Left oven**.
- Two-zone wine storage uses **Upper wine** and **Lower wine**. Doors receive zone names only when each zone reports its own door.
- When a second kitchen timer is reported, the first timer's entities rename from **Kitchen timer** to **Kitchen timer 1**.

### Temperatures

Home Assistant displays temperatures and accepts setpoints in your preferred unit. Because appliances accept whole-degree Fahrenheit setpoints, Celsius requests can be rounded.

Temperature units for each appliance are read at startup and on reload. If the appliance list is temporarily unreachable, the integration uses the last saved unit. If an appliance's temperature unit is unknown, its non-temperature entities still work normally while temperature entities are omitted.

Configured setpoints and measured display temperatures are separate properties. Refrigerator temperature sensors show configured setpoints; display temperature sensors and climate current temperatures appear only when the appliance reports them. The integration never treats a setpoint as a measured temperature.

### Timestamps

Timestamps that include a UTC offset keep that offset. A timestamp without an offset uses the time zone inferred from the appliance's clock in live updates. When that offset matches Home Assistant's time zone, Home Assistant's zone is used so daylight saving changes are followed; otherwise the appliance's fixed offset is used. Status reads do not change the inferred time zone. Before a live update reports the appliance clock, Home Assistant's time zone is used.

## Refrigerators and wine storage

Controls follow each appliance's reported capabilities, limits, and interlocks. Climate entities (**Refrigerator**, **Refrigerator drawer**, **Freezer**, and wine zones) use `cool` mode with a 1°F step and enforce the same limits and interlocks as the corresponding setpoint number controls.

| Control | Range, options, and restrictions |
| --- | --- |
| **Refrigerator setpoint** / **Refrigerator drawer setpoint** | 34 to 42°F, 34 to 45°F, or 34 to 55°F depending on the reported appliance type (with model-based defaults if the type is unavailable). |
| **Freezer setpoint** | -5 to 5°F. Turn off **Max ice** before changing the setpoint; while **Max ice** is on, the setpoint number and **Freezer** climate entity are unavailable. |
| **Crisper setpoint** | Within 2°F of the refrigerator setpoint and within the appliance's refrigerator limits. Switch **Crisper temperature mode** to `Manual` before changing it. In `Automatic` mode, the number control is unavailable while the **Crisper setpoint** sensor continues to show the configured value. |
| **Crisper temperature mode** | `Automatic` or `Manual`. |
| **Ice maker** | `Off`, `On`, and (when reported) `Max ice` and `Night ice`. During `Night ice`, the separate **Ice maker enabled** binary sensor can show Off while its schedule pauses production. |
| **Mode** | `Normal` plus any reported operating modes for the appliance: `Sabbath`, `High use`, `Short vacation`, `Long vacation`. |
| **Humidity control** | `Normal` and `Enhanced` can be selected. `Disabled` and `Low` are displayed when reported, but can only be chosen at the appliance. Applies to the refrigerator zone. |
| **Night mode** | `Disabled` or `Enabled`. Dims interior lights when the room is dark; separate from `Night ice`. |
| **Accent light** | `Off`, `On`, `Low`, `Medium`, or `High`, when reported. |
| **Door open delay** | `Off`, `1 minute`, `2 minutes`, `5 minutes`, or `10 minutes` before an open door triggers a notification. |
| **Air purification** | On/Off switch, when reported. |
| **Internal water dispenser** | On/Off switch, when reported. |
| **Reset air filter** | Sends the reset request and refreshes status. The **Air filter remaining** sensor continues to show the appliance's reported value. |

Monitoring can include refrigerator-drawer setpoints and doors, display temperatures, air and water filter life percentages, **Water filter inserted**, **Water filter capacity remaining** in gallons, **Power** and **Service required** status, and reported **Max ice** and **High use** start and end times. A negative water-filter capacity means reported usage has exceeded the filter's rated capacity. Switches expose their own On/Off state without duplicate binary sensors; **Ice maker** and **Mode** selectors also have separate binary sensors for individual active modes.

Wine storage setpoints range from 40 to 65°F. Each reported wine zone can provide a setpoint number, setpoint sensor, climate entity, display temperature sensor, and door sensor. Wine storage can also report **Wine temperature alert** and a **Mode** selector containing the operating modes offered for that appliance type (such as `Normal` and `Sabbath`).

Sub-Zero's support articles describe [Night ice](https://www.subzero-wolf.com/assistance/answers/sub-zero/common/sub-zero-night-ice-mode), [crisper temperatures](https://www.subzero-wolf.com/assistance/answers/sub-zero/next-classic/sub-zero-classic-series-cl-refrigerator-drawer-temperature-contr), [humidity control](https://www.subzero-wolf.com/assistance/answers/sub-zero/next-classic/next-classic-humidity-control), and [Night mode](https://www.subzero-wolf.com/assistance/answers/sub-zero/next-classic/next-classic-night-mode).

## Dedicated ice makers

Dedicated ice makers provide an **Ice maker** selector (`Off` or `On`) and a **Door open delay** selector (`Off`, `1 minute`, `2 minutes`, `5 minutes`, or `10 minutes`).

The **Ice maker status** sensor reports one of four states in this priority order:

1. `Disabled` when **Ice maker failsafe** is active.
2. `Delayed` when **Ice delay active** is on.
3. `Off` when **Winterized** is active.
4. `On` or `Off` from the appliance's ice-maker power state.

To pause ice production on a schedule, call the [`subzero.schedule_ice_delay`](#subzeroschedule_ice_delay) action (1 to 12 hours, starting immediately or up to 1,439 minutes later, optionally repeating daily). Two buttons manage an active or saved delay:

- **End current ice delay** (available while a delay is active) resumes ice production immediately while keeping a repeating schedule.
- **Cancel ice delay schedule** (available when a scheduled delay duration is configured) removes the schedule completely.

Monitoring can include **Power**, **Ice maker door**, **Sabbath mode** (read-only), **Service required**, **Water filter inserted**, **Water filter remaining**, **Water filter capacity remaining**, **Ice maker failsafe**, **Winterized**, delay sensors (**Ice delay active**, **Ice delay repeats daily**, **Ice delay duration**, **Ice delay start**, **Ice delay end**), and cleaning sensors (**Ice maker cleaning**, **Ice maker cleaning due soon**, **Ice maker cleaning due**, **Ice maker cleaning stage**, **Ice cycles until cleaning**, **Ice maker next cleaning**). Cleaning steps and fault clearing must be performed at the appliance.

## Ovens

Each reported cavity has its own entities.

### Controls and Remote Ready

Changing a cavity's cooking mode or temperature requires the cavity to be running or in **Remote ready**. Ovens with knobs also refuse cooking-mode changes while **Cooking mode change enabled** is Off. Enable Remote Ready at the oven before each remote start; opening the door cancels it.

`Broil`, `Convection broil`, `Proof`, `Self clean`, and Gourmet programs must be started at the oven's control panel. These restrictions apply to both manual controls and automations.

The **Cooking mode** selector lists the modes offered for the reported oven series and cavity (for example, `Convection bake` appears on older E series ovens, which lack `Warm`, while some lower cavities omit `Convection`, `Dehydrate`, or `Bake stone`). Manual-only modes appear in the selector only while the cavity is actively running in that mode so the entity can reflect the current state. Selecting `Off` turns the cavity off. Unknown mode codes cannot be selected.

Each cavity's climate entity supports `off` and `heat` modes with a 5°F step, and reports `hvac_action` as `off`, `preheating`, or `heating` from the cavity's **Cooking** and **Preheated** states (it reports no action while preheated status is unreported). Modes without an adjustable temperature (`Broil`, `Convection broil`, and `Self clean`) omit target-temperature control while keeping on/off controls. Each cavity can also expose an **Oven light** switch.

How remote start works depends on the oven series:

- **Older E series and M series ovens:** Set the cooking mode and temperature at the oven, enable Remote Ready, and press **Start oven** (or turn the climate entity to `heat`).
- **Newer E series, M series, and other ovens:** With the cavity idle in Remote Ready and live updates connected, choose the **Cooking mode** and target temperature in Home Assistant. Choosing a mode keeps a compatible temperature (either previously chosen or already reported by the oven) and otherwise fills in a default temperature for that mode. These choices are held in Home Assistant until you start the oven, and are cleared if Remote Ready ends, the cavity starts, or a connection error occurs.
- **One-step automation start:** After choosing a cooking mode, call `climate.set_temperature` with a temperature and `hvac_mode: heat`. Calling start on an already-running oven updates only its setpoint.

The **Start oven** button is available only while the cavity is off, **Remote ready** is on, the oven door is closed, a supported non-manual and non-Gourmet cooking mode is selected, and the target temperature is within that mode's range.

| Control | Range and restrictions |
| --- | --- |
| **Oven** (climate setpoint) | **Older E series:** `Bake`, `Roast`, `Bake stone`, `Convection bake`, `Convection roast` 170 to 550°F; `Convection` 120 to 550°F; `Proof` 85 to 110°F; `Dehydrate` 110 to 160°F.<br>**M series, ranges, and newer E/M series:** `Bake`, `Roast`, `Bake stone`, `Convection roast`, `Convection` 200 to 550°F; `Proof` 85 to 110°F; `Dehydrate` 110 to 170°F; `Warm` 140 to 200°F.<br>**Unrecognized series:** 85 to 550°F fallback. |
| **Probe target temperature** | 120 to 210°F (5°F step). Available only when **Probe in use** is on and the cavity is running or in **Remote ready**. |
| **Kitchen timer duration** | 0 to 719 minutes (1-minute step). Setting a positive value starts or restarts the timer; setting `0` cancels a running timer or clears a finished one. |

### Readings, timers, and Gourmet programs

Status sensors and binary sensors include **Oven door**, **Cooking**, **Preheated**, **Remote ready**, **Cooking mode change enabled**, **Probe in use**, **Probe within 10° of target**, **Probe target reached**, and **Gourmet mode**.

To avoid showing stale or placeholder readings:

- Oven and probe temperature entities show `unknown` when the appliance reports `0`.
- Measured **Oven temperature** (and climate current temperature) shows `unknown` while the cavity is off.
- Probe temperature and setpoint show `unknown` when **Probe in use** is off, and probe temperature shows `unknown` when the appliance reports `1` (its no-reading value).

**Broil level** is derived from the cavity setpoint while the oven is running in `Broil` mode: below 400°F is `Low`, 400 to 499°F is `Medium`, and 500°F or above is `High`. Otherwise it shows `unknown`.

Cooking timers and up to two kitchen timers expose active, under-one-minute, complete, and end-time entities (cooking timers also report a start time). Kitchen timer number controls display whole minutes remaining while running and count down each minute; end-time sensors can drive countdown cards. **Dismiss kitchen timer** is available while the timer is active or complete, and stops a running timer or clears a finished one. Pressing dismiss for a finished timer will not cancel a newly restarted timer if it restarts before the command runs.

**Gourmet program** translates reported recipe codes into names. It shows `None` when the code is `0` or **Gourmet mode** is off, and `unknown` for unrecognized codes. Select and start Gourmet programs at the oven.

## Hoods

Hoods provide a **Fan** entity (four speeds: 25%, 50%, 75%, and 100%; setting 0% turns the fan off) and a **Task light** entity when reported.

| Control | Range or options |
| --- | --- |
| **Task light** brightness | 5 to 100% (when reported) |
| **Task light** color temperature | 2700 to 5000 K (when reported with brightness) |
| **Delayed shutoff duration** | 0 to 719 minutes |
| **Automatic fan sensitivity** | `Off`, `Low`, `Medium`, `High` |
| Switches | **Halo light** (On/Off), **Delayed shutoff**, **Button tones**, **Control lock** |

Brightness and color-temperature requests outside the supported ranges are clamped to the nearest limit. **Hood filter usage** and **Hood filter allowance** are reported as durations; reset the filter counter at the hood. Hoods do not have Firebase appliance alerts.

## Dishwashers

**Wash cycle** and **Mode** can be changed only while the dishwasher is idle or waiting to start (**Wash status** is `Idle` or `Start pending`). Selecting a cycle does not start the wash.

- **Wash cycle** options: `Auto`, `Normal`, `Heavy`, `Quick`, `Pots and pans`, `Soak and scrub`, `Light`, `Crystal china`, `Rinse and hold`, `Plastics`, `Energy`, `Extra quiet`.
- **Mode** options: `Normal`, `Child lock`, `Sabbath`.
- **Delay start** options: `Off`, or `1 hour` through `12 hours`.
- **Wash option switches** (when reported): **Heated dry**, **Extended dry**, **High temperature wash**, **Sanitize rinse**, **Top rack only**. Home Assistant sends the supported option properties; the dishwasher determines which options apply to the selected cycle.

To enable **Remote ready**, hold **ENTER** on the dishwasher for five seconds, then close the door within four seconds. Opening the door cancels **Remote ready**.

**Start wash cycle** uses the cycle, delay, and options currently set on the dishwasher. It is available only when the wash cycle is off, **Remote ready** is on, the door is closed, and **Mode** is not `Sabbath`.

**Cancel wash cycle** is available while a wash is running, drying, or waiting for a delayed start. When canceling a delayed start, confirmation also requires the active delay to clear.

**Wash status** reports `Idle`, `Start pending`, `Running`, `Restart pending`, `Cancel pending`, `Drying`, `Complete`, `Delayed`, or `Error`. Some dishwashers report `Start pending` whenever they are idle, so automations waiting for the dishwasher to become idle should accept both `Idle` and `Start pending`. Unknown cycle or status codes show as `unknown`.

Other reported entities include **Door**, **Wash cycle active**, **Remote ready**, **Sabbath mode**, **Rinse aid low**, **Softener salt low**, **Service required**, **Wash cycle end**, **Delay start active**, **Delay start timer start**, and **Delay start timer end**.

## Events and Firebase alerts

Every appliance provides an **Appliance event** entity that fires on appliance alerts. It remains available when the appliance disconnects, retaining its last recorded event.

Each event includes these attributes:

| Attribute | Meaning |
| --- | --- |
| `event_type` | Event identifier for automations (for example, `refrigerator_door_ajar`). Unrecognized codes use `unknown`. |
| `code` | Numeric alert code reported by the appliance. |
| `sequence` | Sequence number reported with the alert. |
| `appliance_timestamp` | When the alert occurred, according to the appliance clock. |
| `source` | Where the alert arrived from: `channel` (live update), `history` (notification history in a status read or full update), or `firebase` (Firebase alert). |
| `received_at` | UTC timestamp when Home Assistant processed the alert. |

**Firebase appliance alerts** is optional and off by default in **Configure**. When enabled, Home Assistant receives alerts over a separate Firebase notification channel into the same **Appliance event** entities. Firebase alerts deliver alert notifications only; sensor values still come from appliance status updates. Comparing `appliance_timestamp` and `received_at` helps distinguish a delayed alert from a newly occurred one.

### Event types

Not every appliance sends every alert. For ovens, `oven_*` types apply to a single oven, the upper cavity of a double wall oven, or the right cavity of a range; `lower_oven_*` types apply to a lower cavity or the left cavity of a range. Home Assistant's UI displays fixed labels for each event type (for example, "Lower oven..." even on a two-cavity range), while automations should always match the `event_type` IDs below.

| Appliance | Alert | `event_type` IDs |
| --- | --- | --- |
| Refrigeration, wine, and ice | Door open | `refrigerator_door_ajar`, `freezer_door_ajar`, `wine_door_ajar`, `ice_maker_door_ajar` |
| | Setpoint changed | `refrigerator_setpoint_changed`, `freezer_setpoint_changed`, `wine_setpoint_changed` |
| | Wine temperature out of range | `wine_temperature_alert` |
| | Filter expired | `water_filter_expired`, `air_filter_expired` |
| | Ice maker clean cycle | `ice_cleaning_required`, `ice_cleaning_due_soon`, `ice_cleaning_add_descaler`, `ice_cleaning_add_sanitizer`, `ice_cleaning_cancelled`, `ice_cleaning_complete` |
| | Service required | `refrigerator_service_required` |
| Ovens | Preheat complete | `oven_preheated`, `lower_oven_preheated` |
| | Probe in use | `oven_probe_connected`, `lower_oven_probe_connected` |
| | Probe within 10°F of target | `oven_probe_within_ten_degrees`, `lower_oven_probe_within_ten_degrees` |
| | Probe temperature reached | `oven_probe_target_reached`, `lower_oven_probe_target_reached` |
| | Cooking timer one minute left | `oven_cooking_timer_under_one_minute`, `lower_oven_cooking_timer_under_one_minute` |
| | Cooking timer ended | `oven_cooking_timer_complete`, `lower_oven_cooking_timer_complete` |
| | Kitchen timer one minute left | `kitchen_timer_under_one_minute`, `kitchen_timer_2_under_one_minute` |
| | Kitchen timer ended | `kitchen_timer_complete`, `kitchen_timer_2_complete` |
| | Door open | `oven_door_ajar`, `lower_oven_door_ajar` |
| | Self-clean complete | `oven_self_clean_complete`, `lower_oven_self_clean_complete` |
| | Service required | `oven_service_required` |
| Dishwashers | Wash cycle | `dishwasher_started`, `dishwasher_paused`, `dishwasher_cancelled`, `dishwasher_complete` |
| | Supplies low | `softener_salt_low`, `rinse_aid_low` |
| | Service required | `dishwasher_service_required` |
| Any | Appliance fault | `fault_notification` |
| | Feedback request | `feedback_notification` |
| | Unrecognized code | `unknown` |

Service required, appliance fault, and feedback request alerts are not delivered via Firebase subscriptions; they arrive only through live updates or notification history.

### History, deduplication, and clock behavior

The first full update or status read after loading records existing notification history as a baseline without replaying older events. If that initial observation omits history, later history records are filtered against the integration's startup time. New events found in subsequent status reads are delivered immediately, including while live updates are recovering.

Duplicate alerts across live updates, notification history, and Firebase are suppressed while the integration remains loaded. Separate occurrences with identical identifiers and timestamps cannot be distinguished.

Replay protection depends on the appliance clock:

- If initial history is absent, an appliance clock running ahead of Home Assistant can make an older alert appear new.
- An appliance clock behind Home Assistant can suppress live events after reload until the clock passes the startup cutoff (for example, five minutes behind suppresses five minutes of events).
- Setting an appliance clock backward can suppress events until the clock catches up with the retained cutoff.

### Firebase subscriptions and repairs

Home Assistant registers its own alert subscription identity so enabling or disabling Firebase alerts does not affect notifications on your phone.

- Disabling **Firebase appliance alerts** or deselecting an appliance removes Home Assistant's alert subscriptions for those appliances, retrying automatically if temporary cloud errors prevent cleanup.
- Reloading the integration stops the Firebase listener while retaining credentials and subscriptions for reconnection.
- Removing the integration attempts subscription cleanup for up to 30 seconds and logs any failure. Cleanup requires cloud access and valid authentication.

Home Assistant raises a repair warning when Firebase appliance alerts fail three times in a row: setup fails, alert subscriptions cannot be updated, a selected appliance has no usable state to subscribe with, or the Firebase connection repeatedly drops or fails to connect. The repair names the latest problem and clears automatically after a healthy check, when **Firebase appliance alerts** is turned off, when no appliances are selected, or when the integration is disabled or removed.

## Actions

### `subzero.read_status`

**Sub-Zero: Read appliance status** requests a fresh status read from an appliance and waits for the updated state to reach Home Assistant before returning.

| Field | Required | Description |
| --- | --- | --- |
| `device_id` | Yes | Home Assistant device ID of the appliance to read. |

**Optional response data:**

- `confirmed_at`: ISO timestamp when the state was confirmed.

Concurrent callers share reads, and canceling one caller does not interrupt a read needed by others. The action fails (stopping an automation unless `continue_on_error` is enabled) if the read fails, a conflicting concurrent update arrives during the read, or the live connection drops or reconnects during the read. A status reply that omits an optional key does not fail the action.

`homeassistant.update_entity` also waits for a read; use `subzero.read_status` when failed confirmation must stop an automation.

### `subzero.schedule_ice_delay`

**Sub-Zero: Schedule ice delay** configures a production pause on a dedicated ice maker and refreshes its status.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `device_id` | Yes | | Home Assistant device ID of the dedicated ice maker. |
| `duration` | Yes | | Whole hours to pause production, from `1` to `12`. |
| `start_in` | No | `0` | Whole minutes before the pause starts, from `0` (immediately) to `1439`. |
| `repeat` | No | `false` | Set `true` to repeat the pause daily. |

## Updates, availability, and controls

### Live updates, periodic checks, and availability

Selected appliances under one account share tokens and a single live push connection. During setup, the integration waits briefly for initial live updates and then reads any appliances that have not yet reported. If one appliance cannot be read during setup, it is logged as a warning and stays unavailable until state arrives, while other appliances still load. Setup retries only if no appliance has state.

Changing **State check interval** or **Firebase appliance alerts** in **Configure** applies immediately without reloading entities. Adding or removing appliances, or saving an updated appliance temperature unit, reloads the integration.

- **Periodic state checks:** Off by default (**No periodic checks**). Configure offers 1-, 2-, 5-, and 10-minute intervals, staggered across selected appliances. Incoming updates do not postpone scheduled checks. With **No periodic checks**, silently missed updates can leave a value stale until the appliance reports that property again, though alert follow-up and recovery reads still run.
- **Alert follow-up reads:** When a new appliance alert arrives, the integration runs brief follow-up status checks while a door, timer, or wash cycle remains active so missed state changes catch up promptly. Normal state changes do not start follow-up reads.
- **Home Assistant polling option:** Disabling Home Assistant's **Enable polling for updates** system option pauses periodic state checks, outage checks, alert follow-up reads, and 30-minute **Active faults** refreshes.

Availability during cloud or connection issues behaves as follows:

- **Brief live-connection interruptions:** Established readings stay available for up to two minutes while the integration reconnects, turning unavailable only if neither a live update nor a status read reconfirms state in time. Repeated disconnects or partial updates do not extend that window.
- **How long reconnecting takes:** After a connection that had been up for at least two minutes drops, the integration tries again within five seconds. After a failed attempt or a short-lived connection, retries start about 30 seconds apart and lengthen up to 15 minutes. When Sub-Zero's live-update service has no free connections, it retries every two to four minutes.
- **Longer live-connection outages:** While live updates stay down (and Home Assistant polling is enabled), automatic checks run every minute per appliance regardless of the configured check interval. Each successful read extends availability, so readings stay available as long as status reads succeed, though values can be up to a minute old and brief changes between checks are not seen.
- **Per-appliance and auth errors:** Authentication failures and per-appliance channel errors affect availability immediately. A channel failure on one appliance leaves other appliances on the same account updating normally.
- **Background check failures and rate limits:** A failed background check does not mark an otherwise healthy appliance unavailable while live updates stay connected. When Sub-Zero rate-limits requests, the integration sends nothing more to that service for as long as Sub-Zero asks, or for 10 minutes when it gives no time. Status reads and controls that need that service fail until then, and cached values can age meanwhile.
- **Missing properties after a full refresh:** When a status read or a full update after a reconnect replaces cached state, an entity whose property was absent from that response becomes unavailable rather than holding an old value until the property is reported again.

Periodic checks can repair current state, but they cannot reconstruct brief events that occurred and ended between checks, nor guarantee freshness during cloud outages.

### Control confirmation

Control commands wait for confirmation from appliance state before succeeding:

- **Confirmation and retries:** Property changes wait for the appliance to report the requested state, retrying briefly and reading status if confirmation does not arrive in time. Authentication and rate-limit errors fail immediately without retrying. If confirmation fails after a command error, the error message includes the last command error.
- **Command ordering:** Commands to the same appliance run one at a time, each checking the state left by earlier commands. If a command is canceled in flight, the next command refreshes appliance state before deciding what to send. Multi-step controls (such as ice modes or remote starts) stop immediately if a required write fails.
- **Verified state before writing:** Cached availability during a reconnect grace window does not allow controls to write against unverified interlocks. While live updates are disconnected or recovering, a control reads status first and validates against that fresh state. After a full live update restores state from an interruption, a control waits for the follow-up status read before writing, proceeding on the snapshot's state if that read fails. Settings the appliance does not report are rejected as unsupported.
- **Kitchen timers:** Starting or restarting a kitchen timer requires the reported end time to match the requested duration so an earlier timer state cannot falsely confirm a restart.

## Diagnostics

### Diagnostic sensors

Enabled by default:

- **Wi-Fi signal** (created when reported): signal strength in dBm.
- **Active faults** (created on every appliance): state is the count of active faults; the `faults` attribute lists each active fault with `code` and `description` (when reported), `severity`, `created`, and (when available) `title` and `resolution_steps`. Refreshes every 30 minutes while Home Assistant polling is enabled.

Disabled by default (enable in entity settings when needed):

- **State confirmed:** timestamp of the last full status read or full live update that was not contradicted by a concurrent change. Disabling this sensor does not disable checks or recovery; individual partial updates and cached state kept after failed reads do not advance it.
- **Live reporting mode:** shows `Cloud push` when live updates are connected and the appliance is available, and `Disconnected` otherwise.
- **Uptime:** appliance uptime. Appliances truncate uptime to eight characters, giving 10-second precision from 100 hours and 1-minute precision from 1,000 hours.
- **IP address** and **MAC address** (created when reported).

### Diagnostics downloads and debug logging

Diagnostics downloads use cached state without contacting Sub-Zero. They omit account credentials and appliance names, redact serial numbers and network identifiers (`IP address` and `MAC address`), and list unrecognized property names without their values.

| Field | What it shows |
| --- | --- |
| Connection status and frame times | Whether live updates are connected, when the current connection started, and when the last frame (including heartbeats) arrived. |
| Notification counts | Received, ignored, and invalid appliance notifications since reload (heartbeats are excluded from the received count). |
| Failed connections and disconnects | Up to the last 10 failed connection attempts and up to the last 10 ended connections, with timestamps and errors. |
| Rate limits | Up to the last 10 times requests were rate-limited, with timestamps, target, and wait duration. |
| Firebase | When active: listener connection state, subscription health, alert count, last alert time, last error, and last sync time. |
| Per-appliance push and channel errors | Snapshot and partial-update counts, last message time, unexpected state flips in snapshots, and up to the last 10 channel errors. |
| State verification | Check counts, status reads, and last confirmation time. |
| Cached state and faults | Redacted appliance state, saved temperature unit, inferred clock zone, unrecognized state key names, and all reported faults (active and inactive). |

Healthy connection status and message counts do not by themselves prove that every state change was delivered or applied.

Debug logging for `custom_components.subzero` records live updates, status-read timing and results, published state, and connection errors. Logged state values include recognized scalar properties and exclude serial numbers and network identifiers, though appliance names appear in log messages, so review debug logs before sharing them publicly.
