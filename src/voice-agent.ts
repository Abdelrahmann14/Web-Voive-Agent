/**
 * Frontend LiveKit integration for the Iklipse voice UI.
 *
 * Connects the browser to a LiveKit room, publishes the mic, plays the agent's
 * audio, and renders live transcripts into #bubbles-container. Also keeps the
 * full transcript so the End screen can show a real report. Exposes hooks the
 * index.html script calls: window.__voiceConnect / __voiceDisconnect / __getTranscript.
 *
 * Wake-up: on the free LiveKit plan the cloud agent sleeps between calls and takes
 * 10-20 s to boot. So as soon as a visitor interacts with the page we open the
 * room with the mic OFF ("prepared"); that dispatches the agent, which boots and
 * waits silently. Pressing the mic just publishes it and the agent greets at once.
 */

import {
  Room,
  RoomEvent,
  Track,
  type RemoteTrack,
  type RemoteTrackPublication,
  type RemoteParticipant,
  type Participant,
} from 'livekit-client';

const TOKEN_ENDPOINT = '/token'; // proxied by Vite to the FastAPI server
// If no agent joins within this window after the caller presses the mic, the agent
// worker is down. Generous because a sleeping cloud agent can take ~20 s to boot.
const AGENT_JOIN_TIMEOUT_MS = 30000;
// A prepared (mic-off) room is released after this; the agent gives up at 60 s.
const PREPARED_TTL_MS = 55000;
// Re-prepare at most this many times per page load (each one wakes an agent).
const MAX_PREPARES = 3;

let room: Room | null = null;
let live = false;              // false while the room is only "prepared" (mic off)
let preparing: Promise<void> | null = null;
let preparedTimer = 0;
let prepares = 0;
let localIdentity = '';
let sessionSeq = 0;            // bumps on every connect/disconnect to cancel stale work
let agentJoinTimer = 0;
const audioEls = new Set<HTMLMediaElement>();

// Bubble/transcript state, keyed by transcription segment id (dedupes interim vs final).
// `out` is the element we write text into; agent bubbles reveal it with a typewriter
// (see the tick loop) so words appear in step with the voice instead of all at once.
type Segment = {
  el: HTMLElement;
  out: HTMLElement;         // text sink (a <span> for agent, the <p> for user)
  cursor: HTMLElement | null;
  isUser: boolean;
  text: string;            // full text so far (used for the report + typewriter target)
  order: number;
  shown: number;           // chars currently revealed (agent typewriter)
  ended: boolean;          // stream for this segment finished
};
const segments = new Map<string, Segment>();
let order = 0;

// ---- Typewriter (agent bubbles) -------------------------------------------
// Reveal agent text at a natural pace, speeding up when a lot is buffered so the
// caption never trails the voice by more than ~1s. One shared rAF loop drives all
// active segments.
const typing = new Set<Segment>();
let rafId = 0;
let lastTs = 0;

function tick(ts: number) {
  if (!lastTs) lastTs = ts;
  const dt = Math.min(0.05, (ts - lastTs) / 1000); // clamp big gaps (tab switch)
  lastTs = ts;

  for (const seg of typing) {
    if (seg.shown >= seg.text.length) {
      if (seg.ended) {
        typing.delete(seg);
        if (seg.cursor) { seg.cursor.remove(); seg.cursor = null; }
      }
      continue;
    }
    const remaining = seg.text.length - seg.shown;
    // ~45 chars/s baseline; ramp up with backlog (cap 700) so it stays near the audio.
    const perSec = Math.min(700, Math.max(45, remaining * 3));
    const step = Math.max(1, Math.round(perSec * dt));
    seg.shown = Math.min(seg.text.length, seg.shown + step);
    seg.out.textContent = seg.text.slice(0, seg.shown);
  }

  const c = bubblesContainer();
  if (c) c.scrollTop = c.scrollHeight;

  if (typing.size) {
    rafId = requestAnimationFrame(tick);
  } else {
    rafId = 0;
    lastTs = 0;
  }
}

function ensureTyping() {
  if (!rafId) rafId = requestAnimationFrame(tick);
}

