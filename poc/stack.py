"""Start/stop the always-on stack (proxy, stub, networks)."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
DIRS = ("registry", "audit", "stub", "proxy-conf")


def up() -> None:
    for d in DIRS:
        (STATE / d).mkdir(parents=True, exist_ok=True)
        # Containers run as uid 65534, so the directories they write must be writable by it. The registry is
        # different: the controller writes it and the proxy only reads it, and whoever can replace it decides
        # which caller is which tenant, so it is not world-writable.
        if d == "registry":
            (STATE / d).chmod(0o755)
            continue
        try:                                  # as root: hand the directory to the containers' uid and close it to everyone else
            os.chown(STATE / d, 65534, 65534)
            (STATE / d).chmod(0o750)
        except PermissionError:               # as a normal user (e.g. on a Mac) chown is not allowed: open it to uid 65534
            (STATE / d).chmod(0o777)
    done = subprocess.run(["docker", "compose", "up", "-d", "--build", "--wait"], cwd=ROOT,
                          capture_output=True, text=True)
    if done.returncode:
        sys.exit(f"docker compose up failed:\n{done.stdout}\n{done.stderr}")


def down() -> None:
    subprocess.run(["docker", "compose", "down", "--remove-orphans"], cwd=ROOT, check=True)


if __name__ == "__main__":
    {"up": up, "down": down}[sys.argv[1]]()
