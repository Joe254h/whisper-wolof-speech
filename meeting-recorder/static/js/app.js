"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  config: null,
  sessions: [],
  selectedId: null,
  selected: null,
  editing: false,
  pollTimer: null,
  model: "",
  rec: null, // live recording state
};

const CHUNK_MS = 15000;
const BUSY = new Set(["queued", "transcribing", "writing"]);
const STATUS_LABEL = {
  recording: "Recording", queued: "Queued", transcribing: "Transcribing",
  writing: "Writing", done: "Ready", error: "Needs attention",
};
const LANG = { en: "English", sw: "Kiswahili" };

try { state.model = localStorage.getItem("kikao.model") || ""; } catch (_) { /* storage blocked */ }

// ---------------------------------------------------------------- helpers

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) {
    const err = new Error((body && body.error) || `Request failed (${res.status})`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.classList.remove("show"), 2600);
}

function fmtClock(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function fmtDuration(sec) {
  if (!sec) return null;
  const m = Math.round(sec / 60);
  return m < 1 ? "under a minute" : m === 1 ? "1 minute" : `${m} minutes`;
}

function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function setNotice(el, msg, kind = "") {
  el.textContent = msg || "";
  el.className = "notice" + (kind ? " " + kind : "");
}

function formValues() {
  return {
    title: $("#title").value.trim(),
    language: document.querySelector('input[name="language"]:checked').value,
    report_language: $("#reportLanguage").value,
  };
}

function renderMarkdown(md) {
  if (window.marked && window.DOMPurify) {
    return DOMPurify.sanitize(marked.parse(md, { gfm: true, breaks: true }));
  }
  const pre = document.createElement("pre");
  pre.textContent = md;
  pre.style.whiteSpace = "pre-wrap";
  return pre.outerHTML;
}

// ---------------------------------------------------------------- config / services

async function loadConfig() {
  try {
    state.config = await api("/api/config");
  } catch (e) {
    $("#services").innerHTML = "";
    return;
  }
  const c = state.config;
  const pills = [
    { ok: c.transcriber.ready,
      on: `Whisper · ${c.transcriber.backend === "local" ? "on this computer" : c.transcriber.label || "OpenAI"}`,
      off: c.transcriber.backend === "local" ? "Whisper: install faster-whisper" : `Whisper: add ${c.transcriber.key_name || "an API key"}` },
    { ok: c.writer.ready, on: "Reports · OpenRouter", off: "Reports: add OPENROUTER_API_KEY" },
  ];
  $("#services").innerHTML = "";
  for (const p of pills) {
    const s = document.createElement("span");
    s.className = "pill" + (p.ok ? "" : " off");
    s.textContent = p.ok ? p.on : p.off;
    $("#services").append(s);
  }
  $("#modelInput").placeholder = c.writer.model;
}

// ---------------------------------------------------------------- tape canvas

const tape = {
  canvas: $("#tapeCanvas"),
  meet: new Float32Array(3600),
  me: new Float32Array(3600),
  elapsed: 0,
  draw() {
    const c = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth, h = c.clientHeight;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
    }
    const ctx = c.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const css = getComputedStyle(document.documentElement);
    const grid = css.getPropertyValue("--tape-grid").trim();
    const text = css.getPropertyValue("--tape-text").trim();
    const colMeet = css.getPropertyValue("--lane-meet").trim();
    const colMe = css.getPropertyValue("--lane-me").trim();
    const total = (state.config ? state.config.max_minutes : 60) * 60;

    const labelH = 18;
    const laneGap = 10;
    const laneH = (h - labelH - laneGap) / 2;
    const lanes = [
      { y: 0, data: this.meet, color: colMeet },
      { y: laneH + laneGap, data: this.me, color: colMe },
    ];

    // minute grid: a tick per 5 minutes, labels every 10
    ctx.font = `500 11px ${css.getPropertyValue("--sans")}`;
    ctx.textBaseline = "bottom";
    for (let m = 0; m <= total / 60; m += 5) {
      const x = Math.round((m * 60 / total) * (w - 1)) + 0.5;
      ctx.strokeStyle = grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h - labelH);
      ctx.stroke();
      const every = w < 560 ? 20 : 10;
      if (m % every === 0) {
        const last = m * 60 >= total;
        ctx.fillStyle = text;
        ctx.textAlign = m === 0 ? "left" : last ? "right" : "center";
        ctx.fillText(m === 0 ? "0" : w < 560 && !last ? `${m}` : `${m} min`, x, h);
      }
    }

    // lane baselines + level bars, one column per pixel
    for (const lane of lanes) {
      const mid = lane.y + laneH / 2;
      ctx.strokeStyle = grid;
      ctx.beginPath();
      ctx.moveTo(0, mid + 0.5);
      ctx.lineTo(w, mid + 0.5);
      ctx.stroke();
      ctx.fillStyle = lane.color;
      const until = Math.min(this.elapsed, total);
      const pxEnd = Math.floor((until / total) * w);
      for (let px = 0; px < pxEnd; px++) {
        const a = Math.floor((px / w) * total), b = Math.max(a + 1, Math.floor(((px + 1) / w) * total));
        let peak = 0;
        for (let i = a; i < b && i < lane.data.length; i++) peak = Math.max(peak, lane.data[i]);
        const amp = Math.max(1, peak * (laneH / 2 - 2));
        ctx.fillRect(px, mid - amp, 1, amp * 2);
      }
    }

    // playhead
    if (this.elapsed > 0) {
      const x = Math.min(w - 1, (this.elapsed / total) * w);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(Math.round(x), 0, 2, h - labelH);
    }
  },
  reset() {
    this.meet.fill(0);
    this.me.fill(0);
    this.elapsed = 0;
    this.draw();
  },
};

