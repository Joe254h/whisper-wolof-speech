"""End-to-end tests with the OpenAI and OpenRouter calls stubbed out.

Run from meeting-recorder/:  python -m pytest -q
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["KIKAO_NO_AUTOAPP"] = "1"
os.environ["SEGMENT_SECONDS"] = "120"  # small pieces so a short file still splits

import services  # noqa: E402
from app import create_app  # noqa: E402

services.SEGMENT_SECONDS = 120


class FakeTranscriber:
    name = "fake"
    ready = True
    model = "fake-whisper"

    def __init__(self):
        self.calls = []

    def transcribe(self, path, language, prompt=None):
        self.calls.append((Path(path).name, language))
        self.prompts = getattr(self, 'prompts', []) + [prompt]
        n = len(self.calls)
        return [services.Segment(0.0, 5.0, f"Part {n} opening ({language})."),
                services.Segment(60.0, 65.0, f"Part {n} decision: ship on Friday.")]


class FakeWriter:
    ready = True
    model = "fake/llm"
    slides_reply = "not json"  # falls back to slides built from the report

    def __init__(self, fail=False):
        self.messages = None
        self.fail = fail

    def write(self, messages, model=None):
        if self.fail:
            raise RuntimeError("OpenRouter request failed (401): bad key")
        if "slide deck" in messages[0]["content"]:
            self.slide_messages = messages
            return self.slides_reply, model or self.model
        self.messages = messages
        return "# Report\n\n## Decisions\n- Ship on Friday", model or self.model


def make_audio(path: Path, seconds: int, fmt_args=("-c:a", "libopus", "-b:a", "32k")):
    subprocess.run(
        [services.ffmpeg(), "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
         "-ac", "1", *fmt_args, str(path)],
        check=True,
    )


def wait_done(client, sid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/sessions/{sid}")
        assert r.status_code == 200, r.data[:300]
        s = r.get_json()
        if s["status"] in ("done", "error"):
            return s
        time.sleep(0.2)
    raise AssertionError("job did not finish")


@pytest.fixture()
def env(tmp_path):
    t, w = FakeTranscriber(), FakeWriter()
    app = create_app(tmp_path / "data", transcriber=t, writer=w)
    return app.test_client(), t, w, tmp_path


def test_rejects_languages_other_than_english_and_swahili(env):
    client, *_ = env
    for lang in ("fr", "wo", "", "auto"):
        r = client.post("/api/sessions", json={"title": "x", "language": lang})
        assert r.status_code == 400
    assert client.post("/api/sessions", json={"language": "sw"}).status_code == 201


def test_live_chunks_are_appended_in_order_then_processed(env):
    client, transcriber, writer, tmp = env
    src = tmp / "meeting.webm"
    make_audio(src, 300)  # 5 minutes -> 3 pieces of 120 s
    blob = src.read_bytes()

    sid = client.post("/api/sessions", json={"title": "Sync", "language": "sw", "report_language": "en"}).get_json()["id"]
    pieces = [blob[i:i + 7000] for i in range(0, len(blob), 7000)]
    for seq, piece in enumerate(pieces):
        r = client.post(f"/api/sessions/{sid}/chunk?seq={seq}", data=piece)
        assert r.status_code == 200, r.get_json()
    # a retried chunk is ignored, an out-of-order one is refused
    assert client.post(f"/api/sessions/{sid}/chunk?seq=0", data=pieces[0]).status_code == 200
    assert client.post(f"/api/sessions/{sid}/chunk?seq={len(pieces) + 3}", data=b"x").status_code == 409

    stored = client.get(f"/api/sessions/{sid}/audio").data
    assert stored == blob

    assert client.post(f"/api/sessions/{sid}/finish", json={}).status_code == 200
    s = wait_done(client, sid)
    assert s["status"] == "done", s["error"]
    assert 295 <= s["duration_seconds"] <= 305
    assert [c[1] for c in transcriber.calls] == ["sw", "sw", "sw"]
    # timestamps are offset by piece
    starts = [seg["start"] for seg in s["transcript"]]
    assert starts == [0, 60, 120, 180, 240, 300]
    assert s["report"].startswith("# Report")

    system, user = writer.messages[0]["content"], writer.messages[1]["content"]
    assert "Write the entire report in English" in system
    assert "Spoken language: Kiswahili" in user
    assert "[02:00] Part 2 opening (sw)." in user
    assert "## Action items" in user  # default template included

    txt = client.get(f"/api/sessions/{sid}/transcript.txt")
    assert txt.status_code == 200 and b"[04:00] Part 3 opening" in txt.data
    assert client.get(f"/api/sessions/{sid}/report.md").data.startswith(b"# Report")
    # no more chunks after finishing
    assert client.post(f"/api/sessions/{sid}/chunk?seq={len(pieces)}", data=b"x").status_code == 409


def test_upload_flow_and_custom_template(env):
    client, transcriber, writer, tmp = env
    client.put("/api/template", json={"template": "# {{title}}\n## Hoja\n## Maamuzi\n"})
    mp3 = tmp / "rec.mp3"
    make_audio(mp3, 30, ("-c:a", "libmp3lame", "-b:a", "64k"))
    with open(mp3, "rb") as fh:
        r = client.post("/api/sessions/upload", data={"audio": (fh, "board meeting.mp3"), "language": "sw",
                                                       "report_language": "same", "model": "x/model"},
                        content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    s = wait_done(client, r.get_json()["id"])
    assert s["status"] == "done", s["error"]
    assert s["title"] == "board meeting" and s["model"] == "x/model"
    user = writer.messages[1]["content"]
    assert "# board meeting\n## Hoja\n## Maamuzi" in user
    assert "Write the entire report in Kiswahili" in writer.messages[0]["content"]


def test_report_failure_can_be_retried_without_retranscribing(tmp_path):
    t, w = FakeTranscriber(), FakeWriter(fail=True)
    client = create_app(tmp_path / "d", transcriber=t, writer=w).test_client()
    wav = tmp_path / "a.wav"
    make_audio(wav, 10, ("-c:a", "pcm_s16le"))
    with open(wav, "rb") as fh:
        sid = client.post("/api/sessions/upload", data={"audio": (fh, "a.wav"), "language": "en"},
                          content_type="multipart/form-data").get_json()["id"]
    s = wait_done(client, sid)
    assert s["status"] == "error" and "401" in s["error"] and s["has_transcript"]
    w.fail = False
    calls_before = len(t.calls)
    client.post(f"/api/sessions/{sid}/retry", json={"report_only": True})
    s = wait_done(client, sid)
    assert s["status"] == "done" and len(t.calls) == calls_before


def test_empty_recording_reports_a_clear_error(env):
    client, *_ = env
    sid = client.post("/api/sessions", json={"language": "en"}).get_json()["id"]
    client.post(f"/api/sessions/{sid}/finish", json={})
    s = wait_done(client, sid)
    assert s["status"] == "error" and "No audio" in s["error"]


def test_bad_ids_and_unsupported_upload(env):
    client, *_ = env
    assert client.get("/api/sessions/../../etc").status_code == 404
    assert client.get("/api/sessions/zzzzzzzzzzzz").status_code == 404
    r = client.post("/api/sessions/upload", data={"audio": (open(__file__, "rb"), "x.py"), "language": "en"},
                    content_type="multipart/form-data")
    assert r.status_code == 400


def test_index_and_config(env):
    client, *_ = env
    assert b"Share meeting tab and start" in client.get("/").data
    cfg = client.get("/api/config").get_json()
    assert cfg["languages"] == {"en": "English", "sw": "Kiswahili"} and cfg["max_minutes"] == 60


def test_recording_longer_than_limit_is_trimmed_not_rejected(env, monkeypatch):
    client, transcriber, writer, tmp = env
    monkeypatch.setattr(services, "MAX_MINUTES", 1)
    monkeypatch.setattr(services, "SEGMENT_SECONDS", 40)
    src = tmp / "long.webm"
    make_audio(src, 90)
    sid = client.post("/api/sessions", json={"language": "en"}).get_json()["id"]
    assert client.post(f"/api/sessions/{sid}/chunk?seq=0", data=src.read_bytes()).status_code == 200
    client.post(f"/api/sessions/{sid}/finish", json={})
    s = wait_done(client, sid)
    assert s["status"] == "done", s["error"]
    assert s["duration_seconds"] == 60
    assert len(transcriber.calls) == 2  # 0-40 s and 40-60 s; nothing past the limit


def test_upload_longer_than_limit_is_refused_immediately(env, monkeypatch):
    client, transcriber, writer, tmp = env
    monkeypatch.setattr(services, "MAX_MINUTES", 1)
    mp3 = tmp / "long.mp3"
    make_audio(mp3, 120, ("-c:a", "libmp3lame", "-b:a", "32k"))
    with open(mp3, "rb") as fh:
        r = client.post("/api/sessions/upload", data={"audio": (fh, "long.mp3"), "language": "en"},
                        content_type="multipart/form-data")
    assert r.status_code == 400 and "2 minutes long" in r.get_json()["error"]
    assert client.get("/api/sessions").get_json()["sessions"] == []
    assert transcriber.calls == []


def test_pieces_are_transcribed_in_parallel_and_kept_in_order(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(services, "SEGMENT_SECONDS", 60)

    class SlowParallel(FakeTranscriber):
        parallel = True

        def __init__(self):
            super().__init__()
            self.active = 0
            self.peak = 0
            self.lock = threading.Lock()

        def transcribe(self, path, language, prompt=None):
            idx = int(Path(path).stem.split("_")[1])
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.6 - idx * 0.1)  # later pieces finish first
            with self.lock:
                self.active -= 1
            return [services.Segment(1.0, 2.0, f"piece {idx}")]

    t = SlowParallel()
    client = create_app(tmp_path / "d", transcriber=t, writer=FakeWriter()).test_client()
    mp3 = tmp_path / "m.mp3"
    make_audio(mp3, 300, ("-c:a", "libmp3lame", "-b:a", "32k"))
    with open(mp3, "rb") as fh:
        sid = client.post("/api/sessions/upload", data={"audio": (fh, "m.mp3"), "language": "en"},
                          content_type="multipart/form-data").get_json()["id"]
    s = wait_done(client, sid)
    assert s["status"] == "done", s["error"]
    assert t.peak == 5
    assert [seg["text"] for seg in s["transcript"]] == [f"piece {i}" for i in range(5)]
    assert [seg["start"] for seg in s["transcript"]] == [1, 61, 121, 181, 241]


def test_groq_request_shape_and_no_retry_when_out_of_credit(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.delenv("TRANSCRIBER", raising=False)
    t = services.make_transcriber()
    assert (t.name, t.model) == ("groq", "whisper-large-v3")

    sent = []

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._body, self.headers = code, body, {}
            self.ok = code < 400
            self.text = str(body)

        def json(self):
            return self._body

    replies = [Resp(200, {"segments": [{"start": 0.0, "end": 3.0, "text": " Habari"}]}),
               Resp(429, {"error": {"message": "You have no credits remaining."}})]

    def fake_post(url, headers, data, files, timeout):
        sent.append((url, headers["Authorization"], dict(data)))
        return replies.pop(0)

    monkeypatch.setattr(services.requests, "post", fake_post)
    piece = tmp_path / "p.mp3"
    piece.write_bytes(b"x")
    assert t.transcribe(piece, "sw")[0].text == "Habari"
    url, auth, data = sent[0]
    assert url == "https://api.groq.com/openai/v1/audio/transcriptions" and auth == "Bearer gsk-test"
    assert data["language"] == "sw" and data["response_format"] == "verbose_json"
    with pytest.raises(RuntimeError, match="no transcription credit left"):
        t.transcribe(piece, "sw")
    assert len(sent) == 2  # out-of-credit 429 fails at once instead of retrying


def test_meta_writes_survive_a_file_briefly_locked(tmp_path, monkeypatch):
    """Simulates Windows refusing to replace meta.json while it is open elsewhere."""
    store = services.Store(tmp_path)
    meta = store.create("x", "en", "same", "live")
    real = Path.replace
    failures = {"n": 3}

    def flaky(self, target):
        if failures["n"] > 0:
            failures["n"] -= 1
            raise PermissionError(5, "Access is denied")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    assert store.update(meta["id"], stage="Transcribing")["stage"] == "Transcribing"
    assert store.load(meta["id"])["stage"] == "Transcribing"
    assert not list((tmp_path / "sessions" / meta["id"]).glob("*.tmp"))


def test_concurrent_polling_and_updates(tmp_path):
    import threading

    store = services.Store(tmp_path)
    sid = store.create("x", "en", "same", "live")["id"]
    errors = []

    def writer():
        for i in range(300):
            try:
                store.update(sid, progress=i / 300)
            except Exception as e:  # pragma: no cover
                errors.append(e)

    def reader():
        for _ in range(300):
            try:
                store.load(sid)
                store.list()
            except Exception as e:  # pragma: no cover
                errors.append(e)

    threads = [threading.Thread(target=writer), threading.Thread(target=writer),
               threading.Thread(target=reader), threading.Thread(target=reader)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []


def test_vocabulary_instructions_header_and_footer(env):
    client, transcriber, writer, tmp = env
    r = client.put("/api/settings", json={
        "vocabulary": "Northwind Institute\nWanjiru, M-Pesa\nnorthwind institute",
        "instructions": "Use a formal tone.",
        "header": "**ACME Ltd. Minutes of {{title}}**",
        "footer": "Confidential. Prepared {{date}}.",
    })
    assert r.status_code == 200
    assert client.get("/api/settings").get_json()["instructions"] == "Use a formal tone."

    wav = tmp / "v.wav"
    make_audio(wav, 10, ("-c:a", "pcm_s16le"))
    with open(wav, "rb") as fh:
        sid = client.post("/api/sessions/upload", data={"audio": (fh, "Board.wav"), "language": "en"},
                          content_type="multipart/form-data").get_json()["id"]
    s = wait_done(client, sid)
    assert s["status"] == "done", s["error"]
    assert transcriber.prompts == ["Northwind Institute, Wanjiru, M-Pesa."]  # duplicate removed
    system, user = writer.messages[0]["content"], writer.messages[1]["content"]
    assert "Use a formal tone." in system
    assert "VOCABULARY (correct spellings)\nNorthwind Institute, Wanjiru, M-Pesa" in user
    assert s["report"].startswith("**ACME Ltd. Minutes of Board**\n\n# Report")
    assert s["report"].rstrip().endswith(".") and "Confidential. Prepared" in s["report"]
    assert "{{date}}" not in s["report"]


RICH_REPORT = """# Weekly sync

