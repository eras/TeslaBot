import asyncio
import datetime
import importlib
import json
import threading
import types
import unittest
from typing import Any, Optional
from unittest import mock

import requests

jinja2: Optional[types.ModuleType]
try:
    jinja2 = importlib.import_module("jinja2")
except ImportError:
    jinja2 = None

from teslabot import control, tesla
import tests.test_action_refresh_delay as delay


class ObservationClock(datetime.datetime):
    current = datetime.datetime(2026, 10, 3, tzinfo=datetime.timezone.utc)
    calls = 0

    @classmethod
    def now(cls, tz: Optional[datetime.tzinfo] = None) -> "ObservationClock":
        cls.calls += 1
        current = cls(cls.current.year, cls.current.month, cls.current.day,
                      cls.current.hour, cls.current.minute, cls.current.second,
                      cls.current.microsecond, tzinfo=cls.current.tzinfo, fold=cls.current.fold)
        return current.replace(tzinfo=None) if tz is None else current.astimezone(tz)


class RefreshTimestampTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self) -> None:
        self.fixture = delay.RealSDKDelayTests()
        self.fixture.setUp()
        self.mqtt, self.app, self.http = self.fixture.mqtt, self.fixture.app, self.fixture.http
        self.published = self.fixture.fixture.publications
        self.retained = self.fixture.fixture.retained
        self.topic = f"teslabot/{self.fixture.fixture.identity}/state"
        self.snapshots: list[tesla.VehicleSnapshot] = []
        original = self.app.refresh_vehicle
        async def capture(
            vehicle_name: Optional[str], context: Optional[control.CommandContext] = None,
            vehicle_id: Optional[str] = None,
        ) -> tesla.VehicleSnapshot:
            snapshot = await original(vehicle_name, context, vehicle_id)
            self.snapshots.append(snapshot)
            return snapshot
        self.capture = mock.patch.object(self.app, "refresh_vehicle", side_effect=capture)
        self.capture.start()
        ObservationClock.calls = 0
        ObservationClock.current = datetime.datetime(2026, 10, 3, tzinfo=datetime.timezone.utc)

    async def asyncTearDown(self) -> None:
        self.capture.stop()
        await self.fixture.asyncTearDown()

    def latest(self) -> dict[str, Any]:
        state: dict[str, Any] = json.loads(self.retained[self.topic])
        return state

    async def read_job(self, event: asyncio.Event) -> None:
        job = self.fixture.session.jobs[self.fixture.fixture.identity]
        event.set()
        await asyncio.wait_for(job, 1)

    def assert_timestamp(self, expected: datetime.datetime) -> dict[str, Any]:
        state = self.latest()
        config = self.mqtt._discovery(self.fixture.fixture.identity, "Synthetic car")["sensor/last_refresh"]
        self.assertEqual(config["state_topic"], self.topic)
        self.assertEqual(config["device_class"], "timestamp")
        self.assertEqual(config["value_template"], "{{ value_json.observed_at }}")
        self.assertEqual(state["observed_at"], expected.isoformat())
        self.assertEqual(datetime.datetime.fromisoformat(state["observed_at"]), expected)
        self.assertEqual(datetime.datetime.fromisoformat(state["observed_at"]).utcoffset(), datetime.timedelta(0))
        self.assertEqual(self.snapshots[-1].observed_at, expected)
        publication = [entry for entry in self.published if entry[0] == self.topic][-1]
        self.assertEqual(publication[2], {"qos": 1, "retain": True})
        return state

    async def test_optional_ha_template_renders_observed_utc_timestamp(self) -> None:
        if jinja2 is None:
            self.skipTest("Jinja2 is needed to render the optional HA template check")
        with mock.patch.object(datetime, "datetime", ObservationClock):
            await self.fixture.handle("refresh", "")
            state = self.assert_timestamp(ObservationClock.current)
            config = self.mqtt._discovery(self.fixture.fixture.identity, "Synthetic car")["sensor/last_refresh"]
            rendered = jinja2.Environment().from_string(config["value_template"]).render(value_json=state)
            self.assertEqual(rendered, ObservationClock.current.isoformat())
            self.assertEqual(datetime.datetime.fromisoformat(rendered), ObservationClock.current)

    async def test_all_automatic_reads_update_ha_timestamp_like_manual_even_unchanged(self) -> None:
        base = ObservationClock.current
        with mock.patch.object(datetime, "datetime", ObservationClock), self.fixture.clock.install():
            for mode in (0, 5):
                self.mqtt.action_refresh_delay = mode
                for operation, payload in (("ac", "ON"), ("ac", "OFF"), ("sauna", "ON"),
                                           ("sauna", "OFF"), ("charge_limit", "70")):
                    with self.subTest(mode=mode, operation=operation, payload=payload):
                        base += datetime.timedelta(minutes=3)
                        ObservationClock.current = base
                        await self.fixture.handle("refresh", "")
                        previous = self.assert_timestamp(base)
                        reads, timestamps = self.http.data_reads, ObservationClock.calls
                        enqueue = base + datetime.timedelta(seconds=10)
                        observed = base + datetime.timedelta(minutes=1)
                        ObservationClock.current = enqueue if mode else observed
                        await self.fixture.handle(operation, payload)
                        if mode:
                            event = await self.fixture.clock.next()
                            self.assertEqual(self.http.data_reads, reads)
                            self.assertEqual(ObservationClock.calls, timestamps)
                            self.assertEqual(self.latest(), previous)
                            ObservationClock.current = observed
                            await self.read_job(event)
                        current = self.assert_timestamp(observed)
                        self.assertEqual(self.http.data_reads, reads + 1)
                        self.assertEqual(ObservationClock.calls, timestamps + 1)
                        self.assertNotEqual(current["observed_at"], previous["observed_at"])
                        self.assertNotEqual(current["observed_at"], enqueue.isoformat())
                        self.assertEqual({key: value for key, value in current.items() if key != "observed_at"},
                                         {key: value for key, value in previous.items() if key != "observed_at"})
        self.fixture.fixture.assert_worker_requests()

    async def test_failed_reads_and_cancelled_sleep_leave_retained_timestamp(self) -> None:
        with mock.patch.object(datetime, "datetime", ObservationClock), self.fixture.clock.install():
            await self.fixture.handle("refresh", "")
            prior = self.assert_timestamp(ObservationClock.current)
            count = ObservationClock.calls
            self.http.data_status = 400
            for mode in (0, 5):
                self.mqtt.action_refresh_delay = mode
                for operation, payload in (("ac", "ON"), ("sauna", "OFF"), ("charge_limit", "70")):
                    ObservationClock.current += datetime.timedelta(minutes=1)
                    with self.assertLogs("teslabot.mqtt", "WARNING"):
                        await self.fixture.handle(operation, payload)
                        if mode:
                            event = await self.fixture.clock.next()
                            await self.read_job(event)
                    self.assertEqual(self.latest(), prior)
                    self.assertEqual(ObservationClock.calls, count)
            with self.assertLogs("teslabot.mqtt", "WARNING"):
                await self.fixture.handle("refresh", "")
            self.assertEqual(self.latest(), prior)
            self.http.data_status = 200
            reads = self.http.data_reads
            await self.fixture.handle("ac", "ON")
            event = await self.fixture.clock.next()
            job = self.fixture.session.jobs[self.fixture.fixture.identity]
            ObservationClock.current += datetime.timedelta(minutes=1)
            await self.mqtt.close()
            self.assertTrue(job.cancelled())
            event.set()
            self.assertEqual(self.http.data_reads, reads)
            self.assertEqual(self.latest(), prior)
            self.assertEqual(ObservationClock.calls, count)
        self.fixture.fixture.assert_worker_requests()

    async def test_superseded_inflight_read_cannot_advance_timestamp_before_latest_read(self) -> None:
        entered, release = threading.Event(), threading.Event()
        self.fixture.releases.append(release)
        original = self.http.send
        with mock.patch.object(datetime, "datetime", ObservationClock), self.fixture.clock.install():
            await self.fixture.handle("refresh", "")
            prior = self.assert_timestamp(ObservationClock.current)
            count = ObservationClock.calls
            def send(request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
                if "vehicle_data" in (request.url or ""):
                    entered.set()
                    release.wait(2)
                return original(request, **kwargs)
            send_patch = mock.patch.object(self.http, "send", side_effect=send)
            send_patch.start()
            self.addCleanup(send_patch.stop)
            ObservationClock.current += datetime.timedelta(minutes=1)
            await self.fixture.handle("ac", "ON")
            (await self.fixture.clock.next()).set()
            while not entered.is_set():
                await self.fixture.clock.real_sleep(0.001)
            old = self.fixture.session.jobs[self.fixture.fixture.identity]
            newer = asyncio.create_task(self.fixture.handle("ac", "OFF"))
            self.fixture.tasks.append(newer)
            await self.fixture.clock.real_sleep(0.01)
            self.assertFalse(old.done())
            self.assertFalse(newer.done())
            self.assertEqual(self.latest(), prior)
            release.set()
            await asyncio.wait_for(newer, 1)
            self.assertTrue(old.cancelled())
            self.assertEqual(self.latest(), prior)
            self.assertEqual(ObservationClock.calls, count)
            event = await self.fixture.clock.next()
            ObservationClock.current += datetime.timedelta(minutes=1)
            await self.read_job(event)
            self.assert_timestamp(ObservationClock.current)
            self.assertEqual(ObservationClock.calls, count + 1)
            self.assertEqual(len(self.snapshots), 2)  # manual and latest, not cancelled observation
        self.fixture.fixture.assert_worker_requests()
