import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from lelamp.agent.pi_service import PiAgentService


def test_existing_agent_is_reused(monkeypatch):
    monkeypatch.setenv("PI_AGENT_TOKEN", "test-token")
    service = PiAgentService()
    service._healthy = AsyncMock(return_value=True)
    with patch("lelamp.agent.pi_service.subprocess.Popen") as spawn:
        asyncio.run(service.ensure_ready())
    spawn.assert_not_called()
    assert service.process is None


def test_missing_agent_is_started_once(monkeypatch):
    monkeypatch.setenv("PI_AGENT_TOKEN", "test-token")
    service = PiAgentService()
    service._healthy = AsyncMock(side_effect=(False, False, True))
    fake = MagicMock()
    fake.poll.return_value = None
    with patch("lelamp.agent.pi_service.subprocess.Popen", return_value=fake) as spawn:
        asyncio.run(service.ensure_ready())
    spawn.assert_called_once()
    assert service.process is fake


def test_remote_agent_is_not_replaced_locally(monkeypatch):
    monkeypatch.setenv("PI_AGENT_URL", "http://192.168.40.2:18792")
    monkeypatch.setenv("PI_AGENT_TOKEN", "test-token")
    service = PiAgentService()
    service._healthy = AsyncMock(return_value=False)
    with patch("lelamp.agent.pi_service.subprocess.Popen") as spawn:
        try:
            asyncio.run(service.ensure_ready())
        except RuntimeError as exc:
            assert "远程 Pi Agent" in str(exc)
        else:
            raise AssertionError("unreachable remote agent should fail")
    spawn.assert_not_called()
