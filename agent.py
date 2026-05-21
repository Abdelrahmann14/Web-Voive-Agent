from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from livekit.agents import (
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    mcp,
)
import numpy as np
from livekit.plugins import assemblyai, silero, google
from livekit.plugins.silero import onnx_model as silero_onnx
from livekit.agents.types import NOT_GIVEN
from google.cloud import texttospeech as _gcloud_tts
from dotenv import load_dotenv
from api import VoiceAssistant
from prompts import INSTRUCTIONS, WELCOME_MESSAGE

load_dotenv()

N8N_MCP_URL = "https://iklipse.app.n8n.cloud/mcp/16e081d9-f84a-4867-8345-0d18985d7416"
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
ROOMS_DIR = os.path.join(os.path.dirname(__file__), "rooms")

_STANDARD_VOICE_GENDER: dict[str, str] = {
    "en-US-Standard-A": "male",   "en-US-Standard-B": "male",
    "en-US-Standard-C": "female", "en-US-Standard-D": "male",
    "en-US-Standard-E": "female", "en-US-Standard-F": "female",
    "en-US-Standard-G": "female", "en-US-Standard-H": "female",
    "en-US-Standard-I": "male",   "en-US-Standard-J": "male",
}




def prewarm(proc: JobProcess):
    vad = silero.VAD.load(
        sample_rate=8000,
        activation_threshold=0.3,
        min_silence_duration=0.25,
        prefix_padding_duration=0.1,
    )

    model = silero_onnx.OnnxModel(onnx_session=vad._onnx_session, sample_rate=8000)
    dummy = np.zeros(model.window_size_samples, dtype=np.float32)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(model, dummy).result()

    proc.userdata["vad"] = vad


def get_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {
        "language": "en",
        "voice_id": "EXAVITQu4vr4xnSDxMaL",
        "instructions": INSTRUCTIONS,
        "welcome_message": WELCOME_MESSAGE,
        "lead_data": {},
    }


def inject_variables(text: str, lead: dict) -> str:
    """Replace all {{variable}} placeholders with real values."""
    tz_str = lead.get("lead_time_zone", "UTC") or "UTC"
    try:
        tz = ZoneInfo(tz_str)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    fmt = "%B %d, %Y" 

    replacements = {
        "{{lead_name}}":           lead.get("lead_name", "") or "",
        "{{lead_email}}":          lead.get("lead_email", "") or "",
        "{{lead_phone}}":          lead.get("lead_phone", "") or "",
        "{{lead_time_zone}}":      tz_str,
        "{{current_date}}":        now.strftime(fmt),
        "{{earliest_booking_date}}": (now + timedelta(days=1)).strftime(fmt),
        "{{latest_booking_date}}": (now + timedelta(days=45)).strftime(fmt),
        "{{selected_date}}":       "",
        "{{selected_time}}":       "",
        "{{available}}":           "",
    }

    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)

    return text


async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    await ctx.wait_for_participant()

    config = get_config()

    # Per-room lead data (from /call payload) takes priority over config
    room_name = ctx.room.name or ""
    room_file = os.path.join(ROOMS_DIR, f"{room_name}.json")
    if os.path.exists(room_file):
        with open(room_file, encoding="utf-8") as f:
            lead = json.load(f)
        os.remove(room_file)  # clean up after reading
    else:
        lead = config.get("lead_data", {})

    llm = google.LLM(
        model="gemini-3-flash-preview",
        temperature=0.7,
    )

    # Inject lead variables + auto-computed dates into instructions
    raw_instructions = config.get("instructions", INSTRUCTIONS)
    instructions = inject_variables(raw_instructions, lead)

    tts_voice = config.get("tts_voice", "en-US-Standard-C")
    tts_speed = max(0.5, min(2.0, float(config.get("tts_speed", 1.0))))

    # Google Cloud TTS credentials — explicit file takes priority, else ADC
    creds_file = os.getenv("GOOGLE_TTS_CREDENTIALS_FILE") or None

    # Detect call type — SIP rooms are named "call-XXXXXXXX", web rooms are "room-XXXXXXXX"
    is_sip = room_name.startswith("call-")
    aai_cfg = config.get("agent_settings_sip" if is_sip else "agent_settings_web", {})
    print(f"[PROFILE] {'SIP' if is_sip else 'Web'} — {'phone' if is_sip else 'web test'} settings")

    # VAD — lower threshold = detects speech onset faster = faster interruption
    vad = ctx.proc.userdata["vad"]
    if is_sip:
        vad.update_options(activation_threshold=0.45, min_silence_duration=0.20)
    else:
        vad.update_options(activation_threshold=0.3, min_silence_duration=0.12)

    stt = assemblyai.STT(
        api_key=os.getenv("ASSEMBLYAI_API_KEY"),
        model="universal-streaming-multilingual",
        end_of_turn_confidence_threshold=float(aai_cfg.get("aai_eot_confidence", 0.65 if is_sip else 0.6)),
        min_turn_silence=int(aai_cfg.get("aai_min_turn_silence", 200 if is_sip else 160)),
        max_turn_silence=int(aai_cfg.get("aai_max_turn_silence", 2000 if is_sip else 2400)),
        vad_threshold=float(aai_cfg.get("aai_vad_threshold", 0.5 if is_sip else 0.4)),
        format_turns=bool(aai_cfg.get("aai_format_turns", True)),
    )
    print(f"[STT] AssemblyAI — universal-streaming-multilingual")

    # Validate voice — reset to safe default if not a Standard voice
    if "Standard" not in tts_voice:
        print(f"[TTS] WARNING — '{tts_voice}' is not a Standard voice. Resetting to en-US-Standard-C.")
        tts_voice = "en-US-Standard-C"

    _gender = _STANDARD_VOICE_GENDER.get(tts_voice, "female")

    # LINEAR16: WAV (header + 16-bit PCM) — required for Standard voices.
    # use_streaming=False: streaming_synthesize rejects non-Chirp3-HD voices.
    tts = google.TTS(
        model_name="chirp_3",
        voice_name=tts_voice,
        gender=_gender,
        speaking_rate=tts_speed,
        sample_rate=16000,
        audio_encoding=_gcloud_tts.AudioEncoding.LINEAR16,
        credentials_file=creds_file if creds_file else NOT_GIVEN,
        language="en-US",
        use_streaming=False,
    )
    print(f"[TTS] Google Standard — voice={tts_voice}, gender={_gender}, encoding=LINEAR16, sample_rate=16000")


    n8n_server = mcp.MCPServerHTTP(
        url=N8N_MCP_URL,
        transport_type="streamable_http",
        timeout=30,
        client_session_timeout_seconds=30,
    )
    n8n_toolset = mcp.MCPToolset(id="n8n", mcp_server=n8n_server)

    welcome_message = inject_variables(config.get("welcome_message", WELCOME_MESSAGE), lead)
    assistant = VoiceAssistant(instructions=instructions, welcome_message=welcome_message)

    session = AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad,
        tools=[n8n_toolset],
        turn_handling=TurnHandlingOptions(
            endpointing={
                "min_delay": 0.05,
                "max_delay": 0.2 if is_sip else 0.15,
            },
            interruption={
                "enabled":      True,
                "min_duration": 0.05,
                "min_words":    1,
            },
        ),
    )
    await session.start(assistant, room=ctx.room)

    # Disconnect after 6 minutes max
    async def _max_duration():
        await asyncio.sleep(6 * 60)
        await ctx.room.disconnect()

    asyncio.ensure_future(_max_duration())


if __name__ == "__main__":
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=prewarm,
        agent_name="iklipse-dialer",
        num_idle_processes=3,
    ))
