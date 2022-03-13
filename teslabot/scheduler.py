#!/usr/bin/env python

import asyncio
import time
import datetime
import aiounittest
import unittest
import traceback
import logging
from typing import List, Optional, Awaitable, Callable, Tuple, Coroutine, Any
from typing_extensions import Protocol

logger = logging.getLogger(__name__)

class CallbackProtocol(Protocol):
    def __call__(self) -> Awaitable[None]:
        ...

class Entry:
    callback: CallbackProtocol

    def __init__(self,
                 callback: Callable[[], Awaitable[None]]) -> None:
        self.callback = callback

    def when_is_next(self, now: float) -> Optional[float]:
        """When is next activation in unix time stamp after the given timestamp

or None if no such activation is in this schedule"""
        return None

class Daily(Entry):
    time: datetime.time

    def __init__(self,
                 callback: Callable[[], Awaitable[None]],
                 time: datetime.time) -> None:
        super().__init__(callback)
        self.time = time

    def when_is_next(self, now: float) -> Optional[float]:
        """When is next activation in unix time stamp after the given timestamp

or None if no such activation is in this schedule"""

        now_dt = datetime.datetime.fromtimestamp(now, self.time.tzinfo)
        if now_dt.timetz() < self.time:
            now_dt = datetime.datetime.combine(now_dt.date(), self.time)
        elif now_dt.timetz() > self.time:
            now_dt = datetime.datetime.combine(now_dt.date() + datetime.timedelta(days=1), self.time)
        assert now_dt.timetz() >= self.time
        return now_dt.timestamp()

    def __str__(self) -> str:
        return f"Daily at {self.time}" 

class OneShot(Entry):
    time: datetime.datetime

    def __init__(self,
                 callback: Callable[[], Awaitable[None]],
                 time: datetime.datetime) -> None:
        super().__init__(callback)
        self.time = time

    def when_is_next(self, now: float) -> Optional[float]:
        if self.time.timestamp() > now:
            return self.time.timestamp()
        else:
            return None

class AsyncSleepProtocol(Protocol):
    def __call__(self, delta: float, condition: asyncio.Condition) -> Coroutine[Any, Any, None]:
        ...

async def default_sleep(delta: float, condition: asyncio.Condition) -> None:
    try:
        await asyncio.wait_for(condition.wait(), timeout=delta)
    except asyncio.TimeoutError:
        # The caller will need to determine if this has slept this interval in full
        pass

class Scheduler:
    sleep: AsyncSleepProtocol

    def __init__(self) -> None:
        self._entries = [] # type: List[Entry]
        self._entries_cond = asyncio.Condition()
        self._task: Optional[asyncio.Task[None]] = None
        async def get_time() -> float:
            return time.time()
        self.now = get_time # allows overriding time retrieval functino for test purposes
        self.sleep = default_sleep # allows overriding the sleeping function for test purposes

    async def start(self) -> None:
        logger.info(f"Starting")
        assert not self._task
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._scheduler())

    async def stop(self) -> None:
        logger.info(f"Stopping")
        assert self._task
        self._task.cancel()
        self._task = None
        logger.info(f"Stopped")
        
    def get_earliest(self, now: float, blacklist: Tuple[float, List[Entry]] = (0.0, [])) -> Optional[Tuple[float, Entry]]:
        earliest: Optional[Tuple[float, Entry]] = None
        for entry in self._entries:
            when = entry.when_is_next(now)
            if when is not None \
               and ((earliest is None or when < earliest[0]) \
                    and (when > blacklist[0] or not [x for x in blacklist[1] if x is entry])):
                earliest = (when, entry)
        return earliest
            
    async def _scheduler(self) -> None:
        try:
            previously_activated: List[Entry] = []
            previously_activate_time = 0.0
            while True:
                earliest: List[Optional[Tuple[float, Entry]]] = [None]
                now = await self.now()
                async with self._entries_cond:
                    def grab_earliest() -> bool:
                        earliest[0] = self.get_earliest(now, (previously_activate_time, previously_activated))
                        return earliest[0] is not None
                    await self._entries_cond.wait_for(grab_earliest)

                assert earliest[0]

                next_time = earliest[0][0]
                next_entry = earliest[0][1]

                now = await self.now()
                till_next = next_time - now
                logger.info(f"Sleeping {till_next} seconds to {datetime.datetime.fromtimestamp(next_time)} before running task")
                if till_next > 0:
                    async with self._entries_cond:
                        await self.sleep(till_next, self._entries_cond)

                now = await self.now()
                if now >= next_time:
                    if next_time != previously_activate_time:
                        previously_activated = []
                    previously_activate_time = next_time
                    previously_activated.append(next_entry)
                    try:
                        await next_entry.callback()
                    except asyncio.CancelledError:
                        pass
                    except:
                        logger.info(f"Scheduler task threw an exception, ignoring: {traceback.format_exc()}")
        except asyncio.CancelledError:
            pass
        except:
            traceback.print_exc()
            raise
                
    async def add(self, entry: Entry) -> None:
        async def adder(entries: List[Entry]) -> List[Entry]:
            logger.info(f"Adding entry {entry}")
            entries.append(entry)
            return entries
        await self.update_entries(adder)

    async def remove(self, entry: Entry) -> None:
        async def remover(entries: List[Entry]) -> List[Entry]:
            logger.info(f"Removing entry {entry}")
            return [e for e in entries if e is not entry]
        await self.update_entries(remover)

    async def update_entries(self, updater: Callable[[List[Entry]], Awaitable[List[Entry]]]) -> None:
        async with self._entries_cond:
            logger.info(f"Updating entries")
            self._entries = await updater(self._entries)
            self._entries_cond.notify_all()

