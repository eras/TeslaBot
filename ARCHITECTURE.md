# Architecture

TeslaBot is a single-process Python asyncio application that connects Tesla
vehicles to Matrix, Slack, and MQTT/Home Assistant. Multiple adapters share one
application, TeslaPy session, state store, and chat-command scheduler. There is
no inbound HTTP server or separate worker service.

This document describes implementation boundaries and maintenance entry points.
See [README.md](README.md) for user commands and MQTT behavior, and
[config.ini.example](config.ini.example) for adapter settings. Some older setup
text predates multi-control support; the implementation and the configuration
caveats below are important when deploying.

## Component Map

| Source                                                                             | Responsibility                                                                                                                               |
| ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| [teslabot/**main**.py](teslabot/__main__.py), [main.py](teslabot/main.py)          | Module entry point, configuration assembly, adapter construction, supervision, shutdown.                                                     |
| [config.py](teslabot/config.py), [env.py](teslabot/env.py)                         | CLI/INI access and the shared configuration/state dependency bundle.                                                                         |
| [control.py](teslabot/control.py)                                                  | Chat admission/parsing entry point, command/message contexts, local commands, `MultiControl` routing and adapter lifetimes.                  |
| [matrix.py](teslabot/matrix.py), [slack.py](teslabot/slack.py)                     | Chat transport setup, ingress, room/channel selection, delivery, reconnects, and transport state.                                            |
| [mqtt.py](teslabot/mqtt.py)                                                        | Restricted typed commands, Home Assistant discovery, retained observations, availability, reconciliation, and delayed refresh jobs.          |
| [tesla.py](teslabot/tesla.py)                                                      | `App`, Tesla authorization and serialized SDK operations, command registration, vehicle selection, typed actions/snapshots, chat formatting. |
| [commands.py](teslabot/commands.py), [parser.py](teslabot/parser.py)               | Command registry, invocation tokenization, composable argument parsers and marked parse errors.                                              |
| [appscheduler.py](teslabot/appscheduler.py), [scheduler.py](teslabot/scheduler.py) | Persisted command timers and the generic one-shot/periodic execution engine.                                                                 |
| [locations.py](teslabot/locations.py)                                              | Named coordinates, distance/nearest-location lookup, location commands and persistence.                                                      |
| [state.py](teslabot/state.py), [filestate.py](teslabot/filestate.py)               | Section-based state API, save contributors, local INI or Firestore storage.                                                                  |
| [asyncthread.py](teslabot/asyncthread.py), [utils.py](teslabot/utils.py)           | Blocking SDK offload with cancellation draining, delayed progress notices, and shared utilities.                                             |
| [gcp_secret_reader.py](teslabot/gcp_secret_reader.py), [log.py](teslabot/log.py)   | Optional GCP configuration source and stdout logging setup.                                                                                  |

`tesla.py` contains both application orchestration and Tesla-specific behavior;
it is not merely an HTTP client. Adapters should use its operations rather than
create independent Tesla sessions or manipulate live SDK vehicles.

## Runtime Lifecycle

1. `python -m teslabot` calls `main.main()`, which runs `async_main()` through
   `asyncio.run()`. Logging and CLI parsing happen first; `--version` exits
   before configuration and clients are created.
2. Startup loads either INI configuration or the GCP secret-source dictionary,
   validates `common.control`, creates `FileState`, and shares these via `Env`.
   Control names are unique, lowercase `matrix`, `slack`, or `mqtt`, separated
   by commas with surrounding whitespace allowed. A missing control value
   defaults to Slack.
3. Selected adapters are imported and constructed, wrapped in `MultiControl`,
   and connected to one `tesla.App`. MQTT also receives the application directly
   through `set_app()` and subscribes to authorization-change events.
4. `App.initialize()` restores shared settings before ingress starts. With a
   chat adapter present it loads timers and registers their state contributor.
   Cached authorization triggers an initial vehicle enumeration attempt.
5. `MultiControl.run()` starts each adapter's setup/run task independently.
   Alongside it, `App.run()` starts the scheduler when chat is enabled, sends
   chat startup/authorization notices, and monitors the scheduler task.
6. An uncaught task failure or unexpected top-level return terminates the
   process with a nonzero exit. Cleanup cancels and awaits owned tasks, closes
   adapters, then closes the application/Tesla session. Transient transport
   failures handled inside an adapter do not trigger this process-wide path.

MQTT-only startup requires cached Tesla authorization. Mixed chat/MQTT startup
can wait for chat authorization while MQTT remains offline. MQTT-only neither
loads/runs saved chat timers nor registers the contributor that rewrites them.

## Chat Command Flow

1. The transport admits messages from its configured/discovered command rooms
   or channels and constructs `CommandContext` with adapter identity, admin-room
   role, and a transaction identifier.
2. `Control.process_message()` applies the shared `require_bang` setting.
   `Invocation.parse()` strips `#` comments and splits command/arguments.
   Adapter-local commands such as `ping` and Matrix `sameroom` stay local;
   other commands go to `App.command_callback()`.
3. The command registry validates arguments through `parser.py`, then invokes
   an application handler. Commands include vehicle information, climate,
   charging, locks, heaters, sharing, locations, settings, and timers.
   Authorization and logout require the admin-room role.
4. Vehicle work crosses the serialized SDK boundary described below. Replies
   use `CommandContext.to_message_context()` so `MultiControl` sends them only
   through the originating adapter and room role. A failed interactive reply
   is not redirected to another destination.

Origin-free startup and scheduled messages broadcast concurrently to chat
adapters only. Each destination has finite delivery bounds; failure is logged
and dropped rather than queued. The composite's default per-chat bound is ten
seconds. Matrix opts out of that outer bound and implements separate readiness
and delivery budgets, each defaulting to 120 seconds. Its delivery budget
includes lock queueing, encryption/key sharing, and HTTP work. Cancellation
waits for owned SDK cleanup, so a timeout is not a hard shutdown deadline.

Chat `info delta` histories are in-memory and separated by adapter/room role;
they advance only after successful delivery. Scheduled info produces full
output without advancing those histories. Chat actions do not automatically
publish MQTT telemetry.

## Tesla Boundary and Data

`App` owns a `TeslaSession` (a TeslaPy subclass), a task-reentrant operation
gate, an authorization lock, and an authorization generation counter.
Selection, enumeration, wake-up, requests, and authorization operations share
the gate. This serializes access across all adapters and the scheduler, not
just within one vehicle or transport.

Blocking SDK work runs in the default executor through `asyncthread.to_async()`.
Cancellation cannot undo a sent request: the coroutine drains the running
thread asynchronously before releasing the operation gate. `TeslaSession.send()`
enforces the configured 30-second HTTP timeout even where OAuth/TeslaPy would
otherwise pass no timeout. This bounds connect/read phases, not the complete
wake/retry sequence or absolute wall-clock duration. Retry logic is in
`App._retry()`; do not layer uncoordinated SDK retries into adapters.

Authorization changes increment `auth_generation`, clear cached vehicle
metadata, and notify listeners without waiting for MQTT. Logout closes
admission and invalidates the generation before draining work and removing
credentials. Superseded requests/results are rejected; a failed logout remains
fail-closed. Observed OAuth and supported HTTP authorization failures also
invalidate authorization. Browser authorization is explicit through admin chat,
not an interactive prompt inside the SDK worker.

`plain_data()` copies SDK mappings into plain nested data inside the worker.
Cached vehicle metadata and `VehicleSnapshot.data` must not retain SDK vehicles
or sessions: parser validation, location lookup, and formatting must not cause
lazy network fetches. `refresh_vehicle()` returns a `VehicleSnapshot` with an
aware UTC observation timestamp and normalized optional scalar telemetry.
Missing/invalid measurements remain unknown instead of becoming requested
values. Typed actions return `ActionResult` for expected request failures;
invalid arguments and unexpected faults can still raise.

Vehicle topic identifiers are the first 16 hexadecimal characters of a SHA-256
hash of VIN, falling back to display name. `Unnamed vehicle` is a presentation
fallback, not an identity or parser-selectable fabricated name. MQTT publishes
an explicit scalar projection of snapshots, not raw diagnostic data or location.

## MQTT and Home Assistant

`MqttControl` uses `aiomqtt` and does not expose the chat command interpreter,
authorization commands, or timer commands. Its `_handle()` is the vehicle-topic
and payload admission boundary; `_discovery()` defines Home Assistant entities.
When adding controls, update both alongside typed application operations and
tests rather than forwarding arbitrary Tesla SDK command names.

On each clean broker connection or authorization-generation change,
`_reconcile()` publishes offline availability, instance settings/version
discovery, enumerates authorized vehicles, cleans obsolete owned discovery and
retained state, publishes current discovery, and installs subscriptions. Only
the current authorized generation becomes online. Broker/network interruptions
retry locally with capped backoff; unexpected faults propagate to supervision.
A retained last will supplies offline availability on lost connections.

Commands are non-retained messages. Retained requests, unknown vehicles,
unsupported routes, and invalid payloads cannot initiate vehicle operations.
Action outcomes go to non-retained `<prefix>/<vehicle-id>/result`; observations
go to retained `<prefix>/<vehicle-id>/state`. There is no automatic polling.
Manual refresh reads immediately, subject to serialization and draining older
work. Successful observations get a new `observed_at` even if values did not
change; failed reads preserve the prior retained state.

After a valid adjustment, the MQTT adapter publishes its result and arranges a
single follow-up observation, including after expected action failure if the
session remains current. This is adapter behavior, not an implicit read inside
the typed action. `action_refresh_delay` defaults to five seconds, accepts
integer seconds `0..300`, and starts at operation completion. Zero awaits the
read inline; positive values create a background job outside the operation
gate. New valid work supersedes, cancels, and drains the prior job for that
vehicle. Other vehicles have independent timers, but all actual SDK work still
shares the gate. Disconnect, logout, reconciliation, and shutdown invalidate
and drain connection-owned jobs; reconnect does not replay them.

Discovery ownership is persisted before publication so interrupted setup can
be cleaned on the next connection. Ownership and the saved refresh-delay scalar
are scoped by broker host/port plus state/discovery prefixes. Use one process
per broker/topic namespace and a distinct prefix per instance. Preserve the
state store across restarts. There is no atomic transaction across storage and
the broker: delay changes commit to storage before retained notification, and
publication failure does not roll back a durable change.

## Scheduling and Locations

`AppScheduler` translates `at`, `every`, `until`, `atrm`, and `atq` into generic
`scheduler.OneShot`/`Periodic` entries. The scheduler uses a condition variable
to wake when entries change and awaits callbacks serially. Timer state stores
command tokens, identifiers, activation times, recurrence intervals, and
optional end times as JSON values in the `timers` section. Timing uses local
datetime/wall-clock conventions, unlike UTC MQTT observation timestamps.

Activation advances/removes the timer and persists state before executing its
command with a scheduled, non-admin context. Expected validation or vehicle
failures consume one-shots but retain unexpired recurring timers. Unexpected
failures can terminate the scheduler and reach process supervision. These bot
timers are distinct from Tesla's vehicle-side scheduled-charging command and
from MQTT's transient settling-delay jobs.

`Locations` persists user-defined labels and coordinates in the `locations`
section. It computes nearest locations locally for chat location-detail modes;
it is not a geocoding service. Saving the current vehicle location requires
usable observed coordinates.

## Configuration and Persistence

`Config` uses `ConfigParser`. Relative configuration filenames are tried under
the package directory first, then from the working directory. Other relative
paths (state, credentials, Matrix store) are used relative to the process
working directory, not rebased to the configuration file's directory.

| Data                        | Storage and owner                                                                                                                       |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Static settings             | INI selected by `--config` (default `config.ini`), or a complete GCP plugin dictionary instead of INI.                                  |
| Shared application settings | `tesla` state section: location detail and vehicle filter; `control` section: `require_bang`. Saved values override bootstrap settings. |
| Timers and named locations  | `timers` and `locations` sections managed by their `StateElement` contributors.                                                         |
| Matrix session/routing      | `matrix` section: access token, device ID, sync token, normal/admin room IDs; separate SDK encryption store at `matrix.store_path`.     |
| Slack routing               | Resolved normal `channel_id` in the `slack` section; configured admin channel remains separate.                                         |
| MQTT bookkeeping            | `mqtt_owned` ownership manifest and `mqtt_action_refresh_delay` committed scalar, keyed by namespace hash.                              |
| Tesla credentials           | TeslaPy cache, default `cache.json`, overridden by `tesla.credentials_store`, or cloud cache callbacks under the selector noted below.  |

`State.save()` asks registered `StateElement`s to populate sections, then calls
the storage backend. Local `FileState` writes `<state_file>~` and replaces the
target with `os.replace()`. Its default filename is `state.ini`. Firestore state
uses the `tesla/state` document. Storage calls are synchronous even though the
save API is async; do not assume all persistence is offloaded or transactional.

Current configuration caveats, not guarantees to paper over:

- `main.py` and `App.__init__()` read `common.storage` without a fallback.
  The example INI omits it; add `storage = local` for local operation.
- Application state selects Firestore for `storage = firestore`, but Tesla
  credential callbacks select Firestore only for `storage = cloud` (document
  `tesla/cache`). These selectors are inconsistent; do not assume one value
  moves both stores to Firestore.
- The Firestore state loader passes `DocumentReference.get()` directly to
  `ConfigParser.read_dict()`, unlike the credential loader's `.to_dict()`.
  Treat cloud state loading as requiring validation/fixing, not a verified
  deployment path.
- `ENVIRONMENT=gcp` enables the installed `secret_sources` entry point. The
  supplied plugin builds Slack/common/Tesla settings from environment variables
  and Google Secret Manager; it is not a general INI overlay and does not supply
  Matrix/MQTT configuration. Slack additionally supports direct
  `SLACK_API_TOKEN`, `SLACK_APP_TOKEN`, and `SLACK_ADMIN_CHANNEL_ID` overrides.

## Integration and Security Boundaries

- Matrix uses `matrix-nio`, restores or creates a login, then runs continuous
  sync. Initial invitations establish the admin room first and normal room
  second; `sameroom` can combine them. `trust_mxids` verifies those users'
  devices after initial sync. It is not a robust defense against active device
  substitution and is not a per-command sender allowlist.
- Slack uses outbound Socket Mode WebSockets and Web API replies. It
  acknowledges envelopes before executing commands and admits human messages
  only from the normal/admin channels. This is not a durable command queue.
- MQTT relies on broker authentication, topic ACLs, and optional TLS; it has no
  chat-style admin authorization flow. Restrict broker access to the intended
  namespace and account for retained readings being stale after restart.
- Detailed logs preserve command context, payloads, vehicle data, exceptions,
  and tracebacks. `log.py` installs a stdout handler, with per-module levels
  configured by startup/modules. Logs, token caches, state, and Matrix stores
  can contain sensitive information; protect them rather than suppress useful
  diagnostics. See the logging policy in [AGENTS.md](AGENTS.md).

## Development and Verification

[setup.py](setup.py) requires Python 3.10 or later and declares `matrix`,
`slack`, and `mqtt` extras. [requirements.txt](requirements.txt) pins TeslaPy
2.9.2 and includes the Google libraries even for local operation. Install only
the adapter extras needed in production; development across adapters should
install all three. Matrix installation also needs the platform's Olm/FFI
dependencies described in the README.

```sh
python -m pip install -e '.[matrix,slack,mqtt]'
python -m pip install mypy -r requirements-types.txt
python -m teslabot --version
python -m teslabot --config /absolute/path/to/config.ini
python -m unittest discover
python -m unittest teslabot.scheduler
mypy -p teslabot -p tests
```

The runtime command above connects to real configured services; it is not a
safe smoke test against arbitrary existing credentials. The scheduler command
explicitly runs tests embedded in `scheduler.py`, outside the normal `test*.py`
discovery naming pattern. The quality workflow uses `unittest discover` and
`mypy -p teslabot -p tests`. Jinja2 is optional for Home Assistant template
rendering tests; install it to run those checks rather than skip them.

Useful test entry points:

| Area                                    | Tests                                                                                                                                    |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Grammar and commands                    | `tests/test_parser.py`, `tests/test_commands.py`, `tests/test_control.py`                                                                |
| SDK/data isolation and vehicle behavior | `tests/test_tesla.py`, `tests/test_sdk_boundary.py`, `tests/test_unnamed_vehicles.py`, `tests/test_asyncthread.py`                       |
| Multi-adapter routing and lifecycle     | `tests/test_multi_control.py`, `tests/test_multi_control_review.py`, `tests/test_interrupted_response.py`                                |
| Matrix deadlines and cleanup            | `tests/test_matrix_timeout.py`                                                                                                           |
| MQTT discovery, refresh jobs, telemetry | `tests/test_mqtt.py`, `tests/test_action_refresh_delay.py`, `tests/test_action_refresh_timestamp.py`, `tests/test_expanded_telemetry.py` |
| Optional dependencies and observability | `tests/test_optional_jinja.py`, `tests/test_logging.py`                                                                                  |

Tests use fake controls, clients, vehicles, and storage to exercise failure and
cancellation boundaries without requiring production services. They do not
replace live compatibility checks against Tesla, Matrix, Slack, a broker/Home
Assistant, or Firestore.

[Dockerfile](Dockerfile) builds with all adapter extras and runs from `/data`
using `/data/config.ini`. Its builder reconstructs source from the copied Git
repository, so uncommitted working-tree changes are not the image's source.
Version metadata comes from Versioneer. Review
[docker-compose.yaml](docker-compose.yaml) for deployment-specific mounts and
settings rather than assuming it is a production template.