window.addEventListener("resize", () => tape.draw());
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") tape.draw(); });
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => tape.draw());

// ---------------------------------------------------------------- live recording

function levelOf(analyser, buf) {
  analyser.getFloatTimeDomainData(buf);
  let sum = 0;
  for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
  const rms = Math.sqrt(sum / buf.length);
  return Math.min(1, Math.sqrt(rms) * 1.6); // perceptual-ish scaling
}

function pickMime() {
  const options = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
  return options.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
}

async function startRecording() {
  const notice = $("#liveNotice");
  setNotice(notice, "");

  if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
    setNotice(notice, "This browser cannot capture another tab. Open Kikao in Chrome or Edge on a computer.", "error");
    return;
  }
  if (!window.isSecureContext) {
    setNotice(notice, "Capture needs a secure page. Open Kikao on https:// or http://localhost.", "error");
    return;
  }
  const mime = pickMime();
  if (!mime) {
    setNotice(notice, "This browser cannot record WebM audio. Use Chrome or Edge.", "error");
    return;
  }

  let display, mic = null, micBlocked = false;
  try {
    // Ask for the share first, while the click still counts as a user gesture.
    display = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      preferCurrentTab: false,
      selfBrowserSurface: "exclude",
      systemAudio: "include",
    });
  } catch (e) {
    setNotice(notice, e.name === "NotAllowedError" ? "Sharing was cancelled. Press start and pick the meeting tab." : `Could not start sharing: ${e.message}`, "error");
    return;
  }

  if (display.getAudioTracks().length === 0) {
    display.getTracks().forEach((t) => t.stop());
    setNotice(notice, "No meeting audio came through. Share the meeting tab again and keep “Also share tab audio” switched on.", "error");
    return;
  }

  if ($("#includeMic").checked) {
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (e) {
      micBlocked = true;
      setNotice(notice, "Microphone blocked, so only the meeting audio is recorded. Your own voice will be missing.", "error");
    }
  }

  const v = formValues();
  let session;
  try {
    session = await api("/api/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(v),
    });
  } catch (e) {
    display.getTracks().forEach((t) => t.stop());
    mic && mic.getTracks().forEach((t) => t.stop());
    setNotice(notice, e.message, "error");
    return;
  }

  // Mix tab audio and microphone into one track.
  const ctx = new AudioContext();
  const dest = ctx.createMediaStreamDestination();
  const mk = (stream) => {
    const src = ctx.createMediaStreamSource(new MediaStream(stream.getAudioTracks()));
    const an = ctx.createAnalyser();
    an.fftSize = 1024;
    src.connect(an);
    src.connect(dest);
    return an;
  };
  const anMeet = mk(display);
  const anMe = mic ? mk(mic) : null;

  const recorder = new MediaRecorder(dest.stream, { mimeType: mime, audioBitsPerSecond: 48000 });
  const rec = {
    id: session.id, ctx, display, mic, recorder, anMeet, anMe,
    buf: new Float32Array(1024), started: performance.now(), seq: 0,
    queue: Promise.resolve(), pending: 0, offline: false, stopping: false, timer: 0, stopTimer: 0,
    maxSec: (state.config ? state.config.max_minutes : 60) * 60,
  };
  state.rec = rec;

  recorder.ondataavailable = (ev) => {
    if (!ev.data || ev.data.size === 0) return;
    const seq = rec.seq++;
    rec.pending++;
    rec.queue = rec.queue.then(() => sendChunk(rec, seq, ev.data));
  };
  recorder.onerror = (ev) => {
    setNotice(notice, `Recording error: ${ev.error ? ev.error.message : "unknown"}. Stopping.`, "error");
    stopRecording();
  };
  display.getAudioTracks()[0].addEventListener("ended", () => {
    if (!rec.stopping) {
      toast("Sharing ended, so the recording stopped.");
      stopRecording();
    }
  });
  recorder.start(CHUNK_MS);

  tape.reset();
  setRecordingUI(true);
  if (!micBlocked) {
    setNotice(notice, mic ? "Recording the meeting and your microphone. Keep this tab open." : "Recording the meeting audio only. Keep this tab open.", "good");
  }
  window.addEventListener("beforeunload", warnUnload);
  refreshSessions();

  // Timers, not requestAnimationFrame: rAF pauses while the user is in the meeting tab.
  rec.stopTimer = setTimeout(() => {
    toast("Reached the time limit, so the recording stopped.");
    stopRecording();
  }, rec.maxSec * 1000);
  const tick = () => {
    const elapsed = (performance.now() - rec.started) / 1000;
    const sec = Math.min(3599, Math.floor(elapsed));
    tape.meet[sec] = Math.max(tape.meet[sec], levelOf(rec.anMeet, rec.buf));
    if (rec.anMe) tape.me[sec] = Math.max(tape.me[sec], levelOf(rec.anMe, rec.buf));
    tape.elapsed = elapsed;
    if (document.visibilityState === "visible") {
      tape.draw();
      $("#clock").textContent = fmtClock(elapsed);
      $("#clockSub").textContent = `${fmtClock(rec.maxSec - elapsed)} left`;
    }
    if (elapsed >= rec.maxSec + 2) stopRecording(); // backup for the one-shot timer
  };
  rec.timer = setInterval(tick, 200);
  tick();
}

