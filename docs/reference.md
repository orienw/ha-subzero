# Reference

Detailed appliance behavior, event handling, and recovery rules. For installation, supported appliances, and a feature overview, see the [README](../README.md).

## Entities and values

Entities are created from recognized properties reported by each appliance, both at setup and when new properties appear in push updates. There is no model allowlist. Properties that the Sub-Zero app ignores for an appliance type are also ignored here, together with entities that depend on them. For example, an ignored probe does not produce probe-temperature entities.

Installing, restarting, or reconnecting the integration does not change appliance settings. Settings change when a control or automation requests it.

### Temperatures

Home Assistant displays temperatures and accepts setpoints in your preferred unit. The appliance API uses whole-degree Fahrenheit setpoints, so Celsius requests can be rounded.

Appliance units are read at startup and on reload. If the appliance list is temporarily unavailable, the integration uses the last saved unit. An appliance with unknown units keeps its non-temperature entities.

Configured setpoints and display temperatures are separate properties. On the primary tested refrigerator, temperature entities are configured setpoints; the integration does not turn a setpoint into an inferred measured temperature.

### Timestamps

Timestamps with explicit UTC offsets retain those offsets. A timestamp without an offset uses the time zone inferred from the appliance's last pushed clock. If that offset matches Home Assistant's time zone, the integration uses that zone so daylight saving changes are followed. Otherwise, it uses the reported fixed offset.

Status reads report the clock in UTC and do not change the inferred appliance time zone. Before a push supplies the clock, Home Assistant's time zone is used, following the app's use of the phone's local time.

## Refrigerators and wine storage

The refrigerator controls follow the appliance's reported capabilities and limits.

| Setting | Behavior |
| --- | --- |
| Crisper temperature | Select Manual before changing the setpoint. In Automatic mode, the control is unavailable while its sensor continues to show the configured value. The manual range stays within 2°F of the refrigerator setpoint and within the appliance's refrigerator limits. |
| Refrigerator limits | Limits follow the reported appliance type, with model-based defaults when the type is unavailable. |
| Freezer setpoint | Turn off Max ice before changing it. Climate entities use the same limits and interlocks as the number controls. |
| Ice maker | The selector shows the selected mode. During Night ice, the separate Ice maker enabled status can be Off while its schedule pauses production. |
| Humidity | Normal and Enhanced can be selected. Disabled and Low are displayed when reported. Humidity control applies to the refrigerator zone. |
| Night mode | Dims interior lights when the room is dark. It is separate from Night ice. |
| Reset air filter | Sends the reset request and refreshes status. The filter-life sensor continues to show the appliance's reported value. |
| Door-open delay | Off (zero), 1, 2, 5, or 10 minutes before an open door triggers a notification. |
| Accent lighting | Off, On, Low, Medium, or High, when reported. |
| Operating mode | Normal, Sabbath, High use, Short vacation, or Long vacation. |
| Other switches | Air purification and the internal water dispenser, when reported. |

Monitoring includes refrigerator-drawer readings, air and water-filter life, water-filter capacity in gallons, power and service status, and reported Max ice and High use start/end times.

A negative water-filter capacity means reported usage has exceeded the filter capacity. Switches expose their own On/Off state without duplicate binary sensors. Ice-maker modes and operating-mode selectors also have reported on/off indicators.

Wine storage setpoints range from 40–65°F. Each reported wine zone can provide a setpoint control, climate entity, display temperature, and door status. Two zones are named Upper wine and Lower wine; doors receive zone names only when each zone has its own reported door. The Mode selector contains the operating modes offered for the appliance type, such as Sabbath.

Sub-Zero's guides explain [Night ice](https://www.subzero-wolf.com/assistance/answers/sub-zero/common/sub-zero-night-ice-mode), [crisper temperatures](https://www.subzero-wolf.com/assistance/answers/sub-zero/next-classic/sub-zero-classic-series-cl-refrigerator-drawer-temperature-contr), [humidity control](https://www.subzero-wolf.com/assistance/answers/sub-zero/next-classic/next-classic-humidity-control), and [Night mode](https://www.subzero-wolf.com/assistance/answers/sub-zero/next-classic/next-classic-night-mode).

## Dedicated ice makers

Dedicated ice makers can report power, door-open delay, Sabbath mode, water-filter status, cleaning progress, delay schedules, and fault or winterization flags.

Use **Sub-Zero: Schedule ice delay** to pause production for 1–12 hours. The action accepts the number of minutes before starting, with zero meaning immediately, and an option to repeat daily. The schedule settings are sent together, followed by a status refresh.

- **End current ice delay** resumes production while preserving a repeating schedule.
- **Cancel ice delay schedule** removes the scheduled delay.

