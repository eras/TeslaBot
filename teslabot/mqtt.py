"""Restricted MQTT and Home Assistant adapter for vehicle state and controls."""

import asyncio
import json
import ssl
import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional
import oauthlib.oauth2
import requests.exceptions
import teslapy
import urllib.error
import urllib3.exceptions

from . import __version__, control, log
from .env import Env
from .tesla import ActionResult, App, AppException, VehicleSnapshot, is_transient_error, vehicle_display_name

logger = log.getLogger(__name__)


def refresh_delay(value: str) -> int:
    if type(value) is not str or not re.fullmatch(r"0|[1-9][0-9]{0,2}", value) or int(value) > 300:
        raise ValueError(f"Action refresh delay must be an ASCII integer 0..300, got {value!r}")
    return int(value)


@dataclass
class _Session:
    client: Any
    generation: int
    active: bool = True
    jobs: Dict[str, asyncio.Task[None]] = field(default_factory=dict)
    tokens: Dict[str, object] = field(default_factory=dict)
    cancelling: set[asyncio.Task[None]] = field(default_factory=set)
    failed: asyncio.Event = field(default_factory=asyncio.Event)
    error: Optional[Exception] = None
    receiver: Optional[asyncio.Task[None]] = None
    stop_task: Optional[asyncio.Task[None]] = None


