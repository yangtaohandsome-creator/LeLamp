import os
import unittest
from unittest.mock import AsyncMock, patch

from lelamp import agent


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


if __name__ == "__main__":
    unittest.main()
