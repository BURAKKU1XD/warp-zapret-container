"""Validate Compose defaults and apply them in a network-isolated smoke test."""

import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RuntimeDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=ROOT, capture_output=True, check=True, timeout=30,
        )
        cls.service = json.loads(result.stdout)["services"]["warp-zapret1"]

    def test_balanced_allocator_defaults(self):
        environment = self.service["environment"]
        self.assertEqual(environment["MALLOC_TRIM_THRESHOLD_"], "1048576")
        self.assertEqual(environment["MALLOC_MMAP_THRESHOLD_"], "1048576")

    def test_memory_and_combined_swap_budget(self):
        self.assertEqual(int(self.service["mem_limit"]), 256 * 1024 * 1024)
        self.assertEqual(int(self.service["memswap_limit"]), int(self.service["mem_limit"]))

    def test_defaults_reach_runtime_and_cgroup(self):
        environment = self.service["environment"]
        command = [
            "docker", "run", "--rm", "--network", "none",
            "--memory", str(self.service["mem_limit"]),
            "--memory-swap", str(self.service["memswap_limit"]),
        ]
        for name in ("MALLOC_TRIM_THRESHOLD_", "MALLOC_MMAP_THRESHOLD_"):
            command.extend(["--env", name + "=" + environment[name]])
        command.extend([
            "--entrypoint", "/usr/bin/bash", self.service["image"], "-c",
            'set -eu; printf "%s\\n" "$MALLOC_TRIM_THRESHOLD_" "$MALLOC_MMAP_THRESHOLD_"; '
            'cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.swap.max',
        ])
        result = subprocess.run(command, capture_output=True, check=True, timeout=30)
        self.assertEqual(
            result.stdout.decode().splitlines(),
            ["1048576", "1048576", str(256 * 1024 * 1024), "0"],
        )


if __name__ == "__main__":
    unittest.main()