/** Mark an agent segment's stream as done so its caret clears once text catches up. */
function finishSegment(segId: string) {
  const seg = segments.get(segId);
  if (seg && !seg.isUser) {
    seg.ended = true;
    ensureTyping();
  }
}

// ---- UI helpers -----------------------------------------------------------

function setStatus(text: string) {
  const el = document.getElementById('speaking-status');
  if (el) el.textContent = text;
}

function bubblesContainer(): HTMLElement | null {
  return document.getElementById('bubbles-container');
}

function resetTranscript() {
  const c = bubblesContainer();
  if (c) c.innerHTML = '';
  segments.clear();
  order = 0;
  typing.clear();
  if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
  lastTs = 0;
}

function upsertSegment(segId: string, text: string, isUser: boolean) {
  const container = bubblesContainer();
  if (!container) return;
  const clean = text.trim();
  if (!clean) return;

  let seg = segments.get(segId);

  // Guard: if this is a brand-new segment whose text equals the most recent
  // bubble of the same speaker, treat it as a duplicate and reuse that bubble.
  if (!seg) {
    let lastSame: Segment | undefined;
    for (const s of segments.values()) {
      if (s.isUser === isUser && (!lastSame || s.order > lastSame.order)) lastSame = s;
    }
    if (lastSame && lastSame.text.trim() === clean) {
      segments.set(segId, lastSame);
      return;
    }
  }

  if (!seg) {
    const el = document.createElement('div');
    el.className = 'bubble ' + (isUser ? 'user' : 'agent');
    // Agent bubbles "emerge from the orb" (fly in from the left where the orb sits);
    // user bubbles rise from the right. Pure GPU transform, no latency cost.
    el.style.cssText = isUser
      ? 'align-self:flex-end;max-width:80%;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.1);border-radius:24px 24px 4px 24px;padding:20px 24px;transform-origin:right center;animation:fadeUp 0.5s cubic-bezier(0.2,0.8,0.2,1) forwards'
      : 'align-self:flex-start;max-width:85%;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.06);border-radius:24px 24px 24px 4px;padding:20px 24px;transform-origin:left center;animation:orbEmerge 0.6s cubic-bezier(0.2,0.8,0.2,1) forwards';
    const p = document.createElement('p');
    p.style.cssText = 'font-size:15px;line-height:1.6';

    let out: HTMLElement;
    let cursor: HTMLElement | null = null;
    if (isUser) {
      out = p; // user transcript shows live, no typewriter
    } else {
      // Agent: a text span the typewriter grows, plus a blinking caret.
      out = document.createElement('span');
      cursor = document.createElement('span');
      cursor.className = 'typing-cursor';
      cursor.setAttribute('aria-hidden', 'true');
      p.appendChild(out);
      p.appendChild(cursor);
    }
    el.appendChild(p);
    container.appendChild(el);
    seg = { el, out, cursor, isUser, text: '', order: order++, shown: 0, ended: false };
    segments.set(segId, seg);
  }

  seg.text = clean;
  if (isUser) {
    seg.out.textContent = clean;             // instant for the caller's own words
    container.scrollTop = container.scrollHeight;
  } else {
    typing.add(seg);                         // let the typewriter reveal it in step with audio
    ensureTyping();
  }
}

/** Ordered, de-duplicated transcript for the End-screen report. */
function getTranscript(): { role: 'You' | 'Ikli'; text: string }[] {
  const seen = new Set<Segment>();
  const list: Segment[] = [];
  for (const s of segments.values()) {
    if (seen.has(s)) continue; // duplicate ids point at the same segment
    seen.add(s);
    list.push(s);
  }
  list.sort((a, b) => a.order - b.order);
  return list
    .filter((s) => s.text.trim())
    .map((s) => ({ role: s.isUser ? ('You' as const) : ('Ikli' as const), text: s.text.trim() }));
}

// ---- Orb reactivity -------------------------------------------------------

