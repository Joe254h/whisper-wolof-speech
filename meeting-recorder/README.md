# ICPAC Minutes: meeting recorder and report writer

ICPAC Minutes records a Microsoft Teams or Google Meet meeting from your browser for up to one hour. It transcribes the recording with OpenAI Whisper in **English or Kiswahili**, then has an LLM on OpenRouter turn the transcript into a report in your chosen format.

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

- **Capture.** Press *Share meeting tab and start*, pick the Meet or Teams tab, and keep *Also share tab audio* on. Your microphone is mixed in so your own voice is included. The app doesn't join the call as a bot, so nobody needs to admit anything.
- **Safe for long meetings.** Audio goes to the server every 15 seconds. If the tab crashes, everything up to that point is kept, and the meeting shows a *Process what was recorded* button.
- **One-hour limit.** Recording stops by itself at 60 minutes (`MAX_RECORDING_MINUTES`).
- **Languages.** Only `en` and `sw` are accepted, and Whisper is told the language explicitly instead of guessing. The report can be written in the meeting's language, in English, or in Kiswahili.
- **Report format.** Open *Report settings* to edit the Markdown template. The model keeps your headings, order and tables. `{{title}}`, `{{date}}` and `{{language}}` are filled in for you. You can also choose the OpenRouter model there.
- **Report.** Rendered in the app, editable in place, and rewritable from the transcript.
- **Slides for skimming.** After the report, the AI makes a short deck: at a glance, key points, decisions, an action-items table, risks and next steps. Flip through it in the app with the arrow keys or full screen, or download it. *Update from report* rebuilds it after you edit the report.
- **Downloads.**
  - Report: Word (`.docx`), PDF, web page (`.html`), Markdown and plain text.
  - Slides: PowerPoint (`.pptx`) and PDF.
  - Transcript: Word, text, subtitles (`.srt`, `.vtt`) and JSON.
  - The original audio.
  - PDFs come from the browser's print dialog: choose *Save as PDF*.
- **Report settings.**
  - AI model: Fast (the `.env` default), Smart (`google/gemini-2.5-pro`), Best writing (`anthropic/claude-sonnet-4.5`) or any OpenRouter model name. Saved on the server for every report.
  - Vocabulary: names and terms passed to Whisper as a spelling hint and to the report writer.
  - Template: your report format.
  - Instructions: extra guidance for the AI, for example tone or length.
  - Header and footer: fixed text added word for word to every report.
- **Uploads.** You can also upload an existing recording up to 60 minutes long and 1 GB in size (MP3, M4A, WAV, WebM, MP4 and more). A longer file is refused before it uploads.
- **Speed.** The audio is cut into 10-minute pieces and all of them go to Whisper at the same time, so an hour-long meeting takes about as long as one 10-minute piece. Change this with `TRANSCRIBE_PARALLEL`.

## Keys you need

Transcription needs **one** of these:

| Provider | Model | Cost per hour of audio | Variable |
|---|---|---|---|
| Groq (recommended) | open-source Whisper large-v3 | free tier, then about $0.11 | `GROQ_API_KEY` |
| OpenAI | whisper-1 | about $0.36 | `OPENAI_API_KEY` |
| This computer | faster-whisper | free, slow without a GPU | `TRANSCRIBER=local` |

Reports are written through OpenRouter (`OPENROUTER_API_KEY`, `OPENROUTER_MODEL`). An hour-long report with a Flash-class model costs a cent or two.

When `GROQ_API_KEY` is set and `TRANSCRIBER` is empty, the app uses Groq.

To run Whisper on your own machine instead, set `TRANSCRIBER=local` and `pip install faster-whisper`. The model is downloaded from Hugging Face on first use.

| `LOCAL_WHISPER_MODEL` | Download | Kiswahili quality | Speed on a laptop CPU |
|---|---|---|---|
| `deepdml/faster-whisper-large-v3-turbo-ct2` (default) | about 1.6 GB | good | about as long as the meeting, or longer |
| `small` | about 0.5 GB | weak | faster than the meeting |

With an NVIDIA GPU, turbo runs many times faster than real time.

## Run locally

```bash
cd meeting-recorder
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then paste your keys into .env
python app.py               # http://localhost:8000
```

ffmpeg comes with the Python packages (`imageio-ffmpeg`), so there's nothing extra to install. If ffmpeg is already on your PATH, that copy is used instead.

Open **http://localhost:8000 in Chrome or Edge**. Tab-audio capture only works in Chromium browsers, and only on `localhost` or over HTTPS.

### Teams desktop app

The browser can capture audio only from one of its own tabs, or from the whole system on Windows. With the Teams desktop app, choose *Entire screen* and tick *Share system audio* (Windows only). On macOS, join Teams from the browser (teams.microsoft.com) instead.

## Deploy

```bash
docker build -t icpac-minutes meeting-recorder
docker run -p 8000:8000 --env-file meeting-recorder/.env -v icpac-minutes-data:/home/user/app/data icpac-minutes
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