**Date:** 22 November 2026

## Summary
The team agreed to launch the pilot on the 10th. Budget review moves to Friday.

## Decisions
- Launch the **pilot** on the 10th
- Move budget review to Friday

## Action items
| Action | Owner | Due date |
|---|---|---|
| Prepare budget | Amina | Friday |
| Book venue | Unassigned | Not set |

## Next steps
Meet again next Tuesday.
"""


def _done_session(client, tmp, writer):
    writer_write = writer.write

    def write(messages, model=None):
        text, used = writer_write(messages, model)
        return (RICH_REPORT if text.startswith("# Report") else text), used

    writer.write = write
    wav = tmp / "d.wav"
    make_audio(wav, 10, ("-c:a", "pcm_s16le"))
    with open(wav, "rb") as fh:
        sid = client.post("/api/sessions/upload", data={"audio": (fh, "Weekly sync.wav"), "language": "sw"},
                          content_type="multipart/form-data").get_json()["id"]
    s = wait_done(client, sid)
    assert s["status"] == "done", s["error"]
    return sid, s


def test_slides_fall_back_to_report_sections(env):
    client, transcriber, writer, tmp = env
    sid, s = _done_session(client, tmp, writer)
    deck = s["slides"]
    titles = [sl["title"] for sl in deck["slides"]]
    assert titles == ["Summary", "Decisions", "Action items", "Next steps"]
    actions = deck["slides"][2]["table"]
    assert actions["columns"] == ["Action", "Owner", "Due date"] and actions["rows"][0] == ["Prepare budget", "Amina", "Friday"]
    assert deck["slides"][1]["bullets"][0] == "Launch the pilot on the 10th"
    assert "Kiswahili" in deck["subtitle"]


def test_slides_from_model_json(env):
    client, transcriber, writer, tmp = env
    writer.slides_reply = '```json\n{"title":"Weekly sync","subtitle":"x","slides":[' \
        '{"title":"At a glance","lead":"Pilot launches on the 10th.","bullets":[]},' \
        '{"title":"Action items","table":{"columns":["Action","Owner","Due"],"rows":[["Budget","Amina","Fri"]]}},' \
        '{"title":"","bullets":["dropped: no title"]}]}\n```'
    sid, s = _done_session(client, tmp, writer)
    assert [sl["title"] for sl in s["slides"]["slides"]] == ["At a glance", "Action items"]
    assert "Kiswahili" in writer.slide_messages[0]["content"]
    # rebuilding after an edit uses the edited report
    client.patch(f"/api/sessions/{sid}", json={"report": "## Only section\n- One point"})
    writer.slides_reply = "garbage"
    deck = client.post(f"/api/sessions/{sid}/slides", json={}).get_json()["slides"]
    assert [sl["title"] for sl in deck["slides"]] == ["Only section"]


def test_every_download_format(env):
    import io
    from docx import Document
    from pptx import Presentation

    client, transcriber, writer, tmp = env
    sid, _ = _done_session(client, tmp, writer)

    def get(what, fmt):
        r = client.get(f"/api/sessions/{sid}/download/{what}.{fmt}")
        assert r.status_code == 200, (what, fmt, r.data[:200])
        assert f"weekly-sync-{what}.{fmt}" in r.headers["Content-Disposition"]
        return r.data

    assert get("report", "md").startswith(b"# Weekly sync")
    txt = get("report", "txt").decode()
    assert "**" not in txt and "|---" not in txt and "Prepare budget  |  Amina" in txt
    assert b"<table>" in get("report", "html")
    doc = Document(io.BytesIO(get("report", "docx")))
    assert any(p.text == "Decisions" and p.style.name.startswith("Heading") for p in doc.paragraphs)
    assert doc.tables[0].cell(1, 1).text == "Amina"
    prs = Presentation(io.BytesIO(get("slides", "pptx")))
    assert len(prs.slides) == 1 + 4
    assert b'"slides"' in get("slides", "json")
    srt = get("transcript", "srt").decode()
    assert srt.startswith("1\n00:00:00,000 --> 00:00:05,000\n")
    assert get("transcript", "vtt").startswith(b"WEBVTT")
    assert b"Part 1 opening" in get("transcript", "txt")
    Document(io.BytesIO(get("transcript", "docx")))
    assert b'"segments"' in get("transcript", "json")
    assert client.get(f"/api/sessions/{sid}/download/report.exe").status_code == 404


def test_transcript_and_report_reads_never_see_half_written_files(tmp_path):
    import threading

    store = services.Store(tmp_path)
    sid = store.create("x", "en", "same", "live")["id"]
    big = [{"start": i, "end": i + 1, "text": "maneno " * 40} for i in range(400)]
    store.write_json(sid, "transcript.json", big)
    errors = []

    def writer():
        for _ in range(60):
            store.write_json(sid, "transcript.json", big)
            store.write_text(sid, "report.md", "# R\n" + "x" * 50000)

    def reader():
        for _ in range(200):
            try:
                data = store.read_json(sid, "transcript.json")
                assert data is None or len(data) == 400
                text = store.read_text(sid, "report.md")
                assert text is None or len(text) == 50004
            except Exception as e:
                errors.append(repr(e))

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [], errors[:3]


def test_misspelt_transcriber_and_padded_keys(monkeypatch):
    monkeypatch.setenv("TRANSCRIBER", "qrok")
    monkeypatch.setenv("GROQ_API_KEY", " gsk-x ")
    monkeypatch.setenv("OPENROUTER_API_KEY", " sk-or-x")
    t = services.make_transcriber()
    assert t.name == "groq" and t.api_key == "gsk-x"
    assert services.OpenRouterWriter().api_key == "sk-or-x"


def test_openrouter_caps_output_tokens(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-x")
    sent = {}

    class Resp:
        status_code, ok, headers, text = 200, True, {}, ""

        def json(self):
            return {"model": "m", "choices": [{"message": {"content": "# R"}}]}

    def fake_post(url, headers, json, timeout):
        sent.update(json)
        return Resp()

    monkeypatch.setattr(services.requests, "post", fake_post)
    w = services.OpenRouterWriter()
    assert w.write([{"role": "user", "content": "x"}])[0] == "# R"
    assert sent["max_tokens"] == 6000


def test_model_saved_in_settings_is_used_and_prompt_is_smart(env):
    client, transcriber, writer, tmp = env
    client.put("/api/settings", json={"model": "google/gemini-2.5-pro"})
    used = []
    real = writer.write

    def spy(messages, model=None):
        used.append(model)
        return real(messages, model)

    writer.write = spy
    wav = tmp / "m.wav"
    make_audio(wav, 10, ("-c:a", "pcm_s16le"))
    with open(wav, "rb") as fh:
        sid = client.post("/api/sessions/upload", data={"audio": (fh, "m.wav"), "language": "en"},
                          content_type="multipart/form-data").get_json()["id"]
    s = wait_done(client, sid)
    assert s["status"] == "done" and s["model"] == "google/gemini-2.5-pro"
    assert used == ["google/gemini-2.5-pro", "google/gemini-2.5-pro"]  # report and slides
    system = writer.messages[0]["content"]
    assert "DECIDED" in system and "[mm:ss]" in system and "Kiswahili" in system
    import time as _t
    assert _t.strftime("%A", _t.localtime(s["created_at"])) in system
