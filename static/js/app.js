const els = {
  tabs: document.querySelectorAll(".tab"),
  panels: document.querySelectorAll(".tab-panel"),
  modelStatus: document.getElementById("modelStatus"),
  pulse: document.querySelector(".pulse"),
  micSelect: document.getElementById("micSelect"),
  refreshMics: document.getElementById("refreshMics"),
  startRecording: document.getElementById("startRecording"),
  stopRecording: document.getElementById("stopRecording"),
  sendRecording: document.getElementById("sendRecording"),
  recordingPreview: document.getElementById("recordingPreview"),
  waveform: document.getElementById("waveform"),
  meterFill: document.getElementById("meterFill"),
  timer: document.getElementById("timer"),
  levelHint: document.getElementById("levelHint"),
  fileInput: document.getElementById("fileInput"),
  dropzone: document.getElementById("dropzone"),
  dropzoneText: document.getElementById("dropzoneText"),
  uploadPreview: document.getElementById("uploadPreview"),
  sendUpload: document.getElementById("sendUpload"),
  sampleGrid: document.getElementById("sampleGrid"),
  transcript: document.getElementById("transcript"),
  resultMeta: document.getElementById("resultMeta"),
  copyText: document.getElementById("copyText"),
  downloadText: document.getElementById("downloadText"),
  clearText: document.getElementById("clearText"),
  toast: document.getElementById("toast"),
};

const TARGET_SAMPLE_RATE = 16000;
let audioContext;
let analyser;
let processor;
let sourceNode;
let silentGain;
let mediaStream;
let recording = false;
let recordingStartedAt = 0;
let timerInterval;
let animationFrame;
let audioChunks = [];
let recordedBlob = null;
let uploadFile = null;
let lastTranscript = "";

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("is-visible");
  clearTimeout(showToast.timeout);
  showToast.timeout = setTimeout(() => els.toast.classList.remove("is-visible"), 3200);
}

function setBusy(isBusy, message = "Working") {
  document.body.classList.toggle("is-busy", isBusy);
  if (isBusy) {
    els.resultMeta.textContent = message;
  }
}

function setTranscript(text, meta) {
  lastTranscript = text || "";
  els.transcript.textContent = lastTranscript || "No transcript returned.";
  els.transcript.classList.toggle("is-placeholder", !lastTranscript);
  els.resultMeta.textContent = meta || "Done";
  els.copyText.disabled = !lastTranscript;
  els.downloadText.disabled = !lastTranscript;
}

function resetTranscript() {
  lastTranscript = "";
  els.transcript.textContent = "Your Wolof transcription will appear here.";
  els.transcript.classList.add("is-placeholder");
  els.resultMeta.textContent = "No audio yet";
  els.copyText.disabled = true;
  els.downloadText.disabled = true;
}

async function loadModelInfo() {
  try {
    const response = await fetch("/api/model");
    const info = await response.json();
    els.modelStatus.textContent = `${info.device.toUpperCase()} ready on first request`;
    els.pulse.classList.add("is-ready");
  } catch (_error) {
    els.modelStatus.textContent = "Model status unavailable";
  }
}

function switchTab(name) {
  els.tabs.forEach((tab) => tab.classList.toggle("is-active", tab.dataset.tab === name));
  els.panels.forEach((panel) => panel.classList.toggle("is-active", panel.id === `tab-${name}`));
}

async function listMicrophones() {
  if (!navigator.mediaDevices?.enumerateDevices) {
    els.micSelect.innerHTML = '<option value="">Browser microphone unavailable</option>';
    return;
  }
  const devices = await navigator.mediaDevices.enumerateDevices();
  const microphones = devices.filter((device) => device.kind === "audioinput");
  els.micSelect.innerHTML = "";
  microphones.forEach((device, index) => {
    const option = document.createElement("option");
    option.value = device.deviceId;
    option.textContent = device.label || `Microphone ${index + 1}`;
    els.micSelect.appendChild(option);
  });
  if (!microphones.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No microphone found";
    els.micSelect.appendChild(option);
  }
}

function drawWaveform() {
  const canvas = els.waveform;
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, width, height);
  context.strokeStyle = "#d8dee6";
  context.beginPath();
  context.moveTo(0, height / 2);
  context.lineTo(width, height / 2);
  context.stroke();

  if (!analyser) {
    return;
  }

  const data = new Uint8Array(analyser.fftSize);
  analyser.getByteTimeDomainData(data);
  context.lineWidth = 2;
  context.strokeStyle = "#14766f";
  context.beginPath();
  const sliceWidth = width / data.length;
  let peak = 0;
  data.forEach((value, index) => {
    const centered = (value - 128) / 128;
    peak = Math.max(peak, Math.abs(centered));
    const x = index * sliceWidth;
    const y = (value / 255) * height;
    if (index === 0) {
      context.moveTo(x, y);
    } else {
      context.lineTo(x, y);
    }
  });
  context.stroke();

  const level = Math.min(100, Math.round(peak * 140));
  els.meterFill.style.width = `${level}%`;
  els.meterFill.classList.toggle("is-hot", level > 85);
  if (recording) {
    els.levelHint.textContent = level < 8 ? "Low input" : level > 85 ? "Input is hot" : "Input level good";
  }

  animationFrame = requestAnimationFrame(drawWaveform);
}

