# Sub-Zero for Home Assistant

<img src="custom_components/subzero/brand/icon.png" alt="Sub-Zero integration icon" width="80">

A custom integration for connected Sub-Zero refrigerators, freezers, wine storage, and ice makers, Wolf ovens and hoods, and Cove dishwashers, installed through HACS.

Sign in with your Sub-Zero Group Owner email and password directly in Home Assistant. Appliances are monitored and controlled over Sub-Zero's cloud service using their existing Wi-Fi connections. Bluetooth is not required.

## Releases

[![Latest stable release](https://img.shields.io/github/v/release/orienw/ha-subzero?sort=date&style=for-the-badge&label=stable&color=blue)](https://github.com/orienw/ha-subzero/releases/latest)
[![Latest beta release](https://img.shields.io/github/v/release/orienw/ha-subzero?include_prereleases&filter=*b*&sort=date&style=for-the-badge&label=beta&color=orange)](https://github.com/orienw/ha-subzero/releases)

## Install with HACS

Requires Home Assistant **2026.8.0 or newer** and an appliance already connected to your Sub-Zero account.

The Sub-Zero Group Owner's App is available in **Canada, Mexico, and the United States**. See [Sub-Zero's country availability](https://www.subzero-wolf.com/assistance/answers/multi-brand/sub-zero-group-owner-s-app-location-availability).

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=orienw&repository=ha-subzero&category=integration)

Use the button above, or find the integration in HACS:

1. Open **HACS** and search for **Sub-Zero**.
2. Download **Sub-Zero**, then restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → Sub-Zero**.
4. Enter your Sub-Zero account email and password, then select the appliances to include. If your account uses multi-factor authentication, setup first verifies it by text message code or phone call, and may ask you to type the characters shown in an image.

To change the selection later, open **Settings → Devices & services → Sub-Zero → Configure**. This refreshes the account's appliance list using the saved connection. Deselecting an appliance removes its Home Assistant device and entities.

For manual installation, copy `custom_components/subzero` into your Home Assistant configuration's `custom_components` directory, restart, and follow steps 3–4.

## Entities

Entities are created only for recognized properties that each appliance reports, at setup and as new properties appear in push updates. There is no model allowlist. Properties the Sub-Zero app ignores for a specific appliance type, such as the accent light on some refrigerators, are ignored here too. Entities the app hides along with them, such as a lower oven probe's readings when the probe is ignored, are not created, and entities that earlier versions created are removed.

Celsius and Fahrenheit appliance settings are supported. Home Assistant displays temperatures and accepts setpoints in your preferred unit. Setpoints use whole-degree Fahrenheit precision, so Celsius requests may be rounded. Appliance units are read at startup and on reload, falling back to the last saved unit if the appliance list is temporarily unavailable. Appliances with unknown units keep all of their non-temperature entities.

Timestamps keep any offset they include. A timestamp without an offset is read in the time zone of the appliance clock, from the offset of the clock time each push reports. When that offset matches Home Assistant's time zone, that zone is used, so daylight saving changes are followed. Status reads report the clock in UTC, so they do not change it. Until a push reports the clock, Home Assistant's time zone is used, as the Sub-Zero app uses the phone's.

Each appliance reports **Active faults**: the count of currently active faults and their details, refreshed every 30 minutes. Clearing faults happens at the appliance.

Installing, restarting, or reconnecting the integration never changes appliance settings. Settings change only when you use a control or run an automation.

## Sub-Zero refrigerators

### Controls

| Control | Settings |
| --- | --- |
| Temperature setpoints | Refrigerator, refrigerator drawer, freezer, crisper |
| Crisper temperature mode | Automatic, Manual |
| Humidity control | Normal, Enhanced; Disabled and Low are shown when the appliance reports them |
| Ice maker | Off, On, Max ice, Night ice |
| Mode | Normal, Sabbath, High use, Short vacation, Long vacation |
| Night mode | Disabled, Enabled |
| Air purification | On, Off |
| Internal water dispenser | On, Off, on models that report it |
| Reset air filter | Send a reset request after replacing the filter, on models that report air-filter life |
| Accent light | Off, On, Low, Medium, High, on models that report accent lighting |
| Door open delay | Off, 1, 2, 5, or 10 minutes before an open door triggers a notification, on models that report it |

The **Ice maker** control shows the selected mode. In [**Night ice**](https://www.subzero-wolf.com/assistance/answers/sub-zero/common/sub-zero-night-ice-mode), the separate **Ice maker enabled** status may be Off while the schedule pauses ice production.

Select **Manual** crisper temperature mode to adjust its setpoint. In Automatic mode, the setpoint control is unavailable and the sensor continues to show the configured value. The manual range stays within 2°F of the refrigerator setpoint and within that appliance's refrigerator limits. Refrigerator limits follow the reported appliance type, with model-based defaults when the type is unavailable. See [Sub-Zero's crisper temperature guide](https://www.subzero-wolf.com/assistance/answers/sub-zero/next-classic/sub-zero-classic-series-cl-refrigerator-drawer-temperature-contr).

Turn off **Max ice** before adjusting the freezer setpoint. Refrigerator and freezer zones also provide climate entities for thermostat cards, with the same temperature limits and Max ice interlock as the number controls.

The **Reset air filter** button sends the reset request and refreshes the appliance state. The filter-life sensor continues to show the value reported by the appliance.

[Humidity control](https://www.subzero-wolf.com/assistance/answers/sub-zero/next-classic/next-classic-humidity-control) affects the refrigerator zone. [Night mode](https://www.subzero-wolf.com/assistance/answers/sub-zero/next-classic/next-classic-night-mode) dims the interior lights when the room is dark; **Night ice** is a separate ice-maker setting.

### Monitoring

| Type | Available properties |
| --- | --- |
| Temperature setpoints | Refrigerator, refrigerator drawer, freezer, crisper |
| Display temperatures | Refrigerator, refrigerator drawer, freezer, crisper, on models that report them |
| Filters | Air and water filter life remaining, water filter capacity in gallons |
| Doors | Refrigerator, refrigerator drawer, and freezer door open |
| Ice-maker settings | Enabled, max ice, night ice, plus Max ice start and end times |
| Operating modes | Sabbath, high use, short vacation, long vacation, plus High use start and end times |
| Device status | Service required, power |
| Diagnostic | Wi-Fi signal strength |

Ice-maker settings and operating modes report their on/off state alongside their selectors. Switches report their own On/Off state, so they have no duplicate binary sensors.

Refrigerator temperatures on the primary tested model are **configured setpoints**. The integration does not infer a measured temperature from a setpoint. A negative water filter capacity indicates usage beyond the reported filter capacity.

Wine storage units expose setpoint controls from 40–65°F, climate entities, display temperatures, and door status for each reported wine zone. Units with two zones name them **Upper wine** and **Lower wine**, as the app does; a door is named by zone only when each zone has its own. They also get the **Mode** control with the operating modes the app offers for their type, such as Sabbath.

## Sub-Zero dedicated ice makers

Dedicated ice makers provide an On/Off ice control and a door-open delay setting. Status includes Sabbath mode, the door, water filter, delay schedule, cleaning stage, next cleaning, and fault or winterization flags when reported.

Use the **Sub-Zero: Schedule ice delay** action to pause production for 1–12 hours. Choose the ice maker, how many minutes from now to begin (zero starts immediately), and whether to repeat daily. The settings are sent together and the appliance status is refreshed.

**End current ice delay** resumes production without removing a repeating schedule. **Cancel ice delay schedule** removes the scheduled delay. Cleaning steps must be performed at the appliance; cleaning sensors only report progress.

## Wolf ovens

Each reported oven cavity has its own entities. Ovens with two cavities name them **Upper oven** and **Lower oven**, or **Right oven** and **Left oven** on ranges, as the app does. Installs upgraded from earlier releases keep their first-cavity entity IDs, such as `climate.kitchen_oven`; only the names change. When a second cavity or wine zone is first reported after the first one's entities exist, the first one's entities are renamed. Renaming keeps existing entity IDs.

| Type | Available properties |
| --- | --- |
| Temperatures | Measured oven and probe temperatures, oven and probe setpoints, broil level (Low, Medium, High) while broiling |
| Status | Door, cooking, preheated, remote ready, probe in use, probe within 10° of target, probe target reached, Gourmet mode |
| Timers | Cooking timer active, under one minute, or complete; both kitchen timers active, under one minute, or complete; reported start/end times |
| Cooking mode | Recognized mode name and whether the appliance permits mode changes |
| Gourmet program | Named program reported by each cavity, such as Baked potato or Fresh pizza |
| Shared status | Sabbath mode, service required, Wi-Fi signal strength |

Controls include:

- A climate entity for each cavity, with temperature control and on/off actions. Temperature changes require the oven to be running or in Remote Ready. Known oven series use limits for the selected cooking mode, such as 85–110°F for Proof and 140–200°F for Warm. Modes without an adjustable temperature keep their on/off controls. Unrecognized series retain the 85–550°F fallback range.
- A cooking-mode selector and an interior-light switch for each cavity. Selecting Off turns that cavity off. Each selector lists the modes the app offers for that oven and cavity, so Convection bake appears only on older E series ovens, which have no Warm mode, and some lower ovens leave out Convection, Dehydrate, or Bake stone.
- A Start oven button for each cavity, available only when the oven reports Remote Ready and a supported cooking mode and temperature are configured. Starting sends the same writes as the app: E series and M series receive the power command alone, and every other series receives the cooking mode, power, and setpoint in that order.
- A probe target control for each reported probe, from 120–210°F. Connect the probe and have the cavity running or in Remote Ready to adjust it.
- Two kitchen-timer duration controls, from 0 to 719 minutes. Setting a duration starts or restarts that timer; 0 cancels it or clears a finished timer. A Dismiss kitchen timer button for each timer clears it once it finishes, like the app's Tap to Dismiss. The number shows the configured duration when reported start/end times permit it. End-time sensors can drive countdown dashboards.

Enable **Remote Ready at the oven before each remote start**. Opening a door cancels it. Broil, Convection broil, Proof, Self clean, and Gourmet must be started at the appliance. Those restrictions also apply to automations. See [Wolf's Remote Ready guide](https://www.subzero-wolf.com/assistance/answers/wolf/m-series-oven/sub-zero-group-owners-app---set-up-remote-access).

Oven temperature fields that report zero while idle show as unknown; probe readings also show as unknown when the probe is not in use or reports 1, the app's no-reading value. The measured oven temperature shows as unknown while that oven is off, because ranges keep reporting their last reading after turning off. The broil level follows the Broil setpoint: below 400°F is Low, 400–499°F Medium, and 500°F or more High. Unknown cooking-mode codes show as unknown and cannot be selected.

Gourmet program sensors report the appliance's recipe code as a name. Code 0 shows None, and so does a cavity that reports Gourmet mode off; unrecognized codes show as unknown. Select and start Gourmet programs at the oven.

## Wolf hoods

Hoods provide a fan entity with four speeds and a task-light entity with brightness and white-temperature controls, when reported. Brightness ranges from 5–100%, and white temperature from 2700–5000 K. Requests outside these ranges use the nearest limit.

Other controls include halo lighting, automatic fan sensitivity (Off, Low, Medium, High), delayed shutoff with a 0–719 minute duration, button tones, and the control lock. Filter usage and allowance are reported as durations. Reset the hood filter counter at the appliance.

## Cove dishwashers

| Type | Features |
| --- | --- |
| Cycle monitoring | Wash cycle, wash status, cycle active, cycle end time |
| Cycle selection | Choose a cycle while idle or waiting to start |
| Mode | Normal, Child lock, Sabbath, while idle or waiting to start |
| Status | Door, Remote Ready, rinse aid low, softener salt low, service required |
| Options | Heated dry, Extended dry, High temperature wash, Sanitize rinse, Top rack only |
| Delay start | Off or 1–12 hours, active status and reported start/end times |
| Start | Start wash cycle button, available when Remote Ready is enabled; starts with the cycle and delay currently set on the dishwasher |
| Cancel | Cancel wash cycle button, available while a cycle is running, drying, or waiting for a delayed start |

Selecting a cycle does not start it. To enable remote starting, hold ENTER on the dishwasher for five seconds, then close the door within four seconds. Opening the door cancels Remote Ready. See [Cove's Remote Ready guide](https://www.subzero-wolf.com/assistance/answers/cove/dishwasher/cove-dishwasher-remote-ready-feature).

Some dishwashers report Start pending whenever they are idle, which the app shows as Idle. Automations that wait for an idle dishwasher should accept both Idle and Start pending. Unknown wash cycle/status codes show as unknown. The integration sends only supported option properties; the appliance enforces which options apply to its selected cycle.

## Appliance events

Each appliance has an **Appliance event** entity for automations. It reports events such as oven preheat, probe targets, timer completion, dishwasher cycles, door alerts, and maintenance notifications. Its attributes include the event type, numeric code, sequence, and appliance timestamp. Automations match event type IDs such as `refrigerator_door_ajar`, which Home Assistant shows by name, such as Refrigerator door open. Unrecognized codes use the `unknown` event type and keep their numeric code.

The event entity keeps its last occurrence when the connection drops. Events found during a status read are delivered immediately, including while the push connection is recovering.

Startup history is not replayed. The first snapshot or status read after loading only sets a baseline, so events reported while the integration starts do not fire, even when the appliance clock runs ahead. While the integration is loaded, repeated notifications and reconnect history are deduplicated, including when the appliance resets its sequence counter. Events from before the integration loaded are ignored; events that occur while Home Assistant is stopped do not trigger automations on startup. Event timestamps without an offset are read in the appliance clock's time zone, as described above.

Replay protection relies on the appliance clock. A clock five minutes behind Home Assistant can suppress the first five minutes of live events after a reload. If the clock moves backward, events can also be ignored until it catches up with the retained history cutoff.

## Diagnostics

Wi-Fi signal strength is enabled by default. Uptime, IP address, MAC address, and live reporting mode are diagnostic sensors disabled by default. Enable them from the entity settings when needed. Appliances cut their uptime to eight characters, so from 100 hours it is accurate to ten seconds, and from 1,000 hours to the minute.

Download diagnostics from the integration or individual device page. Downloads use the cached appliance state and omit account credentials, appliance names, serial numbers, and network identifiers. They also list unrecognized state key names, without their values.

Integration diagnostics count appliance notifications received, ignored, or invalid since the last reload. Heartbeats are excluded from the received count. Each appliance also records parsed snapshots and updates, with the time of the last one, and the time of the last message from its update channel, including messages without state. These counts help distinguish incoming messages from a connection that only receives heartbeats; they do not prove every state change was received or applied.

Enable debug logging for `custom_components.subzero` to record channel-open attempts, notification types and payload key names, and parsed state updates. State values exclude network identifiers and nested objects.

## Compatibility

**Sub-Zero CL4850UFDID is the primary tested appliance.** Cloud status and push snapshots have also been tested with Wolf SO3050PMSP. The additional fridge features, dedicated ice makers, hoods, oven controls, second-cavity support, and Cove entities are covered by automated tests using simulated appliance responses and have not yet been verified against physical appliances.

Other models can be added if the cloud service returns their status. Their entities depend on which recognized properties they report.

Local network access, verification methods other than text message or phone call, and accounts that sign in through an external provider are not supported.

## How it works

Selected appliances share account tokens and one cloud notification connection. Setup opens the push connection and waits up to 16 seconds for initial state, then requests any missing state. Lost connections reconnect with increasing delays, and rate-limit responses are honored.

State checks run every five minutes per appliance by default, staggered across selected appliances. Incoming updates do not postpone these checks. Each check reopens the appliance's update channel and waits up to 16 seconds for a full snapshot, then reads status if no snapshot arrives. If status succeeds but the channel remains silent, the notification connection is renewed once until a full snapshot arrives again. Configure offers one-, two-, five-, and ten-minute intervals, or **Push only** to disable checks. Home Assistant's system option to disable polling also disables these checks. Rate limits may postpone verification.

After an error, a reopened channel or an incoming state update triggers a fresh status read if no full push snapshot has restored the appliance. A status read that fails while the push connection stays open, such as one confirming a control, starts recovery right away. Failed recovery reads retry with increasing delays. A failed state read marks the appliance unavailable until recovery succeeds. Diagnostics record state-check attempts, fallback reads, connection renewals, and the last successful verification.

For a reminder that depends on an appliance still being in a particular state, call `homeassistant.update_entity` before checking the condition and skip the reminder if the entity is unavailable. Periodic checks can repair current state, but cannot reconstruct a brief event that was never delivered.

Control changes are confirmed from appliance status, not from the command acknowledgement. Property writes use up to three attempts, each allowing eight seconds for the request and its push confirmation. A command error or missing push confirmation then prompts a status read, which that deadline does not cut short; authentication and rate-limit errors stop immediately. A command that resends a value the appliance already reports, such as a cancel while the cycle already reports off, counts only when the cloud acknowledges it, so a remote start stops if a resent cooking mode or setpoint fails. Each retry rechecks whether the change is still allowed. Ice modes and remote starts preserve the app's ordered writes, including repeated values, and later writes stop if a setting cannot be confirmed. Queued ice-mode changes and remote starts use the state left by earlier commands, and a start for an appliance that is already running only sends a new setpoint. A kitchen-timer write counts only when the reported end time is within 65 seconds of the request time plus the new duration, so restarts require a fresh update or status read. Failed confirmation includes the last command error when one was reported.

Sub-Zero does not document an API quota. Push updates keep requests low, but multiple appliances or unstable connections can still hit rate limits.

Your password is used for sign-in and is not saved. Home Assistant stores renewable account tokens in its configuration and refreshes them automatically. If renewal fails, Home Assistant asks you to sign in again. Protect Home Assistant backups as you would other account credentials.

This is an unofficial integration using the mobile application's cloud endpoints and application settings. Changes to Sub-Zero's login service, API, or shared application key can require an integration update. It is not affiliated with Sub-Zero Group.

## Reporting issues

[Open an issue](https://github.com/orienw/ha-subzero/issues) with your Home Assistant version, integration version, appliance model, and steps to reproduce the problem. Include whether the same operation works in the Sub-Zero app, plus any relevant `custom_components.subzero` log messages. Remove account details and tokens before posting logs.

## Development

Use Python 3.14:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```
