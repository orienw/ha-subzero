# Sub-Zero and Wolf for Home Assistant

<img src="custom_components/subzero/brand/icon.png" alt="Sub-Zero integration icon" width="80">

A custom integration for connected Sub-Zero and Wolf appliances, installed through HACS. Sign in with your Sub-Zero Group Owner email and password directly in Home Assistant.

The integration reads appliance status over Sub-Zero's cloud service using the appliance's existing Wi-Fi connection. Bluetooth is not required.

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

## Entities

Entities are created only for recognized properties reported by the appliance. New recognized properties can also be discovered during push updates.

| Type | Available properties |
| --- | --- |
| Temperature setpoints | Refrigerator, freezer, crisper, oven, oven probe |
| Measured temperatures | Oven and oven probe |
| Filters | Air and water filter life remaining |
| Doors | Refrigerator, freezer, and oven door open |
| Ice-maker settings | Enabled, max ice, night ice |
| Operating modes | Sabbath, high use, short vacation, long vacation |
| Device status | Service required, power, air purification |
| Oven status | Cooking, preheated, light, remote ready, probe in use, probe target reached, Gourmet mode |
| Oven timers | Cooking timer complete, both kitchen timers active or complete |
| Diagnostic | Wi-Fi signal strength |

All reported, recognized properties are enabled by default. Ice-maker settings and operating modes report their current on/off states. Wi-Fi signal strength appears under Diagnostics.

Refrigerator temperatures are **configured setpoints**. Ovens also report measured oven and probe temperatures. Oven temperature fields that report zero while idle show as unknown; probe readings also show as unknown when the probe is not in use.

Temperature entities require an appliance configured in Fahrenheit in the Sub-Zero app. For other app temperature settings, this release omits temperature entities until their units can be verified; other entities remain available. Home Assistant can display Fahrenheit readings in your preferred temperature unit.

Oven cooking mode names and timer countdowns are not yet mapped.

## Compatibility

**Cloud status and push snapshots tested with Sub-Zero CL4850UFDID and Wolf SO3050PMSP. There is no model allowlist.** Other models can be added if the cloud service returns their status. Their available entities depend on which recognized properties they report. Adding an appliance does not imply every feature of that model is supported.

This integration provides status monitoring. Appliance controls and local network access are not implemented. Accounts requiring additional verification or an external sign-in provider are not supported yet.

## Updates and account access

Selected appliances share account tokens and one SignalR notification connection. Each appliance gets a full status read at setup and after 30 minutes without a changed state update. The integration does not poll appliance status every minute. Connection heartbeats keep the notification socket alive; reconnect attempts back off after failures. HTTP 429 responses honor `Retry-After` where provided.

Sub-Zero has not published an API quota that this project has verified. Push reduces repeated status requests, but does not guarantee immunity from rate limits, particularly with multiple appliances or unstable connections.

Your password is used for sign-in and is not saved. Home Assistant stores renewable account tokens in its configuration and refreshes them automatically. If renewal fails, Home Assistant asks you to sign in again. Protect Home Assistant backups as you would other account credentials.

This is an unofficial integration using the mobile application's cloud endpoints and application settings. Changes to Sub-Zero's login service, API, or shared application key can require an integration update. It is not affiliated with Sub-Zero Group.

## Development

Use Python 3.14:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```
