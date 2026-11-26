import asyncio


async def wait_until(check, timeout=20.0, interval=0.05):
    """Polls an async or sync condition; fails the test on timeout instead of hanging."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(interval)