function driveOrb(participants: Participant[]) {
  const agentSpeaking = participants.some((p) => p.identity !== localIdentity);
  const userSpeaking = participants.some((p) => p.identity === localIdentity);

  if (agentSpeaking) {
    setStatus('Speaking');
    window.setOrbSentiment?.(0.8);
    window.__setAgentSpeaking?.(true);   // don't let the idle watch talk over the agent
  } else if (userSpeaking) {
    setStatus('Listening');
    window.setOrbSentiment?.(0.5);
    window.__setAgentSpeaking?.(false);
    window.__collectActivity?.();         // caller is talking -> reset the idle clock
  } else {
    setStatus('Connected');
    window.setOrbSentiment?.(0.5);
    window.__setAgentSpeaking?.(false);
  }
}

// ---- Connect / disconnect -------------------------------------------------

function clearAgentJoinTimer() {
  if (agentJoinTimer) { clearTimeout(agentJoinTimer); agentJoinTimer = 0; }
}

function removeAudioEls() {
  for (const el of audioEls) el.remove();
  audioEls.clear();
}

function clearPreparedTimer() {
  if (preparedTimer) { clearTimeout(preparedTimer); preparedTimer = 0; }
}

function agentPresent(r: Room): boolean {
  return r.remoteParticipants.size > 0;
}

/** Open the room with the mic off so the agent is dispatched (and boots) early. */
async function prepare(): Promise<void> {
  if (room) return;
  if (preparing) return preparing;
  const mySession = ++sessionSeq;
  const cancelled = () => mySession !== sessionSeq;

  preparing = (async () => {
    // 1. Get a token + server URL. Every page load is a brand-new session: we never
    //    persist anything across reloads, so we never pass a remembered name (the
    //    agent always greets fresh and asks who it's talking to).
    const res = await fetch(TOKEN_ENDPOINT, { method: 'POST' });
    if (!res.ok) throw new Error(`token request failed: ${res.status}`);
    const { url, token, identity } = await res.json();
    if (cancelled()) return;

    // 2. Set up the room.
    const r = new Room({ adaptiveStream: true, dynacast: true });
    room = r;
    live = false;
    localIdentity = identity;

    r.on(RoomEvent.TrackSubscribed, (track: RemoteTrack, _pub: RemoteTrackPublication, _p: RemoteParticipant) => {
      if (track.kind === Track.Kind.Audio) {
        const audioEl = track.attach();
        audioEl.autoplay = true;
        audioEl.style.display = 'none';
        document.body.appendChild(audioEl);
        audioEls.add(audioEl);
      }
    });
    r.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
      for (const el of track.detach()) { el.remove(); audioEls.delete(el); }
    });

    r.on(RoomEvent.ParticipantConnected, () => clearAgentJoinTimer());
    // A prepared room whose agent gave up waiting is useless: drop it so the next
    // press starts fresh instead of waiting for an agent that already left.
    r.on(RoomEvent.ParticipantDisconnected, () => {
      if (room === r && !live && !agentPresent(r)) releasePrepared();
    });
    r.on(RoomEvent.ActiveSpeakersChanged, (speakers: Participant[]) => driveOrb(speakers));
    r.on(RoomEvent.Disconnected, () => {
      // disconnect() clears `room` first, so reaching here with room === r means the
      // network / server dropped us (not the End button).
      if (room !== r) return;
      const wasLive = live;
      room = null;
      live = false;
      localIdentity = '';
      sessionSeq++;
      clearAgentJoinTimer();
      clearPreparedTimer();
      removeAudioEls();
      if (wasLive) {
        // Wrap the call up so the caller isn't left on a live-looking screen.
        setStatus('Disconnected');
        window.__onVoiceDropped?.();
      } else {
        armPrepare();
      }
    });

    // The agent signals the browser over the reliable data channel (topic 'ikli').
    // Contact details are kept IN MEMORY only (window globals) for this page load,
    // so a refresh or reopen starts a completely fresh session with no memory.
    r.on(RoomEvent.DataReceived, (payload: Uint8Array, _p?: unknown, _k?: unknown, topic?: string) => {
      if (topic && topic !== 'ikli') return;
      let msg: any;
      try { msg = JSON.parse(new TextDecoder().decode(payload)); } catch { return; }
      if (!msg || typeof msg !== 'object') return;
      switch (msg.type) {
        case 'name':
          if (msg.name) window.__ikliName = msg.name;
          break;
        case 'save':
          if (msg.phone) window.__ikliPhone = msg.phone;
          if (msg.email) window.__ikliEmail = msg.email;
          break;
        case 'open_form':
          window.__openCollectForm?.();
          break;
        case 'close_form':
          window.__closeCollectFormUI?.();
          break;
      }
    });

    // 3. Transcript handler, key each bubble by segment id so interim results
    //    update in place instead of spawning duplicates. Registered before
    //    connecting so the agent's (pre-rendered, instant) greeting is never missed.
    try {
      r.registerTextStreamHandler('lk.transcription', async (reader: any, participantInfo: any) => {
        const identity: string | undefined = participantInfo?.identity;
        const isUser = identity === localIdentity;
        const attrs = reader?.info?.attributes ?? {};
        const segId: string = attrs['lk.segment_id'] || reader?.info?.id || String(Math.random());
        let text = '';
        for await (const chunk of reader) {
          text += chunk;
          upsertSegment(segId, text, isUser);
        }
        finishSegment(segId); // stream done, let the caret clear once text catches up
      });
    } catch (e) {
      console.warn('transcription handler not registered', e);
    }

    // 4. Connect with the mic still off. The agent is dispatched now and waits.
    try {
      await r.connect(url, token);
    } catch (e) {
      if (room === r) room = null;
      throw e;
    }
    if (cancelled()) {
      if (room === r) room = null;
      await r.disconnect();
      return;
    }
    clearPreparedTimer();
    preparedTimer = window.setTimeout(() => {
      preparedTimer = 0;
      if (room === r && !live) releasePrepared();
    }, PREPARED_TTL_MS);
  })();

  try {
    await preparing;
  } finally {
    preparing = null;
  }
}

