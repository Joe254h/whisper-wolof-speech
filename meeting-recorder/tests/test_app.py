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

    def transcribe(self, path, language):
        self.calls.append((Path(path).name, language))
        n = len(self.calls)
        return [services.Segment(0.0, 5.0, f"Part {n} opening ({language})."),
                services.Segment(60.0, 65.0, f"Part {n} decision: ship on Friday.")]


class FakeWriter:
    ready = True
    model = "fake/llm"

    def __init__(self, fail=False):
        self.messages = None
        self.fail = fail

    def write(self, messages, model=None):
        if self.fail:
            raise RuntimeError("OpenRouter request failed (401): bad key")
        self.messages = messages
        return "# Report\n\n## Decisions\n- Ship on Friday", model or self.model


def make_audio(path: Path, seconds: int, fmt_args=("-c:a", "libopus", "-b:a", "32k")):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
         "-ac", "1", *fmt_args, str(path)],
        check=True,
    )


def wait_done(client, sid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get(f"/api/sessions/{sid}").get_json()
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
