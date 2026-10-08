import subprocess
import sys
import unittest


class OptionalJinjaTests(unittest.TestCase):
    def test_missing_jinja_discovery_preserves_core_timestamp_tests(self) -> None:
        script = r'''
import importlib.abc
import io
import sys
import unittest

class MissingJinja(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "jinja2" or fullname.startswith("jinja2."):
            raise ModuleNotFoundError("No module named 'jinja2'", name="jinja2")
        return None

sys.meta_path.insert(0, MissingJinja())
loader = unittest.TestLoader()
discovered = loader.discover("tests")
def cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from cases(test)
        else:
            yield test
assert not any(type(test).__name__ == "_FailedTest" for test in cases(discovered)), loader.errors
import tests.test_mqtt as mqtt
import tests.test_action_refresh_timestamp as timestamp
assert mqtt.jinja2 is None and timestamp.jinja2 is None
suite = loader.loadTestsFromNames([
    "tests.test_action_refresh_timestamp.RefreshTimestampTests",
    "tests.test_mqtt.TestMqtt",
    "tests.test_expanded_telemetry.ExpandedTelemetryTests",
    "tests.test_expanded_telemetry.InstanceVersionTests",
])
output = io.StringIO()
result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
assert result.wasSuccessful(), output.getvalue()
assert {test.id() for test, reason in result.skipped} == {
    "tests.test_action_refresh_timestamp.RefreshTimestampTests.test_optional_ha_template_renders_observed_utc_timestamp",
    "tests.test_mqtt.TestMqtt.test_ac_template_resets_unknown_state",
    "tests.test_expanded_telemetry.ExpandedTelemetryTests.test_optional_templates_reset_known_zero_false_unknown_and_older_payloads",
}, output.getvalue()
for name in (
    "test_all_automatic_reads_update_ha_timestamp_like_manual_even_unchanged",
    "test_failed_reads_and_cancelled_sleep_leave_retained_timestamp",
    "test_superseded_inflight_read_cannot_advance_timestamp_before_latest_read",
):
    assert name + " " in output.getvalue() and name in output.getvalue()
assert result.testsRun - len(result.skipped) >= 3
print("Discovery imports successfully; core timestamps pass; expanded telemetry passes; only three optional rendering checks skip")
'''
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("core timestamps pass", result.stdout)