// Keeps retrying until the server has the chunk: a dropped connection must not lose
// the rest of the meeting. Later chunks wait in memory behind this one.
async function sendChunk(rec, seq, blob) {
  for (let attempt = 0; ; attempt++) {
    try {
      await api(`/api/sessions/${rec.id}/chunk?seq=${seq}`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: blob,
      });
      rec.pending--;
      if (rec.offline) {
        rec.offline = false;
        setNotice($("#liveNotice"), "Connection is back. All audio so far is saved.", "good");
      }
      return true;
    } catch (e) {
      if (e.status === 404 || e.status === 409 || rec.abandon) {
        rec.pending--;
        rec.lost = (rec.lost || 0) + 1;
        return false;
      }
      rec.offline = true;
      setNotice($("#liveNotice"),
        `Can’t reach the server. Still recording; ${rec.pending} piece${rec.pending === 1 ? "" : "s"} of audio will upload when the connection returns.`,
        "error");
      await new Promise((r) => setTimeout(r, Math.min(10000, 1000 * 2 ** attempt)));
    }
  }
}

function warnUnload(e) {
  e.preventDefault();
  e.returnValue = "";
}

async function stopRecording() {
  const rec = state.rec;
  if (!rec || rec.stopping) return;
  rec.stopping = true;
  clearInterval(rec.timer);
  clearTimeout(rec.stopTimer);
  $("#stopBtn").disabled = true;
  setNotice($("#liveNotice"), "Saving the last few seconds…");

  await new Promise((resolve) => {
    if (rec.recorder.state === "inactive") return resolve();
    rec.recorder.addEventListener("stop", resolve, { once: true });
    rec.recorder.stop();
  });
  rec.display.getTracks().forEach((t) => t.stop());
  rec.mic && rec.mic.getTracks().forEach((t) => t.stop());
  rec.ctx.close();

  // Give queued audio up to two minutes to reach the server before processing.
  const deadline = Date.now() + 120000;
  let drained = false;
  rec.queue.then(() => { drained = true; });
  while (!drained && Date.now() < deadline) {
    if (rec.pending > 0) setNotice($("#liveNotice"), `Uploading the last ${rec.pending} piece${rec.pending === 1 ? "" : "s"} of audio… keep this tab open.`);
    await new Promise((r) => setTimeout(r, 500));
  }
  if (!drained) rec.abandon = true;
  window.removeEventListener("beforeunload", warnUnload);

  try {
    await api(`/api/sessions/${rec.id}/finish`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: state.model }),
    });
    if (drained && !rec.lost) {
      setNotice($("#liveNotice"), "Recording saved. Transcribing now — the report appears below.", "good");
    } else {
      setNotice($("#liveNotice"), "The connection didn’t recover in time, so the last part of the audio is missing. Transcribing everything that reached the server.", "error");
    }
  } catch (e) {
    setNotice($("#liveNotice"), e.message, "error");
  }
  state.rec = null;
  setRecordingUI(false);
  $("#clockSub").textContent = `of ${state.config ? state.config.max_minutes : 60}:00 available`;
  await refreshSessions();
  selectSession(rec.id);
}

