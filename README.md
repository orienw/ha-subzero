# Sub-Zero for Home Assistant

<img src="custom_components/subzero/brand/icon.png" alt="Sub-Zero integration icon" width="80">

A custom integration for connected Sub-Zero refrigerators and freezers, Wolf ovens, and Cove dishwashers, installed through HACS.

Sign in with your Sub-Zero Group Owner email and password directly in Home Assistant.

Monitor and control your appliances over Sub-Zero's cloud service using their existing Wi-Fi connections. Bluetooth is not required.

## Install with HACS

Requires Home Assistant **2026.8.0 or newer** and an appliance already connected to your Sub-Zero account.

The Sub-Zero Group Owner's App is available in **Canada, Mexico, and the United States**. See [Sub-Zero's country availability](https://www.subzero-wolf.com/assistance/answers/multi-brand/sub-zero-group-owner-s-app-location-availability).

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=orienw&repository=ha-subzero&category=integration)

Use the button above, or add the repository manually:

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/orienw/ha-subzero` with type **Integration**.
3. Download **Sub-Zero**, then restart Home Assistant.
4. Open **Settings → Devices & services → Add integration → Sub-Zero**.
5. Enter your Sub-Zero account email and password, then select the appliances to include.

To add appliances later, open **Settings → Devices & services → Sub-Zero → Configure**. This refreshes the account's appliance list and lets you change the selection using your saved connection. Deselecting an appliance removes its Home Assistant device and entities.

For manual installation, copy `custom_components/subzero` into your Home Assistant configuration's `custom_components` directory, restart, and follow steps 4–5.

## Sub-Zero fridge entities

Entities are created only for recognized properties reported by the appliance. New recognized properties can also be discovered during push updates.

### Controls

| Control | Settings |
| --- | --- |
| Temperature setpoints | Refrigerator, freezer, crisper |
| Crisper temperature mode | Automatic, Manual |
| Humidity control | Normal, Enhanced |
| Ice maker | Off, On, Max ice, Night ice |
| Mode | Normal, Sabbath, High use, Short vacation, Long vacation |
| Night mode | Disabled, Enabled |
| Air purification | On, Off |
| Accent light | 0–100%, optional control for glass-front models, disabled by default |

The available choices follow the properties reported by each fridge. Settings change only when you use a control or run an automation. Installing, restarting, or reconnecting the integration does not change appliance settings.

The **Ice maker** control shows the selected mode. In [**Night ice**](https://www.subzero-wolf.com/assistance/answers/sub-zero/common/sub-zero-night-ice-mode), the separate **Ice maker enabled** status may be Off while the schedule pauses ice production.

Select **Manual** crisper temperature mode to adjust its setpoint. In Automatic mode, the setpoint control is unavailable and the temperature sensor continues to show the configured value. The manual range stays within 2°F of the refrigerator setpoint, between 34°F and 42°F. See [Sub-Zero's crisper temperature guide](https://www.subzero-wolf.com/assistance/answers/sub-zero/next-classic/sub-zero-classic-series-cl-refrigerator-drawer-temperature-contr).

Turn off **Max ice** before adjusting the freezer setpoint. Home Assistant enforces the supported temperature ranges and converts your preferred display unit to whole Fahrenheit setpoints.

[Humidity control](https://www.subzero-wolf.com/assistance/answers/sub-zero/next-classic/next-classic-humidity-control) affects the refrigerator zone. [Night mode](https://www.subzero-wolf.com/assistance/answers/sub-zero/next-classic/next-classic-night-mode) dims the interior lights when the room is dark; **Night ice** is a separate ice-maker setting.

### Monitoring

| Type | Available properties |
| --- | --- |
| Temperature setpoints | Refrigerator, freezer, crisper |
| Filters | Air and water filter life remaining |
| Doors | Refrigerator and freezer door open |
| Ice-maker settings | Enabled, max ice, night ice |
| Operating modes | Sabbath, high use, short vacation, long vacation |
| Device status | Service required, power |
| Diagnostic | Wi-Fi signal strength |

Most reported, recognized properties are enabled by default. Accent light and the optional diagnostic sensors are disabled by default. Ice-maker settings and operating modes report their current on/off states. Wi-Fi signal strength appears under Diagnostics.

Ice-maker settings and operating-mode sensors remain available alongside their selectors. Switches report their own On/Off state for dashboards and automations.

Fridge and freezer zones also provide climate entities for thermostat cards. They use the same temperature limits and Max ice interlock as the number controls.

Additional sensors report water filter capacity remaining in gallons and the start/end times for Max ice and High use when available. A negative filter capacity indicates usage beyond the reported filter capacity.

Refrigerator temperatures on the primary tested model are **configured setpoints**. Separate display-temperature sensors appear on models that report them. The integration does not infer a measured temperature from a setpoint.

Temperature entities require an appliance configured in Fahrenheit in the Sub-Zero app. Temperature entities are omitted for other app temperature settings until their units can be verified. Other entities remain available. Home Assistant can display Fahrenheit readings in your preferred temperature unit.

Appliance temperature units refresh automatically at startup and whenever the integration reloads. After changing the unit in the Sub-Zero app, reload the integration to apply it immediately. Other app temperature settings still leave temperature entities unavailable.

## Wolf ovens

Each reported oven cavity has its own entities. Existing first-cavity entity IDs are preserved; a second cavity uses names prefixed with **Lower oven**.

| Type | Available properties |
| --- | --- |
| Temperatures | Measured oven and probe temperatures, oven and probe setpoints |
| Status | Door, cooking, preheated, remote ready, probe in use, probe target reached, Gourmet mode |
| Timers | Cooking timer active or complete, both kitchen timers active or complete, reported start/end times |
| Cooking mode | Recognized mode name and whether the appliance permits mode changes |
| Shared status | Sabbath mode, service required, Wi-Fi signal strength |

Controls include:

- A climate entity for each cavity, with temperature control and on/off actions. Temperature changes require the oven to be running or in Remote Ready. The overall range is 85–550°F; the appliance can impose narrower limits for its current mode.
- A cooking-mode selector and an interior-light switch for each cavity. Selecting Off turns that cavity off.
- A Start oven button for each cavity, available only when the oven reports Remote Ready and a supported cooking mode and temperature are configured.
- Two kitchen-timer duration controls, from 0 to 660 minutes. Setting a duration starts or restarts that timer; 0 cancels it. The number shows the configured duration when reported start/end times permit it. End-time sensors can drive countdown dashboards.

Enable **Remote Ready at the oven before each remote start**. Opening a door cancels it. Broil, Convection broil, Proof, Self clean, and Gourmet must be started at the appliance. Those restrictions also apply to automations. See [Wolf's Remote Ready guide](https://www.subzero-wolf.com/assistance/answers/wolf/m-series-oven/sub-zero-group-owners-app---set-up-remote-access).

Oven temperature fields that report zero while idle show as unknown; probe readings also show as unknown when the probe is not in use. Unknown cooking-mode codes show as unknown and cannot be selected.

## Cove dishwashers

Entities are created for the recognized properties reported by the dishwasher:

| Type | Features |
| --- | --- |
| Cycle monitoring | Wash cycle, wash status, cycle active, cycle end time |
| Status | Door, Remote Ready, rinse aid low, softener salt low, service required |
| Options | Heated dry, Extended dry, High temperature wash, Sanitize rinse, Top rack only |
| Delay start | Off or 1–12 hours, active status and reported start/end times |
| Start | Start wash cycle button, available when Remote Ready is enabled |

Select the wash cycle using the appliance or official app. To enable remote starting, hold ENTER on the dishwasher for five seconds, then close the door within four seconds. Opening the door cancels Remote Ready. See [Cove's Remote Ready guide](https://www.subzero-wolf.com/assistance/answers/cove/dishwasher/cove-dishwasher-remote-ready-feature).

Unknown wash cycle/status codes show as unknown. The integration sends only supported option properties; the appliance enforces which options apply to its selected cycle.

## Diagnostics

Wi-Fi signal strength is enabled by default. Uptime, IP address, MAC address, and live reporting mode are diagnostic sensors disabled by default. Enable them from the entity settings when needed.

Download diagnostics from the integration or individual device page. Downloads use the cached appliance state and omit account credentials, appliance names, serial numbers, and network identifiers. They also list unrecognized state key names seen since the last reload, without their values, to help investigate support for other models.

Timer timestamps require an explicit timezone offset, either in the timestamp or the appliance clock. A missing timestamp or an unverifiable timezone shows as unknown.

## Compatibility

**Sub-Zero CL4850UFDID is the primary tested appliance.** Cloud status and push snapshots have also been tested with Wolf SO3050PMSP. The additional fridge features, oven controls, second-cavity support, and Cove entities have automated coverage using simulated appliance responses. These additions have not yet been tested against physical appliances.

There is no model allowlist. Other models can be added if the cloud service returns their status. Their available entities depend on which recognized properties they report. Adding an appliance does not imply every feature of that model is supported.

Local network access and accounts requiring additional verification or an external sign-in provider are not supported.

## Updates and account access

Selected appliances share account tokens and one SignalR notification connection. Each appliance gets a full status read at setup, then receives push updates. An idle fridge does not trigger periodic status requests.

Connection heartbeats keep the notification socket alive, including while another appliance is slow to connect. A disconnected socket reconnects, and failed appliance channels retry independently, with increasing delays. Reopening an appliance channel supplies a fresh push snapshot. API HTTP 429 responses honor `Retry-After` where provided.

The notification connection renews before its separate SignalR token expires, at least every 50 minutes. Renewal reopens the selected appliance channels without periodic status polling. Status reads support both direct and wrapped snapshots, with a remembered `get_async` fallback for appliances that acknowledge `get` without returning state.

A healthy cloud socket does not independently verify an idle appliance's connectivity. Without periodic status reads, an appliance that silently stops reporting may go undetected until a notification or a failed control request reveals it.

Control changes are confirmed from appliance status. When a push update does not arrive, the integration makes one status request to check the setting. Changing a mode sends only the settings that differ, one at a time, and stops if a change fails.

Kitchen-timer commands are confirmed from their active state and end time. A successful command acknowledgement alone does not confirm that an appliance applied a change.

Sub-Zero has not published an API quota that this project has verified. Push reduces repeated status requests, but does not guarantee immunity from rate limits, particularly with multiple appliances or unstable connections.

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
