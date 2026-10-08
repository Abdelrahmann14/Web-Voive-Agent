# Iklipse Web Voice Agent — Run Guide

Real-time voice agent wired to the Three.js orb UI.

```
Browser (index.html + livekit-client)
  1. POST /token ─────────────► FastAPI token server   (backend/server.py, :8000)
  2. WebRTC audio ────────────► LiveKit Cloud
  3. transcripts + agent voice◄ LiveKit Agent worker    (backend/agent.py)
                                   STT  Deepgram nova-3
                                   LLM  Claude Sonnet 5.5 (effort low)
                                   TTS  ElevenLabs Flash v2.5  (voice aMSt68OGf4xUZAnLpTU8)
```

LiveKit runs in the cloud, so you start **3 local processes**: token server, agent worker, frontend.

---

## 0. One-time install

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

Secrets live in `backend/.env` (gitignored). Copy `backend/.env.example` and fill it in.

---

## 1. LiveKit server — LiveKit Cloud

Uses a hosted LiveKit Cloud project — nothing to install or run locally.
`backend/.env` holds the project `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`.
To use a different project: create one at https://cloud.livekit.io and paste its 3 values into `backend/.env`.

> A local agent worker registers with the same Cloud project as the deployed
> agent, so while it runs LiveKit may route some calls (including live-site
> calls) to your machine. Stop it when you're done testing.

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
.venv\Scripts\python.exe agent.py start
```
Wait for `registered worker`. It auto-joins every room a browser creates.
(`agent.py dev` also works and hot-reloads, but is slower per call.)

## 4. Frontend  (new terminal)
```bash
npm run dev
```
Open **https://localhost:3000** (accept the self-signed certificate once) → click the mic →
**allow microphone** → talk. HTTPS lets phones on your Wi-Fi use the mic too.

If your browser refuses the certificate, run plain HTTP instead (fine on localhost):
```bash
npx vite --port=3000 --mode http
```
and open http://localhost:3000.

---

## Notes
- **LLM**: `backend/.env` sets `ANTHROPIC_API_KEY`, `LLM_MODEL` (default `claude-sonnet-5-5`)
  and `LLM_EFFORT` (default `low`, the fastest first word). Don't rename these to
  `CLAUDE_*`: the Claude desktop tools export `CLAUDE_EFFORT` and it would win.
- Each agent turn logs a `turn latency` line (end-of-turn wait, Claude first token,
  voice first audio, total) so you can see where time goes.
- **Mic needs a secure context**: `localhost` is fine. On a LAN IP use HTTPS.
- Frontend talks to the token server through the Vite proxy (`/token` → `:8000`),
  so no CORS setup needed in dev.
- If no agent joins within 20 s the UI returns to the start screen with an
  "assistant not available" message — check that step 3 is running.