function setRecordingUI(on) {
  $("#startBtn").hidden = on;
  $("#stopBtn").hidden = !on;
  $("#stopBtn").disabled = false;
  $("#recState").classList.toggle("live", on);
  $("#recStateText").textContent = on ? "Recording" : "Ready";
  for (const el of $$("#setupForm input, #setupForm select, #includeMic, .mode")) el.disabled = on;
}

// ---------------------------------------------------------------- upload

let pickedFile = null;

// Reads the length from the file itself so an over-long recording is refused before uploading.
function fileDuration(file) {
  return new Promise((resolve) => {
    const el = document.createElement("audio");
    const url = URL.createObjectURL(file);
    const done = (v) => { URL.revokeObjectURL(url); resolve(v); };
    el.preload = "metadata";
    el.onloadedmetadata = () => done(Number.isFinite(el.duration) ? el.duration : null);
    el.onerror = () => done(null);
    setTimeout(() => done(null), 8000);
    el.src = url;
  });
}

async function pickFile(file) {
  const maxMin = state.config ? state.config.max_minutes : 60;
  if (file) {
    const secs = await fileDuration(file);
    if (secs && secs > maxMin * 60 + 30) {
      setNotice($("#uploadNotice"), `That recording is ${Math.round(secs / 60)} minutes long. Choose one that is ${maxMin} minutes or shorter.`, "error");
      $("#fileInput").value = "";
      file = null;
    } else {
      setNotice($("#uploadNotice"), secs ? `${fmtDuration(secs)} of audio. Ready to transcribe.` : "");
    }
  }
  pickedFile = file || null;
  $("#dropzone").classList.toggle("has-file", !!file);
  $("#dropSub").textContent = file ? `${file.name} · ${(file.size / 1048576).toFixed(1)} MB` : "MP3, M4A, WAV, WebM or MP4, up to 60 minutes";
  $("#uploadBtn").disabled = !file;
  if (file && !$("#title").value.trim()) $("#title").value = file.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ");
}

function uploadFile() {
  if (!pickedFile) return;
  const v = formValues();
  const fd = new FormData();
  fd.append("audio", pickedFile);
  fd.append("title", v.title);
  fd.append("language", v.language);
  fd.append("report_language", v.report_language);
  if (state.model) fd.append("model", state.model);

  const notice = $("#uploadNotice");
  const btn = $("#uploadBtn");
  btn.disabled = true;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/sessions/upload");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) setNotice(notice, `Uploading… ${Math.round((e.loaded / e.total) * 100)}%`);
  };
  xhr.onload = async () => {
    let body = {};
    try { body = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
    if (xhr.status >= 200 && xhr.status < 300) {
      setNotice(notice, "Uploaded. Transcribing now — the report appears below.", "good");
      pickFile(null);
      $("#fileInput").value = "";
      await refreshSessions();
      selectSession(body.id);
    } else {
      setNotice(notice, body.error || `Upload failed (${xhr.status}).`, "error");
      btn.disabled = false;
    }
  };
  xhr.onerror = () => {
    setNotice(notice, "Upload failed. Check your connection and try again.", "error");
    btn.disabled = false;
  };
  xhr.send(fd);
}

// ---------------------------------------------------------------- sessions list

async function refreshSessions() {
  try {
    const { sessions } = await api("/api/sessions");
    state.sessions = sessions;
  } catch (_) {
    return;
  }
  renderList();
  schedulePoll();
}

function renderList() {
  const ul = $("#sessionList");
  ul.innerHTML = "";
  $("#listEmpty").hidden = state.sessions.length > 0;
  for (const s of state.sessions) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.className = "session-item" + (s.id === state.selectedId ? " active" : "");
    const badgeClass = s.status === "done" ? "done" : s.status === "error" ? "error" : s.status === "recording" ? "recording" : "busy";
    const live = state.rec && state.rec.id === s.id;
    const meta = [LANG[s.language], fmtDuration(s.duration_seconds), fmtDate(s.created_at)].filter(Boolean).join(", ");
    b.innerHTML = `<span class="s-title"></span><span class="badge ${badgeClass}"></span><span class="s-meta"></span>`;
    b.querySelector(".s-title").textContent = s.title;
    b.querySelector(".badge").textContent = live ? "Live" : s.status === "recording" ? "Unfinished" : STATUS_LABEL[s.status] || s.status;
    b.querySelector(".s-meta").textContent = meta;
    b.addEventListener("click", () => selectSession(s.id));
    li.append(b);
    ul.append(li);
  }
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  const anyBusy = state.sessions.some((s) => BUSY.has(s.status));
  if (anyBusy) {
    state.pollTimer = setTimeout(async () => {
      await refreshSessions();
      if (state.selectedId && !state.editing) loadSelected();
    }, 2500);
  }
}

