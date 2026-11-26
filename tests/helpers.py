"""Small helpers shared by tests that use real processes and real time."""

import time


def wait_until(condition, what, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")
