"""Restricted MQTT and Home Assistant adapter for vehicle state and controls."""

import asyncio
import json
import ssl
from dataclasses import asdict
from typing import Any, Dict, Optional

from . import control, log
from .env import Env
from .tesla import ActionResult, App, VehicleSnapshot

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
        if not self.prefix or not self.discovery_prefix or any(c in self.prefix for c in "+#"):
            raise control.ConfigError("Invalid MQTT topic prefix")
        self.app: Optional[App] = None
        self.vehicles: Dict[str, str] = {}

    def set_app(self, app: App) -> None:
        self.app = app

    async def setup(self) -> None:
        if self.app is None:
            raise control.ConfigError("MQTT app not initialized")
        if not self.app.tesla.authorized:
            raise control.ConfigError("Authorize Tesla using Matrix or Slack before using MQTT")
        vehicles = await self.app._get_vehicle_list()
        self.vehicles = {self.app._vehicle_id(v): v["display_name"] for v in vehicles}
        if len(self.vehicles) != len(vehicles):
            raise control.ConfigError("MQTT vehicle identifiers must be unique")

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
        return {
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

    async def _publish_snapshot(self, client: Any, snapshot: VehicleSnapshot) -> None:
        state = asdict(snapshot)
        del state["data"]
        state["observed_at"] = snapshot.observed_at.isoformat()
        await client.publish(f"{self.prefix}/{snapshot.vehicle_id}/state", json.dumps(state), qos=1, retain=True)

    async def _handle(self, client: Any, topic: str, payload: str, retained: bool) -> None:
        if retained or self.app is None:
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
            await client.publish(f"{self.prefix}/{vehicle_id}/result", json.dumps(asdict(result)), qos=0)
            if result.success:
                snapshot = await self.app.refresh_vehicle(None, vehicle_id=vehicle_id)
                await self._publish_snapshot(client, snapshot)
        except Exception as exn:
            logger.warning("MQTT %s for %s failed: %s", action_topic, vehicle_id, exn)
            if result is None:
                result = ActionResult(vehicle_id, action_topic[:-4], payload, False, str(exn))
                await client.publish(f"{self.prefix}/{vehicle_id}/result", json.dumps(asdict(result)), qos=0)

    async def run(self) -> None:
        import aiomqtt

        while True:
            try:
                tls_context = ssl.create_default_context() if self.tls else None
                will = aiomqtt.Will(f"{self.prefix}/availability", "offline", qos=1, retain=True)
                async with aiomqtt.Client(self.host, port=self.port, username=self.username,
                                          password=self.password, tls_context=tls_context, will=will) as client:
                    for vehicle_id, name in self.vehicles.items():
                        for key, config in self._discovery(vehicle_id, name).items():
                            kind, entity = key.split("/")
                            await client.publish(f"{self.discovery_prefix}/{kind}/teslabot_{vehicle_id}/{entity}/config",
                                                 json.dumps(config), qos=1, retain=True)
                        await client.subscribe(f"{self.prefix}/{vehicle_id}/+/set", qos=0)
                    await client.publish(f"{self.prefix}/availability", "online", qos=1, retain=True)
                    try:
                        async for message in client.messages:
                            await self._handle(client, str(message.topic), message.payload.decode("utf-8", errors="replace"),
                                               message.retain)
                    finally:
                        try:
                            await client.publish(f"{self.prefix}/availability", "offline", qos=1, retain=True)
                        except Exception:
                            pass  # A broken connection's last will publishes offline instead.
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MQTT connection lost; retrying")
                await asyncio.sleep(5)