// ---------------------------------------------------------------- detail

async function selectSession(id) {
  if (!id) return;
  if (state.selectedId !== id) deckState.index = 0;
  state.selectedId = id;
  state.editing = false;
  renderList();
  await loadSelected(true);
  if (window.innerWidth < 900) $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadSelected(fresh = false) {
  const id = state.selectedId;
  let s;
  try {
    s = await api(`/api/sessions/${id}`);
  } catch (e) {
    if (e.status === 404) {
      state.selectedId = null;
      $("#detailBody").hidden = true;
      $("#detailEmpty").hidden = false;
    }
    return;
  }
  if (id !== state.selectedId) return;
  const prev = state.selected;
  state.selected = s;
  renderDetail(s, fresh || !prev || prev.id !== s.id || prev.updated_at !== s.updated_at);
}

function renderDetail(s, full) {
  $("#detailEmpty").hidden = true;
  $("#detailBody").hidden = false;

  if (document.activeElement !== $("#detailTitle")) $("#detailTitle").value = s.title;
  const meta = [
    `Spoken in ${LANG[s.language]}`,
    fmtDuration(s.duration_seconds),
    new Date(s.created_at * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }),
    s.model ? `report by ${s.model}` : null,
  ].filter(Boolean);
  $("#detailMeta").textContent = meta.join("  /  ");

  // pipeline
  const steps = { audio: $('[data-step="audio"]'), transcript: $('[data-step="transcript"]'), report: $('[data-step="report"]') };
  Object.values(steps).forEach((el) => el.className = "");
  const live = state.rec && state.rec.id === s.id;
  if (s.has_audio && s.status !== "recording") steps.audio.classList.add("done");
  else if (s.status === "recording") steps.audio.classList.add(live ? "active" : "failed");
  if (s.has_transcript) steps.transcript.classList.add("done");
  else if (s.status === "transcribing" || s.status === "queued") steps.transcript.classList.add("active");
  else if (s.status === "error") steps.transcript.classList.add("failed");
  if (s.status === "done" || (s.has_report && !BUSY.has(s.status))) steps.report.classList.add("done");
  else if (s.status === "writing") steps.report.classList.add("active");
  else if (s.status === "error" && s.has_transcript) steps.report.classList.add("failed");

  const busy = BUSY.has(s.status);
  $("#progressWrap").hidden = !busy;
  $("#progressBar").style.width = `${Math.round((s.progress || 0) * 100)}%`;
  $("#progressText").textContent = s.stage || STATUS_LABEL[s.status];

  const orphan = s.status === "recording" && !live;
  $("#errorBox").hidden = !(s.status === "error" || orphan);
  $("#errorText").textContent = orphan
    ? "This recording was interrupted before it was submitted. You can still transcribe the audio that was saved."
    : s.error || "";
  $("#retryBtn").hidden = orphan;
  $("#retryBtn").textContent = s.has_transcript ? "Retry report" : "Retry";
  $("#finishOrphanBtn").hidden = !orphan;

  // report
  for (const id of ["copyReport", "printReport", "editReport"]) $("#" + id).disabled = !s.report;
  updateDownloads(s);
  $("#regenReport").disabled = !s.has_transcript || busy;
  $("#deleteBtn").disabled = busy || live;

  if (full && !state.editing) {
    $("#report").innerHTML = s.report
      ? renderMarkdown(s.report)
      : `<p class="placeholder">${busy ? "The report will appear here when processing finishes." : live ? "The report will be written after you stop recording." : "No report yet."}</p>`;
    $("#reportEditor").hidden = true;
    $("#report").hidden = false;
    setBtn($("#editReport"), "i-edit", "Edit");
    renderTranscript(s);
    renderSlides(s);
  }
}

function renderTranscript(s) {
  const ol = $("#transcript");
  ol.innerHTML = "";
  const player = $("#player");
  const src = s.has_audio ? `/api/sessions/${s.id}/audio` : "";
  if (player.dataset.src !== src) {
    player.dataset.src = src;
    if (src) player.src = src; else player.removeAttribute("src");
  }
  if (!s.transcript) {
    const li = document.createElement("li");
    li.innerHTML = '<span></span><span class="tx placeholder"></span>';
    li.querySelector(".tx").textContent = "The transcript will appear here after Whisper finishes.";
    ol.append(li);
    return;
  }
  for (const seg of s.transcript) {
    if (!seg.text) continue;
    const li = document.createElement("li");
    const ts = document.createElement("button");
    ts.type = "button";
    ts.className = "ts";
    ts.textContent = fmtClock(seg.start);
    ts.setAttribute("aria-label", `Play from ${fmtClock(seg.start)}`);
    ts.addEventListener("click", () => {
      player.currentTime = seg.start;
      player.play().catch(() => {});
    });
    const tx = document.createElement("span");
    tx.className = "tx";
    tx.textContent = seg.text;
    li.append(ts, tx);
    ol.append(li);
  }
}

