Copyright Erkki Seppälä <flux aet inside.org> 2022-2023

# TeslaBot

..for [Matrix](https://matrix.org) and
[Slack](https://slack.com). Licensed under the [MIT
license](LICENSE.MIT).

TeslaBot allows interfacing with your Tesla vehicle over Matrix or
Slack. It provides functions such as turning climate control on or
off, determining the location of the vehicle (with a list of
pre-configured locations for labeling location or for limiting
information), and adding timers for those functions.

# Setup

First choose if you want to control the bot via Matrix or Slack. Can't
do both with one bot this time.

## Matrix

Chose Matrix? Good! Then you need to create a new Matrix id (aka mxid)
in the homeserver of your choice. Once you have that, use that as the
configuration key `matrix.mxid` (see [the example config.ini](config.ini.example)).
The homeserver also needs to be configured at this time. You will also
need to enter a password here.

On the first connect TeslaBot will create an access token and a device
id and write that to the _state file_. After this point the password
no longer needs to be available in the config file.

The bot supports end-to-end encryption. To make use of this you need
to add the list of trusted mxids in the configuration. This is not
very secure against active attacks where e.g. an attacker is able to
introduce new devices to the device list, but it should be secure
against passive attacks where an attacker gains access to the
(encrypted) messages.

You also may want to add the bot device as a trusted device in your
Matrix client of choice.

Contributions for a more secure way to do this are welcome, but
ultimately it will be solved once [matrix-nio supports
cross-signing](https://github.com/poljar/matrix-nio/issues/229),
making the normal operation of this function quite a lot less tedious.

## Slack

Controlling your corporate fleet? First you need to create a new app
for this in the Slack workspace.

The bot uses websockets, so no need to configure any inbound hooks,
just any random box will do.

TODO: make these instructions a bit more complete

Invite the bot to the room in the configuration.

Set environment variables: 
  - ENVIRONMENT: if running on google cloud, use gcp
  - CHANNEL: slack channel
  - SLACK_ADMIN_CHANNEL_ID: Channel's id that's used for authentication
  - CONTROL: slack
  - EMAIL: tesla login email
  - STORAGE: type of storage (local / firestore)
  - GCP_PROJECT_ID: speaks for itself
  - SLACK_APP_SECRET_ID: secret id for retrieving slack app key in google secret manager
  - SLACK_API_SECRET_ID: secret id for retrieving slack api key in google secret manager

Firestore requires the bot to be run on gcp, because authentication is done automatically there.

## Tesla

On the first startup the bot needs authorization to the Tesla API. In
the bot's admin room:

1. Run `!authorize` and open the URL TeslaBot provides.
2. Authenticate with Tesla in the browser.
3. After reaching the `/void/callback` URL, copy the full browser URL.
   The resulting page may display an error or Page Not Found; this is
   expected.
4. Send `!authorize <paste URL here>` in the admin room.

TeslaBot stores the resulting token in its configured credential store;
TeslaPy refreshes it automatically. If an old cached token no longer
works, run `!logout` and start this flow again.

## MQTT and Home Assistant

Install `TeslaBot[mqtt]` (the Docker image includes it), plus the extras for
each selected chat adapter. Set `common.control = matrix,mqtt`,
`slack,mqtt`, or `matrix,slack,mqtt` and configure those sections as in
`config.ini.example`. Names are strictly lowercase; surrounding whitespace
is trimmed. Empty components, duplicate names, and unknown names fail startup
before clients are constructed. Existing single names work unchanged and an
absent setting still defaults to `slack`.

All adapters share one Tesla session, application, persisted settings, and
scheduler. Interactive replies stay on the admitted adapter and admin/normal
room role, including authorization links and delayed wake notices. Slack
accepts human commands only from its configured normal and admin channels.
Local commands (`ping`, Matrix `sameroom`) remain local. `require_bang` is
shared immediately and restored before ingress starts. Startup and timer
notifications fan out only to chats, independently with finite send bounds;
unsuccessful notices are dropped, not queued or rerouted. Other chat adapters
retain a ten-second composite bound. Matrix uses separate `[matrix]`
`readiness_timeout` and `send_timeout` budgets, each defaulting to 120 seconds.
Both accept positive finite seconds (including fractions); zero, negative,
nonfinite, and nonnumeric values fail startup. Waiting for initial sync does
not consume the encrypted delivery budget, which includes key claims/sharing
and HTTP work. Direct/local and composite Matrix sends use the same limits;
the composite does not impose a shorter Matrix deadline. Healthy siblings
receive broadcasts immediately and continue serving while Matrix waits. The
broadcast caller, including a timer, waits for these finite sequential phase
limits and cancellation cleanup before proceeding. Shutdown cancels and awaits
pending Matrix sends before closing its client. Timed-out messages are not automatically resent:
delivery may already have reached the server. Encryption, device verification,
trust configuration, and existing SDK session warnings are unchanged. A failed
interactive reply never falls back to another destination. `info delta`
history is per adapter and room role and advances only after a successful
send. Scheduled info always sends full output and does not advance chat
delta histories.

Matrix serializes SDK delivery calls; queueing for that critical section is
included in the send budget. If this adapter interrupts key claiming, it
releases only the sharing event created by that owned call after HTTP work
has drained, allowing a new explicit send to prepare keys normally. Other
senders' sharing events are not removed or signalled. Caller cancellation and
close signal cancellation once, then await actual SDK cleanup even if callers
are cancelled repeatedly. Concurrent close calls join one client-close operation.

Mixed mode can start without Tesla authorization: MQTT stays offline while
an admin authorizes through either chat. Auth changes notify MQTT without
waiting for the broker. MQTT becomes online only after the latest generation's
vehicle enumeration, discovery, and subscriptions are ready. MQTT-only
(`common.control = mqtt`) requires cached authorization and otherwise exits
nonzero with a diagnostic. MQTT-only neither runs nor overwrites saved timers.
MQTT deliberately has no authorization or other admin commands. Use a broker
account with access limited to the TeslaBot topics; enable TLS when connecting
to a remote broker.

TeslaBot publishes retained Home Assistant MQTT discovery configurations for
each vehicle: battery and charging sensors, a last-refresh timestamp, climate
switch, charge-limit number, five seat-heater numbers (0 off through 3 high),
steering wheel heater switch, refresh button, and separate max-defrost on/off
buttons. The latter are buttons because not all vehicle responses expose a
reliable max-defrost state. Vehicle topic IDs are stable hashes of the VIN,
falling back to the display name when no VIN is supplied. The topic prefix is
`teslabot` by default. Commands are non-retained messages to:

| Topic | Payload |
|-------|---------|
| `teslabot/<id>/refresh/set` | Any payload; fetch vehicle state |
| `teslabot/<id>/ac/set` | `ON` or `OFF` |
| `teslabot/<id>/sauna/set` | `ON` or `OFF` |
| `teslabot/<id>/charge_limit/set` | Integer `0` through `100` |
| `teslabot/<id>/seat_heater_left/set` | Integer `0` through `3` |
| `teslabot/<id>/seat_heater_right/set` | Integer `0` through `3` |
| `teslabot/<id>/seat_heater_rear_left/set` | Integer `0` through `3` |
| `teslabot/<id>/seat_heater_rear_center/set` | Integer `0` through `3` |
| `teslabot/<id>/seat_heater_rear_right/set` | Integer `0` through `3` |
| `teslabot/<id>/steering_wheel_heater/set` | `ON` or `OFF` |

The retained `teslabot/<id>/state` JSON contains observed values and
`observed_at`. Non-retained `teslabot/<id>/result` reports action outcomes.
No automatic polling is performed: use a Home Assistant automation to press
the refresh button periodically if desired. Successful and failed adjustments
trigger a single follow-up read after a configurable settling delay; a failed
or delayed read never substitutes the requested value for observed state.
Old retained readings may be stale after a restart;
use the last-refresh sensor to assess freshness. Location is not published.
The Home Assistant climate and steering wheel heater switches, charge-limit
number, and seat-heater numbers are optimistic: they
display the requested value immediately, then reconcile with the next observed
state. Max defrost uses stateless ON/OFF buttons, which have no optimistic state.
If the follow-up read fails, the optimistic display may persist until a later
successful refresh; command results alone do not correct that display.
Every successful manual or automatic observation publishes a fresh UTC
`observed_at` for the Last refresh timestamp sensor, even when telemetry values
are unchanged. It records the completed observation, not command/job enqueue
time. Failed, cancelled, or superseded reads do not initiate a new timestamp
publication; packets already submitted to the broker cannot be recalled.
Chat commands do not automatically update MQTT state.

Seat controls retain the existing `seat_heater_left/right/rear_left/rear_center/rear_right`
state fields, with integer levels 0 through 3. Discovery removes the old seat
sensor definitions and replaces them with number controls. Steering wheel heat
is observed from `steering_wheel_heater` as a boolean. Unsupported or missing
heater readings become unknown, rather than being inferred from a command.

### Read-Only Telemetry

Each successful manual or action-follow-up observation also updates these
read-only Home Assistant entities from the same retained vehicle state:

| Information | State fields and units |
|-------------|------------------------|
| Inside/outside temperature | `inside_temp`, `outside_temp`, Celsius regardless of GUI units |
| Charge current limit | Existing `charge_amps`, requested current in A, not actual current draw |
| Charging power | `charger_power_kw`, kW |
| Range-added charging rate | `charge_rate_kmh`, km of range added per charging hour, not amps or road speed |
| Estimated charge completion | `charge_finish_eta`, aware UTC ISO timestamp while Charging only |
| Odometer | `odometer_km`, km with fractional precision |
| Four tire pressures | `tpms_pressure_fl/fr/rl/rr`, bar; a measured zero remains zero |
| Software update information | `software_update_status/version`, `software_update_download_percent/install_percent`, `software_update_expected_duration_s` |
| Door lock | `locked`, true means locked in JSON; HA's lock binary sensor is OFF when locked, ON when unlocked |
| Four doors | `door_driver_front/rear_open`, `door_passenger_front/rear_open` |
| Four windows | `window_driver_front/rear_open`, `window_passenger_front/rear_open` |
| Other openings | `frunk_open`, `trunk_open`, `charge_port_door_open` |
| Car firmware | `car_version`, including the complete observed version/hash |

Seats and tires use physical left/right labels. Doors and windows use
driver/passenger labels, including on right-hand-drive vehicles. Opening code
zero means closed; positive integer codes, including vented windows, mean open.
Booleans and numeric strings are not interpreted as opening codes or heat levels.

Tesla API odometer and charging range rate are in miles and are converted by
1.609344 independently of GUI distance preferences. Temperatures and tire
pressures are already Celsius and bar. The completion estimate uses positive
`minutes_to_full_charge` preferentially, otherwise positive
`time_to_full_charge` hours, added to that observation's `observed_at`. It is an
estimate to the vehicle's charging target, not a guarantee of 100% SOC. Missing,
nonpositive, invalid, overflowing, or non-Charging estimates become unknown.
There is no additional HTTP request, timer, or polling to update an ETA.

Unavailable or unsupported fields are published as JSON `null` on each
successful observation. Discovery templates explicitly reset them to HA's
`None`/unknown state, including when an older retained payload lacks the field;
valid zero/false observations are preserved. Invalid continuous numbers,
negative measurements, and nonfinite converted values are unknown. Software
strings are trimmed without guessing an update status. Text exceeding HA's
255-character state limit remains complete in MQTT but renders unknown in HA;
it is not silently truncated. Raw diagnostic data remains detached inside the
application and is not added to the public MQTT state.

One instance-level diagnostic **TeslaBot version** sensor uses retained
`teslabot/version` and discovery
`homeassistant/sensor/teslabot/version/config`. It shares the Action refresh
delay device and its `sw_version` metadata, with custom prefixes handled as for
the existing setting. It is published during reconciliation without requiring
a vehicle observation, even with zero vehicles or before authorization; shared
availability remains offline until authorized reconciliation completes. It
does not change any vehicle timestamp or belong to the vehicle ownership
manifest. New vehicle configurations follow existing owned-vehicle cleanup.

These entities introduce no seat, lock, door, software-install, or other command
topics. The command whitelist remains the four vehicle routes above and the
existing action-refresh-delay setting. Chat's existing charge-rate line now
uses km/h or mi/h to match its distance preference, while current limit stays A.

### Action Refresh Delay

`[mqtt] action_refresh_delay = 5` sets the bootstrap delay in integer seconds,
inclusive range `0..300`. It applies to MQTT AC ON/OFF, sauna/max-defrost ON/OFF,
and charge-limit changes. `0` preserves the immediate, awaited result-then-read
behavior. Manual refresh has no settling delay. Positive-delay follow-ups are
one-shot background jobs outside the Tesla operation gate; the receiver never
sleeps for the settling interval. The interval starts at operation completion,
whether successful or failed, not message arrival or completion of result
publication.

Home Assistant discovery adds one instance-level **Action refresh delay**
configuration number on the TeslaBot device. Its exact topics are:

| Topic | Contract |
|-------|----------|
| `<prefix>/action_refresh_delay/set` | Non-retained ASCII decimal integer `0..300` |
| `<prefix>/action_refresh_delay/state` | Retained committed integer seconds, QoS 1 |

Command payloads reject signs, spaces, leading zeros except `0`, fractions,
booleans, Unicode digits, and nonfinite values. Ordinary INI whitespace is
trimmed. A separately persisted scalar, scoped to broker endpoint/topic namespace,
overrides the INI bootstrap on restart. Invalid saved values fail startup clearly.
The setting is unavailable while Tesla authorization is absent, consistent with
instance availability. Discovery and committed setting state are republished on
each connection before online, including when no vehicles are selected.

Storage success commits a setting change before state notification. Save failure
restores the prior effective value and in-memory entry and logs the full error;
publication failure afterward does not undo the durable value, and reconnect
republishes it. Storage reporting failure after actually committing is inherently
uncertain: there is no cross-storage/broker transaction. Changes affect future
actions only; already pending timers are not retimed or invalidated.
A settings update sends no Tesla request and changes no auth generation.

Jobs coalesce per vehicle. Before dispatching any newer **valid** vehicle action
or manual refresh, TeslaBot invalidates, cancels once, and awaits the older job,
including queued/active SDK reads and in-flight state publication. Rapid valid
changes therefore leave one read after the latest operation's interval. A newer
valid but failed action still supersedes the old read and schedules a replacement;
invalid, retained, unknown-vehicle, or unsupported requests leave useful pending
work alone.
Vehicles have independent timers, while actual SDK operations remain serialized.
Manual refresh is immediate only in the settling sense: an already sent request
or publication must drain safely, and the foreground receiver can wait for that
drain or for a vehicle operation. Packets already submitted cannot be recalled.

Pending jobs belong to one client connection/auth generation. Logout, reconciliation,
disconnect, or shutdown invalidates and drains them before client exit; reconnect
does not replay them. Expected read failures retain the previous observed state
and do not rewrite command results. Broker publication failures reconnect
locally; unexpected programming faults reach process supervision. Detailed job,
payload, exception, and traceback diagnostics remain available.

The delay controls observed-state reads, not the optimistic Home Assistant display.
It is not confirmation polling or a guarantee that Tesla has settled.
Even after waiting, only observed values are published; stale readings
may still change the dashboard, not issue reversing OFF commands. Larger values
trade dashboard freshness for settling time and may help charge-limit or defrost
as well as AC observations. Chat actions still produce no automatic MQTT state
publication.

Telemetry crosses the Tesla SDK boundary as detached plain nested data, copied
inside the serialized worker operation. Snapshots and cached name metadata do
not contain live SDK vehicles; formatting, name validation, and location lookup
cannot trigger SDK lazy fetches. MQTT publishes only an explicit scalar state
projection, never raw diagnostic data. Missing/null sections or wrong scalar
types produce `null` observations rather than requested/guessed values. Numeric
strings and booleans are not silently treated as measurements. Complete chat
info wording is retained; missing or invalid fields required by that view give
a controlled availability error, while optional climate/location values may be
unknown. Unavailable coordinates cannot create a saved location. Vehicles with
empty, null, or absent display names remain operable by VIN or single-vehicle
selection and are not selectable by the presentation label `Unnamed vehicle`.
SDK wake logging uses a worker-local temporary label when needed, restored
before returning data; it never supplies an identity or fabricated parser name.
Required state/id_s or identity errors remain explicit instead of causing hidden
fetches. Raw diagnostic fields remain
available in detached snapshots and detailed logs.

Valid typed AC, sauna, and charge-limit calls return `ActionResult(success=False)`
for expected vehicle, HTTP, timeout, connection/protocol, or OAuth failures after
the existing retry/auth-generation handling. Results retain resolved/requested
identity, action/value, and actual error details. Invalid arguments may raise;
cancellation and configuration/programming failures propagate. Failed actions
do not trigger telemetry refresh. If a successful action's follow-up read fails,
its acceptance result stays truthful and prior retained state stays unchanged.

Discovery ownership is recorded in the configured state store, scoped by
broker endpoint and topic namespace. Obsolete owned discovery configs and
retained state are deleted before online, including after restart or logout.
Keep the state store when restarting; prior versions did not record ownership,
so pre-existing obsolete discovery may require manual removal. Use a distinct
`mqtt.prefix` per instance: custom prefixes also have distinct discovery IDs.
The default prefix preserves existing discovery IDs. Only one process may
own a given broker/topic namespace. Every reconnect uses a clean session;
retained commands are rejected and old-generation queued work is discarded.
Changing `override_vehicles` triggers reconciliation without polling.

Tesla operations (selection, wake, request, enumeration, and auth) are
serialized. An MQTT action and its follow-up refresh are two serialized
observations, so a chat request may run between them. Logout closes admission
and invalidates the generation before draining an in-flight request and
clearing credentials. An already sent blocking request cannot be undone;
its superseded result is discarded. Failed logout stays fail-closed until
explicit authorization. Observed OAuth errors or HTTP 401/403 invalidate
authorization without polling. Failed token exchange stays offline; successful
exchange commits before response delivery, and initializes the shared vehicle
cache when enumeration succeeds. Terminal adapter/scheduler failures or
unexpected returns exit nonzero and clean up owned tasks/sessions. Transient
MQTT/Slack disconnects retry locally; Matrix uses its native sync retries.
Interrupted MQTT discovery also retries transient Tesla HTTP 408/429/5xx,
timeout, and connection failures locally, offline, with backoff capped at
60 seconds and interrupted by auth changes. Successful discovery is not polled.
Expected delivery failures drop the response without a second error send or
fallback; other adapters keep serving. Expected scheduled validation or vehicle
request failures do not stop the scheduler: one-shot timers are consumed and
unexpired recurring timers retain their next activation, including across restart.
Programming or infrastructure failures remain terminal.
Tesla HTTP sends enforce a 30-second connect/read timeout at the actual SDK
HTTP boundary, including authorization GET, token exchange, and refresh.
This is not an absolute wall-clock deadline for DNS, a trickling response,
redirects, or an entire wake loop. Cancellation drains an already running
thread asynchronously before releasing the session gate; shutdown may take
the remaining request/wake duration rather than zero time. Retries and unsent
work are cancelled on shutdown. Rejected or unsuccessfully logged-out SDK
credentials are cleared before creating a fresh authorization URL; failure
to initialize that flow gives an actionable error rather than a `None` URL.
Startup authorization notices are optional and skipped when superseded by an
auth-generation change. Default application INFO logs show selected controls,
chat readiness, and MQTT reconciliation/readiness. Diagnostic logs retain
command payloads, vehicle data, SDK output, exception details, and tracebacks;
they may contain sensitive information and should be stored and shared accordingly.

## Setup with Docker

```
mkdir tesla-data
curl https://raw.githubusercontent.com/eras/TeslaBot/master/config.ini.example > tesla-data/config.ini
emacs tesla-data/config.ini # edit for your needs

# Use -ti first time to easily see if everything is alright; once it works, replace it with -d
# Also consider --restart always
docker run -ti --name teslabot -d -v $PWD/teslabot-data:/data ghcr.io/eras/teslabot:latest
```
You should not use `latest` but the correct version tag. The image currently weighs around 130MB, so it's not tiny, but not huge either.

You can build the Docker image yourself with:
```
git pull https://github.com/eras/TeslaBot
cd TeslaBot
docker build -t teslabot .
```

and then replace the `ghcr..` in the `docker run` command with `teslabot`.

You can also use `docker-compose up` to build and start in one go. Review [the very basic yaml file](docker-compose.yaml) first.

## Installation without docker

```
# optional:
python3 -m venv teslabot
. teslabot/bin/activate
pip3 install wheel

sudo apt install -y libolm-dev libffi-dev
git clone https://github.com/eras/TeslaBot
pip3 install ./TeslaBot[matrix,slack]
curl https://raw.githubusercontent.com/eras/TeslaBot/master/config.ini.example > config.ini
emacs config.ini # edit for your needs

# Run and do the API authentication
python3 -m teslabot --config config.ini
```

You can use e.g. `screen`, `tmux` or `systemd` to arrange this process to run on the background.

## Commands

Note that by default you need to prefix commands with ```!```.

| command                                     | description                                                                                                                                    |
|---------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------|
| help                                        | Show the list of commands supported.                                                                                                           |
| authorize                                   | Start the authorization flow. Works only in admin room (though you could only have one and same for control and admin).                        |
| authorize url                               | Last phase of the authorization flow.                                                                                                          |
| logout                                      | Remove authorization tokens.                                                                                                                   |
| climate on [name]                           | Sets climate on. Needs vehicle name if you have more than one Tesla.                                                                           |
| climate off [name]                          | Sets climate off.                                                                                                                              |
| ac on/off [name]                            | Same as !climate.                                                                                                                              |
| sauna on/off [name]                         | Sets max defrost on/off.                                                                                                                       |
| info [delta] [name]                         | Show information about the device, such as about location, climate and charging. "delta" shows only differences to the previous info.          |
| at 06:00 command                            | At 06:00 (not before the current time) issue a command. Can be climate or info, maybe more in future.                                          |
| at 600 command                              | Same                                                                                                                                           |
| at 10m command                              | Schedule at now + 10 minutes                                                                                                                   |
| at 1h1m command                             | Schedule at now + 1 hour 1 minute                                                                                                              |
| at tomorrow 02:00 command                   | Schedule at 02:00 tomorrow                                                                                                                     |
| at wednesday 02:00 command                  | Schedule at 02:00 this or next wednesday (not in the past)                                                                                     |
| at 06:00 every 10m command                  | Schedule at 06:00 and re-do every ten minutes                                                                                                  |
| at 06:00 every 10m until 30m command        | Schedule at 06:00 and re-do every ten minutes for 30 minutes                                                                                   |
| at 06:00 every 10m until 7:00 command       | Schedule at 06:00 and re-do every ten minutes until 30m                                                                                        |
| at 06:00 every 10m until wed 7:00 command   | Schedule at 06:00 and re-do every ten minutes until 30m on wednesday                                                                           |
| atrm 42                                     | Cancels a timer                                                                                                                                |
| atrm 42 44                                  | Cancels two timers                                                                                                                             |
| atq                                         | Lists timers                                                                                                                                   |
| set location-detail detail                  | Defines how precisely the location is displayed. See !help.                                                                                    |
| set require-! false                         | After this commands no longer need the ! prefix to work.                                                                                       |
| location add lat,lon [near 200 m] [address] | Add a new named location. These are for the bot only, not related to Tesla navigation. You can replace "lat,lon" with "current [vehicle name]" |
| location rm location name                   | Remove a named location.                                                                                                                       |
| location ls                                 | List location.s                                                                                                                                |
| charge start/stop                           | Start or stop charging.                                                                                                                        |
| charge port open/close                      | Open or close charging flap.                                                                                                                   |
| charge amps 10                              | Set charging amperage (per phase).                                                                                                             |
| charge limit 70                             | Set charging limit in percents.                                                                                                                |
| charge schedule 04:00                       | Schedule charging to start at given time. This uses Tesla's scheduler, not the one in this bot.                                                |
| charge schedule disable                     | Disable charging schedule.                                                                                                                     |
| heater seat n off/low/medium/high           | Adjust seat heaters. Works only if AC is on.                                                                                                   |
| heater steering off/high                    | Adjust steering wheel heater. Works only if AC is on.                                                                                          |
| command # comment                           | Run command; ignore # comment                                                                                                                  |
| # comment                                   | Ignore message                                                                                                                                 |
