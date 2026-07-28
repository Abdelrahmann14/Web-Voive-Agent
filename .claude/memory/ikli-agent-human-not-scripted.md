---
name: ikli-agent-human-not-scripted
description: "Ikli voice agent prompt must be principle-based (conversational freedom + boundaries), never a rigid rulebook"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: d05554d6-609e-48e9-b1fd-81e45adceb52
  modified: 2026-07-28T21:07:50.300Z
---

For the Iklipse voice agent "Ikli" (backend/knowledge.py system prompt), the user
strongly wants it to feel like a real human consultant, NOT scripted. A rule-style
prompt (understand→recommend→ask follow-up→guide to call) made the model execute
if/then: it re-introduced itself every turn, listed all services when asked one thing,
and bolted a canned two-option question onto every reply.

**Why:** the whole point of the agent is that a visitor forgets they're talking to an AI.
Mechanical, predictable, template-y replies kill that.

**How to apply:** write prompts as principled freedom inside hard boundaries — let the
model reason per moment (what/how much to say, whether to ask at all), match the user's
style, vary wording, not end every reply with a question, not reintroduce itself. Keep
boundaries firm: only true Iklipse facts, never invent, stay professional/on-brand, offer
a next step only when the user leans that way. LLM stays Google Gemini. Prompt changes
need a redeploy (`lk agent deploy` from backend/) to take effect on LiveKit Cloud.
