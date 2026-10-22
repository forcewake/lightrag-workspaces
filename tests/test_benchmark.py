"""Keep synthetic process peaks independent of the fixture parent's memory."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import benchmark_nano_load as benchmark


class MemoryMeasurementTests(unittest.TestCase):
    def test_linux_uses_current_address_space_high_water(self):
        with patch.object(benchmark.sys, "platform", "linux"), \
             patch.object(benchmark.resource, "getrusage", return_value=SimpleNamespace(ru_maxrss=500000)), \
             patch.object(Path, "read_text", return_value="Name:\tpython\nVmHWM:\t100000 kB\n"):
            result = benchmark.memory_high_water()
        self.assertEqual(result["peak_rss_bytes"], 100000 * 1024)
        self.assertEqual(result["ru_maxrss_bytes"], 500000 * 1024)
        self.assertEqual(result["peak_rss_source"], "proc.status.VmHWM")

    def test_unavailable_linux_proc_has_explicit_fallback(self):
        with patch.object(benchmark.sys, "platform", "linux"), \
             patch.object(benchmark.resource, "getrusage", return_value=SimpleNamespace(ru_maxrss=123)), \
             patch.object(Path, "read_text", side_effect=OSError("proc unavailable")):
            result = benchmark.memory_high_water()
        self.assertEqual(result["peak_rss_bytes"], 123 * 1024)
        self.assertEqual(result["peak_rss_source"], "getrusage.ru_maxrss")

    def test_macos_preserves_byte_units_without_proc(self):
        with patch.object(benchmark.sys, "platform", "darwin"), \
             patch.object(benchmark.resource, "getrusage", return_value=SimpleNamespace(ru_maxrss=456)), \
             patch.object(Path, "read_text", side_effect=AssertionError("macOS has no proc")):
            result = benchmark.memory_high_water()
        self.assertEqual(result["peak_rss_bytes"], 456)
        self.assertEqual(result["peak_rss_source"], "getrusage.ru_maxrss")


if __name__ == "__main__":
    unittest.main()