Cleaning steps and fault clearing must be performed at the appliance. Cleaning sensors report progress.

## Ovens

Each reported cavity has separate entities. Double ovens use Upper oven and Lower oven; ranges with two cavities use Right oven and Left oven. Names can change when a second cavity is first reported, while existing entity IDs remain stable.

### Controls and Remote Ready

Temperature changes require the cavity to be running or in Remote Ready. Enable Remote Ready at the oven before each remote start; opening a door cancels it. Broil, Convection broil, Proof, Self clean, and Gourmet must be started at the appliance. These restrictions also apply to automations.

The cooking-mode selector lists the modes offered for the reported oven and cavity. For example, Convection bake appears on older E series ovens, which lack Warm; some lower ovens omit Convection, Dehydrate, or Bake stone. Selecting Off turns the cavity off. Unknown mode codes cannot be selected.

Each cavity also has an interior-light switch.

The Start oven button requires Remote Ready and a supported configured mode and temperature. It follows the app's command order: E series and M series receive the power command alone; other series receive cooking mode, power, then setpoint.

| Control | Range and restrictions |
| --- | --- |
| Oven temperature | Known series use limits for the selected cooking mode, including 85–110°F for Proof and 140–200°F for Warm. Unrecognized series use an 85–550°F fallback. Modes without adjustable temperatures retain their on/off controls. |
| Probe target | 120–210°F. The probe must be connected and the cavity running or in Remote Ready. |
| Kitchen timer | 0–719 minutes. A duration starts or restarts the timer; zero cancels it or clears a finished timer. |

### Readings and timers

Status includes cooking, preheated, Remote Ready, probe in use, probe within 10° of target, probe target reached, and Gourmet mode. Cooking and both kitchen timers expose active, under-one-minute, and complete states.

Idle oven-temperature fields reporting zero show as unknown. Measured oven temperature also shows as unknown while the oven is off, because ranges can keep reporting their last reading. Probe readings show as unknown when the probe is unused or reports 1, the app's no-reading value.

Broil level is calculated from the setpoint while broiling: below 400°F is Low, 400–499°F is Medium, and 500°F or above is High.

Kitchen-timer numbers show the configured duration when reported start and end times permit it. End-time sensors can drive countdown dashboards. A Dismiss kitchen timer button clears a completed timer, following the app's Tap to Dismiss behavior.

Gourmet program sensors translate reported recipe codes into names. Code zero and a cavity reporting Gourmet mode off show None. Unrecognized codes show as unknown. Select and start Gourmet programs at the oven.

## Hoods

Hoods provide four fan speeds and task-light brightness and white-temperature controls when reported.

| Setting | Range |
| --- | --- |
| Task-light brightness | 5–100% |
| White temperature | 2700–5000 K |
| Delayed shutoff | 0–719 minutes |
| Automatic fan sensitivity | Off, Low, Medium, High |

Brightness and white-temperature requests outside the supported ranges use the nearest limit. Other reported controls include halo lighting, button tones, and the control lock. Filter usage and allowance are durations; reset the filter counter at the appliance.

## Dishwashers

Cycle and mode selection are available while idle or waiting to start. Selecting a cycle does not start it. Start wash cycle uses the cycle and delay currently configured on the dishwasher and requires Remote Ready.

Modes are Normal, Child lock, and Sabbath. Reported status includes the door, Remote Ready, rinse aid low, softener salt low, and service required.

To enable remote starting, hold ENTER for five seconds, then close the door within four seconds. Opening the door cancels Remote Ready.

Delay start supports Off or 1–12 hours. Cancel wash cycle is available while running, drying, or waiting for a delayed start. When cancelling a delayed wash, confirmation also requires the active delay to end.

Some dishwashers report Start pending whenever they are idle, which the app displays as Idle. Automations waiting for idle should accept both values. Unknown cycle and status codes show as unknown.

The integration sends supported option properties; the appliance determines which options apply to the selected cycle. Options can include Heated dry, Extended dry, High temperature wash, Sanitize rinse, and Top rack only.

## Events and Firebase alerts

The **Appliance event** entity reports occurrences such as preheating, probe targets, timer completion, dishwasher cycles, door alerts, and maintenance notifications. Its attributes include event type, numeric code, sequence, appliance timestamp, `source`, and `received_at`.

Automations match event-type IDs such as `refrigerator_door_ajar`; Home Assistant displays readable names. An unrecognized code uses the `unknown` type and retains its numeric code. The event entity keeps its last occurrence when disconnected.

**Firebase appliance alerts** is optional and disabled by default. It receives alerts over the mobile app's separate Google Firebase channel and uses the same event entities. Alert metadata does not supply current sensor values. Occurrence and receipt times allow a late alert to be distinguished from a new occurrence.

