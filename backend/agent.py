"""
LiveKit voice agent worker, latency-optimized for English.

Pipeline:
    Deepgram nova-3 (STT, English, fast endpointing)
      -> Claude Sonnet 5.5 (LLM, effort low, prompt caching, preemptive generation)
      -> ElevenLabs Flash v2.5 (TTS, auto_mode)
    Silero VAD + STT-based turn detection for fast, natural turn-taking.

Latency choices:
  * language pinned to English everywhere (no auto-detect overhead)
  * STT endpointing 60 ms + no_delay -> STT finalizes quickly
  * turn_detection="stt" -> no heavy multilingual EOU model on the critical path
  * VAD min_silence_duration 0.2 s -> responds fast after the user stops
  * endpointing min_delay 0.1 s, preemptive generation -> reply starts early
  * aec_warmup_duration 0.5 s -> agent can speak ~2.5 s sooner at call start
  * prewarm loads the VAD once per worker (off the per-call path)
  * greeting via session.say() -> spoken instantly, no LLM roundtrip

Personalization:
  * If the caller's name is known (passed in participant metadata from the
    frontend, e.g. a returning visitor), greet them by name and skip asking.
  * Otherwise the greeting asks for their name and the model uses it from the
    conversation (no tool call, so no extra LLM round trip).

Run:  python agent.py start   (fast: jobs run in the warm worker, use this)
      python agent.py dev     (hot-reload, but re-imports everything per call)
"""

import asyncio
import json
import logging
import os
import re
import time

import anthropic as anthropic_sdk
import numpy as np
import requests
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import NOT_GIVEN, Agent, AgentSession, function_tool
from livekit.agents.utils import is_given
from livekit.plugins import anthropic, deepgram, elevenlabs, silero
from livekit.plugins.anthropic import llm as _anthropic_llm
from livekit.plugins.elevenlabs import tts as el_tts

from knowledge import full_instructions
from messaging import digits_only, send_email, send_whatsapp, whatsapp_ready

load_dotenv()

logger = logging.getLogger("ikli")

# Fixed first-time greeting, pre-rendered once at startup so it plays instantly
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


# The plugin only knows that the 4.6 models reject a trailing assistant message
# (prefill). Current models reject it too, so a reply triggered without the caller
# speaking (form submitted/closed, idle check) would 400. Treat every Claude model
# as no-prefill so the plugin appends the trailing user turn it needs.
_anthropic_llm._NO_PREFILL_PATTERNS = ("claude-",)


class ClaudeLLM(anthropic.LLM):
    """Claude tuned for a voice call.

    - effort "low": measured first token ~0.85 s vs ~1.3 s at the default effort,
      with replies just as good for short spoken answers. (Turning thinking off
      entirely via "between_tools" measured slower, ~1.5 s, so it stays adaptive.)
    - server-side refusal fallback: if a safety classifier ever declines a turn,
      the API re-runs it on a fallback model inside the same call instead of
      leaving the caller in silence.
    """

    def __init__(self, *, api_key: str | None = None, **kwargs):
        # The plugin's default client is built on `httpx`, which the current
        # Anthropic SDK (built on `httpx2`) rejects with a TypeError, so every call
        # crashed at start. Hand it an SDK-native client instead.
        client = anthropic_sdk.AsyncAnthropic(
            api_key=api_key, timeout=anthropic_sdk.Timeout(30.0, connect=5.0)
        )
        super().__init__(client=client, api_key=api_key or NOT_GIVEN, **kwargs)

    def chat(self, *, extra_kwargs=NOT_GIVEN, **kwargs):
        extra = dict(extra_kwargs) if is_given(extra_kwargs) else {}
        extra.setdefault("output_config", {"effort": os.environ.get("LLM_EFFORT", "low")})
        extra.setdefault("extra_headers", {"anthropic-beta": "server-side-fallback-2026-07-01"})
        extra.setdefault("extra_body", {"fallbacks": "default"})
        return super().chat(extra_kwargs=extra, **kwargs)


