"""Import the non-Game-Filter strategies from zapret-discord-youtube 1.10.4.

Run explicitly with the unpacked release directory. Never executes batch files,
downloads updates, replaces user lists, or copies Windows executables.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex


ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.10.4"
QUEUE_DEFAULTS = """NFQUEUE_NUM="200"
NFQWS_PORTS_TCP="80,443,2053,2083,2087,2096,8443"
NFQWS_PORTS_UDP="443,19294:19344,50000:50100"
NFQWS_TCP_CONNBYTES="1:40"
NFQWS_UDP_CONNBYTES="1:12"
"""


def strategy_name(filename):
    if filename == "general.bat":
        return "general"
    match = re.fullmatch(
        r"general \((ALT(?:[2-9]|1[0-3])?|EXP|FAKE TLS AUTO(?: ALT[23]?)?|SIMPLE FAKE(?: ALT2?)?)\)\.bat",
        filename,
    )
    if not match:
        raise ValueError(f"Unexpected strategy filename: {filename}")
    return match[1].lower().replace(" ", "-")


def parse_batch(text):
    """Keep ordered nfqws-compatible options, dropping only disabled game profiles."""
    tcp = re.findall(r"--wf-tcp=(\S+)", text)
    udp = re.findall(r"--wf-udp=(\S+)", text)
    if tcp != ["80,443,2053,2083,2087,2096,8443,%GameFilterTCP%"]:
        raise ValueError("Unexpected Windows TCP interception boundary")
    if udp != ["443,19294-19344,50000-50100,%GameFilterUDP%"]:
        raise ValueError("Unexpected Windows UDP interception boundary")
    arguments = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("--"):
            continue
        if line.startswith(("--filter-tcp=%GameFilterTCP%", "--filter-udp=%GameFilterUDP%")):
            continue
        line = line.removesuffix("^").strip().replace("^!", "!")
        line = line.replace("%LISTS%", "/config/lists/")
        line = line.replace("%BIN%", "/opt/zapret/files/fake/")
        arguments.extend(shlex.split(line))
    if arguments and arguments[-1] == "--new":
        arguments.pop()
    if not arguments or not arguments[0].startswith("--filter-"):
        raise ValueError("No strategy profiles found")
    if any(not arg.startswith("--") or "%" in arg or "^" in arg for arg in arguments):
        raise ValueError("Untranslated Windows argument")
    return arguments


def import_release(source, destination=ROOT):
    if (source / "utils/game_filter.enabled").exists():
        raise ValueError("Expected the release with Game Filter disabled")
    batches = sorted(source.glob("general*.bat"))
    if len(batches) != 22:
        raise ValueError(f"Expected all 22 strategies from {VERSION}; found {len(batches)}")

    # Validate the complete input set before writing generated artifacts.
    strategies = {}
    payloads = {}
    for batch in batches:
        name = strategy_name(batch.name)
        data = batch.read_bytes()
        arguments = parse_batch(data.decode("utf-8-sig"))
        for arg in arguments:
            value = arg.partition("=")[2]
            if value.startswith("/opt/zapret/files/fake/"):
                payload = Path(value).name
                if value != "/opt/zapret/files/fake/" + payload:
                    raise ValueError(f"Unexpected payload path: {value}")
                payloads[payload] = (source / "bin" / payload).read_bytes()
            elif value.startswith("/config/lists/"):
                local = destination / "assets/lists" / Path(value).name
                if not local.is_file():
                    raise ValueError(f"Missing existing list: {local.name}")
        strategies[name] = (batch.name, data, arguments)

    for name, data in payloads.items():
        target = destination / "assets/fake" / name
        if target.exists() and target.read_bytes() != data:
            raise ValueError(f"Existing payload differs from release: {name}")

    configs = destination / "config/strategies"
    fixtures = destination / "tests/fixtures/strategies-1.10.4"
    configs.mkdir(parents=True, exist_ok=True)
    fixtures.mkdir(parents=True, exist_ok=True)
    manifest = {"project": "zapret-discord-youtube", "version": VERSION,
                "game_filter": False, "strategies": {}, "payloads": {}}
    for name, (filename, data, arguments) in sorted(strategies.items()):
        header = f"# Imported from zapret-discord-youtube {VERSION}: {filename}\n"
        header += "# Game Filter disabled; Windows capture replaced by container NFQUEUE.\n"
        if name == "general":
            # Keep the existing CONFIG path and verified default byte-for-byte.
            config = header + "source /config/zapret1-warp.conf\n"
        else:
            config = header + QUEUE_DEFAULTS + "\nNFQWS_ARGS=(\n"
            config += "".join("  " + shlex.quote(arg) + "\n" for arg in arguments)
            config += ")\n"
        (configs / (name + ".conf")).write_text(config, encoding="utf-8", newline="\n")
        (fixtures / filename).write_bytes(data)
        manifest["strategies"][name] = {
            "source": filename, "sha256": hashlib.sha256(data).hexdigest(),
            "profiles": arguments.count("--new") + 1,
        }
    for name, data in sorted(payloads.items()):
        (destination / "assets/fake" / name).write_bytes(data)
        manifest["payloads"][name] = hashlib.sha256(data).hexdigest()
    (fixtures / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n",
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Unpacked zapret-discord-youtube 1.10.4 directory")
    args = parser.parse_args()
    result = import_release(args.source)
    print(f"Imported {len(result['strategies'])} strategies, {len(result['payloads'])} payloads.")
