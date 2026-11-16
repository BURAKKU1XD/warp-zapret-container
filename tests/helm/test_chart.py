"""Render the real chart offline. Requires Helm and PyYAML; no cluster or WARP."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts/warp-zapret"


def helm(*args):
    return subprocess.run(
        ["helm", *map(str, args)], cwd=ROOT, capture_output=True,
        encoding="utf-8", timeout=30,
    )


def render(values=None, release="warp-zapret", chart=CHART):
    with tempfile.TemporaryDirectory(prefix="warp-helm-") as directory:
        overrides = Path(directory) / "values.yaml"
        overrides.write_text(yaml.safe_dump(values or {}), encoding="utf-8")
        result = helm("template", release, chart, "--namespace", "proxy", "-f", overrides)
    if result.returncode:
        raise AssertionError(result.stderr)
    documents = list(yaml.safe_load_all(result.stdout))
    return {item["kind"]: item for item in documents if item}


class ChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.default = render()
        cls.deployment = cls.default["Deployment"]
        cls.pod = cls.deployment["spec"]["template"]["spec"]
        cls.container = cls.pod["containers"][0]

    def test_strict_lint(self):
        result = helm("lint", CHART, "--strict")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dns_defaults_and_override_reach_pod_and_container(self):
        for servers in (["1.1.1.1", "8.8.8.8"], ["9.9.9.9"],
                        ["10.0.0.53", "1.0.0.1", "8.8.4.4"]):
            with self.subTest(servers=servers):
                actual = self.default if servers == ["1.1.1.1", "8.8.8.8"] else render({
                    "dnsServers": servers,
                })
                pod = actual["Deployment"]["spec"]["template"]["spec"]
                self.assertEqual(pod["dnsPolicy"], "None")
                self.assertEqual(pod["dnsConfig"], {"nameservers": servers})
                env = {v["name"]: v["value"] for v in pod["containers"][0]["env"]}
                self.assertEqual(env["DNS_SERVERS"], " ".join(servers))

    def test_packaged_chart_renders_the_same_resources(self):
        with tempfile.TemporaryDirectory(prefix="warp-helm-package-") as directory:
            result = helm("package", CHART, "--destination", directory)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            package, = Path(directory).glob("*.tgz")
            self.assertEqual(render(chart=package), self.default)

    def test_selectors_connect_service_deployment_and_policy(self):
        self.assertEqual(set(self.default), {
            "Deployment", "Service", "PersistentVolumeClaim", "NetworkPolicy",
        })
        labels = self.deployment["spec"]["template"]["metadata"]["labels"]
        for selector in (
            self.deployment["spec"]["selector"]["matchLabels"],
            self.default["Service"]["spec"]["selector"],
            self.default["NetworkPolicy"]["spec"]["podSelector"]["matchLabels"],
        ):
            self.assertTrue(selector)
            self.assertTrue(all(labels[key] == value for key, value in selector.items()))

    def test_single_process_state_and_recreate_upgrade(self):
        self.assertEqual(self.deployment["spec"]["replicas"], 1)
        self.assertEqual(self.deployment["spec"]["strategy"], {"type": "Recreate"})
        volumes = {v["name"]: v for v in self.pod["volumes"]}
        pvc = self.default["PersistentVolumeClaim"]
        self.assertEqual(volumes["state"]["persistentVolumeClaim"]["claimName"],
                         pvc["metadata"]["name"])
        self.assertEqual(pvc["metadata"]["annotations"]["helm.sh/resource-policy"], "keep")
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "1Gi")
        self.assertNotIn("storageClassName", pvc["spec"])

    def test_runtime_privileges_and_private_namespaces(self):
        security = self.container["securityContext"]
        self.assertEqual(security["runAsUser"], 0)
        self.assertFalse(security["privileged"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertEqual(set(security["capabilities"]["add"]), {"NET_ADMIN", "NET_RAW"})
        self.assertFalse(self.pod["automountServiceAccountToken"])
        for namespace in ("hostNetwork", "hostPID", "hostIPC"):
            self.assertFalse(self.pod.get(namespace, False))
        self.assertEqual(self.pod["nodeSelector"], {
            "kubernetes.io/os": "linux", "kubernetes.io/arch": "amd64",
        })
        volumes = {v["name"]: v for v in self.pod["volumes"]}
        self.assertEqual(volumes["tun"]["hostPath"],
                         {"path": "/dev/net/tun", "type": "CharDevice"})
        for name in ("tmp", "run"):
            self.assertEqual(volumes[name]["emptyDir"]["medium"], "Memory")
        mounts = {v["name"]: v["mountPath"] for v in self.container["volumeMounts"]}
        self.assertEqual(mounts["state"], "/var/lib/cloudflare-warp")
        self.assertEqual(mounts["tun"], "/dev/net/tun")

    def test_ports_are_internal_tcp_and_match_runtime(self):
        service = self.default["Service"]["spec"]
        self.assertEqual(service["type"], "ClusterIP")
        ports = {p["name"]: p for p in self.container["ports"]}
        self.assertEqual({p["port"] for p in service["ports"]}, {8888, 1080})
        for port in service["ports"]:
            self.assertEqual(ports[port["targetPort"]]["containerPort"], port["port"])
            self.assertEqual(port["protocol"], "TCP")
        self.assertTrue(all("hostPort" not in port for port in ports.values()))
        env = {v["name"]: v["value"] for v in self.container["env"]}
        self.assertEqual(env["PUBLIC_HTTP_PROXY_PORT"], "8888")
        self.assertEqual(env["PUBLIC_SOCKS_PROXY_PORT"], "1080")
        self.assertEqual(env["ZAPRET_STRATEGY"], "general")
        self.assertEqual(env["MALLOC_TRIM_THRESHOLD_"], "1048576")
        self.assertEqual(env["MALLOC_MMAP_THRESHOLD_"], "1048576")
        self.assertEqual(self.container["resources"]["limits"]["memory"], "256Mi")

    def test_probes_verify_real_proxy_health_but_liveness_is_local(self):
        for name in ("startupProbe", "readinessProbe"):
            probe = self.container[name]
            self.assertEqual(probe["exec"]["command"], ["/healthcheck.sh"])
            self.assertGreaterEqual(probe["timeoutSeconds"], 12)
        liveness = self.container["livenessProbe"]
        self.assertEqual(liveness["tcpSocket"]["port"], "http-proxy")
        self.assertNotIn("exec", liveness)

    def test_default_network_policy_is_opt_in_same_namespace(self):
        policy = self.default["NetworkPolicy"]["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress"])
        self.assertNotIn("egress", policy)
        self.assertEqual(policy["ingress"][0]["from"], [{
            "podSelector": {"matchLabels": {"warp-zapret-client": "true"}},
        }])
        self.assertEqual({p["port"] for p in policy["ingress"][0]["ports"]}, {8888, 1080})

    def test_other_namespace_requires_both_selectors_on_the_same_peer(self):
        peer = {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "apps"}},
            "podSelector": {"matchLabels": {"warp-zapret-client": "true"}},
        }
        actual = render({"networkPolicy": {"ingressFrom": [peer]}})
        self.assertEqual(actual["NetworkPolicy"]["spec"]["ingress"][0]["from"], [peer])

    def test_empty_ingress_denies_all_and_policy_can_be_disabled_explicitly(self):
        actual = render({"networkPolicy": {"ingressFrom": []}})
        self.assertEqual(actual["NetworkPolicy"]["spec"]["ingress"], [])
        actual = render({"networkPolicy": {"enabled": False}})
        self.assertNotIn("NetworkPolicy", actual)

    def test_existing_claim_and_ephemeral_state(self):
        actual = render({"persistence": {"existingClaim": "private-state"}})
        self.assertNotIn("PersistentVolumeClaim", actual)
        volumes = actual["Deployment"]["spec"]["template"]["spec"]["volumes"]
        state = next(v for v in volumes if v["name"] == "state")
        self.assertEqual(state["persistentVolumeClaim"]["claimName"], "private-state")
        actual = render({"persistence": {"enabled": False}})
        self.assertNotIn("PersistentVolumeClaim", actual)
        volumes = actual["Deployment"]["spec"]["template"]["spec"]["volumes"]
        state = next(v for v in volumes if v["name"] == "state")
        self.assertEqual(state["emptyDir"], {})

    def test_storage_class_default_disabled_and_custom(self):
        for value in ("", "fast"):
            with self.subTest(storage_class=value):
                actual = render({"persistence": {"storageClass": value}})
                self.assertEqual(actual["PersistentVolumeClaim"]["spec"]["storageClassName"],
                                 value)

    def test_name_image_strategy_and_scheduling_overrides(self):
        actual = render({
            "fullnameOverride": "shared-proxy",
            "image": {"repository": "example.test/proxy", "tag": "sha-fixture",
                      "pullPolicy": "IfNotPresent"},
            "imagePullSecrets": [{"name": "registry"}],
            "strategy": "alt12",
            "nodeSelector": {"pool": "network"},
            "tolerations": [{"key": "network", "operator": "Exists", "effect": "NoSchedule"}],
            "podAnnotations": {"example.test/collector": "enabled"},
        }, release="custom")
        for resource in actual.values():
            self.assertEqual(resource["metadata"]["name"], "shared-proxy")
        pod = actual["Deployment"]["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(container["image"], "example.test/proxy:sha-fixture")
        self.assertEqual(container["imagePullPolicy"], "IfNotPresent")
        self.assertEqual(pod["imagePullSecrets"], [{"name": "registry"}])
        self.assertEqual(pod["nodeSelector"]["pool"], "network")
        self.assertEqual(pod["nodeSelector"]["kubernetes.io/arch"], "amd64")
        self.assertEqual(pod["tolerations"][0]["key"], "network")
        self.assertEqual(next(e["value"] for e in container["env"]
                              if e["name"] == "ZAPRET_STRATEGY"), "alt12")
        self.assertEqual(actual["Deployment"]["spec"]["template"]["metadata"]["annotations"],
                         {"example.test/collector": "enabled"})

    def test_generated_names_are_consistent_for_long_release_names(self):
        actual = render(release="a" * 53)
        names = {item["metadata"]["name"] for item in actual.values()}
        self.assertEqual(len(names), 1)
        self.assertLessEqual(len(names.pop()), 63)

    def test_schema_rejects_invalid_runtime_inputs(self):
        cases = [
            {"dnsServers": []},
            {"dnsServers": ["1.1.1.1"] * 4},
            {"dnsServers": ["256.1.1.1"]},
            {"dnsServers": ["dns.example.test"]},
            {"dnsServers": ["01.1.1.1"]},
            {"strategy": "../custom"},
            {"strategy": "missing"},
            {"nodeSelector": {"kubernetes.io/arch": "arm64"}},
            {"nodeSelector": {"kubernetes.io/os": "windows"}},
            {"image": {"tag": ""}},
            {"persistence": {"enabled": False, "existingClaim": "private-state"}},
            {"networkPolicy": {"ingressFrom": [{}]}},
        ]
        for values in cases:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "invalid.yaml"
                path.write_text(yaml.safe_dump(values), encoding="utf-8")
                result = helm("template", "warp-zapret", CHART, "-f", path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("schema", result.stderr.lower())

    def test_schema_strategies_match_the_image_sources(self):
        schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))
        names = {path.stem for path in (ROOT / "config/strategies").glob("*.conf")}
        self.assertEqual(set(schema["properties"]["strategy"]["enum"]), names)


if __name__ == "__main__":
    unittest.main()
