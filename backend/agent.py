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

import asyncio
import json
import os
import re
import uuid

import numpy as np
import requests
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions, function_tool
from livekit.plugins import deepgram, elevenlabs, google, silero
from livekit.plugins.elevenlabs import tts as el_tts

from knowledge import full_instructions

load_dotenv()

# Fixed first-time greeting — pre-rendered once at startup so it plays instantly
# (no live TTS synthesis on the call's critical path).
FIXED_GREETING = "Hey! I'm Ikli, from Iklipse. Who am I talking to?"
GREETING_SR = 24000  # ElevenLabs pcm_24000

# Deepgram nova-3 Keyterm Prompting (English only): boost brand + domain words so
# the STT stops mishearing them (e.g. "Iklipse"/"Ikli" transcribed as "Eclipse").
KEYTERMS = [
    "Iklipse", "Ikli", "Digiredo", "Freyusion",
    "AI production", "AI-infused production", "brand experiences",
    "social media management", "post-production", "video editing",
    "digital marketing", "SEO", "media buying", "motion design", "VFX",
    "color grading", "virtual influencer", "Webflow",
    "Nabil", "Reem", "Cast your shadow",
]


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


# ---- Booking: Calendly link + WhatsApp send --------------------------------

def _digits_only(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _calendly_booking_url() -> str:
    """Create a fresh single-use Calendly scheduling link; fall back to the static
    booking URL if the API call fails or isn't configured. Blocking — call via
    asyncio.to_thread from async code."""
    token = os.environ.get("CALENDLY_TOKEN")
    event_type = os.environ.get("CALENDLY_EVENT_TYPE")
    static = os.environ.get("CALENDLY_BOOKING_URL", "")
    if token and event_type:
        try:
            r = requests.post(
                "https://api.calendly.com/scheduling_links",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"max_event_count": 1, "owner": event_type, "owner_type": "EventType"},
                timeout=10,
            )
            if r.status_code in (200, 201):
                url = (r.json() or {}).get("resource", {}).get("booking_url")
                if url:
                    return url
        except Exception:
            pass
    return static


def _send_whatsapp(phone: str, text: str) -> bool:
    """Send a WhatsApp message via GREEN-API. Blocking — call via asyncio.to_thread."""
    host = os.environ.get("GREENAPI_HOST", "https://api.green-api.com").rstrip("/")
    idi = os.environ.get("GREENAPI_ID")
    token = os.environ.get("GREENAPI_TOKEN")
    digits = _digits_only(phone)
    if not (idi and token and digits):
        return False
    try:
        r = requests.post(
            f"{host}/waInstance{idi}/sendMessage/{token}",
            json={"chatId": f"{digits}@c.us", "message": text},
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


def prewarm(proc: agents.JobProcess):
    # Load Silero VAD once per worker process, off the per-call critical path.
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=0.2)
    # Pre-render the fixed greeting so first-time callers hear it instantly.
    proc.userdata["greeting_pcm"] = _synth_greeting_pcm()


