import asyncio
import unittest
import time
from typing import List
from teslabot import utils

class TestUtils(unittest.TestCase):
    def __init__(self, method_name: str) -> None:
        super().__init__(method_name)
        self.longMessage = True

    def test_call_with_delay_info(self) -> None:
        with self.subTest():
            async def test() -> None:
                report = []
                async def report_fn() -> None:
                    report.append(True)
                async def work_fn() -> int:
                    return 4
                t0 = time.monotonic()
                value = await utils.call_with_delay_info(0.1,
                                                         report_fn,
                                                         work_fn())
                t1 = time.monotonic()
                self.assertEqual(value, 4)
                self.assertEqual(report, [])
                self.assertAlmostEqual(t1 - t0, 0.0, places=2)
            asyncio.get_event_loop().run_until_complete(test())

        with self.subTest():
            async def test2() -> None:
                report = []
                async def report_fn() -> None:
                    report.append(True)
                async def work_fn() -> int:
                    await asyncio.sleep(0.2)
                    return 4
                t0 = time.monotonic()
                value = await utils.call_with_delay_info(0.1,
                                                         report_fn,
                                                         work_fn())
                t1 = time.monotonic()
                self.assertEqual(value, 4)
                self.assertEqual(report, [True])
                self.assertAlmostEqual(t1 - t0, 0.2, places=2)
            asyncio.get_event_loop().run_until_complete(test2())

        with self.subTest():
            async def test3() -> None:
                report = []
                async def report_fn() -> None:
                    report.append(True)
                async def work_fn() -> int:
                    raise Exception("err")
                t0 = time.monotonic()
                with self.assertRaises(Exception):
                    value = await utils.call_with_delay_info(0.1,
                                                             report_fn,
                                                             work_fn())
                t1 = time.monotonic()
                self.assertEqual(report, [])
                self.assertAlmostEqual(t1 - t0, 0.0, places=2)
            asyncio.get_event_loop().run_until_complete(test3())

        with self.subTest():
            async def test4() -> None:
                report = []
                async def report_fn() -> None:
                    report.append(True)
                async def work_fn() -> int:
                    await asyncio.sleep(0.2)
                    raise Exception("err")
                t0 = time.monotonic()
                with self.assertRaises(Exception):
                    value = await utils.call_with_delay_info(0.1,
                                                             report_fn,
                                                             work_fn())
                t1 = time.monotonic()
                self.assertEqual(report, [True])
                self.assertAlmostEqual(t1 - t0, 0.2, places=2)
            asyncio.get_event_loop().run_until_complete(test4())

        with self.subTest():
            async def test5() -> None:
                report: List[bool] = []
                async def report_fn() -> None:
                    raise Exception("err")
                async def work_fn() -> int:
                    await asyncio.sleep(0.2)
                    raise Exception("err")
                t0 = time.monotonic()
                with self.assertRaises(Exception):
                    value = await utils.call_with_delay_info(0.1,
                                                             report_fn,
                                                             work_fn())
                t1 = time.monotonic()
                self.assertEqual(report, [])
                self.assertAlmostEqual(t1 - t0, 0.0, places=2)
            asyncio.get_event_loop().run_until_complete(test3())
