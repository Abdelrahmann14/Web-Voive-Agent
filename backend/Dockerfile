# Agent worker image — deploy to LiveKit Cloud Agents (recommended, lowest
# latency) or any persistent Docker host (Render / Railway / Fly / a VM).
# Vercel CANNOT run this (it's a long-lived process, not a serverless function).
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .

# Pre-download model files (Silero VAD, etc.) into the image so cold start is fast.
RUN python agent.py download-files

# `start` = production thread executor (no per-call process spawn).
CMD ["python", "agent.py", "start"]
