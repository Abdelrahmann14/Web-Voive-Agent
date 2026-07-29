/**
 * Frontend LiveKit integration for the Iklipse voice UI.
 *
 * Connects the browser to a LiveKit room, publishes the mic, plays the agent's
 * audio, and renders live transcripts into #bubbles-container. Also keeps the
 * full transcript so the End screen can show a real report. Exposes hooks the
 * index.html script calls: window.__voiceConnect / __voiceDisconnect / __getTranscript.
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

let room: Room | null = null;
let localIdentity = '';

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
    // user bubbles rise from the right. Pure GPU transform — no latency cost.
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
  } else if (userSpeaking) {
    setStatus('Listening');
    window.setOrbSentiment?.(0.5);
  } else {
    setStatus('Connected');
    window.setOrbSentiment?.(0.5);
  }
}

// ---- Connect / disconnect -------------------------------------------------

async function connect() {
  if (room) return;

  // Mic needs a secure context. Over a plain-http LAN IP, navigator.mediaDevices
  // is undefined (that's the "getUserMedia of undefined" error).
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error(
      `Microphone blocked on ${location.origin}. Open the app on https:// or http://localhost (not a plain http:// LAN IP).`
    );
  }

  setStatus('Connecting');
  resetTranscript();

  // 1. Get a token + server URL. If we already know the caller's name (returning
  //    visitor), pass it so the agent greets them by name and skips asking.
  let knownName = '';
  try {
    knownName = localStorage.getItem('ikli_user_name') || '';
  } catch {}
  const tokenUrl = knownName
    ? `${TOKEN_ENDPOINT}?name=${encodeURIComponent(knownName)}`
    : TOKEN_ENDPOINT;
  const res = await fetch(tokenUrl, { method: 'POST' });
  if (!res.ok) throw new Error(`token request failed: ${res.status}`);
  const { url, token, identity } = await res.json();
  localIdentity = identity;

  // 2. Connect to the room.
  room = new Room({ adaptiveStream: true, dynacast: true });

  room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack, _pub: RemoteTrackPublication, _p: RemoteParticipant) => {
    if (track.kind === Track.Kind.Audio) {
      const audioEl = track.attach();
      audioEl.autoplay = true;
      audioEl.style.display = 'none';
      document.body.appendChild(audioEl);
    }
  });

  room.on(RoomEvent.ActiveSpeakersChanged, (speakers: Participant[]) => driveOrb(speakers));
  room.on(RoomEvent.Disconnected, () => setStatus('Disconnected'));

  // The agent pushes signals here via its participant attributes: the caller's
  // name / phone (persist for next call), and open/close the phone form mid-call.
  room.on(RoomEvent.ParticipantAttributesChanged, (changed: Record<string, string>) => {
    const n = changed?.user_name;
    if (n) {
      try { localStorage.setItem('ikli_user_name', n); } catch {}
    }
    const phone = changed?.save_user_phone;
    if (phone) {
      try { localStorage.setItem('ikli_user_phone', phone); } catch {}
    }
    if (changed?.open_phone_form) window.__openPhoneForm?.();
    if (changed?.close_phone_form) window.__closePhoneFormUI?.();
  });

  await room.connect(url, token);

  // 3. Transcript handler — key each bubble by segment id so interim results
  //    update in place instead of spawning duplicates.
  try {
    room.registerTextStreamHandler('lk.transcription', async (reader: any, participantInfo: any) => {
      const identity: string | undefined = participantInfo?.identity;
      const isUser = identity === localIdentity;
      const attrs = reader?.info?.attributes ?? {};
      const segId: string = attrs['lk.segment_id'] || reader?.info?.id || String(Math.random());
      let text = '';
      for await (const chunk of reader) {
        text += chunk;
        upsertSegment(segId, text, isUser);
      }
      finishSegment(segId); // stream done — let the caret clear once text catches up
    });
  } catch (e) {
    console.warn('transcription handler not registered', e);
  }

  // 4. Publish mic + allow audio playback (must run inside the click gesture).
  await room.localParticipant.setMicrophoneEnabled(true);
  await room.startAudio().catch(() => {});

  setStatus('Connected');
}

async function disconnect() {
  if (!room) return;
  try {
    await room.disconnect();
  } finally {
    room = null;
    localIdentity = '';
  }
}

// ---- Expose to the vanilla script in index.html ---------------------------

declare global {
  interface Window {
    __voiceConnect?: () => void;
    __voiceDisconnect?: () => void;
    __getTranscript?: () => { role: string; text: string }[];
    setOrbSentiment?: (v: number) => void;
    // Phone-form bridge (UI lives in index.html, room lives here).
    __openPhoneForm?: () => void;
    __closePhoneFormUI?: () => void;
    __submitPhone?: (phone: string) => void;
    __phoneFormIdle?: () => void;
  }
}

window.__voiceConnect = () => {
  connect().catch((err) => {
    console.error(err);
    setStatus('Connection failed');
    alert('Voice connection failed: ' + err.message);
  });
};

window.__voiceDisconnect = () => {
  disconnect().catch((err) => console.error(err));
};

// Caller submitted their phone number in the form -> tell the agent (via our
// participant attribute) and remember it for the WhatsApp export pre-fill.
window.__submitPhone = (phone: string) => {
  const p = (phone || '').trim();
  if (!p || !room) return;
  try { localStorage.setItem('ikli_user_phone', p); } catch {}
  room.localParticipant.setAttributes({ phone_number: p }).catch(() => {});
};

// Form has sat empty for a few seconds -> nudge the agent to check in.
// Value changes each time so a re-open still fires the agent's change handler.
window.__phoneFormIdle = () => {
  if (!room) return;
  room.localParticipant.setAttributes({ phone_form_idle: Date.now().toString() }).catch(() => {});
};

window.__getTranscript = () => getTranscript();
