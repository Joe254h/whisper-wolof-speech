import os
import tempfile
import threading
from pathlib import Path

import librosa
import torch
from flask import Flask, jsonify, render_template, request, send_from_directory
from transformers import pipeline
from transformers.utils import logging as transformers_logging
from werkzeug.utils import secure_filename


os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TQDM_DISABLE", "1")
transformers_logging.disable_progress_bar()

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_PATH = BASE_DIR / "final-whisper-wolof"
DATA_DIR = BASE_DIR / "data"
SAMPLE_RATE = 16_000
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024

_pipe = None
_pipe_lock = threading.Lock()


def model_location() -> str:
    return os.environ.get("MODEL_REPO_ID") or os.environ.get("MODEL_PATH") or str(DEFAULT_MODEL_PATH)


def get_asr_pipeline():
    global _pipe
    if _pipe is None:
        with _pipe_lock:
            if _pipe is None:
                device = 0 if torch.cuda.is_available() else -1
                _pipe = pipeline(
                    "automatic-speech-recognition",
                    model=model_location(),
                    device=device,
                    model_kwargs={"low_cpu_mem_usage": True},
                )
    return _pipe


def allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def prepare_audio(path: Path):
    audio, _ = librosa.load(path, sr=SAMPLE_RATE, mono=True)
    duration = float(len(audio) / SAMPLE_RATE) if len(audio) else 0.0
    if duration < 0.15:
        raise ValueError("The audio is too short to transcribe clearly.")
    return audio, duration


def transcribe_audio(path: Path):
    audio, duration = prepare_audio(path)
    asr = get_asr_pipeline()
    result = asr({"array": audio, "sampling_rate": SAMPLE_RATE})
    return {
        "text": (result.get("text") or "").strip(),
        "duration_seconds": round(duration, 2),
        "sample_rate": SAMPLE_RATE,
        "channels": "mono",
    }


def sample_metadata():
    samples = {}
    data_file = DATA_DIR / "data.txt"
    if data_file.exists():
        current_name = None
        for raw_line in data_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("Saved :"):
                current_name = line.split(":", 1)[1].strip()
            elif line.startswith("Text :") and current_name:
                samples[current_name] = line.split(":", 1)[1].strip()
                current_name = None

    items = []
    for wav_path in sorted(DATA_DIR.glob("*.wav")):
        items.append(
            {
                "filename": wav_path.name,
                "reference": samples.get(wav_path.name, ""),
                "url": f"/samples/{wav_path.name}",
            }
        )
    return items


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "model_loaded": _pipe is not None})


@app.get("/api/model")
def model_info():
    return jsonify(
        {
            "model": model_location(),
            "loaded": _pipe is not None,
            "device": "cuda" if torch.cuda.is_available() else "cpu",
            "requirements": {
                "best_format": ".wav",
                "also_supported": [".mp3", ".m4a", ".flac"],
                "sampling_frequency": "16 kHz",
                "audio_type": "mono waveform",
            },
        }
    )


@app.get("/api/samples")
def samples():
    return jsonify({"samples": sample_metadata()})


@app.get("/samples/<path:filename>")
def serve_sample(filename):
    safe_name = secure_filename(filename)
    if safe_name != filename or not (DATA_DIR / safe_name).exists():
        return jsonify({"error": "Sample not found."}), 404
    return send_from_directory(DATA_DIR, safe_name, mimetype="audio/wav")


@app.post("/api/transcribe")
def transcribe_upload():
    uploaded = request.files.get("audio")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "No audio file was received."}), 400

    if not allowed_file(uploaded.filename):
        return jsonify({"error": "Please upload .wav, .mp3, .m4a, or .flac audio."}), 400

    suffix = Path(uploaded.filename).suffix.lower()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_path = Path(temp_file.name)
            uploaded.save(temp_file)

        payload = transcribe_audio(temp_path)
        payload["filename"] = secure_filename(uploaded.filename)
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


@app.post("/api/transcribe-sample/<path:filename>")
def transcribe_sample(filename):
    safe_name = secure_filename(filename)
    sample_path = DATA_DIR / safe_name
    if safe_name != filename or not sample_path.exists():
        return jsonify({"error": "Sample not found."}), 404

    try:
        payload = transcribe_audio(sample_path)
        payload["filename"] = safe_name
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.errorhandler(413)
def file_too_large(_error):
    return jsonify({"error": f"Audio must be smaller than {os.environ.get('MAX_UPLOAD_MB', '50')} MB."}), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