/** Quietly drop a prepared room the caller never used. */
function releasePrepared() {
  if (!room || live) return;
  const r = room;
  room = null;
  localIdentity = '';
  sessionSeq++;
  clearPreparedTimer();
  removeAudioEls();
  r.disconnect().catch(() => {});
  armPrepare();               // wake it again on the visitor's next interaction
}

/** Prepare on the visitor's first real interaction (not on load, so bots and
 *  link-preview fetchers don't wake the agent). */
function armPrepare() {
  if (prepares >= MAX_PREPARES) return;
  const events = ['pointermove', 'pointerdown', 'touchstart', 'keydown', 'scroll', 'wheel'];
  const once = () => {
    for (const ev of events) window.removeEventListener(ev, once);
    if (room || preparing || prepares >= MAX_PREPARES) return;
    prepares++;
    prepare().catch((e) => console.warn('voice prepare failed', e));
  };
  for (const ev of events) window.addEventListener(ev, once, { passive: true });
}

/** Caller pressed the mic: use the prepared room (or open one now) and go live. */
async function connect() {
  if (room && live) return;

  // Mic needs a secure context. Over a plain-http LAN IP, navigator.mediaDevices
  // is undefined (that's the "getUserMedia of undefined" error).
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error(
      `Microphone blocked on ${location.origin}. Open the app on https:// or http://localhost (not a plain http:// LAN IP).`
    );
  }

  setStatus('Connecting');
  resetTranscript();

  if (preparing) await preparing.catch(() => {});   // woken a moment ago, still joining
  if (!room) await prepare();
  const r = room;
  if (!r) return;             // ended while connecting
  const mySession = sessionSeq;
  const cancelled = () => mySession !== sessionSeq || room !== r;
  live = true;
  clearPreparedTimer();

  // The agent is already in the room if it was woken early; otherwise it may still
  // be booting. If it never shows up, fail clearly instead of a silent call.
  if (!agentPresent(r)) {
    agentJoinTimer = window.setTimeout(() => {
      agentJoinTimer = 0;
      if (room === r && !agentPresent(r)) {
        disconnect().finally(() =>
          window.__onVoiceFailed?.('The assistant is not available right now. Please try again in a moment.')
        );
      }
    }, AGENT_JOIN_TIMEOUT_MS);
  }

  // 5. Publish mic + allow audio playback (must run inside the click gesture).
  //    Publishing the mic is the agent's cue to start and greet.
  await r.localParticipant.setMicrophoneEnabled(true);
  await r.startAudio().catch(() => {});
  if (cancelled()) return;

  setStatus(agentPresent(r) ? 'Connected' : 'Connecting');
}