### History and duplicate events

The first snapshot or status read after loading establishes a history baseline without replaying its events. If it omits history, later records are filtered against the startup timestamp. Events found during subsequent status reads are delivered immediately, including while push is recovering.

Repeated notifications and reconnect history are deduplicated while the integration remains loaded, including across appliance sequence-counter resets. Firebase copies are paired with state-notification copies using their code, sequence, and timestamps less than one second apart, accounting for Firebase's whole-second precision. Separate occurrences with identical identifiers and timestamps cannot be distinguished.

Replay protection depends on the appliance clock:

- If initial history is absent, a clock running ahead can make an older alert appear new.
- A clock five minutes behind Home Assistant can suppress the first five minutes of live events after reload.
- A backward clock adjustment can suppress events until the clock catches up with the retained history cutoff.

### Subscription lifecycle

The integration creates its own alert subscription identity. Disabling Firebase alerts or deselecting an appliance removes its saved subscriptions, with failed cleanup retried every five minutes. Reloading stops the listener but retains credentials and subscriptions for reconnection.

Deleting the integration attempts cleanup for up to 30 seconds and logs failures. Cleanup depends on cloud access and valid authentication. The API supports unsubscribing events but has no known operation for deleting the registration record itself.

## Diagnostics

Wi-Fi signal strength is enabled by default. **State confirmed**, uptime, IP address, MAC address, and live reporting mode are disabled diagnostic sensors; enable them in entity settings when needed. Disabling State confirmed does not disable checks or recovery.

Appliances truncate uptime to eight characters. At 100 hours it has ten-second precision; at 1,000 hours it has minute precision.

Diagnostics downloads use cached state. They omit account credentials, appliance names, serial numbers, and network identifiers, and list unrecognized state-key names without their values.

| Diagnostic | What it establishes |
| --- | --- |
| Connection handshake and last-frame times | Whether a connection was established and received frames, including heartbeats. |
| Received, ignored, and invalid notification counts | Appliance-notification processing since reload. Heartbeats are excluded from the received count. |
| Per-appliance snapshot and update counts | Parsed state traffic and when it last arrived. |
| Last appliance message | Channel traffic, including messages without entity state. |
| Verification statistics and missing keys | Check attempts, complete pushed snapshots, status reads, and the keys each source's latest observation lacked. |

Connection health and message counts do not prove that every state change arrived or was applied.

Debug logging for `custom_components.subzero` records channel-open attempts, notification types and key names, source timestamps and sequence values with their runtime types, parsed updates, status-response timing and values, and published coordinator state. Connection failures also record the WebSocket message type, close code/reason, and exception type when available. This distinguishes the returned status from the state published after concurrent updates. Logged state values are recognized scalar properties, excluding network identifiers.

## State checks and recovery

Selected appliances share tokens and one notification connection. Setup waits up to 16 seconds for initial push state, then requests missing state. Lost connections reconnect with increasing delays, and rate-limit responses are honored.

While the notification connection is open, accepted appliance channels receive another `open_cloud_async` command every 30 minutes. Incoming messages do not postpone this schedule. This does not request status or restart the shared connection, and runs even when periodic state checks or Home Assistant polling are disabled. A new connection starts the channel schedule again. Reopen failures are logged without marking an otherwise working channel unavailable; rate limits defer further reopens. The 30-minute interval is an experimental recovery policy, not a measured appliance-channel expiration time.

Periodic state checks are disabled by default. When enabled, checks run at the selected interval per appliance, staggered across selected appliances. Incoming updates do not postpone them. Checks read status without reopening channels or restarting the shared connection. Known offline appliance channels wait for their existing channel retry before automatic checks resume.

Configure offers one-, two-, five-, and ten-minute intervals. **No periodic checks** keeps alert follow-up and reconnect recovery reads; silently missed updates can leave stale values until those values are reported again. Existing saved intervals are preserved. Home Assistant's system option to disable polling turns off scheduled checks and alert follow-up reads.

Changes to the check interval and Firebase alerts apply without reloading entities. Changing the appliance selection or its saved temperature units reloads the integration.

A new appliance alert starts a status read after two seconds, then at most two more at one- and two-minute delays while a door, timer, or wash remains active. Reads stay at least one minute apart per appliance. Normal state changes do not start these reads. Alerts do not extend an existing window, except that an alert during its final read starts another window. Clear conditions, unavailability, or rate limits end it; a transient read failure uses the next attempt.

An accepted channel or incoming state can trigger a status read for an unavailable appliance or one awaiting reconfirmation after a push interruption, even with periodic checks disabled. Failed recovery reads back off while the stream and appliance channel remain open; stream disconnection stops recovery.

