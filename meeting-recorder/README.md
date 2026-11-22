# Kikao: meeting recorder and report writer

Kikao records a Microsoft Teams or Google Meet meeting from your browser for up to one hour. It transcribes the recording with OpenAI Whisper in **English or Kiswahili**, then has an LLM on OpenRouter turn the transcript into a report in your chosen format.

## How it works

```
Meeting tab audio ─┐
                   ├─ mixed in the browser ─► 15-second chunks ─► server (audio.webm)
Your microphone  ──┘                                                   │
                                     ffmpeg: 16 kHz mono, 10-minute pieces
                                                                       │
                          OpenAI Whisper, language forced to en or sw ─┤
                                                                       │
                         OpenRouter LLM + your report template ─► report.md
```

- **Capture.** Press *Share meeting tab and start*, pick the Meet or Teams tab, and keep *Also share tab audio* on. Your microphone is mixed in so your own voice is included. Kikao doesn't join the call as a bot, so nobody needs to admit anything.
- **Safe for long meetings.** Audio goes to the server every 15 seconds. If the tab crashes, everything up to that point is kept, and the meeting shows a *Process what was recorded* button.
- **One-hour limit.** Recording stops by itself at 60 minutes (`MAX_RECORDING_MINUTES`).
- **Languages.** Only `en` and `sw` are accepted, and Whisper is told the language explicitly instead of guessing. The report can be written in the meeting's language, in English, or in Kiswahili.
- **Report format.** Open *Report template* to edit the Markdown template. The model keeps your headings, order and tables. `{{title}}`, `{{date}}` and `{{language}}` are filled in for you. You can also choose the OpenRouter model there.
- **Outputs.** Rendered report, edit in place, copy as Markdown, download `.md`, save as PDF through the print dialog, timestamped transcript with click-to-play, `.txt` and audio downloads.
- **Uploads.** You can also upload an existing recording (MP3, M4A, WAV, WebM, MP4 and more).

## Keys you need

| Service | Used for | Variable |
|---|---|---|
| OpenAI | Whisper transcription (`whisper-1`) | `OPENAI_API_KEY` |
| OpenRouter | Writing the report | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` |

OpenRouter only provides chat models, not the Whisper speech-to-text endpoint, so transcription needs its own OpenAI key. To skip OpenAI, set `TRANSCRIBER=local` and `pip install faster-whisper`. Whisper then runs on your own machine. That is free but slow on CPU: expect roughly the length of the meeting with the `small` model. For good Kiswahili accuracy, use `LOCAL_WHISPER_MODEL=large-v3`, which needs a GPU.

## Run locally

```bash
cd meeting-recorder
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then paste your keys into .env
python app.py               # http://localhost:8000
```

You also need `ffmpeg` on your PATH (`winget install ffmpeg`, `brew install ffmpeg` or `apt install ffmpeg`).

Open **http://localhost:8000 in Chrome or Edge**. Tab-audio capture only works in Chromium browsers, and only on `localhost` or over HTTPS.

### Teams desktop app

The browser can capture audio only from one of its own tabs, or from the whole system on Windows. With the Teams desktop app, choose *Entire screen* and tick *Share system audio* (Windows only). On macOS, join Teams from the browser (teams.microsoft.com) instead.

## Deploy

```bash
docker build -t kikao meeting-recorder
docker run -p 8000:8000 --env-file meeting-recorder/.env -v kikao-data:/home/user/app/data kikao
```

Put it behind HTTPS, which screen capture requires. Keep a single gunicorn worker, because jobs run in a background thread inside it. There is no login, so don't expose it publicly without adding authentication in front of it.

## Tests

```bash
pip install pytest
python -m pytest -q
```

The tests stub the OpenAI and OpenRouter calls and cover several cases: chunk ordering and retries, ffmpeg splitting with timestamp offsets, the English/Kiswahili restriction, the prompt contents, custom templates, report-only retries and error states.

## Privacy

Recordings, transcripts and reports are stored under `data/` on the server. Audio is sent to OpenAI for transcription and the transcript to OpenRouter for the report. Tell participants that the meeting is being recorded, and follow your organisation's rules.
