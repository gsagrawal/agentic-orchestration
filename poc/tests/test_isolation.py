"""Isolation and limits: what a hostile command cannot do to the host or to the sandbox's limits."""
import subprocess

import pytest

from sandbox_controller import Limits
from conftest import py, sh


@pytest.mark.parametrize("user,uid", [("65534:65534", "65534"), ("0:0", "0")])
def test_hardening_is_in_effect(make_sandbox, user, uid):
    # CapBnd is the bounding set: what --cap-drop ALL empties, and what a process could ever
    # regain. (CapEff is 0 for any non-root process anyway, so it would prove nothing.)
    ctl, sbx = make_sandbox(user=user)
    status = dict(line.split(":\t") for line in
                  sh(ctl, sbx, "grep -E '^(CapBnd|CapEff|NoNewPrivs)' /proc/self/status").stdout.splitlines())
    assert sh(ctl, sbx, "id -u").stdout.strip() == uid
    assert int(status["CapBnd"], 16) == 0 and int(status["CapEff"], 16) == 0
    assert status["NoNewPrivs"] == "1"


@pytest.mark.parametrize("user", ["65534:65534", "0:0"])
def test_root_filesystem_is_read_only(make_sandbox, user):
    # Also as uid 0: read-only must hold by mount, not merely by file permissions.
    ctl, sbx = make_sandbox(user=user)
    r = sh(ctl, sbx, "touch /etc/pwned; echo x > /usr/local/bin/pwned")
    assert r.exit_code != 0 and "Read-only file system" in r.stderr


def test_scratch_and_workdir_are_writable_for_the_sandbox_user(make_sandbox):
    ctl, sbx = make_sandbox()
    assert sh(ctl, sbx, "echo a > /tmp/scratch && echo b > /work/out").exit_code == 0
    assert (sbx.workdir / "out").read_text() == "b\n"


def test_tmpfs_is_size_capped(make_sandbox):
    ctl, sbx = make_sandbox(limits=Limits(tmpfs_mb=8))
    r = sh(ctl, sbx, "dd if=/dev/zero of=/tmp/big bs=1M count=64")
    assert r.exit_code != 0 and "No space left" in r.stderr


@pytest.mark.slow
def test_script_past_its_timeout_is_killed(make_sandbox):
    ctl, sbx = make_sandbox()
    r = sh(ctl, sbx, "sleep 60", timeout_s=2)
    assert r.timed_out and r.duration_s < 10
    gone = subprocess.run(["docker", "inspect", sbx.name], capture_output=True)
    assert gone.returncode != 0, "container must be gone, not just the client"


@pytest.mark.slow
def test_fork_bomb_is_contained_by_pids_limit(make_sandbox):
    ctl, sbx = make_sandbox(limits=Limits(pids=32))
    r = py(ctl, sbx, "import os,sys\nn=0\nwhile True:\n"
                     "    try:\n        pid=os.fork()\n    except OSError:\n        break\n"
                     "    if pid==0:\n        import time; time.sleep(30); os._exit(0)\n    n+=1\nprint(n)")
    assert r.exit_code == 0 and int(r.stdout) < 32  # fork() started failing before the limit was exceeded
    ctl.stop(sbx)
    assert subprocess.run(["docker", "inspect", sbx.name], capture_output=True).returncode != 0


@pytest.mark.slow
def test_classic_bash_fork_bomb_is_bounded(make_sandbox):
    ctl, sbx = make_sandbox(limits=Limits(pids=32))
    r = sh(ctl, sbx, ":(){ :|:& };:", timeout_s=5)
    assert r.duration_s < 15          # returned; the host was not taken down
    ctl.stop(sbx)


@pytest.mark.slow
def test_memory_hog_is_killed_by_the_memory_limit(make_sandbox):
    ctl, sbx = make_sandbox(limits=Limits(memory_mb=128))
    r = py(ctl, sbx, "x = bytearray(2 * 1024**3); print('allocated')")
    assert r.exit_code == 137 and "allocated" not in r.stdout   # SIGKILL from the cgroup OOM killer


def test_sandbox_cannot_read_host_files(make_sandbox, tmp_path):
    secret = tmp_path / "host-only.txt"
    secret.write_text("HOST-SECRET")
    ctl, sbx = make_sandbox()
    r = sh(ctl, sbx, f"cat {secret}; find / -xdev -name host-only.txt 2>/dev/null")
    assert "HOST-SECRET" not in r.stdout and "host-only.txt" not in r.stdout
    assert sh(ctl, sbx, "ls /var/run/docker.sock").exit_code != 0


def test_sandbox_cannot_read_another_sandboxs_workdir(make_sandbox, tmp_path):
    ctl, a = make_sandbox("a")
    _, b = make_sandbox("b")
    (a.workdir / "a-private.txt").write_text("A-SECRET")
    (b.workdir / "link").symlink_to(a.workdir / "a-private.txt")   # symlink to A's host path
    r = sh(ctl, b, f"cat /work/link; cat {a.workdir}/a-private.txt; ls /work")
    assert "A-SECRET" not in r.stdout
    assert "a-private.txt" not in sh(ctl, b, "ls -R /work 2>&1 | grep -v link").stdout


@pytest.mark.slow
def test_cpu_limit_throttles_a_busy_loop(make_sandbox):
    ctl, sbx = make_sandbox(limits=Limits(cpus=0.5))
    r = py(ctl, sbx, "import time\nw=time.monotonic(); c=time.process_time()\n"
                     "while time.monotonic()-w < 4: pass\n"
                     "print((time.process_time()-c)/(time.monotonic()-w))")
    assert 0.3 < float(r.stdout) < 0.7      # asked for half a core; a spinning loop gets about half
