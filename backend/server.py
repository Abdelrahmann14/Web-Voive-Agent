"""
FastAPI token server.

The browser calls POST /token to get a short-lived LiveKit access token +
the server URL, then connects to the LiveKit room over WebRTC. The agent
worker (agent.py) auto-joins that room and runs the STT -> LLM -> TTS pipeline.
"""

import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from datetime import timedelta

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from livekit import api

from messaging import send_email, send_whatsapp

load_dotenv()

log = logging.getLogger("token-server")

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5-5")

# The export endpoints send through the company's WhatsApp/SMTP accounts, so cap
# what one client can push through them (they are public, unauthenticated URLs).
MAX_REPORT_CHARS = 8000
MAX_TRANSCRIPT_CHARS = 30000
EXPORTS_PER_WINDOW = 5
EXPORT_WINDOW_S = 600
_export_log: dict[str, deque] = defaultdict(deque)

app = FastAPI(title="Voice Agent Token Server")

# Allow the Vite dev server (and anything, for local dev) to call us directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _export_allowed(request: Request) -> bool:
    """Sliding-window rate limit per client IP (best effort, per server instance)."""
    now = time.monotonic()
    hits = _export_log[_client_ip(request)]
    while hits and now - hits[0] > EXPORT_WINDOW_S:
        hits.popleft()
    if len(hits) >= EXPORTS_PER_WINDOW:
        return False
    hits.append(now)
    return True


def _report_text(payload: dict) -> str:
    text = (payload.get("message") or "Your Iklipse session report.").strip()
    if len(text) > MAX_REPORT_CHARS:
        text = text[:MAX_REPORT_CHARS].rstrip() + "\n\n(truncated)"
    return text


@app.get("/health")
def health():
    return {"ok": True, "livekit_url": LIVEKIT_URL}


@app.post("/token")
def create_token(name: str | None = None):
    """Mint a join token for a brand-new room. The room and identity are always
    server-generated so a client can never join someone else's live session.
    `name` (a known caller name) is embedded as participant metadata so the agent
    can greet a returning visitor by name."""
    room = f"voice-{uuid.uuid4().hex[:12]}"
    identity = f"user-{uuid.uuid4().hex[:8]}"

    grant = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    builder = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grant)
        .with_ttl(timedelta(minutes=15))
    )
    if name and name.strip():
        builder = builder.with_metadata(json.dumps({"name": name.strip()[:60]}))
    return {"url": LIVEKIT_URL, "token": builder.to_jwt(), "room": room, "identity": identity}


@app.post("/summarize")
def summarize(payload: dict):
    """Summarize a finished call. Body: {"transcript": "You: ...\nIkli: ..."}."""
    transcript = ((payload or {}).get("transcript") or "").strip()[-MAX_TRANSCRIPT_CHARS:]
    if not transcript:
        return {"summary": "No conversation was recorded."}
    if not ANTHROPIC_API_KEY:
        return {"summary": "Summary unavailable (no Claude key configured)."}

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=30.0)
        resp = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            output_config={"effort": "low"},
            # If a safety classifier declines, the API re-runs on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=(
                "Summarize this voice call between a user (You) and the assistant (Ikli) "
                "in 2 concise sentences. Focus on what the user wanted and the outcome. "
                "Plain text only, no markdown."
            ),
            messages=[{"role": "user", "content": transcript}],
        )
        if resp.stop_reason == "refusal":
            return {"summary": "Summary could not be generated."}
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return {"summary": text or "Summary could not be generated."}
    except Exception:  # keep the UI working even if the LLM call fails
        log.exception("summary failed")
        return {"summary": "Summary unavailable right now."}


@app.post("/export/whatsapp")
def export_whatsapp(payload: dict, request: Request):
    """Send the session report to a WhatsApp number via GREEN-API.

    Body: {"phone": "+20100...", "message": "..."}  (phone: any format, digits extracted)
    """
    payload = payload or {}
    if not _export_allowed(request):
        return {"ok": False, "error": "Too many sends. Please try again in a few minutes."}
    ok, error = send_whatsapp(payload.get("phone", ""), _report_text(payload))
    return {"ok": True} if ok else {"ok": False, "error": error}


@app.post("/export/email")
def export_email(payload: dict, request: Request):
    """Email the session report via SMTP using an app password (no OAuth).

    Body: {"email": "to@x.com", "message": "..."}
    """
    payload = payload or {}
    if not _export_allowed(request):
        return {"ok": False, "error": "Too many sends. Please try again in a few minutes."}
    ok, error = send_email(
        payload.get("email", ""), "Your Iklipse Voice Session Report", _report_text(payload)
    )
    return {"ok": True} if ok else {"ok": False, "error": error}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("TOKEN_SERVER_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
