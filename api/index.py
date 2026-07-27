"""
Vercel serverless entry point.

Serves the FastAPI token / summarize / export API (from backend/server.py) as
same-origin endpoints for the Vercel-hosted frontend:  /token, /summarize,
/export/whatsapp, /export/email, /health.

NOTE: the LiveKit *agent worker* is NOT here. Vercel functions are short-lived
and cannot hold the agent's persistent WebSocket + real-time audio. Deploy the
agent separately (see DEPLOY.md — LiveKit Cloud Agents recommended).
"""
import os
import sys

# backend/** is bundled via vercel.json includeFiles; make it importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from server import app  # noqa: E402,F401  -> Vercel serves this ASGI `app`