// ---------------------------------------------------------------- detail actions

async function saveTitle() {
  const s = state.selected;
  const v = $("#detailTitle").value.trim();
  if (!s || !v || v === s.title) return;
  await api(`/api/sessions/${s.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: v }) });
  s.title = v;
  refreshSessions();
  toast("Title saved");
}

async function toggleEdit() {
  const s = state.selected;
  if (!s || !s.report) return;
  if (!state.editing) {
    state.editing = true;
    $("#reportEditor").value = s.report;
    $("#reportEditor").hidden = false;
    $("#report").hidden = true;
    setBtn($("#editReport"), "i-check", "Save edits");
    $("#reportEditor").focus();
    return;
  }
  const text = $("#reportEditor").value;
  await api(`/api/sessions/${s.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ report: text }) });
  state.editing = false;
  s.report = text;
  await loadSelected(true);
  toast("Report saved");
}

async function retry(reportOnly) {
  const s = state.selected;
  if (!s) return;
  try {
    await api(`/api/sessions/${s.id}/retry`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ report_only: reportOnly, model: state.model }),
    });
  } catch (e) {
    toast(e.message);
    return;
  }
  state.editing = false;
  await refreshSessions();
  loadSelected(true);
}

async function finishOrphan() {
  const s = state.selected;
  try {
    await api(`/api/sessions/${s.id}/finish`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: state.model }),
    });
  } catch (e) {
    toast(e.message);
    return;
  }
  await refreshSessions();
  loadSelected(true);
}

async function deleteSelected() {
  const s = state.selected;
  if (!s || !confirm(`Delete “${s.title}” with its audio, transcript and report?`)) return;
  try {
    await api(`/api/sessions/${s.id}`, { method: "DELETE" });
  } catch (e) {
    toast(e.message);
    return;
  }
  state.selectedId = null;
  state.selected = null;
  $("#detailBody").hidden = true;
  $("#detailEmpty").hidden = false;
  refreshSessions();
  toast("Meeting deleted");
}

// ---------------------------------------------------------------- template dialog

async function openTemplate() {
  const [{ template }, settings] = await Promise.all([api("/api/template"), api("/api/settings")]);
  $("#templateText").value = template;
  $("#vocabText").value = settings.vocabulary;
  $("#instructionsText").value = settings.instructions;
  $("#headerText").value = settings.header;
  $("#footerText").value = settings.footer;
  $("#modelInput").value = state.model;
  switchSettingsTab("vocab");
  $("#templateDialog").showModal();
}

function switchSettingsTab(name) {
  for (const t of $$("[data-stab]")) {
    const on = t.dataset.stab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on);
  }
  for (const panel of $$("[data-stab-panel]")) panel.hidden = panel.dataset.stabPanel !== name;
}

