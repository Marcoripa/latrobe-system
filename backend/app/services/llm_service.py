"""LLM service providing autonomous ReAct agent reasoning and tool orchestration."""

import logging
from typing import Any

from app.services.agent_runner import AgentRunner, agent_runner

logger = logging.getLogger(__name__)


class LLMService:
    """Service providing dynamic autonomous reasoning using Gemini ReAct loop."""

    def __init__(self, runner: AgentRunner | None = None) -> None:
        self._runner = runner or agent_runner

    async def run_agent(
        self,
        prompt: str,
        history: list[dict[str, Any]] | None = None,
    ) -> str:
        """Run the multi-step Autonomous ReAct agent against the given user prompt."""
        return await self._runner.run(prompt=prompt, history=history)


llm_service = LLMService()
