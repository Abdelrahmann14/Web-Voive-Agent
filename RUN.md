# Iklipse Web Voice Agent — Run Guide

Real-time voice agent wired to the Three.js orb UI.

```
Browser (index.html + livekit-client)
  1. POST /token ─────────────► FastAPI token server   (backend/server.py, :8000)
  2. WebRTC audio ────────────► LiveKit server          (:7880)
  3. transcripts + agent voice◄ LiveKit Agent worker    (backend/agent.py)
                                   STT  Deepgram nova-3
                                   LLM  Google Gemini
                                   TTS  ElevenLabs Flash v2.5  (voice aMSt68OGf4xUZAnLpTU8)
```

LiveKit runs in the cloud, so you start **3 local processes**: token server, agent worker, frontend.

---

## 0. One-time install (already done in this setup)

```bash
# frontend deps
npm install
```
```bash
# backend deps (Python 3.12 venv — NOT 3.14, plugins have no 3.14 wheels)
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Secrets live in `backend/.env` (already filled, gitignored).

---

## 1. LiveKit server — LiveKit Cloud (already configured)

Uses a hosted LiveKit Cloud project — nothing to install or run locally.
`backend/.env` already has the project `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`.
To use a different project: create one at https://cloud.livekit.io and paste its 3 values into `backend/.env`.

---

## 2. Token server  (new terminal)
```bash
cd backend
.venv\Scripts\python.exe server.py
```
→ http://localhost:8000/health should return `{"ok":true,...}`

## 3. Agent worker  (new terminal)
```bash
cd backend
.venv\Scripts\python.exe agent.py dev
```
Wait for `registered worker`. It auto-joins every room a browser creates.

## 4. Frontend  (new terminal)
```bash
npm run dev
```
Open http://localhost:3000 → click the mic → **allow microphone** → talk.

---

## Notes
- **Rotate the API keys** — they were shared in chat (Deepgram, ElevenLabs, Gemini).
- **Gemini model**: `backend/.env` sets `GEMINI_MODEL=gemini-3-flash-preview` (verified working
  on your key). Alternatives on the same key: `gemini-3-pro-preview`, `gemini-3.5-flash`.
- **Mic needs a secure context**: `localhost` is fine. On a LAN IP use HTTPS.
- Frontend talks to the token server through the Vite proxy (`/token` → `:8000`),
  so no CORS setup needed in dev.
```