async function saveTemplate() {
  const json = (body) => ({ method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  try {
    await Promise.all([
      api("/api/template", json({ template: $("#templateText").value })),
      api("/api/settings", json({
        vocabulary: $("#vocabText").value, instructions: $("#instructionsText").value,
        header: $("#headerText").value, footer: $("#footerText").value,
      })),
    ]);
  } catch (e) {
    toast(e.message);
    return;
  }
  state.model = $("#modelInput").value.trim();
  try { localStorage.setItem("kikao.model", state.model); } catch (_) { /* storage blocked */ }
  $("#templateDialog").close();
  toast("Settings saved. They apply to the next report you write.");
}

async function resetTemplate() {
  const { default: def } = await api("/api/template");
  $("#templateText").value = def;
}

// ---------------------------------------------------------------- downloads

function setBtn(el, icon, label) {
  el.innerHTML = `<svg class="ic"><use href="#${icon}"/></svg>`;
  el.append(document.createTextNode(label));
}

function updateDownloads(s) {
  const has = { report: !!s.report, slides: !!s.report, transcript: !!s.transcript, audio: !!s.has_audio };
  for (const a of $$("[data-dl]")) {
    const key = a.dataset.dl;
    const what = key.split(".")[0];
    a.href = key === "audio" ? `/api/sessions/${s.id}/audio?download=1` : `/api/sessions/${s.id}/download/${key}`;
    a.toggleAttribute("aria-disabled", !has[what]);
    a.tabIndex = has[what] ? 0 : -1;
  }
  for (const b of $$("[data-action]")) b.disabled = !s.report;
  $("#audioExt").textContent = s.audio_ext || ".webm";
  $("#downloadToggle").disabled = !(has.report || has.transcript || has.audio);
  $("#pptxLink").href = `/api/sessions/${s.id}/download/slides.pptx`;
}

function toggleMenu(open) {
  const panel = $("#downloadPanel");
  open = open === undefined ? panel.hidden : open;
  panel.hidden = !open;
  $("#downloadToggle").setAttribute("aria-expanded", open);
  if (open) (panel.querySelector('[role="menuitem"]:not([aria-disabled]):not(:disabled)') || panel).focus();
}

// ---------------------------------------------------------------- slides

const deckState = { index: 0, deck: null };

function slideHTML(sl, i, total, deck) {
  const esc = (t) => { const d = document.createElement("div"); d.textContent = t; return d.innerHTML; };
  if (i === -1) {
    return `<div class="slide title-slide"><div class="ts-brand"><span class="dot"></span>Meeting brief</div>
      <h3>${esc(deck.title)}</h3><span class="ts-rule"></span><p>${esc(deck.subtitle || "")}</p>
      <small>About two minutes to read</small></div>`;
  }
  let body = "";
  if (sl.lead) body += `<p class="lead">${esc(sl.lead)}</p>`;
  if (sl.table) {
    body += `<table><thead><tr>${sl.table.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${
      sl.table.rows.map((r) => `<tr>${sl.table.columns.map((_, ci) => `<td>${esc(r[ci] || "")}</td>`).join("")}</tr>`).join("")
    }</tbody></table>`;
  }
  if (sl.bullets && sl.bullets.length) body += `<ul>${sl.bullets.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>`;
  return `<div class="slide"><h3>${esc(sl.title)}</h3><div class="slide-body">${body}</div>
    <footer><span>${esc(deck.title)}</span><span>${i + 2} / ${total + 1}</span></footer></div>`;
}

function renderSlides(s) {
  const deck = s.slides;
  deckState.deck = deck;
  const stage = $("#stage");
  const has = deck && deck.slides && deck.slides.length;
  $("#prevSlide").disabled = $("#nextSlide").disabled = $("#fullscreenSlides").disabled = !has;
  $("#rebuildSlides").disabled = !s.report || BUSY.has(s.status);
  $("#pptxLink").toggleAttribute("aria-disabled", !has);
  if (!has) {
    stage.innerHTML = `<div class="slide empty-slide"><p>${
      BUSY.has(s.status) ? "Slides are made right after the report is written." :
      s.report ? "No slides yet. Press \u201cUpdate from report\u201d to make them." : "Slides appear once the report is written."
    }</p></div>`;
    $("#slideCount").textContent = "0 / 0";
    $("#slidesPrint").innerHTML = "";
    return;
  }
  const total = deck.slides.length;
  deckState.index = Math.min(deckState.index, total);
  $("#slidesPrint").innerHTML = [-1, ...deck.slides.map((_, i) => i)]
    .map((i) => `<div class="slide-page">${slideHTML(deck.slides[i], i, total, deck)}</div>`).join("");
  showSlide(deckState.index);
}

function showSlide(n) {
  const deck = deckState.deck;
  if (!deck || !deck.slides.length) return;
  const total = deck.slides.length;
  n = Math.max(0, Math.min(total, n));
  deckState.index = n;
  $("#stage").innerHTML = slideHTML(deck.slides[n - 1], n - 1, total, deck);
  $("#slideCount").textContent = `${n + 1} / ${total + 1}`;
  $("#prevSlide").disabled = n === 0;
  $("#nextSlide").disabled = n === total;
}

async function rebuildSlides() {
  const s = state.selected;
  if (!s) return;
  const btn = $("#rebuildSlides");
  btn.disabled = true;
  setBtn(btn, "i-refresh", "Updating\u2026");
  try {
    const { slides } = await api(`/api/sessions/${s.id}/slides`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: state.model }),
    });
    s.slides = slides;
    deckState.index = 0;
    renderSlides(s);
    toast("Slides updated from the report");
  } catch (e) {
    toast(e.message);
  }
  setBtn(btn, "i-refresh", "Update from report");
  btn.disabled = false;
}

function printSlides() {
  if (!deckState.deck) return;
  document.body.classList.add("printing-slides");
  window.print();
}
window.addEventListener("afterprint", () => document.body.classList.remove("printing-slides"));

// ---------------------------------------------------------------- wiring

