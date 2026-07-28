---
name: push-to-github-only
description: "For this project, only push code to GitHub — never deploy to or test on Vercel; the user handles that"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: d05554d6-609e-48e9-b1fd-81e45adceb52
  modified: 2026-07-28T21:11:48.325Z
---

For the Web-Voice-Agent project, when work is done just commit and push to GitHub
(main). Do NOT attempt to deploy to Vercel, trigger a Vercel redeploy, or test the
deployed site — the user handles all Vercel deployment and testing themselves.

**Why:** the user has said this repeatedly and firmly ("no you must upload the updates
to githup only iwill handle the rest", "no always just update the repo"). Their Vercel
project also lives under a different account than the connected Vercel connector, so
attempts fail anyway.

**How to apply:** finish → `git push` to GitHub → stop. Don't offer or start a Vercel
deploy/test. Exception: the LiveKit Cloud AGENT is different — redeploying that via
`lk agent deploy` from backend/ is fine and is how agent/prompt changes go live.
See [[ikli-agent-human-not-scripted]].