class TestSchedule(aiounittest.AsyncTestCase): # type: ignore
    def test_empty(self) -> None:
        sch = Scheduler()
        self.assertTrue(sch.get_earliest(0.0) is None)
        
    async def test_one1(self) -> None:
        sch = Scheduler()
        async def callable() -> None:
            return None
        entry = Daily(callable, datetime.time.fromisoformat("00:00+00:00"))
        await sch.add(entry)

        t0 = sch.get_earliest(0.0)
        t1 = sch.get_earliest(1.0)

        self.assertIsNotNone(t0)
        assert t0

        self.assertIsNotNone(t1)
        assert t1

        self.assertEqual(t0[0], 0.0)
        self.assertTrue(t0[1] is entry)
        
        self.assertEqual(t1[0], 24 * 3600.0)
        self.assertTrue(t1[1] is entry)
       
    async def test_one2(self) -> None:
        sch = Scheduler()
        async def callable() -> None:
            return None
        entry = Daily(callable, datetime.time.fromisoformat("04:00+00:00"))
        await sch.add(entry)
        
        t0 = sch.get_earliest(2 * 3600.0)
        t1 = sch.get_earliest(4 * 3600.0)
        t2 = sch.get_earliest(6 * 3600.0)
        
        self.assertIsNotNone(t0)
        assert t0

        self.assertIsNotNone(t1)
        assert t1

        self.assertIsNotNone(t2)
        assert t2

        self.assertEqual(t0[0], (4) * 3600.0)
        self.assertTrue(t0[1] is entry)
        
        self.assertEqual(t1[0], (4) * 3600.0)
        self.assertTrue(t1[1] is entry)
        
        self.assertEqual(t2[0], (4 + 24) * 3600.0)
        self.assertTrue(t2[1] is entry)

    async def test_two(self) -> None:
        sch = Scheduler()
        async def callable() -> None:
            return None
        entry1 = Daily(callable, datetime.time.fromisoformat("00:00+00:00"))
        await sch.add(entry1)
        entry2 = Daily(callable, datetime.time.fromisoformat("01:00+00:00"))
        await sch.add(entry2)

        t0 = sch.get_earliest(0.0)
        t1 = sch.get_earliest(1.0)
        t2 = sch.get_earliest(3599.0)
        t3 = sch.get_earliest(3600.0)
        t4 = sch.get_earliest(3601.0)
        t5 = sch.get_earliest(24 * 3600.0 - 1)
        t6 = sch.get_earliest(24 * 3600.0 + 1)
        
        self.assertIsNotNone(t0)
        assert t0

        self.assertIsNotNone(t1)
        assert t1

        self.assertIsNotNone(t2)
        assert t2

        self.assertIsNotNone(t3)
        assert t3

        self.assertIsNotNone(t4)
        assert t4

        self.assertIsNotNone(t5)
        assert t5

        self.assertIsNotNone(t6)
        assert t6

        self.assertEqual(t0[0], 0.0)
        self.assertTrue(t0[1] is entry1)
        
        self.assertEqual(t1[0], 1 * 3600.0)
        self.assertTrue(t1[1] is entry2)
        
        self.assertEqual(t2[0], 1 * 3600.0)
        self.assertTrue(t2[1] is entry2)
        
        self.assertEqual(t3[0], 1 * 3600.0)
        self.assertTrue(t3[1] is entry2)
        
        self.assertEqual(t4[0], 24 * 3600.0)
        self.assertTrue(t4[1] is entry1)
        
        self.assertEqual(t5[0], 24 * 3600.0)
        self.assertTrue(t5[1] is entry1)
        
        self.assertEqual(t6[0], (24 + 1) * 3600.0)
        self.assertTrue(t6[1] is entry2)

    async def test_add_live1(self) -> None:
        executions_cond = asyncio.Condition()
        ready_flag = [False]
        now = [0.0]
        async def fake_sleep(delta: float, condition: asyncio.Condition) -> None:
            #print(f"\"sleeping\" for {delta}")
            now[0] += delta
        async def fake_now() -> float:
            #print(f"\"now\" is {now[0]}")
            return now[0]
        sch = Scheduler()
        sch.sleep = fake_sleep
        sch.now = fake_now
        
        async def callable() -> None:
            ready_flag[0] = True
            async with executions_cond:
                executions_cond.notify_all()

        await sch.start()

        async def run_operations() -> None:
            #print("run operations")
            await sch.add(Daily(callable, datetime.time.fromisoformat("01:00+00:00")))
            #print("done running operations")
        
        loop = asyncio.get_event_loop()
        task = loop.create_task(run_operations())

        async with executions_cond:
            await executions_cond.wait_for(lambda: ready_flag[0])

        #print("stopping scheduler")
        await sch.stop()
        assert ready_flag[0]

    async def test_add_live2(self) -> None:
        now = [0.0]
        executions = [] # type: List[Tuple[float, str]]
        executions_cond = asyncio.Condition()
        async def fake_sleep(delta: float, condition: asyncio.Condition) -> None:
            #print(f"\"sleeping\" for {delta}")
            now[0] += delta
        async def fake_now() -> float:
            #print(f"\"now\" is {now[0]}")
            return now[0]
        sch = Scheduler()
        sch.sleep = fake_sleep
        sch.now = fake_now

        def mk_callable(label: str) -> Callable[[], Coroutine[Any, Any, None]]:
            async def callable() -> None:
                async with executions_cond:
                    executions.append((now[0], label))
                    executions_cond.notify_all()
                    await asyncio.sleep(0.0000000001)
            return callable

        await sch.start()

        async def run_operations() -> None:
            await sch.add(Daily(mk_callable("callable1"), datetime.time.fromisoformat("01:00+00:00")))
            await sch.add(Daily(mk_callable("callable2"), datetime.time.fromisoformat("02:00+00:00")))
        
        loop = asyncio.get_event_loop()
        task = loop.create_task(run_operations())

        reference_executions = [((1) * 3600.0, "callable1"),
                                ((2) * 3600.0, "callable2"),
                                ((24 + 1) * 3600.0, "callable1"),
                                ((24 + 2) * 3600.0, "callable2")]

        def executions_wait() -> bool:
            #print(f"executions_wait")
            return len(executions) >= len(reference_executions)
        
        #print("stopping scheduler")
        async with executions_cond:
            await executions_cond.wait_for(executions_wait)

        #print("stopping scheduler")
        await sch.stop()

        assert executions == reference_executions

if __name__ == '__main__':
    unittest.main()
