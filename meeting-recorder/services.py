"""Storage, transcription and report generation for Kikao."""

from __future__ import annotations

import json
import re
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid
from dataclasses import dataclass
from pathlib import Path

import requests

import exports

LANGUAGES = {"en": "English", "sw": "Kiswahili"}
REPORT_LANGUAGES = {"same": "Same as meeting", "en": "English", "sw": "Kiswahili"}
MAX_MINUTES = int(os.environ.get("MAX_RECORDING_MINUTES", "60"))
SEGMENT_SECONDS = int(os.environ.get("SEGMENT_SECONDS", "600"))  # 10-minute pieces stay far below 25 MB
# How many pieces are sent to Whisper at once. 6 covers a full hour in a single round.
TRANSCRIBE_PARALLEL = int(os.environ.get("TRANSCRIBE_PARALLEL", "6"))

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")

DEFAULT_TEMPLATE = """# {{title}}

**Date:** {{date}}
**Meeting language:** {{language}}

## Participants
List the people who spoke or were mentioned as attending. Use names when they are said; otherwise describe them (e.g. "Speaker from finance team").

## Summary
Three to five sentences covering the purpose of the meeting and its outcome.

## Key discussion points
One short heading per topic, each followed by the main arguments, figures and concerns raised.

## Decisions
A bullet list of what was agreed. Write "No decisions recorded" if none.

## Action items
| Action | Owner | Due date |
|---|---|---|
Fill one row per task. Use "Unassigned" or "Not set" when the meeting did not say.

## Open questions and risks
Anything left unresolved or flagged as a risk.

## Next steps
The next meeting date or follow-up, if mentioned.
"""


# --------------------------------------------------------------------------- storage


def _replace_with_retry(src: Path, dst: Path, attempts: int = 20) -> None:
    """os.replace, retried briefly: antivirus or the Windows search indexer can hold a file for a moment."""
    for i in range(attempts):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


