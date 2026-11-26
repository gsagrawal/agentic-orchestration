"""Start/stop the always-on stack (proxy, stub, networks)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
DIRS = ("registry", "audit", "stub", "proxy-conf")


def up() -> None:
    for d in DIRS:
        (STATE / d).mkdir(parents=True, exist_ok=True)
        (STATE / d).chmod(0o777)   # containers run as uid 65534
    done = subprocess.run(["docker", "compose", "up", "-d", "--build", "--wait"], cwd=ROOT,
                          capture_output=True, text=True)
    if done.returncode:
        sys.exit(f"docker compose up failed:\n{done.stdout}\n{done.stderr}")


def down() -> None:
    subprocess.run(["docker", "compose", "down", "--remove-orphans"], cwd=ROOT, check=True)


if __name__ == "__main__":
    {"up": up, "down": down}[sys.argv[1]]()
