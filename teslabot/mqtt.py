"""Restricted MQTT and Home Assistant adapter for vehicle state and controls."""

import asyncio
import json
import ssl
import hashlib
import re
from dataclasses import asdict
from typing import Any, Dict, Optional

from . import control, log
from .env import Env
from .tesla import ActionResult, App, VehicleSnapshot, is_transient_error, vehicle_display_name

logger = log.getLogger(__name__)


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
        }
        await client.publish(f"{self.prefix}/{snapshot.vehicle_id}/state", json.dumps(state), qos=1, retain=True)

    async def _handle(self, client: Any, topic: str, payload: str, retained: bool) -> None:
        if retained or self.app is None or not self._current():
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
        result: Optional[ActionResult] = None
        try:
            if action_topic == "refresh/set":
                snapshot = await self.app.refresh_vehicle(None, vehicle_id=vehicle_id)
                if self._current():
                    await self._publish_snapshot(client, snapshot)
                return
            if action_topic == "charge_limit/set":
                if not payload.isdecimal() or not 0 <= int(payload) <= 100:
                    raise ValueError("Charge limit must be an integer from 0 to 100")
                result = await self.app.set_charge_limit(None, int(payload), vehicle_id=vehicle_id)
            else:
                if payload not in ("ON", "OFF"):
                    raise ValueError("Expected ON or OFF")
                enabled = payload == "ON"
                result = (await self.app.set_ac(None, enabled, vehicle_id=vehicle_id) if action_topic == "ac/set"
                          else await self.app.set_sauna(None, enabled, vehicle_id=vehicle_id))
            if not self._current():
                return
            await client.publish(f"{self.prefix}/{vehicle_id}/result", json.dumps(asdict(result)), qos=0)
            if result.success:
                snapshot = await self.app.refresh_vehicle(None, vehicle_id=vehicle_id)
                if self._current():
                    await self._publish_snapshot(client, snapshot)
        except Exception as exn:
            logger.warning("MQTT %s for %s failed: %s", action_topic, vehicle_id, exn, exc_info=True)
            if result is None and self._current():
                result = ActionResult(vehicle_id, action_topic[:-4], payload, False, str(exn) or type(exn).__name__)
                await client.publish(f"{self.prefix}/{vehicle_id}/result", json.dumps(asdict(result)), qos=0)

    def _current(self) -> bool:
        return (self.app is not None and self.app.authorized and
                self._generation == self.app.auth_generation)

    def _config_topic(self, vehicle_id: str, key: str) -> str:
        kind, entity = key.split("/")
        return f"{self.discovery_prefix}/{kind}/{self._entity_prefix}_{vehicle_id}/{entity}/config"

    async def _reconcile(self, client: Any, generation: int) -> bool:
        assert self.app is not None
        await client.publish(f"{self.prefix}/availability", "offline", qos=1, retain=True)
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
            await client.publish(f"{self.prefix}/availability", "online", qos=1, retain=True)
            logger.info("MQTT online: generation %d, %d vehicles", generation, len(mapping))
        return generation == self.app.auth_generation

    async def close(self) -> None:
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
        retry_delay = 5.0
        while True:
            self._auth_event.clear()
            generation = self.app.auth_generation
            try:
                tls_context = ssl.create_default_context() if self.tls else None
                will = aiomqtt.Will(f"{self.prefix}/availability", "offline", qos=1, retain=True)
                async with aiomqtt.Client(self.host, port=self.port, username=self.username,
                                          password=self.password, tls_context=tls_context, will=will,
                                          clean_session=True, timeout=10) as client:
                    try:
                        if not await self._reconcile(client, generation):
                            continue
                        retry_delay = 5.0
                        async def receive() -> None:
                            async for message in client.messages:
                                if not self._current():
                                    return
                                await self._handle(client, str(message.topic), message.payload.decode("utf-8", errors="replace"), message.retain)
                            raise control.ControlException("MQTT message stream returned unexpectedly")
                        receiver = asyncio.create_task(receive())
                        changed = asyncio.create_task(self._auth_event.wait())
                        try:
                            done, _ = await asyncio.wait([receiver, changed], return_when=asyncio.FIRST_COMPLETED)
                            if receiver in done:
                                await receiver
                        finally:
                            receiver.cancel()
                            changed.cancel()
                            await asyncio.gather(receiver, changed, return_exceptions=True)
                    finally:
                        try:
                            await asyncio.wait_for(client.publish(f"{self.prefix}/availability", "offline", qos=1, retain=True), 10)
                        except Exception:
                            pass  # A broken connection's last will publishes offline instead.
            except asyncio.CancelledError:
                raise
            except Exception as exn:
                if not isinstance(exn, aiomqtt.MqttError) and not is_transient_error(exn):
                    raise
                logger.warning("MQTT reconciliation interrupted: %s: %s; retrying in %.0f seconds", type(exn).__name__, exn, retry_delay, exc_info=True)
                await self._wait_retry(retry_delay)
                retry_delay = min(60.0, retry_delay * 2)