def build_session(ctx: agents.JobContext) -> AgentSession:
    vad = ctx.proc.userdata.get("vad") or silero.VAD.load(min_silence_duration=0.2)
    dg_model = os.environ.get("DEEPGRAM_MODEL", "nova-3")
    stt_kwargs = dict(
        model=dg_model,
        language="en",            # English only — no language detection latency
        interim_results=True,
        smart_format=True,
        punctuate=True,
        no_delay=True,            # emit finals without extra hold
        endpointing_ms=60,        # was 25 (too aggressive — clipped trailing words)
        filler_words=False,
        api_key=os.environ.get("DEEPGRAM_API_KEY"),
    )
    # Keyterm prompting is a nova-3 (English) feature; only send it on nova-3.
    if dg_model.startswith("nova-3"):
        stt_kwargs["keyterms"] = KEYTERMS
    return AgentSession(
        stt=deepgram.STT(**stt_kwargs),
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
            # Speak slightly slower so the spoken words and the on-screen caption
            # stay in sync (speed 1.0 was a touch ahead of the typewriter).
            voice_settings=el_tts.VoiceSettings(
                stability=0.5,
                similarity_boost=0.75,
                style=0.0,
                use_speaker_boost=True,
                speed=0.92,
            ),
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

    # Shared per-call state + a merge-safe attribute writer (set_attributes can
    # replace the whole map, so we always re-send every key we've set).
    state = {"phone": None}
    agent_attrs: dict[str, str] = {}

    async def _set_attr(**kw) -> None:
        agent_attrs.update({k: str(v) for k, v in kw.items()})
        try:
            await ctx.room.local_participant.set_attributes(dict(agent_attrs))
        except Exception:
            pass

    @function_tool
    async def record_user_name(name: str) -> str:
        """Save the caller's first name when they tell you it, so we can remember
        and reuse it. Call this exactly once, as soon as you learn their name."""
        clean = (name or "").strip()
        if clean:
            await _set_attr(user_name=clean)  # frontend persists it for next time
        return "saved"

    @function_tool
    async def open_phone_form() -> str:
        """Show the on-screen phone-number form to the caller. A small modal appears
        in the middle of their screen while the call stays live, so they can type
        their phone number (with country code). Call this when they want to book a
        meeting and you're about to collect their number. After calling it, tell them
        the form has appeared and what to enter, then wait for their number."""
        await _set_attr(open_phone_form=uuid.uuid4().hex)  # new value each time -> reopens
        return "form_shown"

    @function_tool
    async def send_booking_link(phone_number: str) -> str:
        """Once the caller has given AND confirmed their phone number, send them the
        Calendly booking link over WhatsApp. Pass the full number including country
        code. Returns 'sent' on success. Only call after they confirm the number."""
        phone = (phone_number or state.get("phone") or "").strip()
        digits = _digits_only(phone)
        if len(digits) < 8:
            return "invalid_number"
        state["phone"] = phone
        await _set_attr(save_user_phone=digits)  # frontend pre-fills WhatsApp export later
        url = await asyncio.to_thread(_calendly_booking_url)
        if not url:
            return "no_link_configured"
        ok = await asyncio.to_thread(
            _send_whatsapp,
            digits,
            f"Hey! Here's your Iklipse booking link — pick a time that suits you: {url}",
        )
        return "sent" if ok else "send_failed"

    session = build_session(ctx)
    await session.start(
        room=ctx.room,
        agent=Agent(
            instructions=instructions_for(known_name),
            tools=[record_user_name, open_phone_form, send_booking_link],
        ),
        room_input_options=RoomInputOptions(),
    )

    # Listen for the phone form's browser-side events (the caller submits a number,
    # or the form sits empty). The frontend sets these as its own participant
    # attributes; drive the next agent turn from them.
    agent_identity = ctx.room.local_participant.identity
    loop = asyncio.get_running_loop()

    async def _on_phone_submitted(phone: str) -> None:
        state["phone"] = phone
        session.generate_reply(
            instructions=(
                f"The caller just submitted their phone number through the form: {phone}. "
                "Read it back to them clearly and naturally, then ask them to confirm it's "
                "right. Do not call any tool or send anything until they confirm."
            )
        )

    async def _on_phone_idle() -> None:
        session.generate_reply(
            instructions=(
                "The phone form has been sitting empty for a few seconds. Gently check in — "
                "ask if they've had a chance to enter their number yet. Keep it brief and warm."
            )
        )

    def _on_attrs_changed(changed: dict, participant) -> None:
        try:
            if getattr(participant, "identity", None) == agent_identity:
                return  # ignore the agent's own attribute writes
            phone = changed.get("phone_number")
            if phone:
                loop.create_task(_on_phone_submitted(phone))
            if changed.get("phone_form_idle"):
                loop.create_task(_on_phone_idle())
        except Exception:
            pass

    ctx.room.on("participant_attributes_changed", _on_attrs_changed)

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