Background read failures preserve otherwise available state while push remains connected. A transport interruption keeps established readings available for up to two minutes without a new full snapshot or status read. Repeated disconnects, handshakes, and partial updates do not extend that window. While push stays down, each successful status read restarts it, so check intervals longer than two minutes alternate availability. Any successful status read or full snapshot restores state, including one that lacks a key reported before. A status read, or a snapshot that replaces cached state, publishes only what it reported, so an entity for a key it lacks is unavailable rather than stale until the key is reported again; a snapshot merged into available state keeps the earlier value. When a snapshot that ends an interruption or restores an unavailable appliance lacks keys snapshots have reported, a status read follows to fill them in, also when that snapshot supersedes a status reply that was in flight. Authentication failures and appliance-channel errors still affect availability immediately. Available cached values can therefore be old during cloud failures or rate limits. Sub-Zero publishes no API quota.

## Explicit status reads

**Sub-Zero: Read appliance status** waits for a read started after its request and for the resulting state to reach Home Assistant. It optionally returns `confirmed_at` and `missing_keys`, the state keys status reads have reported for the appliance model that the published state lacks. A failed read, a conflicting concurrent update, or a push reconnection during the read fails the action, stopping an automation unless `continue_on_error` is enabled. A reply that lacks a key does not.

Concurrent callers share reads. Cancelling a caller does not interrupt a read needed by others. `homeassistant.update_entity` also waits for a read; use the Sub-Zero action when failed confirmation must stop an automation.

**State confirmed** advances for a successful status read, or for a full push snapshot, that no concurrent update contradicts. Its `missing_keys` attribute lists the state keys that observation lacked: a status read is held to the keys status reads have reported for the appliance model, and a snapshot also to the keys snapshots have reported, since a snapshot merges into state from the last read. A key that only arrives in partial updates is held against neither, and notification history is not expected of a read. Diagnostics list each source's latest gaps under `missing_state_keys`. A missing key never keeps an observation from confirming state or restoring availability, and never refuses a control. It matters in two places: only a complete snapshot rescues a failed status request, since the rescue merges into cached state, and a control reads status first while the state lacks a key status reads have reported (see Control confirmation). Individual updates and cached data retained after failed reads do not advance the sensor. A conflicting partial still reaches Home Assistant, but prevents the read from certifying the merged state. A failed GET is rescued only by a concurrent snapshot that lacks no key snapshots or reads have reported, since the rescue merges into cached state and would otherwise confirm whatever a missing key last held; later conflicting updates still prevent confirmation. A reply that arrives after a snapshot has replaced state following a reconnect is superseded by that snapshot in the same way. Clock, uptime, and Wi-Fi signal readings change between any two observations, so they never count as conflicts, and neither does the appliance type, which an observation without a usable one keeps.

A full snapshot for the same model is ignored when its usable clock and uptime both regress by at least one minute. This guard does not order partial updates, which retain arrival order. Periodic checks can repair current state; they cannot reconstruct undelivered brief events or guarantee freshness during cloud failures.

## Control confirmation

Control changes require appliance-state confirmation. Property writes use up to three attempts, each allowing eight seconds for the request and push confirmation. Missing confirmation or command errors prompt a status read outside that deadline. Authentication and rate-limit errors stop immediately.

Repeated writes of an already-reported value also require a cloud acknowledgement. Ordered operations stop if a required write fails. Ice modes and remote starts preserve the app's write order, including repeated values. A start for an already-running oven changes only its setpoint.

Commands are serialized and check the state left by earlier commands before deciding what to send. Cancelling a command can leave its appliance effect unsettled, so the next command reads status first.

Controls require reconfirmed state after a push interruption. Cached availability during the reconnect window does not permit a write based on unverified interlocks. A full push snapshot or a status read must refresh state before controls proceed, and an unavailable push connection requires a fresh read before writing. A write's interlock checks skip keys the state does not carry, so whenever the state lacks a key status reads have reported for the model, whether a reduced reply or a reduced snapshot that replaced state left it that way, a control reads status first and then proceeds on the state that read returned. A reply that still lacks the key does not block the write: the appliance enforces its own interlocks, and refusing would refuse every control on the appliance for as long as its replies lack the key. A setting the state does not report is refused as unsupported. A reconnect attempt that fails before delivering anything does not invalidate a read in progress. Like a status read, the snapshot that ends a push interruption replaces cached state, so a value that only arrives in partial updates is unavailable until it is pushed again.

Kitchen-timer confirmation requires a reported end time within 65 seconds of the request time plus the requested duration. This prevents an old timer state from confirming a restart. Failed confirmation includes the last command error when available.
