"""
LiveKit voice agent worker — latency-optimized for English.

Pipeline (LLM unchanged — Google Gemini):
    Deepgram nova-3 (STT, English, fast endpointing)
      -> Google Gemini (LLM, preemptive generation)
      -> ElevenLabs Flash v2.5 (TTS, auto_mode)
    Silero VAD + STT-based turn detection for fast, natural turn-taking.

Latency choices:
  * language pinned to English everywhere (no auto-detect overhead)
  * STT endpointing 25 ms + no_delay -> STT finalizes quickly
  * turn_detection="stt" -> no heavy multilingual EOU model on the critical path
  * VAD min_silence_duration 0.2 s -> responds fast after the user stops
  * min_endpointing_delay 0.25 s, preemptive_generation -> reply starts early
  * aec_warmup_duration 0.5 s -> agent can speak ~2.5 s sooner at call start
  * prewarm loads the VAD once per worker (off the per-call path)
  * greeting via session.say() -> spoken instantly, no LLM roundtrip

Personalization:
  * If the caller's name is known (passed in participant metadata from the
    frontend, e.g. a returning visitor), greet them by name and skip asking.
  * Otherwise ask for their name right after greeting, then remember it via the
    record_user_name tool (which also pushes it to the frontend to store).

Run:  python agent.py dev     (development)
      python agent.py start   (production, lower overhead)
"""

import json
import os

import numpy as np
import requests
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions, function_tool
from livekit.plugins import deepgram, elevenlabs, google, silero

from knowledge import full_instructions

load_dotenv()

# Fixed first-time greeting — pre-rendered once at startup so it plays instantly
# (no live TTS synthesis on the call's critical path).
FIXED_GREETING = "Hey! I'm Ikli, from Iklipse. Who am I talking to?"
GREETING_SR = 24000  # ElevenLabs pcm_24000


def instructions_for(name: str | None) -> str:
    # Full Iklipse consultant persona + behavior + knowledge base (see knowledge.py).
    return full_instructions(name)


def _synth_greeting_pcm() -> bytes | None:
    """Render FIXED_GREETING to raw 16-bit PCM via the ElevenLabs REST API. Runs
    once at startup. Returns None on any failure (caller falls back to live TTS)."""
    api_key = os.environ.get("ELEVEN_API_KEY") or os.environ.get("ELEVENLABS_API_KEY")
    voice_id = os.environ.get("ELEVEN_VOICE_ID", "aMSt68OGf4xUZAnLpTU8")
    model = os.environ.get("ELEVEN_MODEL", "eleven_flash_v2_5")
    if not api_key:
        return None
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        r = requests.post(
            url,
            params={"output_format": f"pcm_{GREETING_SR}"},
            headers={"xi-api-key": api_key, "accept": "audio/pcm"},
            json={"text": FIXED_GREETING, "model_id": model},
            timeout=20,
        )
        if r.status_code == 200 and r.content:
            return r.content
    except Exception:
        pass
    return None


async def _pcm_to_frames(pcm: bytes):
    """Yield 20 ms rtc.AudioFrames from raw 16-bit mono PCM."""
    samples = np.frombuffer(pcm, dtype=np.int16)
    step = GREETING_SR // 50  # 20 ms
    for i in range(0, len(samples), step):
        chunk = samples[i : i + step]
        yield rtc.AudioFrame(
            data=chunk.tobytes(),
            sample_rate=GREETING_SR,
            num_channels=1,
            samples_per_channel=len(chunk),
        )


def prewarm(proc: agents.JobProcess):
    # Load Silero VAD once per worker process, off the per-call critical path.
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=0.2)
    # Pre-render the fixed greeting so first-time callers hear it instantly.
    proc.userdata["greeting_pcm"] = _synth_greeting_pcm()


def build_session(ctx: agents.JobContext) -> AgentSession:
    vad = ctx.proc.userdata.get("vad") or silero.VAD.load(min_silence_duration=0.2)
    return AgentSession(
        stt=deepgram.STT(
            model=os.environ.get("DEEPGRAM_MODEL", "nova-3"),
            language="en",            # English only — no language detection latency
            interim_results=True,
            smart_format=True,
            punctuate=True,
            no_delay=True,            # emit finals without extra hold
            endpointing_ms=25,        # fast end-of-speech
            filler_words=False,
            api_key=os.environ.get("DEEPGRAM_API_KEY"),
        ),
        llm=google.LLM(              # UNCHANGED per requirement
            model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-exp"),
            api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"),
        ),
        tts=elevenlabs.TTS(
            voice_id=os.environ.get("ELEVEN_VOICE_ID", "aMSt68OGf4xUZAnLpTU8"),
            model=os.environ.get("ELEVEN_MODEL", "eleven_flash_v2_5"),
            language="en",
            auto_mode=True,                    # start synthesis on boundaries -> lower latency
            enable_ssml_parsing=False,
            apply_text_normalization="off",    # skip normalization work
            api_key=os.environ.get("ELEVEN_API_KEY") or os.environ.get("ELEVENLABS_API_KEY"),
        ),
        vad=vad,
        turn_detection="stt",          # use Deepgram endpointing (fast) instead of heavy EOU model
        preemptive_generation=True,    # begin the reply before the user fully stops
        min_endpointing_delay=0.15,    # respond quickly after the user stops
        max_endpointing_delay=3.0,
        min_interruption_duration=0.3,
        aec_warmup_duration=0.5,       # trims ~2.5 s off startup vs the 3 s default
    )


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    # Read a known name from the caller's token metadata (returning visitor).
    participant = await ctx.wait_for_participant()
    known_name = None
    try:
        meta = json.loads(participant.metadata or "{}")
        known_name = (meta.get("name") or "").strip() or None
    except Exception:
        known_name = None

    @function_tool
    async def record_user_name(name: str) -> str:
        """Save the caller's first name when they tell you it, so we can remember
        and reuse it. Call this exactly once, as soon as you learn their name."""
        clean = (name or "").strip()
        if clean:
            try:
                # Push to the frontend so it can persist the name for next time.
                await ctx.room.local_participant.set_attributes({"user_name": clean})
            except Exception:
                pass
        return "saved"

    session = build_session(ctx)
    await session.start(
        room=ctx.room,
        agent=Agent(instructions=instructions_for(known_name), tools=[record_user_name]),
        room_input_options=RoomInputOptions(),
    )

    # Speak first, instantly.
    greeting_pcm = ctx.proc.userdata.get("greeting_pcm")
    if known_name:
        # Personalized greeting must be synthesized live (name varies).
        await session.say(f"Hey {known_name}! What can I do for you?", allow_interruptions=True)
    elif greeting_pcm:
        # Play the pre-rendered greeting audio — no synthesis latency.
        await session.say(
            FIXED_GREETING, audio=_pcm_to_frames(greeting_pcm), allow_interruptions=True
        )
    else:
        # Fallback: live TTS if pre-render failed.
        await session.say(FIXED_GREETING, allow_interruptions=True)


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            # THREAD executor runs jobs inside the already-warm worker process
            # (plugins imported + VAD loaded once). This avoids the ~6s per-call
            # process spawn + re-import you get with `agent.py dev`.
            # IMPORTANT: run `python agent.py start` (dev mode forces PROCESS
            # isolation for hot-reload and re-imports everything each call).
            job_executor_type=agents.JobExecutorType.THREAD,
            num_idle_processes=1,
        )
    )
