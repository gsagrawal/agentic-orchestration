"""Measure sandbox cold start. `make bench`.

Cold start = from asking the controller for a sandbox until the first command
in it has returned. Reported as create (docker run + network create/attach +
registry), first exec (docker exec of `true`) and total. Teardown is reported
separately as `stop` (kill, wait until gone, remove the network): it is not part
of cold start but decides how fast a slot can be reused. Each run uses a fresh
sandbox and a fresh work dir. Percentiles are nearest-rank over N runs.
"""
import argparse
import math
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import stack  # noqa: E402
from agent_network_proxy.registry import Caller, Registry  # noqa: E402
from sandbox_controller import ProxyConfig, SandboxController  # noqa: E402

STATE = ROOT / ".state"


def pct(xs, p):
    xs = sorted(xs)
    return xs[max(0, math.ceil(p / 100 * len(xs)) - 1)]


def measure(image, proxied, runs):
    proxy = None
    if proxied:
        proxy = ProxyConfig("http://proxy:8080", STATE / "proxy-conf" / "mitmproxy-ca-cert.pem",
                            Registry(STATE / "registry" / "registry.json"), ROOT / "config" / "grants.json")
    ctl = SandboxController(image=image, proxy=proxy)
    create, first, total, stop = [], [], [], []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(runs + 1):                     # run 0 is a warm-up (image/layer caches) and is discarded
            caller = Caller(f"bench-{time.time_ns()}", "agent-a", "tenant-a", 1) if proxied else None
            t0 = time.monotonic()
            sbx = ctl.create(Path(tmp) / f"w{i}", caller)
            t1 = time.monotonic()
            ctl.exec(sbx, ["true"])
            t2 = time.monotonic()
            ctl.stop(sbx)
            t3 = time.monotonic()
            if i:
                create.append(t1 - t0); first.append(t2 - t1); total.append(t2 - t0); stop.append(t3 - t2)
    return create, first, total, stop


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=50)
    n = ap.parse_args().runs
    stack.up()
    docker = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}} {{.OperatingSystem}} {{.Architecture}} ncpu={{.NCPU}}"],
                            capture_output=True, text=True).stdout.strip()
    print(f"# Cold start, N={n} per row (+1 discarded warm-up), seconds\n")
    print(f"Host: {platform.system()} {platform.release()} | Docker {docker}\n")
    print("| image | network | metric | min | p50 | p95 | max |\n|---|---|---|---|---|---|---|")
    for image in ("agent-sandbox:poc-nopandoc", "agent-sandbox:poc"):
        for proxied in (False, True):
            net = "proxied (own internal net + registry)" if proxied else "none"
            for metric, xs in zip(("create", "first exec", "total", "stop"), measure(image, proxied, n)):
                print(f"| {image.split(':')[1]} | {net} | {metric} | {min(xs):.3f} | {statistics.median(xs):.3f} "
                      f"| {pct(xs, 95):.3f} | {max(xs):.3f} |")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
