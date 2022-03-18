import asyncio
import unittest
import time
from teslabot import asyncthread

class TestAsyncThread(unittest.TestCase):
    def __init__(self, method_name: str) -> None:
        super().__init__(method_name)
        self.longMessage = True

    def test_simple(self) -> None:
        async def test() -> None:
            def syncfn() -> int:
                return 4
            value = await asyncthread.to_async(syncfn)
            self.assertEqual(value, 4)
        asyncio.get_event_loop().run_until_complete(test())

    def test_exn(self) -> None:
        async def test() -> None:
            def syncfn() -> None:
                raise Exception("moi")
            try:
                await asyncthread.to_async(syncfn)
                self.fail("Did not expect function to return")
            except Exception as exn:
                self.assertEqual(exn.args[0], "moi")
        asyncio.get_event_loop().run_until_complete(test())

    async def confirmed_async_sleep(self, delta: float) -> None:
        """Test that the asynchronous sleep is as long as we expect

        That is, it is not disturbed by long-running synchronous tasks."""
        t0 = time.monotonic()
        await asyncio.sleep(delta)
        t1 = time.monotonic()
        self.assertAlmostEqual(delta, t1 - t0, places=2)

    def test_sched(self) -> None:
        # This test can fail if the system is highly loaded
        async def test() -> None:
            def syncfn() -> None:
                time.sleep(0.5)
                time.sleep(0.5)
            async def asyncfn() -> None:
                t0 = time.monotonic()
                await self.confirmed_async_sleep(0.25)
                await self.confirmed_async_sleep(0.25)
                await self.confirmed_async_sleep(0.25)
                await self.confirmed_async_sleep(0.25)
                t1 = time.monotonic()
                self.assertAlmostEqual(1, t1 - t0, places=2)
            task = asyncio.create_task(asyncthread.to_async(syncfn))
            await asyncfn()
            await task
        asyncio.get_event_loop().run_until_complete(test())
