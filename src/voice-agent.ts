/// <reference types="vite/client" />
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
// Agent bubbles are built word by word: each new word flies out of the orb and lands
// in its place in the bubble (see the word-flight loop below).
type Segment = {
  el: HTMLElement;
  out: HTMLElement;         // text sink: the <p> (user) or a word container (agent)
  isUser: boolean;
  text: string;             // full text so far (used for the report)
  order: number;
  words: string[];          // agent: words received so far
  spans: HTMLElement[];     // agent: one <span> per revealed word
  ended: boolean;           // stream for this segment finished
  lastEmit: number;         // agent: timestamp of the last revealed word
};
const segments = new Map<string, Segment>();
let order = 0;

const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

// ---- Agent audio level (drives the orb's pulse) ---------------------------
// One shared AudioContext, resumed inside the mic-press gesture. The agent's
// remote track is tapped with an analyser; index.html polls __agentLevel()
// every frame and turns it into the orb's heartbeat.
let audioCtx: AudioContext | null = null;
let analyser: AnalyserNode | null = null;
let analyserSrc: MediaStreamAudioSourceNode | null = null;
let analysedTrack: RemoteTrack | null = null;
const levelBuf = new Float32Array(1024);

function ensureAudioCtx(): AudioContext | null {
  try {
    if (!audioCtx) audioCtx = new AudioContext();
    if (audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
  } catch {
    audioCtx = null;
  }
  return audioCtx;
}

function watchAgentAudio(track: RemoteTrack) {
  stopWatchingAgentAudio();
  const ctx = ensureAudioCtx();
  if (!ctx || !track.mediaStreamTrack) return;
  try {
    analyserSrc = ctx.createMediaStreamSource(new MediaStream([track.mediaStreamTrack]));
    analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0;
    analyserSrc.connect(analyser);
    analysedTrack = track;
  } catch {
    stopWatchingAgentAudio();
  }
}

function stopWatchingAgentAudio() {
  try { analyserSrc?.disconnect(); } catch { /* already gone */ }
  analyserSrc = null;
  analyser = null;
  analysedTrack = null;
}

/** Agent voice loudness right now, 0..~1 (RMS of the latest audio frame). */
function agentLevel(): number {
  if (!analyser || !live) return 0;
  analyser.getFloatTimeDomainData(levelBuf);
  let sum = 0;
  for (let i = 0; i < levelBuf.length; i++) sum += levelBuf[i] * levelBuf[i];
  return Math.sqrt(sum / levelBuf.length);
}

// ---- Word reveal + flight (agent bubbles) ---------------------------------
// The agent's transcript arrives word by word, already paced to the voice by the
// server. Each word is placed (invisible) in the bubble, and a copy of it flies
// from the orb's rim along a soft arc into that spot, sharpening as it travels.
// One shared rAF loop paces the words and moves every flying copy.
const typing = new Set<Segment>();
type Flight = {
  el: HTMLElement;
  target: HTMLElement;
  sx: number;
  sy: number;
  t0: number;
  dur: number;
  bow: number;
};
const flights = new Set<Flight>();
let rafId = 0;
const MAX_FLIGHTS = 14;     // beyond this, words just fade in (keeps it cheap on bursts)

function flightLayer(): HTMLElement {
  let layer = document.getElementById('word-flight');
  if (!layer) {
    layer = document.createElement('div');
    layer.id = 'word-flight';
    layer.setAttribute('aria-hidden', 'true');
    document.body.appendChild(layer);
  }
  return layer;
}

function centerOf(el: HTMLElement) {
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

function launchFlight(span: HTMLElement, word: string): boolean {
  const orb = window.__orbScreen?.();
  if (!orb || document.hidden || reduceMotion.matches || flights.size >= MAX_FLIGHTS) return false;

  const to = centerOf(span);
  let dx = to.x - orb.x;
  let dy = to.y - orb.y;
  const dist = Math.hypot(dx, dy) || 1;
  dx /= dist;
  dy /= dist;
  // Start just inside the orb's rim, on the side facing the word.
  const sx = orb.x + dx * orb.r * 0.72;
  const sy = orb.y + dy * orb.r * 0.72;

  const cs = getComputedStyle(span);
  const el = document.createElement('span');
  el.className = 'w-fly';
  el.textContent = word;
  el.style.font = cs.font;
  el.style.letterSpacing = cs.letterSpacing;
  el.style.color = cs.color;
  el.style.transform = `translate3d(${sx}px, ${sy}px, 0) translate(-50%, -50%) scale(0.3)`;
  flightLayer().appendChild(el);

  const travel = Math.hypot(to.x - sx, to.y - sy);
  const dur = Math.min(900, 460 + travel * 0.42);
  el.animate(
    [
      { opacity: 0, filter: 'blur(7px)', textShadow: '0 0 18px rgba(255,76,57,0.95)', color: '#ff4c39' },
      { opacity: 1, offset: 0.22 },
      { filter: 'blur(0px)', textShadow: '0 0 12px rgba(255,76,57,0.55)', color: '#ff4c39', offset: 0.55 },
      { opacity: 1, filter: 'blur(0px)', textShadow: '0 0 0 rgba(255,76,57,0)', color: cs.color },
    ],
    { duration: dur, easing: 'linear', fill: 'forwards' },
  );
  flights.add({ el, target: span, sx, sy, t0: performance.now(), dur, bow: 0.7 + Math.random() * 0.6 });
  return true;
}

function easeOutCubic(t: number) {
  return 1 - Math.pow(1 - t, 3);
}

function stepFlights(now: number) {
  for (const f of flights) {
    const p = Math.min(1, (now - f.t0) / f.dur);
    if (!f.target.isConnected) {
      f.el.remove();
      flights.delete(f);
      continue;
    }
    const e = easeOutCubic(p);
    // Track the live target (the bubble grows / scrolls while the word is in the air).
    const to = centerOf(f.target);
    const mx = (f.sx + to.x) / 2;
    const my = (f.sy + to.y) / 2;
    let nx = -(to.y - f.sy);
    let ny = to.x - f.sx;
    const nl = Math.hypot(nx, ny) || 1;
    nx /= nl;
    ny /= nl;
    if (ny > 0) { nx = -nx; ny = -ny; }       // always bow upward
    const lift = Math.min(110, Math.hypot(to.x - f.sx, to.y - f.sy) * 0.2) * f.bow;
    const cx = mx + nx * lift;
    const cy = my + ny * lift;
    const a = (1 - e) * (1 - e);
    const b = 2 * (1 - e) * e;
    const c = e * e;
    const x = a * f.sx + b * cx + c * to.x;
    const y = a * f.sy + b * cy + c * to.y;
    const s = 0.3 + 0.7 * e;
    f.el.style.transform = `translate3d(${x}px, ${y}px, 0) translate(-50%, -50%) scale(${s})`;

    if (p >= 1) {
      f.target.classList.remove('w-wait');
      f.target.classList.add('w-land');
      f.el.remove();
      flights.delete(f);
    }
  }
}

/** Reveal the next word of an agent segment (with a flight unless `quiet`). */
function emitWord(seg: Segment, quiet: boolean) {
  const i = seg.spans.length;
  const word = seg.words[i];
  if (i > 0) seg.out.appendChild(document.createTextNode(' '));
  const span = document.createElement('span');
  span.className = 'w';
  span.textContent = word;
  seg.out.appendChild(span);
  seg.spans.push(span);

  const c = bubblesContainer();
  if (c) c.scrollTop = c.scrollHeight;

  if (!quiet) {
    span.classList.add('w-wait');            // keeps its spot, invisible until the copy lands
    if (launchFlight(span, word)) {
      window.__orbKick?.(1);
      return;
    }
    span.classList.remove('w-wait');
  }
  span.classList.add('w-in');
}

function tick(now: number) {
  for (const seg of typing) {
    const backlog = seg.words.length - seg.spans.length;
    if (backlog <= 0) {
      if (seg.ended) typing.delete(seg);
      continue;
    }
    // The stream is already voice-paced; this only spreads out bursts. A big
    // backlog (tab was hidden, final flush) catches up at once, animating the tail.
    const gap = backlog > 10 ? 0 : Math.max(45, 140 - backlog * 20);
    if (now - seg.lastEmit < gap) continue;
    const quietCount = backlog > 10 ? backlog - 4 : 0;
    for (let i = 0; i < quietCount; i++) emitWord(seg, true);
    emitWord(seg, false);
    seg.lastEmit = now;
  }

  stepFlights(now);

  if (typing.size || flights.size) {
    rafId = requestAnimationFrame(tick);
  } else {
    rafId = 0;
  }
}

function ensureTyping() {
  if (!rafId) rafId = requestAnimationFrame(tick);
}

/** Mark an agent segment's stream as done. */
function finishSegment(segId: string) {
  const seg = segments.get(segId);
  if (seg && !seg.isUser) {
    seg.ended = true;
    ensureTyping();
  }
}

function clearFlights() {
  for (const f of flights) f.el.remove();
  flights.clear();
}

// ---- UI helpers -----------------------------------------------------------

function setStatus(text: string) {
  const el = document.getElementById('speaking-status');
  if (!el || el.textContent === text) return;
  el.textContent = text;
  el.dataset.state = text.toLowerCase().replace(/\s+/g, '-');
  if (!reduceMotion.matches) {
    el.animate(
      [{ opacity: 0, transform: 'translateY(5px)' }, { opacity: 1, transform: 'translateY(0)' }],
      { duration: 260, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' },
    );
  }
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
  clearFlights();
  if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
}

function splitWords(text: string): string[] {
  return text.split(/\s+/).filter(Boolean);
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
    const p = document.createElement('p');
    el.appendChild(p);
    container.appendChild(el);
    seg = { el, out: p, isUser, text: '', order: order++, words: [], spans: [], ended: false, lastEmit: 0 };
    segments.set(segId, seg);
  }

  seg.text = clean;
  if (isUser) {
    seg.out.textContent = clean;             // instant for the caller's own words
    container.scrollTop = container.scrollHeight;
    return;
  }

  // Agent: words already on screen that changed (rare) are corrected in place;
  // new words are queued for the flight loop.
  const words = splitWords(clean);
  const shown = Math.min(seg.spans.length, words.length);
  for (let i = 0; i < shown; i++) {
    if (seg.spans[i].textContent !== words[i]) seg.spans[i].textContent = words[i];
  }
  seg.words = words;
  typing.add(seg);
  ensureTyping();
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
    window.setOrbSentiment?.(0.0);       // orb cools to the caller's indigo
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
        watchAgentAudio(track);   // the orb pulses with the agent's voice
      }
    });
    r.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
      if (track === analysedTrack) stopWatchingAgentAudio();
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
      stopWatchingAgentAudio();
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
  stopWatchingAgentAudio();
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

  ensureAudioCtx();            // unlock Web Audio inside the click gesture (orb pulse)
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
  stopWatchingAgentAudio();
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
    // Orb hooks implemented in index.html (word flights start at the orb's rim).
    __orbScreen?: () => { x: number; y: number; r: number } | null;
    __orbKick?: (strength?: number) => void;
    __agentLevel?: () => number;
    __ikliDemo?: (text: string) => void;
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
window.__agentLevel = agentLevel;

// Dev-only: play a fake agent line through the word-flight pipeline (no mic needed).
if (import.meta.env.DEV) {
  window.__ikliDemo = (text: string) => {
    const id = 'demo-' + Math.random().toString(36).slice(2);
    const words = text.split(' ');
    let acc = '';
    let i = 0;
    const t = window.setInterval(() => {
      acc += (i ? ' ' : '') + words[i++];
      upsertSegment(id, acc, false);
      if (i >= words.length) { window.clearInterval(t); finishSegment(id); }
    }, 230);
  };
}