class Store:
    """One folder per session: meta.json, audio.webm, transcript.json, report.md."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # template ---------------------------------------------------------------
    @property
    def template_path(self) -> Path:
        return self.root / "report_template.md"

    def get_template(self) -> str:
        if self.template_path.exists():
            return self.template_path.read_text(encoding="utf-8")
        return DEFAULT_TEMPLATE

    def set_template(self, text: str | None) -> str:
        if not text or not text.strip():
            self.template_path.unlink(missing_ok=True)
            return DEFAULT_TEMPLATE
        self.template_path.write_text(text, encoding="utf-8")
        return text

    # settings: vocabulary, report instructions, fixed header and footer ----------
    SETTING_KEYS = ("vocabulary", "instructions", "header", "footer")

    @property
    def settings_path(self) -> Path:
        return self.root / "settings.json"

    def get_settings(self) -> dict:
        data = {}
        if self.settings_path.exists():
            try:
                data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        return {k: str(data.get(k, "")) for k in self.SETTING_KEYS}

    def set_settings(self, values: dict) -> dict:
        current = self.get_settings()
        for k in self.SETTING_KEYS:
            if k in values:
                current[k] = str(values[k] or "").strip()[:5000]
        tmp = self.settings_path.with_name(f"settings.{threading.get_ident()}.tmp")
        with self._lock:
            tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            _replace_with_retry(tmp, self.settings_path)
        return current

    # sessions -----------------------------------------------------------------
    def dir(self, sid: str) -> Path:
        if not sid or not all(c in "0123456789abcdef" for c in sid) or len(sid) != 12:
            raise KeyError(sid)
        return self.sessions / sid

    def create(self, title: str, language: str, report_language: str, source: str) -> dict:
        if language not in LANGUAGES:
            raise ValueError("Choose English or Kiswahili as the meeting language.")
        if report_language not in REPORT_LANGUAGES:
            raise ValueError("Unknown report language.")
        sid = uuid.uuid4().hex[:12]
        now = time.time()
        meta = {
            "id": sid,
            "title": (title or "").strip()[:140] or "Untitled meeting",
            "language": language,
            "report_language": report_language,
            "source": source,
            "status": "recording" if source == "live" else "queued",
            "stage": "",
            "progress": 0.0,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "duration_seconds": None,
            "chunks": 0,
            "audio_bytes": 0,
            "audio_ext": ".webm",
            "model": None,
        }
        self.dir(sid).mkdir(parents=True)
        self.save(meta)
        return meta

    # Reads and writes of meta.json share one lock. On Windows a file cannot be replaced
    # while another thread has it open, so a status poll reading meta.json during a
    # progress update used to fail the whole job with "[WinError 5] Access is denied".
    def load(self, sid: str) -> dict:
        path = self.dir(sid) / "meta.json"
        with self._lock:
            if not path.exists():
                raise KeyError(sid)
            return json.loads(path.read_text(encoding="utf-8"))

    def save(self, meta: dict) -> dict:
        meta["updated_at"] = time.time()
        path = self.dir(meta["id"]) / "meta.json"
        tmp = path.with_name(f"meta.{threading.get_ident()}.tmp")
        with self._lock:
            tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            _replace_with_retry(tmp, path)
        return meta

    def update(self, sid: str, **fields) -> dict:
        with self._lock:
            meta = self.load(sid)
            meta.update(fields)
            return self.save(meta)

    def list(self) -> list[dict]:
        items = []
        with self._lock:
            for path in self.sessions.glob("*/meta.json"):
                try:
                    items.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        return sorted(items, key=lambda m: m["created_at"], reverse=True)

    def delete(self, sid: str) -> None:
        shutil.rmtree(self.dir(sid), ignore_errors=True)

    def audio_path(self, sid: str) -> Path:
        meta = self.load(sid)
        return self.dir(sid) / f"audio{meta.get('audio_ext', '.webm')}"

    def append_chunk(self, sid: str, seq: int, data: bytes) -> dict:
        """Append MediaRecorder chunks in order. Re-sent chunks are ignored."""
        with self._lock:
            meta = self.load(sid)
            if meta["status"] != "recording":
                raise ValueError("This session is no longer recording.")
            if seq < meta["chunks"]:
                return meta  # duplicate retry, already stored
            if seq > meta["chunks"]:
                raise ValueError(f"Expected chunk {meta['chunks']}, received {seq}.")
            with open(self.dir(sid) / "audio.webm", "ab") as fh:
                fh.write(data)
            meta["chunks"] += 1
            meta["audio_bytes"] += len(data)
            return self.save(meta)

    # Transcript, report and slides are written to a temp file and swapped in under the
    # lock, so a status poll never reads a half-written file.
    def read_text(self, sid: str, name: str) -> str | None:
        path = self.dir(sid) / name
        with self._lock:
            return path.read_text(encoding="utf-8") if path.exists() else None

    def write_text(self, sid: str, name: str, text: str) -> None:
        path = self.dir(sid) / name
        tmp = path.with_name(f"{name}.{threading.get_ident()}.tmp")
        with self._lock:
            tmp.write_text(text, encoding="utf-8")
            _replace_with_retry(tmp, path)

    def read_json(self, sid: str, name: str):
        text = self.read_text(sid, name)
        return json.loads(text) if text is not None else None

    def write_json(self, sid: str, name: str, data) -> None:
        self.write_text(sid, name, json.dumps(data, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- audio

_FFMPEG = None


def ffmpeg() -> str:
    """ffmpeg on PATH if installed, otherwise the copy bundled with imageio-ffmpeg."""
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = shutil.which("ffmpeg")
        if not _FFMPEG:
            try:
                import imageio_ffmpeg
                _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                raise RuntimeError("ffmpeg was not found. Run: pip install -r requirements.txt")
    return _FFMPEG


def probe_duration(path: Path) -> float:
    """Duration in seconds. Decodes the file because chunked WebM has no duration header."""
    out = subprocess.run(
        [ffmpeg(), "-nostdin", "-v", "error", "-i", str(path), "-f", "null", "-", "-stats_period", "3600", "-progress", "pipe:1"],
        capture_output=True, text=True,
    )
    seconds = 0.0
    for line in out.stdout.splitlines():
        if line.startswith("out_time_us="):
            try:
                seconds = max(seconds, int(line.split("=", 1)[1]) / 1_000_000)
            except ValueError:
                pass
    if out.returncode != 0 and seconds == 0:
        raise RuntimeError("Could not read the audio. The recording may be empty or damaged.")
    return seconds


def split_audio(path: Path, workdir: Path, segment_seconds: int | None = None,
                max_seconds: int | None = None) -> list[Path]:
    """Convert to 16 kHz mono MP3, keep at most max_seconds, and cut into pieces Whisper accepts (< 25 MB each)."""
    segment_seconds = segment_seconds or SEGMENT_SECONDS
    max_seconds = max_seconds or MAX_MINUTES * 60
    pattern = workdir / "part_%03d.mp3"
    cmd = [
        ffmpeg(), "-nostdin", "-v", "error", "-y", "-i", str(path), "-t", str(max_seconds),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k",
        "-f", "segment", "-segment_time", str(segment_seconds), "-reset_timestamps", "1",
        str(pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Audio conversion failed: {result.stderr.strip()[:300]}")
    parts = sorted(workdir.glob("part_*.mp3"))
    if not parts:
        raise RuntimeError("The recording contains no audio.")
    return parts


# --------------------------------------------------------------------------- transcription


@dataclass
class Segment:
    start: float
    end: float
    text: str


class OpenAITranscriber:
    """Any OpenAI-compatible /audio/transcriptions API: OpenAI itself, or Groq (open-source Whisper large-v3)."""

    parallel = True
    PROVIDERS = {
        "openai": {"label": "OpenAI", "key": "OPENAI_API_KEY", "model_env": "OPENAI_TRANSCRIBE_MODEL",
                   "model": "whisper-1", "url": lambda: OPENAI_BASE_URL},
        "groq": {"label": "Groq", "key": "GROQ_API_KEY", "model_env": "GROQ_TRANSCRIBE_MODEL",
                 "model": "whisper-large-v3",
                 "url": lambda: os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")},
    }

    def __init__(self, provider: str = "openai"):
        cfg = self.PROVIDERS[provider]
        self.name = provider
        self.label = cfg["label"]
        self.key_name = cfg["key"]
        self.api_key = os.environ.get(cfg["key"], "")
        self.model = os.environ.get(cfg["model_env"], cfg["model"])
        self.base_url = cfg["url"]()

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def transcribe(self, path: Path, language: str, prompt: str | None = None) -> list[Segment]:
        if not self.api_key:
            raise RuntimeError(f"{self.key_name} is not set. Add it to meeting-recorder/.env.")
        verbose = "whisper" in self.model  # Whisper models return timestamped segments
        data = {"model": self.model, "language": language, "temperature": "0",
                "response_format": "verbose_json" if verbose else "json"}
        if prompt:
            data["prompt"] = prompt
        for attempt in range(6):
            with open(path, "rb") as fh:
                resp = requests.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data=data, files={"file": (path.name, fh, "audio/mpeg")}, timeout=600,
                )
            retryable = resp.status_code in (500, 502, 503, 504) or (
                resp.status_code == 429 and "credit" not in resp.text.lower() and "quota" not in resp.text.lower())
            if retryable and attempt < 5:
                wait = resp.headers.get("retry-after")
                time.sleep(min(60, float(wait)) if wait and wait.replace(".", "").isdigit() else 2 ** attempt * 3)
                continue
            if not resp.ok:
                raise RuntimeError(f"{self.label} transcription failed ({resp.status_code}): {_error_text(resp)}")
            body = resp.json()
            if verbose and body.get("segments"):
                return [Segment(s["start"], s["end"], s["text"].strip()) for s in body["segments"]]
            return [Segment(0.0, 0.0, (body.get("text") or "").strip())]
        raise RuntimeError(f"{self.label} transcription kept failing. Try again later.")


class LocalTranscriber:
    """faster-whisper on this machine. Slow on CPU: budget roughly real time with 'small'."""

    name = "local"
    label = "this computer"
    key_name = None
    parallel = False  # one model in memory; pieces run one after another

    def __init__(self):
        self.model_name = os.environ.get("LOCAL_WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
        self._model = None
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def transcribe(self, path: Path, language: str, prompt: str | None = None) -> list[Segment]:
        from faster_whisper import WhisperModel

        with self._lock:
            if self._model is None:
                self._model = WhisperModel(self.model_name, device="auto", compute_type="int8")
            segments, _ = self._model.transcribe(str(path), language=language, vad_filter=True,
                                                   initial_prompt=prompt)
            return [Segment(s.start, s.end, s.text.strip()) for s in segments]


def make_transcriber():
    """TRANSCRIBER=groq|openai|local. Unset: Groq when GROQ_API_KEY is present, else OpenAI."""
    choice = os.environ.get("TRANSCRIBER", "").strip().lower()
    if not choice:
        choice = "groq" if os.environ.get("GROQ_API_KEY") else "openai"
    if choice == "local":
        return LocalTranscriber()
    return OpenAITranscriber(choice if choice in OpenAITranscriber.PROVIDERS else "openai")


# --------------------------------------------------------------------------- report


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def transcript_to_text(segments: list[dict], timestamps: bool = True) -> str:
    lines = []
    for s in segments:
        if not s["text"]:
            continue
        lines.append(f"[{fmt_ts(s['start'])}] {s['text']}" if timestamps else s["text"])
    return "\n".join(lines)


def vocabulary_terms(text: str) -> list[str]:
    """One term per line or comma-separated; duplicates removed, order kept."""
    seen, terms = set(), []
    for raw in re.split(r"[\n,;]+", text or ""):
        term = raw.strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    return terms


def whisper_prompt(terms: list[str]) -> str | None:
    """Whisper reads only about 224 tokens of prompt, so keep the list short."""
    if not terms:
        return None
    out = ""
    for t in terms:
        nxt = f"{out}, {t}" if out else t
        if len(nxt) > 600:
            break
        out = nxt
    return out + "."


def fill_placeholders(text: str, meta: dict) -> str:
    date = time.strftime("%d %B %Y", time.localtime(meta["created_at"]))
    return (text.replace("{{title}}", meta["title"]).replace("{{date}}", date)
            .replace("{{language}}", LANGUAGES[meta["language"]]))


def build_messages(meta: dict, template: str, transcript: str, settings: dict | None = None) -> list[dict]:
    settings = settings or {}
    lang = meta["language"] if meta["report_language"] == "same" else meta["report_language"]
    report_lang = LANGUAGES[lang]
    date = time.strftime("%d %B %Y", time.localtime(meta["created_at"]))
    filled = (template.replace("{{title}}", meta["title"])
              .replace("{{date}}", date)
              .replace("{{language}}", LANGUAGES[meta["language"]]))
    system = (
        "You write accurate meeting reports from raw speech-to-text transcripts. "
        "Follow the report template exactly: keep its headings, order and tables, and replace the guidance "
        "text under each heading with content from the meeting. Use only facts stated in the transcript; "
        "never invent names, numbers, dates or decisions. When something is unclear, say so briefly. "
        "Transcripts contain recognition errors: correct obvious ones from context. "
        f"Write the entire report in {report_lang}. Output Markdown only, without code fences."
    )
    terms = vocabulary_terms(settings.get("vocabulary", ""))
    if terms:
        system += (" The vocabulary list gives the correct spelling of names and terms used in this "
                   "organisation; when the transcript has something that sounds like one of them, use that spelling.")
    if settings.get("instructions"):
        system += f"\n\nAdditional instructions from the user:\n{settings['instructions']}"
    user = (
        f"Meeting title: {meta['title']}\nDate: {date}\n"
        f"Spoken language: {LANGUAGES[meta['language']]}\n\n"
        + (f"VOCABULARY (correct spellings)\n{', '.join(terms)}\n\n" if terms else "")
        + f"REPORT TEMPLATE\n---\n{filled}\n---\n\n"
        f"TRANSCRIPT (timestamps are mm:ss from the start of the recording)\n---\n{transcript}\n---"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_slide_messages(meta: dict, report_md: str, settings: dict | None = None) -> list[dict]:
    settings = settings or {}
    lang = meta["language"] if meta["report_language"] == "same" else meta["report_language"]
    system = (
        "You turn meeting reports into a short slide deck for a busy reader who will skim it in two minutes. "
        "Return JSON only, no prose and no code fences, with this shape: "
        '{"title": str, "subtitle": str, "slides": [{"title": str, "lead": str, "bullets": [str], '
        '"table": {"columns": [str], "rows": [[str]]}}]}. '
        "Make 4 to 8 slides in this order where the report has content: at a glance (lead = the one-sentence outcome), "
        "key points, decisions, action items (as a table with columns Action, Owner, Due), risks or open questions, "
        "next steps. Each bullet is under 14 words and starts with the important word. Leave out empty sections. "
        "Use only facts from the report. "
        f"Write every slide in {LANGUAGES[lang]}."
    )
    if settings.get("instructions"):
        system += f"\n\nAdditional instructions from the user:\n{settings['instructions']}"
    user = f"Meeting title: {meta['title']}\n\nREPORT\n---\n{report_md}\n---"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def deck_subtitle(meta: dict) -> str:
    date = time.strftime("%d %B %Y", time.localtime(meta["created_at"]))
    parts = [date, LANGUAGES[meta["language"]]]
    if meta.get("duration_seconds"):
        parts.append(f"{max(1, round(meta['duration_seconds'] / 60))} min meeting")
    return ", ".join(parts)


class OpenRouterWriter:
    def __init__(self):
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.model = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def write(self, messages: list[dict], model: str | None = None) -> tuple[str, str]:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set. Add it to meeting-recorder/.env.")
        model = model or self.model
        for attempt in range(3):
            resp = requests.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "Kikao meeting reports",
                },
                json={"model": model, "messages": messages, "temperature": 0.2},
                timeout=300,
            )
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt * 4)
                continue
            if not resp.ok:
                raise RuntimeError(f"OpenRouter request failed ({resp.status_code}): {_error_text(resp)}")
            body = resp.json()
            try:
                text = body["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError, AttributeError):
                raise RuntimeError(f"OpenRouter returned no report: {str(body)[:300]}")
            if text.startswith("```"):
                text = text.strip("`").removeprefix("markdown").strip()
            return text, body.get("model", model)
        raise RuntimeError("OpenRouter kept failing. Try again later.")


def _error_text(resp) -> str:
    try:
        err = resp.json().get("error")
        return (err.get("message") if isinstance(err, dict) else str(err)) or resp.text[:300]
    except ValueError:
        return resp.text[:300]


# --------------------------------------------------------------------------- jobs


class Processor:
    """Runs transcription + report in a background thread, one job at a time."""

    def __init__(self, store: Store, transcriber=None, writer=None):
        self.store = store
        self.transcriber = transcriber or make_transcriber()
        self.writer = writer or OpenRouterWriter()
        self._queue_lock = threading.Lock()

    def submit(self, sid: str, transcribe: bool = True, model: str | None = None) -> None:
        self.store.update(sid, status="queued", stage="Waiting to start", error=None, progress=0.0)
        threading.Thread(target=self._run, args=(sid, transcribe, model), daemon=True).start()

    def _run(self, sid: str, transcribe: bool, model: str | None) -> None:
        with self._queue_lock:
            try:
                if transcribe:
                    self._transcribe(sid)
                self._report(sid, model)
                self.store.update(sid, status="done", stage="Report ready", progress=1.0)
            except Exception as exc:  # surfaced to the UI
                self.store.update(sid, status="error", error=str(exc), stage="Stopped")

    def _transcribe(self, sid: str) -> None:
        meta = self.store.load(sid)
        audio = self.store.audio_path(sid)
        if not audio.exists() or audio.stat().st_size == 0:
            raise RuntimeError("No audio was recorded for this meeting.")
        self.store.update(sid, status="transcribing", stage="Preparing audio", progress=0.02)
        # Uploads were measured when they arrived; live recordings are measured here.
        # Anything past the limit is trimmed rather than rejected.
        duration = meta.get("duration_seconds") or probe_duration(audio)
        duration = min(duration, MAX_MINUTES * 60)
        self.store.update(sid, duration_seconds=round(duration, 1))
        seg_len = SEGMENT_SECONDS

        with tempfile.TemporaryDirectory() as tmp:
            parts = split_audio(audio, Path(tmp), seg_len, MAX_MINUTES * 60)
            # Encoder padding can leave a sliver past the real end; Whisper tends to invent
            # text for near-silent clips, so keep only pieces that hold at least a second.
            needed = max(1, math.ceil((duration - 1) / seg_len))
            parts = parts[:needed]
            total = len(parts)
            results: dict[int, list] = {}
            workers = min(TRANSCRIBE_PARALLEL, total) if getattr(self.transcriber, "parallel", False) else 1

            def label(done):
                return (f"Transcribing {total} parts at once ({done} of {total} done)" if workers > 1
                        else f"Transcribing part {min(done + 1, total)} of {total}")

            self.store.update(sid, stage=label(0), progress=0.08)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                prompt = whisper_prompt(vocabulary_terms(self.store.get_settings()["vocabulary"]))
                futures = {pool.submit(self.transcriber.transcribe, part, meta["language"], prompt): i
                           for i, part in enumerate(parts)}
                for fut in as_completed(futures):
                    results[futures[fut]] = fut.result()  # re-raises the first failure
                    done = len(results)
                    self.store.update(sid, stage=label(done), progress=0.08 + 0.8 * done / total)

        segments: list[dict] = []
        for i in range(total):
            offset = i * seg_len
            for seg in results[i]:
                start = offset + seg.start
                end = offset + (seg.end or (seg_len if seg.start == 0 else seg.start))
                segments.append({"start": round(start, 2), "end": round(end, 2), "text": seg.text})
        if not any(s["text"] for s in segments):
            raise RuntimeError("Whisper heard no speech. Check that meeting audio was shared.")
        self.store.write_json(sid, "transcript.json", segments)
        self.store.write_text(sid, "transcript.txt", transcript_to_text(segments))

    def _report(self, sid: str, model: str | None) -> None:
        meta = self.store.load(sid)
        segments = self.store.read_json(sid, "transcript.json")
        if not segments:
            raise RuntimeError("There is no transcript to summarise yet.")
        self.store.update(sid, status="writing", stage="Writing the report", progress=0.9)
        settings = self.store.get_settings()
        messages = build_messages(meta, self.store.get_template(), transcript_to_text(segments), settings)
        report, used = self.writer.write(messages, model)
        # Fixed text goes in word for word, not through the model.
        if settings["header"]:
            report = f"{fill_placeholders(settings['header'], meta)}\n\n{report}"
        if settings["footer"]:
            report = f"{report}\n\n---\n\n{fill_placeholders(settings['footer'], meta)}"
        self.store.write_text(sid, "report.md", report)
        self.store.update(sid, model=used, stage="Making the slides", progress=0.96)
        self.make_slides(sid, model)

    def make_slides(self, sid: str, model: str | None = None) -> dict:
        """Skim deck from the saved report. Falls back to the report's own sections, so it never fails the job."""
        meta = self.store.load(sid)
        report = self.store.read_text(sid, "report.md") or ""
        subtitle = deck_subtitle(meta)
        deck = None
        try:
            text, _ = self.writer.write(build_slide_messages(meta, report, self.store.get_settings()), model)
            deck = exports.normalise_deck(exports.parse_json_block(text), meta["title"], subtitle)
        except Exception:
            deck = None
        if deck is None:
            deck = exports.slides_from_report(report, meta["title"], subtitle)
        self.store.write_json(sid, "slides.json", deck)
        return deck

    def recover(self) -> None:
        """Sessions interrupted by a restart are marked so the user can retry."""
        for meta in self.store.list():
            if meta["status"] in ("queued", "transcribing", "writing"):
                self.store.update(meta["id"], status="error", stage="Stopped",
                                  error="The server restarted while this meeting was processing. Press Retry.")