function startTimer() {
  recordingStartedAt = Date.now();
  timerInterval = setInterval(() => {
    const elapsed = Math.floor((Date.now() - recordingStartedAt) / 1000);
    const minutes = String(Math.floor(elapsed / 60)).padStart(2, "0");
    const seconds = String(elapsed % 60).padStart(2, "0");
    els.timer.textContent = `${minutes}:${seconds}`;
  }, 250);
}

async function startRecording() {
  try {
    const selectedMic = els.micSelect.value;
    const constraints = {
      audio: {
        deviceId: selectedMic ? { exact: selectedMic } : undefined,
        channelCount: { ideal: 1 },
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    };

    mediaStream = await navigator.mediaDevices.getUserMedia(constraints);
    await listMicrophones();
    audioContext = new AudioContext();
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 2048;
    processor = audioContext.createScriptProcessor(4096, 1, 1);
    silentGain = audioContext.createGain();
    silentGain.gain.value = 0;
    sourceNode = audioContext.createMediaStreamSource(mediaStream);

    sourceNode.connect(analyser);
    sourceNode.connect(processor);
    processor.connect(silentGain);
    silentGain.connect(audioContext.destination);

    audioChunks = [];
    recordedBlob = null;
    recording = true;
    els.startRecording.disabled = true;
    els.stopRecording.disabled = false;
    els.sendRecording.disabled = true;
    els.recordingPreview.hidden = true;
    els.levelHint.textContent = "Recording";

    processor.onaudioprocess = (event) => {
      if (!recording) return;
      const input = event.inputBuffer.getChannelData(0);
      audioChunks.push(new Float32Array(input));
    };

    startTimer();
    drawWaveform();
  } catch (error) {
    showToast(error.message || "Microphone could not start.");
  }
}

function stopRecording() {
  recording = false;
  clearInterval(timerInterval);
  cancelAnimationFrame(animationFrame);

  if (processor) processor.disconnect();
  if (sourceNode) sourceNode.disconnect();
  if (silentGain) silentGain.disconnect();
  if (mediaStream) mediaStream.getTracks().forEach((track) => track.stop());

  const sampleRate = audioContext?.sampleRate || 48000;
  if (audioContext) audioContext.close();
  audioContext = null;
  analyser = null;

  const merged = mergeBuffers(audioChunks);
  const resampled = resampleBuffer(merged, sampleRate, TARGET_SAMPLE_RATE);
  recordedBlob = encodeWav(resampled, TARGET_SAMPLE_RATE);

  els.startRecording.disabled = false;
  els.stopRecording.disabled = true;
  els.sendRecording.disabled = recordedBlob.size < 1000;
  els.levelHint.textContent = recordedBlob.size < 1000 ? "No speech captured" : "Recording ready";
  els.meterFill.style.width = "0%";

  if (recordedBlob.size >= 1000) {
    els.recordingPreview.src = URL.createObjectURL(recordedBlob);
    els.recordingPreview.hidden = false;
  }
}

function mergeBuffers(chunks) {
  const length = chunks.reduce((total, chunk) => total + chunk.length, 0);
  const result = new Float32Array(length);
  let offset = 0;
  chunks.forEach((chunk) => {
    result.set(chunk, offset);
    offset += chunk.length;
  });
  return result;
}

function resampleBuffer(buffer, sourceRate, targetRate) {
  if (sourceRate === targetRate) return buffer;
  const ratio = sourceRate / targetRate;
  const newLength = Math.round(buffer.length / ratio);
  const result = new Float32Array(newLength);
  for (let i = 0; i < newLength; i += 1) {
    const sourceIndex = i * ratio;
    const before = Math.floor(sourceIndex);
    const after = Math.min(before + 1, buffer.length - 1);
    const weight = sourceIndex - before;
    result[i] = buffer[before] * (1 - weight) + buffer[after] * weight;
  }
  return result;
}

function encodeWav(samples, sampleRate) {
  const bytesPerSample = 2;
  const blockAlign = bytesPerSample;
  const buffer = new ArrayBuffer(44 + samples.length * bytesPerSample);
  const view = new DataView(buffer);
  writeString(view, 0, "RIFF");
  view.setUint32(4, 36 + samples.length * bytesPerSample, true);
  writeString(view, 8, "WAVE");
  writeString(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * blockAlign, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, 16, true);
  writeString(view, 36, "data");
  view.setUint32(40, samples.length * bytesPerSample, true);
  floatTo16BitPCM(view, 44, samples);
  return new Blob([view], { type: "audio/wav" });
}

function writeString(view, offset, text) {
  for (let i = 0; i < text.length; i += 1) {
    view.setUint8(offset + i, text.charCodeAt(i));
  }
}

function floatTo16BitPCM(view, offset, input) {
  for (let i = 0; i < input.length; i += 1, offset += 2) {
    const sample = Math.max(-1, Math.min(1, input[i]));
    view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
}

async function sendAudio(fileOrBlob, filename) {
  const form = new FormData();
  form.append("audio", fileOrBlob, filename);
  setBusy(true, "Transcribing");
  try {
    const response = await fetch("/api/transcribe", { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Transcription failed.");
    const meta = `${payload.duration_seconds}s / ${payload.sample_rate / 1000} kHz / ${payload.channels}`;
    setTranscript(payload.text, meta);
  } catch (error) {
    showToast(error.message);
    els.resultMeta.textContent = "Error";
  } finally {
    setBusy(false);
  }
}

function setUploadFile(file) {
  uploadFile = file;
  els.dropzoneText.textContent = file ? file.name : "Choose audio";
  els.sendUpload.disabled = !file;
  if (file) {
    els.uploadPreview.src = URL.createObjectURL(file);
    els.uploadPreview.hidden = false;
  }
}

async function loadSamples() {
  try {
    const response = await fetch("/api/samples");
    const payload = await response.json();
    els.sampleGrid.innerHTML = "";
    payload.samples.forEach((sample) => {
      const card = document.createElement("article");
      card.className = "sample-card";
      const title = document.createElement("strong");
      title.textContent = sample.filename;
      const reference = document.createElement("p");
      reference.textContent = sample.reference || "Reference text unavailable";
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.src = sample.url;
      const button = document.createElement("button");
      button.type = "button";
      button.innerHTML = '<i data-lucide="send"></i> Transcribe';
      button.addEventListener("click", () => transcribeSample(sample.filename));
      card.append(title, reference, audio, button);
      els.sampleGrid.appendChild(card);
    });
    if (window.lucide) window.lucide.createIcons();
  } catch (_error) {
    els.sampleGrid.textContent = "Samples unavailable.";
  }
}

async function transcribeSample(filename) {
  setBusy(true, "Transcribing sample");
  try {
    const response = await fetch(`/api/transcribe-sample/${encodeURIComponent(filename)}`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Sample transcription failed.");
    const meta = `${payload.filename} / ${payload.duration_seconds}s`;
    setTranscript(payload.text, meta);
  } catch (error) {
    showToast(error.message);
    els.resultMeta.textContent = "Error";
  } finally {
    setBusy(false);
  }
}

function downloadTranscript() {
  if (!lastTranscript) return;
  const blob = new Blob([lastTranscript], { type: "text/plain;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "wolof-transcript.txt";
  link.click();
  URL.revokeObjectURL(link.href);
}

els.tabs.forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));
els.refreshMics.addEventListener("click", listMicrophones);
els.startRecording.addEventListener("click", startRecording);
els.stopRecording.addEventListener("click", stopRecording);
els.sendRecording.addEventListener("click", () => {
  if (recordedBlob) sendAudio(recordedBlob, "microphone-16khz-mono.wav");
});

els.fileInput.addEventListener("change", (event) => setUploadFile(event.target.files[0]));
els.sendUpload.addEventListener("click", () => {
  if (uploadFile) sendAudio(uploadFile, uploadFile.name);
});

["dragenter", "dragover"].forEach((eventName) => {
  els.dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    els.dropzone.classList.add("is-dragging");
  });
});

["dragleave", "drop"].forEach((eventName) => {
  els.dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    els.dropzone.classList.remove("is-dragging");
  });
});

els.dropzone.addEventListener("drop", (event) => {
  const file = event.dataTransfer.files[0];
  if (file) setUploadFile(file);
});

els.copyText.addEventListener("click", async () => {
  await navigator.clipboard.writeText(lastTranscript);
  showToast("Transcript copied.");
});
els.downloadText.addEventListener("click", downloadTranscript);
els.clearText.addEventListener("click", () => {
  resetTranscript();
});

resetTranscript();
loadModelInfo();
listMicrophones();
loadSamples();
if (window.lucide) window.lucide.createIcons();
