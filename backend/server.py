"""
FastAPI token server.

The browser calls POST /token to get a short-lived LiveKit access token +
the server URL, then connects to the LiveKit room over WebRTC. The agent
worker (agent.py) auto-joins that room and runs the STT -> LLM -> TTS pipeline.
"""

import json
import os
import re
import smtplib
import ssl
import uuid
from email.message import EmailMessage

import requests
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from livekit import api

load_dotenv()

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")

GREENAPI_HOST = os.environ.get("GREENAPI_HOST", "https://api.green-api.com").rstrip("/")
GREENAPI_ID = os.environ.get("GREENAPI_ID")
GREENAPI_TOKEN = os.environ.get("GREENAPI_TOKEN")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = (os.environ.get("SMTP_USER") or "").strip()
# Gmail app passwords are displayed with spaces but must be sent without them.
SMTP_PASS = (os.environ.get("SMTP_PASS") or "").replace(" ", "")
SMTP_FROM = os.environ.get("SMTP_FROM") or SMTP_USER

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(title="Voice Agent Token Server")

# Allow the Vite dev server (and anything, for local dev) to call us directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True, "livekit_url": LIVEKIT_URL}


@app.post("/token")
def create_token(
    room: str | None = None,
    identity: str | None = None,
    name: str | None = None,
):
    """Mint a join token for a room. `name` (a known caller name) is embedded as
    participant metadata so the agent can greet a returning visitor by name."""
    room = room or f"voice-{uuid.uuid4().hex[:8]}"
    identity = identity or f"user-{uuid.uuid4().hex[:6]}"

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
    )
    if name and name.strip():
        builder = builder.with_metadata(json.dumps({"name": name.strip()}))
    return {"url": LIVEKIT_URL, "token": builder.to_jwt(), "room": room, "identity": identity}


@app.post("/summarize")
def summarize(payload: dict):
    """Summarize a finished call. Body: {"transcript": "You: ...\\nIkli: ..."}."""
    transcript = (payload or {}).get("transcript", "").strip()
    if not transcript:
        return {"summary": "No conversation was recorded."}
    if not GOOGLE_API_KEY:
        return {"summary": "Summary unavailable (no Gemini key configured)."}

    try:
        from google import genai

        client = genai.Client(api_key=GOOGLE_API_KEY)
        prompt = (
            "Summarize this voice call between a user (You) and the assistant (Ikli) "
            "in 2 concise sentences. Focus on what the user wanted and the outcome.\n\n"
            f"{transcript}"
        )
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return {"summary": (resp.text or "").strip() or "Summary could not be generated."}
    except Exception as e:  # keep the UI working even if the LLM call fails
        return {"summary": f"Summary unavailable: {e}"}


@app.post("/export/whatsapp")
def export_whatsapp(payload: dict):
    """Send the session report to a WhatsApp number via GREEN-API.

    Body: {"phone": "+20100...", "message": "..."}  (phone: any format, digits extracted)
    """
    payload = payload or {}
    phone = re.sub(r"\D", "", payload.get("phone", ""))
    message = (payload.get("message") or "Your Iklipse session report.").strip()

    if len(phone) < 8:
        return {"ok": False, "error": "Enter a valid phone number with country code."}
    if not (GREENAPI_ID and GREENAPI_TOKEN):
        return {"ok": False, "error": "GREEN-API is not configured on the server."}

    url = f"{GREENAPI_HOST}/waInstance{GREENAPI_ID}/sendMessage/{GREENAPI_TOKEN}"
    try:
        r = requests.post(
            url,
            json={"chatId": f"{phone}@c.us", "message": message},
            timeout=25,
        )
        data = r.json() if r.content else {}
        if r.status_code == 200 and data.get("idMessage"):
            return {"ok": True, "idMessage": data["idMessage"]}
        return {"ok": False, "error": f"GREEN-API error ({r.status_code}): {data}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/export/email")
def export_email(payload: dict):
    """Email the session report via SMTP using an app password (no OAuth).

    Body: {"email": "to@x.com", "message": "...", "subject": "..."}
    """
    payload = payload or {}
    to = (payload.get("email") or "").strip()
    body = (payload.get("message") or "Your Iklipse session report.").strip()
    subject = (payload.get("subject") or "Your Iklipse Voice Session Report").strip()

    if not EMAIL_RE.match(to):
        return {"ok": False, "error": "Enter a valid email address."}
    if not (SMTP_USER and SMTP_PASS):
        return {"ok": False, "error": "Email is not configured on the server (set SMTP_USER/SMTP_PASS)."}

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=25) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:  # 587 / STARTTLS
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=25) as s:
                s.starttls(context=ctx)
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("TOKEN_SERVER_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
