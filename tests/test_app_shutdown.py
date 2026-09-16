import asyncio
import signal
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from lelamp import app as app_module
from lelamp.lighting.controller import LightingController


class AppShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_signal_closes_all_started_services(self):
        lamp = MagicMock()
        lamp.tools = object()
        lamp.start = AsyncMock()
        lamp.close = AsyncMock()
        lamp.light_state = MagicMock()
        control = MagicMock(start=AsyncMock(), close=AsyncMock())
        remote = MagicMock(start=AsyncMock(), close=AsyncMock())

        async def voice_forever(_app):
            await asyncio.Event().wait()

        loop = asyncio.get_running_loop()
        handlers = {}

        def remember_handler(signum, callback):
            handlers[signum] = callback

        with (
            patch.object(app_module, "LampApp", return_value=lamp),
            patch.object(app_module, "ControlServer", return_value=control),
            patch.object(app_module, "RemoteTextServer", return_value=remote),
            patch.object(app_module, "run_voice", side_effect=voice_forever),
            patch.object(app_module, "resolve_location", side_effect=app_module.LocationError("offline")),
            patch.object(loop, "add_signal_handler", side_effect=remember_handler),
            patch.object(loop, "remove_signal_handler", return_value=True),
        ):
            task = asyncio.create_task(app_module.run())
            for _ in range(20):
                if signal.SIGTERM in handlers:
                    break
                await asyncio.sleep(0)
            handlers[signal.SIGTERM]()
            await asyncio.wait_for(task, timeout=1)

        remote.close.assert_awaited_once()
        control.close.assert_awaited_once()
        lamp.close.assert_awaited_once()

    async def test_startup_failure_still_closes_app(self):
        lamp = MagicMock()
        lamp.tools = object()
        lamp.start = AsyncMock()
        lamp.close = AsyncMock()
        control = MagicMock(
            start=AsyncMock(side_effect=RuntimeError("bind failed")),
            close=AsyncMock(),
        )

        with (
            patch.object(app_module, "LampApp", return_value=lamp),
            patch.object(app_module, "ControlServer", return_value=control),
            patch.object(app_module, "resolve_location", side_effect=app_module.LocationError("offline")),
        ):
            with self.assertRaisesRegex(RuntimeError, "bind failed"):
                await app_module.run()

        lamp.close.assert_awaited_once()

    def test_lighting_close_clears_initialized_driver(self):
        driver = MagicMock()
        controller = LightingController(rgb=driver)
        controller.close()
        driver.clear.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
