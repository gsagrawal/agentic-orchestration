"""Small helpers shared by tests that use real processes and real time."""

import time


def wait_until(condition, what, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class Clock:
    """A clock the test moves by hand, shared by the manager and the executors."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now
