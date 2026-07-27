# Deploy — Iklipse Voice Agent

Three pieces, three homes. **Vercel cannot run the agent** (it's a long-lived
process, not a serverless function), so the agent goes on a persistent host.

```
Frontend + API  ->  Vercel            (static site + /token /summarize /export)
Agent worker    ->  LiveKit Cloud Agents   (recommended — co-located = lowest latency)
LiveKit         ->  LiveKit Cloud      (already set up)
```

For **lowest latency**, put the agent in a US region (next to Deepgram /
ElevenLabs / Gemini) and set your LiveKit Cloud project region to match your
users. LiveKit Cloud Agents runs the agent inside LiveKit's own datacenter —
the best option.

---

## 1. Frontend + API → Vercel

Files already added: `vercel.json`, `api/index.py`, root `requirements.txt`.
The frontend calls same-origin `/token`, `/summarize`, `/export/*` — Vercel
routes those to the FastAPI app; the rest serves the built site.

Deploy:
```bash
npm i -g vercel
cd C:\Web-Voive-Agent
vercel            # first run: link/create project
vercel --prod
```

Set these **Environment Variables** in the Vercel dashboard (Project → Settings
→ Environment Variables) — needed by the token/summarize/export endpoints:

| Var | Purpose |
|-----|---------|
| `LIVEKIT_URL` `LIVEKIT_API_KEY` `LIVEKIT_API_SECRET` | mint join tokens |
| `GOOGLE_API_KEY` `GEMINI_MODEL` | call summary |
| `GREENAPI_HOST` `GREENAPI_ID` `GREENAPI_TOKEN` | WhatsApp export |
| `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASS` `SMTP_FROM` | email export |

(HTTPS is automatic on Vercel, so the microphone works — no cert steps.)

---

## 2. Agent worker → LiveKit Cloud Agents (recommended)

Files added: `backend/Dockerfile`, `backend/.dockerignore`.

```bash
npm i -g @livekit/cli          # or: brew install livekit-cli
lk cloud auth                  # log in to your LiveKit Cloud account
cd C:\Web-Voive-Agent\backend
lk agent create                # builds the Dockerfile, deploys the worker
```

Set the agent's **secrets** in the LiveKit Cloud dashboard (or `lk agent` env):

| Var | Purpose |
|-----|---------|
| `LIVEKIT_URL` `LIVEKIT_API_KEY` `LIVEKIT_API_SECRET` | connect to LiveKit |
| `DEEPGRAM_API_KEY` `DEEPGRAM_MODEL` | STT (nova-3) |
| `ELEVEN_API_KEY` `ELEVEN_VOICE_ID` `ELEVEN_MODEL` | TTS (Flash v2.5) |
| `GOOGLE_API_KEY` `GEMINI_MODEL` | LLM |

Redeploy after changes: `lk agent deploy`.

### Alternative agent hosts (same Dockerfile)
Render / Railway / Fly.io / a VM — deploy `backend/Dockerfile` as a **Background
Worker / long-running service** (NOT a web service; it exposes no HTTP port).
Pick a **US region** and **≥4 vCPU** (so Silero VAD runs realtime).

---

## 3. LiveKit — already configured
Keep your Cloud project. To lower latency for US users, confirm the project
region is US. Nothing to redeploy.

---

## Local development (unchanged)
```bash
# terminal 1 — token/API server
cd backend && .venv\Scripts\python.exe server.py
# terminal 2 — agent
cd backend && .venv\Scripts\python.exe agent.py start
# terminal 3 — frontend (https://localhost:3000)
npm run dev
```

## Latency notes
- First-time greeting is **pre-rendered at startup** and played instantly (no
  live TTS on the call path). Personalized (returning-name) greetings synth live.
- `agent.py start` runs jobs in-thread (no per-call process spawn).
- Hosting the agent on fast CPU near the providers is the single biggest win —
  a local PC caps you around 3–4 s; a good US host gets ~1–1.5 s first-speak.
