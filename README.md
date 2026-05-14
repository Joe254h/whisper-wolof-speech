---
title: Wolof Whisper Speech
colorFrom: green
colorTo: yellow
sdk: docker
app_port: 7860
suggested_hardware: cpu-upgrade
tags:
  - whisper
  - speech-recognition
  - wolof
  - flask
---

# Wolof Whisper Speech

This project is a small Flask web app around a fine-tuned Whisper model for Wolof speech transcription. It keeps the original model files in `final-whisper-wolof`, includes a few WAV samples in `data`, and adds a browser interface for recording, uploading, previewing, and transcribing audio.

The model was fine-tuned from `openai/whisper-small` on Wolof speech data. The app normalizes audio to the format Whisper expects before sending it to the model.

## Audio format

Best input for the model:

- Format: `.wav`
- Also accepted by the app: `.mp3`, `.m4a`, `.flac`
- Sampling frequency: 16 kHz
- Audio type: mono waveform, single channel

The microphone tab records in the browser, applies common capture constraints such as noise suppression and echo cancellation, then exports a 16 kHz mono WAV file before uploading it to Flask.

## Project layout

- `app.py` - Flask routes and Whisper inference
- `templates/index.html` - main web page
- `static/css/styles.css` - app styling
- `static/js/app.js` - microphone recording, upload flow, samples, and transcript actions
- `final-whisper-wolof/` - local Hugging Face Transformers checkpoint
- `data/` - sample WAV files and reference text
- `Dockerfile` - Hugging Face Spaces deployment
- `requirements.txt` - Python dependencies

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python app.py
```

Open `http://localhost:7860`.

The first transcription takes longer because the model is loaded into memory on demand. After that, short clips should run faster.

## Run with Docker

```bash
docker build -t wolof-whisper .
docker run --rm -p 7860:7860 wolof-whisper
```

## Hugging Face Spaces

This repository is ready for a Docker Space. Hugging Face reads the YAML block at the top of this README, builds the Dockerfile, and exposes the Flask app on port `7860`.

For a Space under your profile, create or push to:

```bash
huggingface.co/spaces/joe254h/wolof-whisper-speech
```

The model checkpoint is tracked with Git LFS because `model.safetensors` is too large for normal Git hosting.

After logging in, the deployment commands are:

```bash
hf repo create spaces/joe254h/wolof-whisper-speech --type space --space-sdk docker --public --exist-ok
hf upload spaces/joe254h/wolof-whisper-speech . . --repo-type space --commit-message "Deploy Wolof Whisper Flask app"
```

For GitHub, create `joelnyongesa1/whisper-wolof-speech`, then run:

```bash
git remote add origin https://github.com/joelnyongesa1/whisper-wolof-speech.git
git push -u origin main
```

## Notes

The app is meant for short speech clips and demos. Very long recordings will work more slowly, especially on CPU hardware. If you move the model to a separate Hugging Face model repository later, set `MODEL_REPO_ID` in the Space settings and the Flask app will load from that model repo instead of the local folder.
