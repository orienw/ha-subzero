# Sub-Zero for Home Assistant

<img src="custom_components/subzero/brand/icon.png" alt="Sub-Zero integration icon" width="80">

A custom integration focused on connected Sub-Zero refrigerators and freezers, installed through HACS. Wolf and Cove support is secondary.

Sign in with your Sub-Zero Group Owner email and password directly in Home Assistant.

Monitor your appliances and change Sub-Zero fridge settings over Sub-Zero's cloud service using the appliance's existing Wi-Fi connection. Bluetooth is not required.

## Install with HACS

Requires Home Assistant **2026.9.0 or newer** and an appliance already connected to your Sub-Zero account.

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
| Device status | Service required, power, air purification |
| Diagnostic | Wi-Fi signal strength |

All reported, recognized properties are enabled by default. Ice-maker settings and operating modes report their current on/off states. Wi-Fi signal strength appears under Diagnostics.

Status sensors remain available alongside the controls for dashboards and automations.

Refrigerator temperatures are **configured setpoints**. Measured interior temperatures are not exposed.

Temperature entities require an appliance configured in Fahrenheit in the Sub-Zero app. Temperature entities are omitted for other app temperature settings until their units can be verified. Other entities remain available. Home Assistant can display Fahrenheit readings in your preferred temperature unit.

After changing the appliance's temperature unit in the Sub-Zero app, open **Configure** and save your appliance selection to refresh the unit information in Home Assistant.

## Other appliances

Wolf oven monitoring is available for recognized properties:

| Type | Available properties |
| --- | --- |
| Temperatures | Measured oven and probe temperatures, oven and probe setpoints |
| Status | Door, cooking, preheated, light, remote ready, probe in use, probe target reached, Gourmet mode |
| Timers | Cooking timer complete, both kitchen timers active or complete |
| Shared status | Sabbath mode, service required, Wi-Fi signal strength |

Oven temperature fields that report zero while idle show as unknown; probe readings also show as unknown when the probe is not in use. Cooking mode names and timer countdowns are not yet mapped.

Cove support has not been verified, and dishwasher-specific entities are not implemented.

## Compatibility

**Sub-Zero CL4850UFDID is the primary tested appliance.** Cloud status and push snapshots have also been tested with Wolf SO3050PMSP.

There is no model allowlist. Other models can be added if the cloud service returns their status. Their available entities depend on which recognized properties they report. Adding an appliance does not imply every feature of that model is supported.

Controls are available for recognized Sub-Zero fridge settings. Wolf and Cove entities provide monitoring. Local network access and accounts requiring additional verification or an external sign-in provider are not supported.

## Updates and account access

Selected appliances share account tokens and one SignalR notification connection. Each appliance gets a full status read at setup and after 30 minutes without a changed state update. The integration does not poll appliance status every minute. Connection heartbeats keep the notification socket alive, including while another appliance is slow to connect; reconnect attempts back off after failures. API HTTP 429 responses honor `Retry-After` where provided.

Control changes are confirmed from appliance status. When a push update does not arrive, the integration makes one status request to check the setting. Changing a mode sends only the settings that differ, one at a time, and stops if a change fails.

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