function switchMode(live) {
  $("#tabLive").classList.toggle("active", live);
  $("#tabUpload").classList.toggle("active", !live);
  $("#tabLive").setAttribute("aria-selected", live);
  $("#tabUpload").setAttribute("aria-selected", !live);
  $("#liveMode").hidden = !live;
  $("#uploadMode").hidden = live;
  if (live) tape.draw();
}

function switchTab(name) {
  for (const t of $$(".dtab")) {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on);
  }
  for (const n of ["report", "slides", "transcript"]) $(`#panel-${n}`).hidden = name !== n;
  if (name === "slides") $("#deck").focus({ preventScroll: true });
}

$("#tabLive").addEventListener("click", () => switchMode(true));
$("#tabUpload").addEventListener("click", () => switchMode(false));
$("#startBtn").addEventListener("click", startRecording);
$("#stopBtn").addEventListener("click", stopRecording);

$("#fileInput").addEventListener("change", (e) => pickFile(e.target.files[0]));
const dz = $("#dropzone");
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, () => dz.classList.remove("over")));
dz.addEventListener("drop", (e) => {
  e.preventDefault();
  if (e.dataTransfer.files[0]) pickFile(e.dataTransfer.files[0]);
});
$("#uploadBtn").addEventListener("click", uploadFile);

$$(".dtab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
$("#downloadToggle").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(); });
$("#downloadPanel").addEventListener("click", (e) => {
  const item = e.target.closest('[role="menuitem"]');
  if (!item) return;
  if (item.hasAttribute("aria-disabled")) { e.preventDefault(); return; }
  if (item.dataset.action === "print-report") { switchTab("report"); window.print(); }
  if (item.dataset.action === "print-slides") printSlides();
  toggleMenu(false);
});
document.addEventListener("click", (e) => { if (!e.target.closest("#downloadMenu")) toggleMenu(false); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#downloadPanel").hidden) { toggleMenu(false); $("#downloadToggle").focus(); }
});
$("#prevSlide").addEventListener("click", () => showSlide(deckState.index - 1));
$("#nextSlide").addEventListener("click", () => showSlide(deckState.index + 1));
$("#deck").addEventListener("keydown", (e) => {
  if (["ArrowRight", "PageDown", " "].includes(e.key)) { e.preventDefault(); showSlide(deckState.index + 1); }
  if (["ArrowLeft", "PageUp"].includes(e.key)) { e.preventDefault(); showSlide(deckState.index - 1); }
  if (e.key === "Home") showSlide(0);
  if (e.key === "End") showSlide(Infinity);
});
$("#stage").addEventListener("click", (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  showSlide(deckState.index + (e.clientX - r.left < r.width / 3 ? -1 : 1));
});
$("#fullscreenSlides").addEventListener("click", () => {
  const d = $("#deck");
  if (document.fullscreenElement) document.exitFullscreen();
  else if (d.requestFullscreen) d.requestFullscreen().then(() => d.focus()).catch(() => toast("Full screen is blocked here."));
});
$("#rebuildSlides").addEventListener("click", rebuildSlides);
$("#pptxLink").addEventListener("click", (e) => { if (e.currentTarget.hasAttribute("aria-disabled")) e.preventDefault(); });
$("#detailTitle").addEventListener("change", saveTitle);
$("#detailTitle").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });
$("#copyReport").addEventListener("click", async () => {
  if (!state.selected || !state.selected.report) return;
  try {
    await navigator.clipboard.writeText(state.selected.report);
    toast("Report copied as Markdown");
  } catch (_) {
    toast("Copy is blocked here. Use Download instead.");
  }
});
$("#printReport").addEventListener("click", () => {
  if (!state.selected || !state.selected.report) return;
  switchTab("report");
  document.body.classList.remove("printing-slides");
  window.print();
});
$("#editReport").addEventListener("click", toggleEdit);
$("#regenReport").addEventListener("click", () => {
  if (state.selected && state.selected.has_report && !confirm("Rewrite the report from the transcript? Your edits will be replaced.")) return;
  retry(true);
});
$("#retryBtn").addEventListener("click", () => retry(!!(state.selected && state.selected.has_transcript)));
$("#finishOrphanBtn").addEventListener("click", finishOrphan);
$("#deleteBtn").addEventListener("click", deleteSelected);

$("#openTemplate").addEventListener("click", openTemplate);
$$("[data-stab]").forEach((t) => t.addEventListener("click", () => switchSettingsTab(t.dataset.stab)));
$("#saveTemplate").addEventListener("click", saveTemplate);
$("#resetTemplate").addEventListener("click", resetTemplate);

(async function init() {
  await loadConfig();
  tape.draw();
  await refreshSessions();
  if (state.sessions[0]) selectSession(state.sessions[0].id);
})();