async function disconnect() {
  sessionSeq++;              // cancels a connect() that's still in flight
  clearAgentJoinTimer();
  clearPreparedTimer();
  const r = room;
  room = null;               // cleared first so the Disconnected handler knows it was us
  live = false;
  localIdentity = '';
  removeAudioEls();
  if (r) await r.disconnect();
}

// ---- Expose to the vanilla script in index.html ---------------------------

declare global {
  interface Window {
    __voiceConnect?: () => void;
    __voiceDisconnect?: () => void;
    __voicePrepare?: () => void;
    __getTranscript?: () => { role: string; text: string }[];
    setOrbSentiment?: (v: number) => void;
    // Booking-form bridge (form UI lives in index.html, the room lives here).
    __openCollectForm?: () => void;
    __closeCollectFormUI?: () => void;
    __collectActivity?: () => void;
    __setAgentSpeaking?: (v: boolean) => void;
    __submitCollect?: (value: string) => void;
    __collectIdleSignal?: () => void;
    __collectIdleEnd?: () => void;
    __collectClosed?: () => void;
    // Call lifecycle callbacks implemented in index.html.
    __onVoiceFailed?: (message: string) => void;
    __onVoiceDropped?: () => void;
    // In-memory session contact (reset on every page load; no persistence).
    __ikliName?: string;
    __ikliPhone?: string;
    __ikliEmail?: string;
  }
}

window.__voiceConnect = () => {
  connect().catch((err) => {
    console.error(err);
    setStatus('Connection failed');
    // Tear down any half-open room, then send the UI back to the start screen.
    disconnect().finally(() => window.__onVoiceFailed?.(friendlyError(err)));
  });
};

function friendlyError(err: unknown): string {
  const e = err as { name?: string; message?: string };
  if (e?.name === 'NotAllowedError') return 'Microphone access was blocked. Allow the mic for this site and try again.';
  if (e?.name === 'NotFoundError') return 'No microphone was found on this device.';
  if (/token request failed|Failed to fetch/i.test(e?.message || '')) return 'Could not reach the server. Please try again in a moment.';
  return e?.message || 'Voice connection failed.';
}

window.__voiceDisconnect = () => {
  disconnect().catch((err) => console.error(err));
};

// Back on the start screen (new session): wake the agent again on the next interaction.
window.__voicePrepare = () => armPrepare();

armPrepare();

// Send a JSON message to the agent over the reliable data channel (topic 'ikli').
function __publishToAgent(obj: any) {
  if (!room) return;
  const bytes = new TextEncoder().encode(JSON.stringify(obj));
  room.localParticipant.publishData(bytes, { reliable: true, topic: 'ikli' }).catch(() => {});
}

// Caller submitted the form (phone OR email) -> tell the agent and remember it
// for the export pre-fill. Type is auto-detected by looking for an '@'.
window.__submitCollect = (value: string) => {
  const v = (value || '').trim();
  if (!v) return;
  if (v.includes('@')) window.__ikliEmail = v; else window.__ikliPhone = v;  // memory only
  __publishToAgent({ type: 'submit', value: v });
};

// Form has sat empty and the caller is silent -> nudge the agent to check in.
window.__collectIdleSignal = () => {
  __publishToAgent({ type: 'idle' });
};

// Prolonged inactivity -> agent says it will end the call for inactivity.
window.__collectIdleEnd = () => {
  __publishToAgent({ type: 'idle_end' });
};

// Caller dismissed the form without submitting -> let the agent react instead of waiting.
window.__collectClosed = () => {
  __publishToAgent({ type: 'form_closed' });
};

window.__getTranscript = () => getTranscript();
