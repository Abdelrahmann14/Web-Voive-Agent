from __future__ import annotations

import asyncio
import logging

from livekit.agents import Agent, JobContext, function_tool, get_job_context

logger = logging.getLogger("voice-agent")
logger.setLevel(logging.INFO)


class VoiceAssistant(Agent):

    def __init__(self, *, instructions: str, welcome_message: str) -> None:
        super().__init__(instructions=instructions)
        self._welcome = welcome_message

    async def on_enter(self) -> None:
        await self.session.say(self._welcome)

    @function_tool(description="End the call and disconnect the participant. Call this only after your farewell has been spoken and there has been at least one exchange after the closing.")
    async def end_call(self) -> str:
        logger.info("end_call triggered — disconnecting after speech drains")
        ctx: JobContext = get_job_context()

        # Let the LLM reply (the farewell that triggered this tool) finish playing
        # before we pull the room out from under the audio pipeline.
        async def _disconnect_after_speech():
            await asyncio.sleep(1.5)
            await ctx.room.disconnect()

        asyncio.ensure_future(_disconnect_after_speech())
        return "Call ended."
