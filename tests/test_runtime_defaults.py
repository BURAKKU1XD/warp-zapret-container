"""Validate Compose defaults and apply them in a network-isolated smoke test."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_general import IMAGE

ROOT = Path(__file__).resolve().parents[1]


class RuntimeDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=ROOT, capture_output=True, check=True, timeout=30,
        )
        cls.service = json.loads(result.stdout)["services"]["warp-zapret"]

    def test_balanced_allocator_defaults(self):
        environment = self.service["environment"]
        self.assertEqual(environment["MALLOC_TRIM_THRESHOLD_"], "1048576")
        self.assertEqual(environment["MALLOC_MMAP_THRESHOLD_"], "1048576")

    def test_memory_and_combined_swap_budget(self):
        self.assertEqual(int(self.service["mem_limit"]), 256 * 1024 * 1024)
        self.assertEqual(int(self.service["memswap_limit"]), int(self.service["mem_limit"]))

    def test_standalone_installation_needs_only_compose_and_env(self):
        with tempfile.TemporaryDirectory(prefix="warp-two-files-") as directory:
            install = Path(directory)
            shutil.copyfile(ROOT / "compose.yaml", install / "compose.yaml")
            shutil.copyfile(ROOT / ".env.example", install / ".env")
            # Exercise automatic discovery and .env loading without shell overrides.
            environment = {name: value for name, value in os.environ.items()
                           if name not in ("WARP_ZAPRET_IMAGE", "ZAPRET_STRATEGY")
                           and not name.startswith("COMPOSE_")}
            for strategy in ("general", "alt12"):
                with self.subTest(strategy=strategy):
                    env_file = install / ".env"
                    env_file.write_text(
                        (ROOT / ".env.example").read_text(encoding="utf-8").replace(
                            "ZAPRET_STRATEGY=general", "ZAPRET_STRATEGY=" + strategy),
                        encoding="utf-8",
                    )
                    result = subprocess.run(
                        ["docker", "compose", "config", "--format", "json"],
                        cwd=install, env=environment, capture_output=True,
                        check=True, timeout=30,
                    )
                    service = json.loads(result.stdout)["services"]["warp-zapret"]
                    self.assertEqual(service["image"],
                                     "ghcr.io/burakku1xd/warp-zapret-container:latest")
                    self.assertNotIn("build", service)
                    self.assertNotIn("env_file", service)
                    self.assertEqual(service["environment"]["ZAPRET_STRATEGY"], strategy)
                    self.assertEqual(len(service["volumes"]), 1)
                    state = service["volumes"][0]
                    self.assertEqual(state["type"], "volume")
                    self.assertEqual(state["source"], "warp_zapret1_state")
                    self.assertEqual(state["target"], "/var/lib/cloudflare-warp")

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
            "--entrypoint", "/usr/bin/bash", IMAGE, "-c",
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
