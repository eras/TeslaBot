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
notifications fan out only to chats, independently with a ten-second send
bound; unsuccessful notices are dropped, not queued or rerouted. A failed
interactive reply never falls back to another destination. `info delta`
history is per adapter and room role and advances only after a successful
send. Scheduled info always sends full output and does not advance chat
delta histories.

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
switch, charge-limit number, refresh button, and separate max-defrost on/off
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

The retained `teslabot/<id>/state` JSON contains observed values and
`observed_at`. Non-retained `teslabot/<id>/result` reports action outcomes.
No automatic polling is performed: use a Home Assistant automation to press
the refresh button periodically if desired. Successful adjustments trigger a
single follow-up read; a failed or delayed read never substitutes the requested
value for observed state. Old retained readings may be stale after a restart;
use the last-refresh sensor to assess freshness. Location is not published.
Chat commands do not automatically update MQTT state.

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
