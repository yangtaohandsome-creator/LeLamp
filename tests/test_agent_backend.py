import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from lelamp import agent
from lelamp.agent.common import AgentConnectionError
from lelamp.app import LampApp


class AgentBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_openclaw_is_default(self):
        with patch.dict(os.environ, {}, clear=False), patch(
            "lelamp.agent.openclaw.ask_agent", new=AsyncMock(return_value="openclaw")
        ) as ask:
            os.environ.pop("AGENT_BACKEND", None)
            self.assertEqual(await agent.ask_agent("你好", "s1"), "openclaw")
            ask.assert_awaited_once_with("你好", "s1")

    async def test_pi_backend(self):
        with patch.dict(os.environ, {"AGENT_BACKEND": "pi"}), patch(
            "lelamp.agent.pi.ask_agent", new=AsyncMock(return_value="pi")
        ) as ask:
            self.assertEqual(await agent.ask_agent("你好", "s2"), "pi")
            ask.assert_awaited_once_with("你好", "s2")

    async def test_only_pi_needs_explicit_session_clear(self):
        clear = AsyncMock()
        with patch.dict(os.environ, {"AGENT_BACKEND": "pi"}), patch(
            "lelamp.agent.pi.clear_session", new=clear
        ):
            await agent.clear_session("s3")
        clear.assert_awaited_once_with("s3")

    async def test_connection_failure_starts_agent_and_retries_once(self):
        motion = MagicMock()
        motion.robot = None
        app = LampApp(motion=motion, lighting=MagicMock())
        app.agent_service = MagicMock(ensure_ready=AsyncMock())
        with patch("lelamp.app.ask_agent", new=AsyncMock(side_effect=[AgentConnectionError("down"), "好了。"])) as ask:
            result = await app.handle_text("你好", "session-1")
        self.assertEqual(result.text, "好了。")
        self.assertEqual(ask.await_count, 2)
        app.agent_service.ensure_ready.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