class MqttControl(control.Control):
    run_scheduled_commands = False
    def __init__(self, env: Env) -> None:
        super().__init__()
        cfg = env.config
        self.host = cfg.get("mqtt", "host")
        self.port = int(cfg.get("mqtt", "port", fallback="1883"))
        self.username = cfg.get("mqtt", "username", fallback="", empty_is_none=False) or None
        self.password = cfg.get("mqtt", "password", fallback="", empty_is_none=False) or None
        self.tls = cfg.get("mqtt", "tls", fallback="false").lower() == "true"
        self.prefix = cfg.get("mqtt", "prefix", fallback="teslabot").strip("/")
        self.discovery_prefix = cfg.get("mqtt", "discovery_prefix", fallback="homeassistant").strip("/")
        if not self.prefix or not self.discovery_prefix or any(c in self.prefix + self.discovery_prefix for c in "+#"):
            raise control.ConfigError("Invalid MQTT topic prefix")
        self.app: Optional[App] = None
        self.vehicles: Dict[str, str] = {}
        self._state = env.state
        self._manifest_key = hashlib.sha256(json.dumps([self.host, self.port, self.prefix, self.discovery_prefix]).encode()).hexdigest()
        # Preserve shipped default discovery IDs; distinct custom prefixes own
        # separate discovery nodes even when vehicles are shared.
        self._entity_prefix = "teslabot" if self.prefix == "teslabot" else "teslabot_" + hashlib.sha256(self.prefix.encode()).hexdigest()[:12]
        self._auth_event = asyncio.Event()
        self._generation: Optional[int] = None
        self._session: Optional[_Session] = None
        self._closed = False
        self._setting_lock = asyncio.Lock()
        try:
            self.action_refresh_delay = refresh_delay(cfg.get("mqtt", "action_refresh_delay", fallback="5").strip())
            saved = self._state.get("mqtt_action_refresh_delay", self._manifest_key, fallback=None)
            if saved is not None:
                self.action_refresh_delay = refresh_delay(saved)
        except ValueError as exn:
            raise control.ConfigError(f"Invalid mqtt.action_refresh_delay configuration/state: {exn}") from exn

    def set_app(self, app: App) -> None:
        self.app = app
        app.auth_events.append(self._auth_event)

    async def setup(self) -> None:
        if self.app is None:
            raise control.ConfigError("MQTT app not initialized")
        logger.info("MQTT initialized; awaiting authorization and broker readiness")

    async def send_message(self, message_context: control.MessageContext, message: str) -> None:
        # App startup and legacy scheduler notices are chat-only; in particular,
        # never forward authorization URLs or unrestricted command output.
        logger.debug("Ignoring chat message in MQTT mode")

    def _discovery(self, vehicle_id: str, name: str) -> Dict[str, Dict[str, Any]]:
        base = f"{self.prefix}/{vehicle_id}"
        device = {"identifiers": [f"teslabot_{vehicle_id}"], "name": name, "manufacturer": "Tesla"}
        state = f"{base}/state"
        common: Dict[str, Any] = {"device": device,
                                  "availability_topic": f"{self.prefix}/availability"}
        entities = {
            "sensor/battery": dict(common, name="Battery", unique_id=f"teslabot_{vehicle_id}_battery",
                                   state_topic=state, value_template="{{ value_json.battery_level }}",
                                   unit_of_measurement="%", device_class="battery"),
            "sensor/charging": dict(common, name="Charging state", unique_id=f"teslabot_{vehicle_id}_charging",
                                    state_topic=state, value_template="{{ value_json.charging_state }}"),
            "sensor/last_refresh": dict(common, name="Last refresh", unique_id=f"teslabot_{vehicle_id}_last_refresh",
                                        state_topic=state, value_template="{{ value_json.observed_at }}",
                                        device_class="timestamp"),
            "switch/ac": dict(common, name="Climate", unique_id=f"teslabot_{vehicle_id}_ac",
                              state_topic=state, value_template="{{ 'ON' if value_json.climate_on is sameas true else 'OFF' if value_json.climate_on is sameas false else 'None' }}",
                              command_topic=f"{base}/ac/set", payload_on="ON", payload_off="OFF"),
            "number/charge_limit": dict(common, name="Charge limit", unique_id=f"teslabot_{vehicle_id}_charge_limit",
                                        state_topic=state, value_template="{{ value_json.charge_limit }}",
                                        command_topic=f"{base}/charge_limit/set", min=0, max=100, step=1,
                                        unit_of_measurement="%"),
            "button/refresh": dict(common, name="Refresh", unique_id=f"teslabot_{vehicle_id}_refresh",
                                   command_topic=f"{base}/refresh/set"),
            "button/sauna_on": dict(common, name="Max defrost on", unique_id=f"teslabot_{vehicle_id}_sauna_on",
                                    command_topic=f"{base}/sauna/set", payload_press="ON"),
            "button/sauna_off": dict(common, name="Max defrost off", unique_id=f"teslabot_{vehicle_id}_sauna_off",
                                     command_topic=f"{base}/sauna/set", payload_press="OFF"),
        }
        # Fixed public telemetry definitions, never inferred from raw SDK data.
        sensors = {
            "inside_temp": ("Inside temperature", "temperature", "\u00b0C", "measurement"),
            "outside_temp": ("Outside temperature", "temperature", "\u00b0C", "measurement"),
            "charge_amps": ("Charge current limit", "current", "A", "measurement"),
            "charger_power_kw": ("Charging power", "power", "kW", "measurement"),
            "charge_rate_kmh": ("Charge range added rate", "speed", "km/h", "measurement"),
            "charge_finish_eta": ("Estimated charge completion", "timestamp", None, None),
            "odometer_km": ("Odometer", "distance", "km", "total_increasing"),
            "seat_heater_left": ("Front left seat heat level", None, None, None),
            "seat_heater_right": ("Front right seat heat level", None, None, None),
            "seat_heater_rear_left": ("Rear left seat heat level", None, None, None),
            "seat_heater_rear_center": ("Rear center seat heat level", None, None, None),
            "seat_heater_rear_right": ("Rear right seat heat level", None, None, None),
            "tpms_pressure_fl": ("Front left tire pressure", "pressure", "bar", "measurement"),
            "tpms_pressure_fr": ("Front right tire pressure", "pressure", "bar", "measurement"),
            "tpms_pressure_rl": ("Rear left tire pressure", "pressure", "bar", "measurement"),
            "tpms_pressure_rr": ("Rear right tire pressure", "pressure", "bar", "measurement"),
            "software_update_status": ("Software update status", None, None, None),
            "software_update_version": ("Software update version", None, None, None),
            "software_update_download_percent": ("Software update download progress", None, "%", None),
            "software_update_install_percent": ("Software update install progress", None, "%", None),
            "software_update_expected_duration_s": ("Expected software update duration", "duration", "s", None),
            "car_version": ("Car firmware version", None, None, None),
        }
        for key, (label, device_class, unit, state_class) in sensors.items():
            value = f"value_json.get('{key}')"
            known = f"{value} is not none"
            if key in ("software_update_status", "software_update_version", "car_version"):
                # HA state strings are limited to 255 characters; retain the
                # complete observed text in MQTT, but reset overlong HA states.
                known += f" and {value} | length <= 255"
            config = dict(common, name=label, unique_id=f"teslabot_{vehicle_id}_{key}",
                          state_topic=state, value_template="{{ " + value + " if " + known + " else 'None' }}")
            if device_class:
                config["device_class"] = device_class
            if unit:
                config["unit_of_measurement"] = unit
            if state_class:
                config["state_class"] = state_class
            if key.startswith("software_update_") or key == "car_version":
                config["entity_category"] = "diagnostic"
            entities[f"sensor/{key}"] = config
        openings = {
            "locked": ("Door lock", "lock"),
            "door_driver_front_open": ("Driver front door", "door"),
            "door_driver_rear_open": ("Driver rear door", "door"),
            "door_passenger_front_open": ("Passenger front door", "door"),
            "door_passenger_rear_open": ("Passenger rear door", "door"),
            "window_driver_front_open": ("Driver front window", "window"),
            "window_driver_rear_open": ("Driver rear window", "window"),
            "window_passenger_front_open": ("Passenger front window", "window"),
            "window_passenger_rear_open": ("Passenger rear window", "window"),
            "frunk_open": ("Frunk", "opening"),
            "trunk_open": ("Trunk", "opening"),
            "charge_port_door_open": ("Charge port door", "opening"),
        }
        for key, (label, device_class) in openings.items():
            value = f"value_json.get('{key}')"
            on, off = ("OFF", "ON") if key == "locked" else ("ON", "OFF")
            entities[f"binary_sensor/{key}"] = dict(
                common, name=label, unique_id=f"teslabot_{vehicle_id}_{key}", state_topic=state,
                device_class=device_class,
                value_template="{{ '" + on + "' if " + value + " is sameas true else '" + off + "' if " + value + " is sameas false else 'None' }}")
        if self._entity_prefix != "teslabot":
            device["identifiers"] = [f"{self._entity_prefix}_{vehicle_id}"]
            for entity in entities.values():
                entity["unique_id"] = entity["unique_id"].replace("teslabot_", self._entity_prefix + "_", 1)
        return entities

    async def _publish_snapshot(self, client: Any, snapshot: VehicleSnapshot) -> None:
        # Project scalar fields explicitly; never traverse raw diagnostic data.
        state = {
            "vehicle_id": snapshot.vehicle_id, "display_name": snapshot.display_name,
            "observed_at": snapshot.observed_at.isoformat(),
            "battery_level": snapshot.battery_level, "charging_state": snapshot.charging_state,
            "charge_limit": snapshot.charge_limit, "charge_amps": snapshot.charge_amps,
            "climate_on": snapshot.climate_on, "defrost_mode": snapshot.defrost_mode,
            "inside_temp": snapshot.inside_temp, "outside_temp": snapshot.outside_temp,
            "temperature_unit": snapshot.temperature_unit,
            "seat_heater_left": snapshot.seat_heater_left,
            "seat_heater_right": snapshot.seat_heater_right,
            "seat_heater_rear_left": snapshot.seat_heater_rear_left,
            "seat_heater_rear_center": snapshot.seat_heater_rear_center,
            "seat_heater_rear_right": snapshot.seat_heater_rear_right,
            "charger_power_kw": snapshot.charger_power_kw,
            "charge_rate_kmh": snapshot.charge_rate_kmh,
            "charge_finish_eta": snapshot.charge_finish_eta,
            "odometer_km": snapshot.odometer_km,
            "tpms_pressure_fl": snapshot.tpms_pressure_fl,
            "tpms_pressure_fr": snapshot.tpms_pressure_fr,
            "tpms_pressure_rl": snapshot.tpms_pressure_rl,
            "tpms_pressure_rr": snapshot.tpms_pressure_rr,
            "software_update_status": snapshot.software_update_status,
            "software_update_version": snapshot.software_update_version,
            "software_update_download_percent": snapshot.software_update_download_percent,
            "software_update_install_percent": snapshot.software_update_install_percent,
            "software_update_expected_duration_s": snapshot.software_update_expected_duration_s,
            "locked": snapshot.locked,
            "door_driver_front_open": snapshot.door_driver_front_open,
            "door_driver_rear_open": snapshot.door_driver_rear_open,
            "door_passenger_front_open": snapshot.door_passenger_front_open,
            "door_passenger_rear_open": snapshot.door_passenger_rear_open,
            "window_driver_front_open": snapshot.window_driver_front_open,
            "window_driver_rear_open": snapshot.window_driver_rear_open,
            "window_passenger_front_open": snapshot.window_passenger_front_open,
            "window_passenger_rear_open": snapshot.window_passenger_rear_open,
            "frunk_open": snapshot.frunk_open,
            "trunk_open": snapshot.trunk_open,
            "charge_port_door_open": snapshot.charge_port_door_open,
            "car_version": snapshot.car_version,
        }
        await client.publish(f"{self.prefix}/{snapshot.vehicle_id}/state", json.dumps(state), qos=1, retain=True)

    async def _handle(self, client: Any, topic: str, payload: str, retained: bool) -> None:
        session = self._session
        if retained or self.app is None or session is None or session.client is not client or not self._valid(session):
            return
        if topic == f"{self.prefix}/action_refresh_delay/set":
            await self._set_delay(session, payload)
            return
        parts = topic.split("/")
        prefix_parts = self.prefix.split("/")
        if parts[:len(prefix_parts)] != prefix_parts or len(parts) != len(prefix_parts) + 3:
            return
        vehicle_id = parts[-3]
        action_topic = "/".join(parts[-2:])
        if vehicle_id not in self.vehicles:
            return
        if action_topic not in ("refresh/set", "ac/set", "sauna/set", "charge_limit/set"):
            return
        try:
            if action_topic == "charge_limit/set":
                if not re.fullmatch(r"0|[1-9][0-9]{0,2}", payload) or int(payload) > 100:
                    raise ValueError("Charge limit must be an integer from 0 to 100")
            elif action_topic != "refresh/set" and payload not in ("ON", "OFF"):
                raise ValueError("Expected ON or OFF")
        except ValueError as exn:
            logger.warning("Invalid MQTT %s for %s payload %r: %s", action_topic, vehicle_id, payload, exn, exc_info=True)
            result = ActionResult(vehicle_id, action_topic[:-4], payload, False, str(exn))
            await client.publish(f"{self.prefix}/{vehicle_id}/result", json.dumps(asdict(result)), qos=0)
            return
        # Admission is the supersession boundary, including commands that fail.
        token = object()
        session.tokens[vehicle_id] = token
        await self._cancel_job(session, vehicle_id)
        if not self._valid(session, vehicle_id, token):
            return
        if action_topic == "refresh/set":
            await self._observe(session, vehicle_id, token, manual_payload=payload)
            return
        try:
            if action_topic == "charge_limit/set":
                result = await self.app.set_charge_limit(None, int(payload), vehicle_id=vehicle_id)
            else:
                enabled = payload == "ON"
                result = (await self.app.set_ac(None, enabled, vehicle_id=vehicle_id) if action_topic == "ac/set"
                          else await self.app.set_sauna(None, enabled, vehicle_id=vehicle_id))
        except (AppException, teslapy.VehicleError, requests.exceptions.HTTPError, requests.exceptions.Timeout,
                requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError,
                urllib.error.HTTPError, urllib3.exceptions.ProtocolError, oauthlib.oauth2.OAuth2Error) as exn:
            logger.warning("MQTT %s for %s failed: %s", action_topic, vehicle_id, exn, exc_info=True)
            result = ActionResult(vehicle_id, action_topic[:-4], payload, False, str(exn) or type(exn).__name__)
        completed = asyncio.get_running_loop().time()
        delay = self.action_refresh_delay
        if not self._valid(session, vehicle_id, token):
            return
        await client.publish(f"{self.prefix}/{vehicle_id}/result", json.dumps(asdict(result)), qos=0)
        if result.success and self._valid(session, vehicle_id, token):
            if delay == 0:
                await self._observe(session, vehicle_id, token)
            else:
                session.jobs[vehicle_id] = asyncio.create_task(self._follow_up(session, vehicle_id, token, completed + delay))
                logger.debug("Scheduled MQTT refresh for %s after %ss at monotonic %s, generation %s, client %s",
                             vehicle_id, delay, completed + delay, session.generation, client)

    def _valid(self, session: _Session, vehicle_id: Optional[str] = None, token: Optional[object] = None) -> bool:
        return (self._session is session and session.active and session.error is None and self._current()
                and self.app is not None and session.generation == self.app.auth_generation
                and (vehicle_id is None or session.tokens.get(vehicle_id) is token))

    async def _observe(self, session: _Session, vehicle_id: str, token: object, manual_payload: Optional[str] = None) -> None:
        assert self.app is not None
        if not self._valid(session, vehicle_id, token):
            return
        try:
            snapshot = await self.app.refresh_vehicle(None, vehicle_id=vehicle_id)
        except (AppException, teslapy.VehicleError, requests.exceptions.HTTPError, requests.exceptions.Timeout,
                requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError,
                urllib.error.HTTPError, urllib3.exceptions.ProtocolError, oauthlib.oauth2.OAuth2Error) as exn:
            logger.warning("MQTT observed refresh for %s generation %s failed: %s", vehicle_id, session.generation, exn, exc_info=True)
            if manual_payload is not None and self._valid(session, vehicle_id, token):
                result = ActionResult(vehicle_id, "refresh", manual_payload, False, str(exn) or type(exn).__name__)
                await session.client.publish(f"{self.prefix}/{vehicle_id}/result", json.dumps(asdict(result)), qos=0)
            return
        if self._valid(session, vehicle_id, token):
            await self._publish_snapshot(session.client, snapshot)

    async def _follow_up(self, session: _Session, vehicle_id: str, token: object, deadline: float) -> None:
        try:
            await self._wait_refresh(deadline)
            await self._observe(session, vehicle_id, token)
        except asyncio.CancelledError:
            raise
        except Exception as exn:
            self._record_failure(session, exn, f"delayed refresh for {vehicle_id}")
        finally:
            if session.jobs.get(vehicle_id) is asyncio.current_task():
                session.jobs.pop(vehicle_id)

    def _record_failure(self, session: _Session, exn: Exception, context: str) -> None:
        import aiomqtt
        logger.error("MQTT session %s generation %s client %s failed: %s",
                     context, session.generation, session.client, exn,
                     exc_info=(type(exn), exn, exn.__traceback__))
        previous = session.error
        recoverable = isinstance(exn, aiomqtt.MqttError) or is_transient_error(exn)
        previous_recoverable = previous is not None and (isinstance(previous, aiomqtt.MqttError) or is_transient_error(previous))
        # Keep the first terminal fault; a later terminal fault must outrank an
        # earlier recoverable broker/network error, including during teardown.
        if previous is None or (previous_recoverable and not recoverable):
            session.error = exn
        session.failed.set()

    async def _wait_refresh(self, deadline: float) -> None:
        await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))

    def _cancel_once(self, session: _Session, task: asyncio.Task[None]) -> None:
        if not task.done() and task not in session.cancelling:
            session.cancelling.add(task)
            task.cancel()

    async def _join(self, task: asyncio.Task[None]) -> None:
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait([task])
            except asyncio.CancelledError:
                cancelled = True
        if not task.cancelled():
            task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _cancel_job(self, session: _Session, vehicle_id: str) -> None:
        task = session.jobs.get(vehicle_id)
        if task is not None:
            self._cancel_once(session, task)
            try:
                await self._join(task)
            finally:
                session.cancelling.discard(task)

    async def _stop_session(self, session: _Session) -> None:
        session.active = False
        session.tokens.clear()
        if session.stop_task is None:
            session.stop_task = asyncio.create_task(self._drain_session(session))
        await self._join(session.stop_task)

    async def _drain_session(self, session: _Session) -> None:
        tasks = list(session.jobs.values())
        if session.receiver is not None:
            tasks.append(session.receiver)
        for task in tasks:
            self._cancel_once(session, task)
        if tasks:
            await asyncio.wait(tasks)
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for task, outcome in zip(tasks, outcomes):
            if isinstance(outcome, Exception):
                self._record_failure(session, outcome, f"owned task {task.get_name()} {task.get_coro()} during drain")
        session.jobs.clear()
        session.cancelling.clear()

    async def _set_delay(self, session: _Session, payload: str) -> None:
        try:
            delay = refresh_delay(payload)
        except ValueError:
            logger.exception("Invalid action_refresh_delay MQTT payload %r", payload)
            return
        async with self._setting_lock:
            if not self._valid(session):
                return
            section = "mqtt_action_refresh_delay"
            previous = self._state.get(section, self._manifest_key, fallback=None)
            if not self._state.has_section(section):
                self._state[section] = {}
            self._state[section][self._manifest_key] = str(delay)
            try:
                await self._state.save()
            except BaseException as exn:
                values = dict(self._state[section].items())
                if previous is None:
                    values.pop(self._manifest_key, None)
                else:
                    values[self._manifest_key] = previous
                self._state[section] = values
                logger.exception("Could not persist action_refresh_delay %s; restoring effective %s and entry %r", delay, self.action_refresh_delay, previous)
                if not isinstance(exn, Exception):
                    raise
                return
            self.action_refresh_delay = delay
            logger.info("Committed action_refresh_delay %s for namespace %s", delay, self._manifest_key)
        if self._valid(session):
            await self._publish_delay(session.client)

    def _delay_discovery(self) -> Dict[str, Any]:
        return {"name": "Action refresh delay", "unique_id": f"{self._entity_prefix}_action_refresh_delay",
                "device": self._instance_device(),
                "availability_topic": f"{self.prefix}/availability", "entity_category": "config",
                "command_topic": f"{self.prefix}/action_refresh_delay/set",
                "state_topic": f"{self.prefix}/action_refresh_delay/state",
                "unit_of_measurement": "s", "min": 0, "max": 300, "step": 1, "mode": "box", "qos": 0, "retain": False}

    def _instance_device(self) -> Dict[str, Any]:
        return {"identifiers": [f"{self._entity_prefix}_instance"], "name": "TeslaBot", "sw_version": __version__}

    def _version_discovery(self) -> Dict[str, Any]:
        return {"name": "TeslaBot version", "unique_id": f"{self._entity_prefix}_version",
                "device": self._instance_device(), "entity_category": "diagnostic",
                "availability_topic": f"{self.prefix}/availability", "state_topic": f"{self.prefix}/version",
                "value_template": "{{ value if value | length <= 255 else 'None' }}"}

    async def _publish_delay(self, client: Any) -> None:
        await client.publish(f"{self.prefix}/action_refresh_delay/state", str(self.action_refresh_delay), qos=1, retain=True)

    def _current(self) -> bool:
        return (self.app is not None and self.app.authorized and
                self._generation == self.app.auth_generation)

    def _config_topic(self, vehicle_id: str, key: str) -> str:
        kind, entity = key.split("/")
        return f"{self.discovery_prefix}/{kind}/{self._entity_prefix}_{vehicle_id}/{entity}/config"

    async def _reconcile(self, client: Any, generation: int) -> bool:
        assert self.app is not None
        await client.publish(f"{self.prefix}/availability", "offline", qos=1, retain=True)
        await client.publish(f"{self.discovery_prefix}/number/{self._entity_prefix}/action_refresh_delay/config",
                             json.dumps(self._delay_discovery()), qos=1, retain=True)
        await self._publish_delay(client)
        await client.publish(f"{self.discovery_prefix}/sensor/{self._entity_prefix}/version/config",
                             json.dumps(self._version_discovery()), qos=1, retain=True)
        await client.publish(f"{self.prefix}/version", __version__, qos=1, retain=True)
        try:
            vehicles = await self.app._get_vehicle_list() if self.app.authorized else []
        except Exception:
            if generation != self.app.auth_generation:
                return False
            raise
        if generation != self.app.auth_generation:
            return False
        mapping = {self.app._vehicle_id(v): vehicle_display_name(v) for v in vehicles}
        if len(mapping) != len(vehicles):
            raise control.ConfigError("MQTT vehicle identifiers must be unique")
        owned = set(json.loads(self._state.get("mqtt_owned", self._manifest_key, fallback="[]") or "[]"))
        if any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{16}", v) for v in owned):
            raise control.ConfigError("Invalid MQTT owned manifest")
        # Write-ahead ownership survives a crash halfway through publishing.
        if not self._state.has_section("mqtt_owned"):
            self._state["mqtt_owned"] = {}
        self._state["mqtt_owned"][self._manifest_key] = json.dumps(sorted(owned | set(mapping)))
        await self._state.save()
        for vehicle_id in owned - set(mapping):
            for key in self._discovery(vehicle_id, ""):
                await client.publish(self._config_topic(vehicle_id, key), "", qos=1, retain=True)
            await client.publish(f"{self.prefix}/{vehicle_id}/state", "", qos=1, retain=True)
        for vehicle_id, name in mapping.items():
            for key, config in self._discovery(vehicle_id, name).items():
                await client.publish(self._config_topic(vehicle_id, key), json.dumps(config), qos=1, retain=True)
            await client.subscribe(f"{self.prefix}/{vehicle_id}/+/set", qos=0)
        self._state["mqtt_owned"][self._manifest_key] = json.dumps(sorted(mapping))
        await self._state.save()
        if generation != self.app.auth_generation:
            return False
        self.vehicles = mapping
        self._generation = generation
        if self.app.authorized:
            await client.subscribe(f"{self.prefix}/action_refresh_delay/set", qos=0)
            await client.publish(f"{self.prefix}/availability", "online", qos=1, retain=True)
            logger.info("MQTT online: generation %d, %d vehicles", generation, len(mapping))
        return generation == self.app.auth_generation

    async def close(self) -> None:
        self._closed = True
        self._auth_event.set()
        if self._session is not None:
            await self._stop_session(self._session)
        if self.app is not None and self._auth_event in self.app.auth_events:
            self.app.auth_events.remove(self._auth_event)

    async def _wait_retry(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._auth_event.wait(), delay)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        import aiomqtt
        assert self.app is not None
        app = self.app
        retry_delay = 5.0
        while not self._closed:
            self._auth_event.clear()
            generation = app.auth_generation
            try:
                tls_context = ssl.create_default_context() if self.tls else None
                will = aiomqtt.Will(f"{self.prefix}/availability", "offline", qos=1, retain=True)
                async with aiomqtt.Client(self.host, port=self.port, username=self.username,
                                          password=self.password, tls_context=tls_context, will=will,
                                          clean_session=True, timeout=10) as client:
                    session = _Session(client, generation)
                    self._session = session
                    self._generation = None
                    try:
                        if not await self._reconcile(client, generation):
                            continue
                        retry_delay = 5.0
                        async def receive() -> None:
                            async for message in client.messages:
                                if not self._valid(session):
                                    if session.active and session.generation == app.auth_generation and not app.authorized:
                                        continue
                                    return
                                await self._handle(client, str(message.topic), message.payload.decode("utf-8", errors="replace"), message.retain)
                            raise control.ControlException("MQTT message stream returned unexpectedly")
                        receiver = asyncio.create_task(receive())
                        session.receiver = receiver
                        changed = asyncio.create_task(self._auth_event.wait())
                        failed = asyncio.create_task(session.failed.wait())
                        try:
                            done, _ = await asyncio.wait([receiver, changed, failed], return_when=asyncio.FIRST_COMPLETED)
                            if session.error is not None:
                                raise session.error
                            if receiver in done:
                                await receiver
                        finally:
                            try:
                                await self._stop_session(session)
                            finally:
                                changed.cancel()
                                failed.cancel()
                                await asyncio.gather(changed, failed, return_exceptions=True)
                    finally:
                        await self._stop_session(session)
                        try:
                            await asyncio.wait_for(client.publish(f"{self.prefix}/availability", "offline", qos=1, retain=True), 10)
                        except Exception:
                            pass  # A broken connection's last will publishes offline instead.
                        if session.error is not None:
                            raise session.error
            except asyncio.CancelledError:
                raise
            except Exception as exn:
                if not isinstance(exn, aiomqtt.MqttError) and not is_transient_error(exn):
                    raise
                logger.warning("MQTT reconciliation interrupted: %s: %s; retrying in %.0f seconds", type(exn).__name__, exn, retry_delay, exc_info=True)
                await self._wait_retry(retry_delay)
                retry_delay = min(60.0, retry_delay * 2)
