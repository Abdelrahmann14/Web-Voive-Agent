"""
Outbound delivery shared by the agent (booking links) and the API server
(session-report exports): WhatsApp via GREEN-API and email via SMTP.

Both helpers are blocking; call them through asyncio.to_thread from async code.
Each returns (ok, error) so callers can surface a useful reason on failure.
"""

import os
import re
import smtplib
import ssl
from email.message import EmailMessage

import requests

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def digits_only(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def send_whatsapp(phone: str, text: str) -> tuple[bool, str]:
    """Send a WhatsApp message via GREEN-API. `phone` may be in any format."""
    host = os.environ.get("GREENAPI_HOST", "https://api.green-api.com").rstrip("/")
    instance = os.environ.get("GREENAPI_ID")
    token = os.environ.get("GREENAPI_TOKEN")
    digits = digits_only(phone)
    if len(digits) < 8:
        return False, "Enter a valid phone number with country code."
    if not (instance and token):
        return False, "WhatsApp is not configured on the server."
    try:
        r = requests.post(
            f"{host}/waInstance{instance}/sendMessage/{token}",
            json={"chatId": f"{digits}@c.us", "message": text},
            timeout=25,
        )
        data = r.json() if r.content else {}
        if r.status_code == 200 and data.get("idMessage"):
            return True, ""
        return False, f"WhatsApp send failed ({r.status_code})."
    except Exception as e:
        return False, f"WhatsApp send failed: {e}"


def send_email(to: str, subject: str, body: str) -> tuple[bool, str]:
    """Send a plain-text email via SMTP with an app password (465 SSL or 587 STARTTLS)."""
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    try:
        port = int(os.environ.get("SMTP_PORT", "465"))
    except ValueError:
        port = 465
    user = (os.environ.get("SMTP_USER") or "").strip()
    # Gmail app passwords are displayed with spaces but must be sent without them.
    pwd = (os.environ.get("SMTP_PASS") or "").replace(" ", "")
    sender = os.environ.get("SMTP_FROM") or user
    to = (to or "").strip()
    if not EMAIL_RE.match(to):
        return False, "Enter a valid email address."
    if not (user and pwd):
        return False, "Email is not configured on the server."
    try:
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=25) as s:
                s.login(user, pwd)
                s.send_message(msg)
        else:  # 587 / STARTTLS
            with smtplib.SMTP(host, port, timeout=25) as s:
                s.starttls(context=ctx)
                s.login(user, pwd)
                s.send_message(msg)
        return True, ""
    except Exception as e:
        return False, f"Email send failed: {e}"