# Contact details the caller says out loud are captured straight from the
# transcript (no LLM tool call, so no extra round trip on that turn) and pushed to
# the browser to pre-fill the booking form and the report export.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SPOKEN_EMAIL_RE = re.compile(r"\b([\w.+-]+) at ([\w-]+(?: dot [\w-]+)+)\b", re.I)
_PHONE_IN_SPEECH = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def _contacts_in(text: str) -> tuple[str | None, str | None]:
    email = None
    if m := _EMAIL_RE.search(text):
        email = m.group().rstrip(".")
    elif m := _SPOKEN_EMAIL_RE.search(text):
        email = f"{m.group(1)}@{m.group(2).replace(' dot ', '.')}".lower()
    phone = None
    for m in _PHONE_IN_SPEECH.finditer(text):
        digits = digits_only(m.group())
        if len(digits) >= 8:
            phone = digits
    return phone, email


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


# ---- Booking: Calendly link -----------------------------------------------

def _calendly_booking_url() -> str:
    """Create a fresh single-use Calendly scheduling link; fall back to the static
    booking URL if the API call fails or isn't configured. Blocking, call via
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


# Phone numbers are spoken one digit at a time for a clear read-back
# ("+201150472975" -> "+2 0 1 1 ..."). Only runs of 7+ digits (optionally split by
# single spaces, dashes or dots) count as a phone number, so years, prices and
# stats like "2019", "150+" or "80%" are still read naturally. Only affects the
# spoken audio; the transcript keeps the compact number.
_PHONE_RUN = re.compile(r"\+?\d(?:[ .\-]?\d){6,}")
# A number that may still be growing at the end of a streamed chunk; held back
# until the next chunk so a phone number split across chunks is spaced as one.
_NUMBER_TAIL = re.compile(r"\+?\d(?:[ .\-]?\d)*[ .\-]?$")
_YEAR_RANGE = re.compile(r"(?:19|20)\d\d[ .\-](?:19|20)\d\d")
_EM_DASH = chr(0x2014)  # em dash, built from its code point so it never appears literally


def _space_phone_digits(text: str) -> str:
    def repl(m: re.Match) -> str:
        run = m.group()
        if _YEAR_RANGE.fullmatch(run):  # "2019-2021" is not a phone number
            return run
        prefix = "+" if run.startswith("+") else ""
        return prefix + " ".join(digits_only(run))

    return _PHONE_RUN.sub(repl, text)


def _ago(t: float) -> str:
    s = max(0, int(time.monotonic() - t))
    return f"{s} s" if s < 90 else f"{s // 60} min"


class Screen:
    """What the caller's page shows right now, mirrored from the browser over the
    data channel. Rendered into a short private note on every LLM call so Ikli
    always knows whether the booking form is open, what's typed in it, whether the
    link went out, and so on."""

    TYPING_WINDOW_S = 2.5   # a keystroke this recent counts as "typing right now"

    def __init__(self) -> None:
        # never_opened | open | closed_by_caller | closed_by_you | submitted
        self.form = "never_opened"
        self.form_since = 0.0
        self.form_opens = 0
        self.field = ""
        self.focused = False
        self.last_typed = 0.0
        self.submitted = ""
        self.link: dict | None = None     # {"channel", "to", "ok", "at"}
        self.hidden_since = 0.0           # tab in the background since (0 = visible)
        self.device = ""
        self.whatsapp_down = False        # the WhatsApp line can't send right now
        self.shown = asyncio.Event()      # set when the page confirms the form is up

    def _form_line(self) -> str:
        if self.form == "never_opened":
            return "Booking form: not opened yet."
        if self.form == "open":
            parts = [f"Booking form: OPEN on their screen ({_ago(self.form_since)})"]
            if self.form_opens > 1:
                parts.append(f"opened {self.form_opens} times this call")
            if self.last_typed and time.monotonic() - self.last_typed < self.TYPING_WINDOW_S:
                parts.append("they're typing right now")
            elif self.focused:
                parts.append("cursor in the field")
            parts.append(f'field contains "{self.field}"' if self.field else "field is empty")
            return ", ".join(parts) + "."
        if self.form == "submitted":
            return f'Booking form: they submitted "{self.submitted}" ({_ago(self.form_since)} ago); it is closed now.'
        who = "they closed it" if self.form == "closed_by_caller" else "you closed it"
        draft = f', draft kept: "{self.field}"' if self.field else ", nothing typed"
        return f"Booking form: closed, {who} ({_ago(self.form_since)} ago){draft}."

    def note(self) -> str:
        lines = ["LIVE SCREEN (private, what the caller's page shows this moment; never read it out):"]
        if self.device:
            lines.append(f"Device: {self.device}.")
        lines.append(self._form_line())
        if self.link:
            l = self.link
            how = "sent" if l["ok"] else "FAILED to send"
            lines.append(f"Booking link: {how} via {l['channel']} to {l['to']} ({_ago(l['at'])} ago).")
        else:
            lines.append("Booking link: not sent.")
        if self.whatsapp_down:
            lines.append("WhatsApp delivery is down right now: offer the booking link by email only.")
        if self.hidden_since:
            lines.append(f"They switched to another tab or app {_ago(self.hidden_since)} ago (can still hear you).")
        return "\n".join(lines)


class IkliAgent(Agent):
    """Agent with output sanitizers so the model's habits never leak through:

    - em dashes are never spoken or shown (replaced with a comma),
    - phone numbers are spoken one digit at a time for a clear read-back, while
      the on-screen transcript keeps them as a compact number.

    Every LLM call also gets the live screen note appended as the last message
    (after the cached history, so prompt caching is unaffected).
    """

    def __init__(self, *, screen: Screen, **kwargs):
        super().__init__(**kwargs)
        self._screen = screen

    def llm_node(self, chat_ctx, tools, model_settings):
        ctx = chat_ctx.copy()
        ctx.add_message(role="system", content=self._screen.note())
        return Agent.default.llm_node(self, ctx, tools, model_settings)

    async def tts_node(self, text, model_settings):
        async def _clean():
            pending = ""
            async for chunk in text:
                buf = pending + chunk.replace(_EM_DASH, ", ")
                tail = _NUMBER_TAIL.search(buf)
                pending = buf[tail.start():] if tail else ""
                head = buf[: tail.start()] if tail else buf
                if head:
                    yield _space_phone_digits(head)
            if pending:
                yield _space_phone_digits(pending)

        async for frame in Agent.default.tts_node(self, _clean(), model_settings):
            yield frame

    async def transcription_node(self, text, model_settings):
        async for chunk in text:
            if isinstance(chunk, str) and _EM_DASH in chunk:
                yield chunk.replace(_EM_DASH, ", ")
            else:
                yield chunk


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
        language="en",            # English only, no language detection latency
        interim_results=True,
        smart_format=True,
        punctuate=True,
        no_delay=True,            # emit finals without extra hold
        endpointing_ms=60,        # was 25 (too aggressive, clipped trailing words)
        filler_words=False,
        api_key=os.environ.get("DEEPGRAM_API_KEY"),
    )
    # Keyterm prompting is a nova-3 (English) feature; only send it on nova-3.
    if dg_model.startswith("nova-3"):
        stt_kwargs["keyterm"] = KEYTERMS
    return AgentSession(
        stt=deepgram.STT(**stt_kwargs),
        llm=ClaudeLLM(
            model=os.environ.get("LLM_MODEL", "claude-sonnet-5-5"),
            caching="ephemeral",           # ~7k-token system prompt served from cache each turn
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
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
                speed=0.82,
            ),
            api_key=os.environ.get("ELEVEN_API_KEY") or os.environ.get("ELEVENLABS_API_KEY"),
        ),
        vad=vad,
        turn_handling={
            # Deepgram endpointing (fast) instead of the heavy EOU model.
            "turn_detection": "stt",
            # Respond as soon as the user stops; cap the wait for a slow trailing pause.
            "endpointing": {"min_delay": 0.10, "max_delay": 2.0},
            # Two words to cut Ikli off: a lone "yeah" / "mm-hmm", or a single word of
            # its own voice leaking back through the caller's speakers, no longer
            # derails the reply mid-sentence.
            "interruption": {"min_duration": 0.3, "min_words": 2},
            # Begin the reply before the user fully stops. (Preemptive TTS was
            # measured too: no gain, only wasted TTS characters.)
            "preemptive_generation": {"enabled": True},
        },
        aec_warmup_duration=0.5,       # trims ~2.5 s off startup vs the 3 s default
    )


# The page opens the room as soon as a visitor interacts with it (mic still off)
# so a sleeping cloud agent boots while they are still looking at the page; the
# call itself starts when they press the mic. If they never do, give the slot back
# (the free LiveKit plan allows only 5 sessions at once).
PRECONNECT_WAIT_S = float(os.environ.get("PRECONNECT_WAIT_S", "60"))


def _has_mic(p: rtc.RemoteParticipant) -> bool:
    return any(
        pub.source == rtc.TrackSource.SOURCE_MICROPHONE for pub in p.track_publications.values()
    )


async def _wait_for_mic(room: rtc.Room, p: rtc.RemoteParticipant, timeout: float) -> bool:
    """True once the caller publishes their mic; False on timeout or if they leave."""
    if _has_mic(p):
        return True
    done = asyncio.get_running_loop().create_future()

    def _published(pub: rtc.RemoteTrackPublication, who: rtc.RemoteParticipant) -> None:
        if who.identity == p.identity and pub.source == rtc.TrackSource.SOURCE_MICROPHONE:
            if not done.done():
                done.set_result(True)

    def _left(who: rtc.RemoteParticipant) -> None:
        if who.identity == p.identity and not done.done():
            done.set_result(False)

    room.on("track_published", _published)
    room.on("participant_disconnected", _left)
    try:
        if _has_mic(p):  # published between the first check and subscribing
            return True
        return await asyncio.wait_for(done, timeout)
    except asyncio.TimeoutError:
        return False
    finally:
        room.off("track_published", _published)
        room.off("participant_disconnected", _left)


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

    # Shared per-call state + a reliable data-message channel to the browser.
    # Data messages (not participant attributes) are used because the Cloud
    # dispatched agent token may lack attribute-update permission, which made the
    # form never open. publish_data always works agent -> client.
    state = {"phone": None, "email": None}

    async def _publish(obj: dict) -> None:
        try:
            await ctx.room.local_participant.publish_data(
                json.dumps(obj), reliable=True, topic="ikli"
            )
        except Exception:
            pass

    screen = Screen()

    async def _check_whatsapp() -> None:
        screen.whatsapp_down = not await asyncio.to_thread(whatsapp_ready)
        if screen.whatsapp_down:
            logger.warning("WhatsApp (GREEN-API) can't send; booking links go by email only")

    asyncio.ensure_future(_check_whatsapp())

    @function_tool
    async def open_contact_form() -> str:
        """Show the on-screen booking form so the caller can type EITHER their phone
        number (with country code) OR their email, whichever they prefer, while the call
        stays live. Call this when they want to book (also to reopen it if they ask
        again; their draft is kept). The result says whether the form actually appeared."""
        if screen.form == "open":
            await _publish({"type": "open_form"})   # the page nudges the open form
            return "already_open: the form is already on their screen"
        screen.shown.clear()
        await _publish({"type": "open_form"})
        try:
            await asyncio.wait_for(screen.shown.wait(), 2.5)
        except asyncio.TimeoutError:
            return "no_confirmation: the page didn't confirm the form appeared; ask if they can see it"
        if screen.field:
            return f'form_shown: their earlier entry "{screen.field}" is already filled in'
        return "form_shown"

    @function_tool
    async def close_contact_form() -> str:
        """Hide the booking form. Only when the caller changes their mind, or would rather
        say their number or email out loud."""
        if screen.form != "open":
            return "not_open"
        await _publish({"type": "close_form"})
        screen.form = "closed_by_you"
        screen.form_since = time.monotonic()
        return "closed"

    @function_tool
    async def send_booking_link(contact: str) -> str:
        """After the caller gives AND confirms their contact, send the Calendly booking
        link. Pass whatever they gave: a phone number (delivered via WhatsApp) or an email
        (delivered via email). The right channel is auto-detected. Returns 'sent' on
        success. Only call after they confirm."""
        raw = (contact or state.get("phone") or state.get("email") or "").strip()
        url = await asyncio.to_thread(_calendly_booking_url)
        if not url:
            return "no_link_configured"
        how_to = (
            "Open it, pick a day and time that suits you, and you'll get a calendar invite "
            "with the Zoom link. It's a free 30-minute intro call with the Iklipse team."
        )
        if "@" in raw:
            addr = raw
            if "." not in addr.split("@")[-1]:
                return "invalid_email"
            state["email"] = addr
            await _publish({"type": "save", "email": addr})
            ok, _ = await asyncio.to_thread(
                send_email,
                addr,
                "Your Iklipse booking link",
                f"Hi!\n\nHere's your Iklipse booking link:\n{url}\n\n{how_to}\n\nSee you soon.",
            )
            screen.link = {"channel": "email", "to": addr, "ok": ok, "at": time.monotonic()}
        else:
            if screen.whatsapp_down:
                return "whatsapp_unavailable: WhatsApp can't send right now, ask for their email instead"
            digits = digits_only(raw)
            if len(digits) < 8:
                return "invalid_number"
            state["phone"] = digits
            await _publish({"type": "save", "phone": digits})
            ok, _ = await asyncio.to_thread(
                send_whatsapp,
                digits,
                f"Hey! Here's your Iklipse booking link: {url}\n\n{how_to}",
            )
            screen.link = {"channel": "WhatsApp", "to": digits, "ok": ok, "at": time.monotonic()}
        return "sent" if ok else "send_failed"

    session = build_session(ctx)

    # One log line per turn with the latency breakdown (end-of-turn wait, Claude
    # first token, ElevenLabs first audio, and the total the caller feels).
    @session.on("conversation_item_added")
    def _log_turn_latency(ev) -> None:
        m = getattr(ev.item, "metrics", None) or {}
        keys = ("transcription_delay", "end_of_turn_delay", "llm_node_ttft", "tts_node_ttfb", "e2e_latency")
        timing = {k: round(m[k], 3) for k in keys if isinstance(m.get(k), (int, float))}
        if timing:
            logger.info("turn latency", extra={"role": ev.item.role, **timing})
    await session.start(
        room=ctx.room,
        agent=IkliAgent(
            screen=screen,
            instructions=instructions_for(known_name),
            tools=[
                open_contact_form,
                close_contact_form,
                send_booking_link,
            ],
        ),
    )

    # Bridge the browser back to the agent over the data channel (topic 'ikli').
    # The page mirrors what the caller sees (form open/closed, what's typed, tab
    # hidden, device) into `screen`, which every LLM call reads; only real moments
    # (a submit, a dismissed form, going quiet) trigger a reply.
    def _on_submitted(value: str) -> None:
        if "@" in value:  # they entered an email
            state["email"] = value
            session.generate_reply(
                instructions=(
                    f"The caller just submitted their email through the form: {value}. "
                    "Read it back clearly to confirm you've got it right, then ask them to "
                    "confirm. Do not send anything until they confirm."
                )
            )
        else:  # they entered a phone number
            state["phone"] = value
            session.generate_reply(
                instructions=(
                    f"The caller just submitted their phone number through the form: {value}. "
                    "In your reply, write the number in plain digits exactly as given, then ask "
                    "them to confirm it's right. Do not send anything until they confirm."
                )
            )

    prompts = {
        "form_closed": (
            "The caller closed the booking form without entering anything. Don't push. "
            "In one short, relaxed line, let them know that's fine and they can also just "
            "say their number or email out loud if that's easier, or carry on chatting. "
            "Don't repeat anything you just said."
        ),
        "form_closed_draft": (
            "The caller closed the booking form without sending what they'd typed. Don't "
            "push. One short, relaxed line: no problem, they can send it whenever, say it "
            "out loud instead, or just carry on chatting. Don't repeat anything you just said."
        ),
        "idle": (
            "The caller has gone quiet with the form open. Check in briefly and warmly: "
            "ask if they're still there and whether they've had a chance to enter it. "
            "One short sentence."
        ),
        "idle_end": (
            "The caller has been inactive for a while and hasn't responded. Politely say "
            "you'll let them go for now since it seems they've stepped away, and to reach "
            "out any time. Warm, brief, one or two short sentences. This ends the call."
        ),
    }

    # A page event that deserves a spoken reaction waits until Ikli isn't mid-sentence,
    # and is dropped if the caller spoke in the meantime or it no longer applies
    # (otherwise a queued reply lands late and repeats what was just said).
    heard = {"at": 0.0}

    async def _reply_when_free(instructions: str, still_relevant) -> None:
        asked = time.monotonic()
        for _ in range(100):                       # up to ~20 s
            if session.agent_state not in ("speaking", "thinking") and session.user_state != "speaking":
                break
            await asyncio.sleep(0.2)
        else:
            return
        if heard["at"] > asked or not still_relevant():
            return
        session.generate_reply(instructions=instructions)

    def _text(msg: dict, key: str = "value") -> str:
        return str(msg.get(key) or "").strip()[:120]

    def _on_data(packet: rtc.DataPacket) -> None:
        try:
            if packet.topic and packet.topic != "ikli":
                return
            msg = json.loads(bytes(packet.data).decode("utf-8"))
            t = msg.get("type")
            now = time.monotonic()
            if t == "client":
                screen.device = {"phone": "phone", "tablet": "tablet"}.get(msg.get("device"), "computer")
            elif t == "form_shown":
                if screen.form != "open":
                    screen.form_opens += 1
                    screen.form_since = now
                screen.form = "open"
                screen.field = _text(msg)
                screen.focused = False
                screen.shown.set()
            elif t == "form_input":
                screen.field = _text(msg)
                screen.last_typed = now
            elif t == "form_focus":
                screen.focused = bool(msg.get("focused"))
            elif t == "visibility":
                screen.hidden_since = now if msg.get("hidden") else 0.0
            elif t == "submit":
                value = _text(msg)
                if value:
                    screen.form = "submitted"
                    screen.form_since = now
                    screen.submitted = screen.field = value
                    screen.focused = False
                    _on_submitted(value)
            elif t == "form_closed":
                screen.form = "closed_by_caller"
                screen.form_since = now
                screen.field = _text(msg)
                screen.focused = False
                asyncio.ensure_future(_reply_when_free(
                    prompts["form_closed_draft" if screen.field else "form_closed"],
                    lambda: screen.form == "closed_by_caller",
                ))
            elif t in ("idle", "idle_end"):
                session.generate_reply(instructions=prompts[t])
        except Exception:
            return

    ctx.room.on("data_received", _on_data)

    @session.on("user_input_transcribed")
    def _capture_contact(ev) -> None:
        if not ev.is_final:
            return
        heard["at"] = time.monotonic()
        phone, email = _contacts_in(ev.transcript or "")
        if phone and phone != state["phone"]:
            state["phone"] = phone
            asyncio.ensure_future(_publish({"type": "save", "phone": phone}))
        if email and email != state["email"]:
            state["email"] = email
            asyncio.ensure_future(_publish({"type": "save", "email": email}))

    # The session above is already live (audio track published, models connected),
    # so once the caller presses the mic the greeting plays with no setup delay.
    if not await _wait_for_mic(ctx.room, participant, PRECONNECT_WAIT_S):
        logger.info("caller never started the call; releasing the session")
        ctx.shutdown(reason="caller never pressed the mic")
        return

    # Speak first, instantly.
    greeting_pcm = ctx.proc.userdata.get("greeting_pcm")
    if known_name:
        # Personalized greeting must be synthesized live (name varies).
        await session.say(f"Hey {known_name}! What can I do for you?", allow_interruptions=True)
    elif greeting_pcm:
        # Play the pre-rendered greeting audio, no synthesis latency.
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
