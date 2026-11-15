"""livekit-agents entrypoint; joins room. Owner: Ming / Febri.

Minimal voice smoke: STT → Gemini → ElevenLabs, Odyssey persona, greet once.
Tools and screen publishing come later.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, JobContext

from agent.voice import build_session, greeting_instructions, load_persona, sync_env_aliases

# Repo-root .env (not .env.example).
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
sync_env_aliases()


class Mentor(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=load_persona())


server = AgentServer()


@server.rtc_session(agent_name="hackuta-mentor")
async def mentor_session(ctx: JobContext) -> None:
    # Job subprocesses need .env loaded again (parent env is not enough).
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    sync_env_aliases()

    session = build_session()
    await session.start(room=ctx.room, agent=Mentor())
    await session.generate_reply(instructions=greeting_instructions())


if __name__ == "__main__":
    agents.cli.run_app(server)
