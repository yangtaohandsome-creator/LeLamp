"""Cancellation bridge for the voice loop's blocking ALSA reads."""
import asyncio


async def capture_call(stop, function, *args, **kwargs):
    """Stop the device and join its reader before propagating cancellation."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        stop()
        # A second takeover/cancellation must not leave a reader behind either.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise
