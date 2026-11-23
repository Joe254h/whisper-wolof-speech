"""Kikao: record a Teams / Google Meet meeting in the browser, transcribe it with
Whisper (English or Kiswahili) and turn it into a formatted report via OpenRouter."""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from flask import Flask, Response, jsonify, render_template, request, send_file  # noqa: E402

import services  # noqa: E402
from services import LANGUAGES, MAX_MINUTES, REPORT_LANGUAGES, Processor, Store  # noqa: E402

# MAX_MINUTES is re-read from services at request time so tests can change it.

UPLOAD_EXTENSIONS = {".webm", ".ogg", ".wav", ".mp3", ".m4a", ".mp4", ".flac", ".aac"}
CHUNK_LIMIT = 8 * 1024 * 1024


def create_app(data_dir: Path | None = None, transcriber=None, writer=None) -> Flask:
    app = Flask(__name__)
    # An hour of uncompressed stereo WAV is about 700 MB, so allow up to 1 GB.
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "1024")) * 1024 * 1024

    store = Store(data_dir or Path(os.environ.get("DATA_DIR", BASE_DIR / "data")))
    processor = Processor(store, transcriber, writer)
    processor.recover()
    app.extensions["kikao"] = {"store": store, "processor": processor}

    def public(meta: dict) -> dict:
        sid = meta["id"]
        out = dict(meta)
        out["has_audio"] = store.audio_path(sid).exists()
        out["has_transcript"] = (store.dir(sid) / "transcript.json").exists()
        out["has_report"] = (store.dir(sid) / "report.md").exists()
        return out

    def get_or_404(sid: str):
        try:
            return store.load(sid), None
        except KeyError:
            return None, (jsonify({"error": "That meeting no longer exists."}), 404)

    # pages --------------------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html", max_minutes=MAX_MINUTES)

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    # config -------------------------------------------------------------------
    @app.get("/api/config")
    def config():
        t, w = processor.transcriber, processor.writer
        return jsonify({
            "languages": LANGUAGES,
            "report_languages": REPORT_LANGUAGES,
            "max_minutes": MAX_MINUTES,
            "transcriber": {"backend": t.name, "ready": t.ready,
                            "label": getattr(t, "label", t.name), "key_name": getattr(t, "key_name", None),
                            "model": getattr(t, "model", None) or getattr(t, "model_name", None)},
            "writer": {"ready": w.ready, "model": w.model},
        })

    @app.get("/api/template")
    def get_template():
        return jsonify({"template": store.get_template(), "default": services.DEFAULT_TEMPLATE})

    @app.put("/api/template")
    def put_template():
        text = (request.get_json(silent=True) or {}).get("template", "")
        if len(text) > 20_000:
            return jsonify({"error": "Keep the template under 20,000 characters."}), 400
        return jsonify({"template": store.set_template(text)})

    # sessions -----------------------------------------------------------------
    @app.get("/api/sessions")
    def list_sessions():
        return jsonify({"sessions": [public(m) for m in store.list()]})

    @app.post("/api/sessions")
    def create_session():
        body = request.get_json(silent=True) or {}
        try:
            meta = store.create(body.get("title", ""), body.get("language", ""),
                                body.get("report_language", "same"), "live")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(public(meta)), 201

    @app.post("/api/sessions/upload")
    def upload_session():
        file = request.files.get("audio")
        if not file or not file.filename:
            return jsonify({"error": "Choose an audio file to upload."}), 400
        ext = Path(file.filename).suffix.lower()
        if ext not in UPLOAD_EXTENSIONS:
            return jsonify({"error": "Upload a .webm, .wav, .mp3, .m4a, .mp4, .ogg, .flac or .aac file."}), 400
        try:
            meta = store.create(request.form.get("title") or Path(file.filename).stem,
                                request.form.get("language", ""),
                                request.form.get("report_language", "same"), "upload")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        meta = store.update(meta["id"], audio_ext=ext)
        path = store.audio_path(meta["id"])
        file.save(path)
        try:
            duration = services.probe_duration(path)
        except RuntimeError:
            store.delete(meta["id"])
            return jsonify({"error": "That file has no audio Kikao can read. Try MP3, M4A or WAV."}), 400
        if duration > services.MAX_MINUTES * 60 + 30:
            store.delete(meta["id"])
            return jsonify({"error": f"That recording is {round(duration / 60)} minutes long. "
                                     f"Upload one that is {services.MAX_MINUTES} minutes or shorter."}), 400
        store.update(meta["id"], audio_bytes=path.stat().st_size, duration_seconds=round(duration, 1))
        processor.submit(meta["id"], model=request.form.get("model") or None)
        return jsonify(public(store.load(meta["id"]))), 201

    @app.get("/api/sessions/<sid>")
    def get_session(sid):
        meta, err = get_or_404(sid)
        if err:
            return err
        out = public(meta)
        out["report"] = store.read_text(sid, "report.md")
        out["transcript"] = store.read_json(sid, "transcript.json")
        return jsonify(out)

    @app.patch("/api/sessions/<sid>")
    def rename_session(sid):
        meta, err = get_or_404(sid)
        if err:
            return err
        body = request.get_json(silent=True) or {}
        fields = {}
        if "title" in body:
            fields["title"] = str(body["title"]).strip()[:140] or meta["title"]
        if body.get("report_language") in REPORT_LANGUAGES:
            fields["report_language"] = body["report_language"]
        if "report" in body and isinstance(body["report"], str):
            store.write_text(sid, "report.md", body["report"])
        return jsonify(public(store.update(sid, **fields)))

    @app.delete("/api/sessions/<sid>")
    def delete_session(sid):
        meta, err = get_or_404(sid)
        if err:
            return err
        if meta["status"] in ("transcribing", "writing"):
            return jsonify({"error": "Wait until processing finishes before deleting."}), 409
        store.delete(sid)
        return jsonify({"deleted": sid})

    @app.post("/api/sessions/<sid>/chunk")
    def upload_chunk(sid):
        meta, err = get_or_404(sid)
        if err:
            return err
        try:
            seq = int(request.args.get("seq", ""))
        except ValueError:
            return jsonify({"error": "Missing chunk number."}), 400
        data = request.get_data(cache=False)
        if len(data) > CHUNK_LIMIT:
            return jsonify({"error": "Chunk too large."}), 413
        try:
            meta = store.append_chunk(sid, seq, data)
        except ValueError as exc:
            return jsonify({"error": str(exc), "expected": meta["chunks"]}), 409
        return jsonify({"chunks": meta["chunks"], "audio_bytes": meta["audio_bytes"]})

    @app.post("/api/sessions/<sid>/finish")
    def finish(sid):
        meta, err = get_or_404(sid)
        if err:
            return err
        if meta["status"] != "recording":
            return jsonify({"error": "This meeting has already been submitted."}), 409
        body = request.get_json(silent=True) or {}
        processor.submit(sid, model=body.get("model") or None)
        return jsonify(public(store.load(sid)))

    @app.post("/api/sessions/<sid>/retry")
    def retry(sid):
        meta, err = get_or_404(sid)
        if err:
            return err
        if meta["status"] in ("queued", "transcribing", "writing"):
            return jsonify({"error": "This meeting is already processing."}), 409
        body = request.get_json(silent=True) or {}
        only_report = bool(body.get("report_only")) and (store.dir(sid) / "transcript.json").exists()
        processor.submit(sid, transcribe=not only_report, model=body.get("model") or None)
        return jsonify(public(store.load(sid)))

    # downloads ----------------------------------------------------------------
    def slug(meta):
        return re.sub(r"[^A-Za-z0-9]+", "-", meta["title"]).strip("-").lower()[:60] or "meeting"

    @app.get("/api/sessions/<sid>/audio")
    def audio(sid):
        meta, err = get_or_404(sid)
        if err:
            return err
        path = store.audio_path(sid)
        if not path.exists():
            return jsonify({"error": "No audio stored."}), 404
        return send_file(path, as_attachment=request.args.get("download") == "1",
                         download_name=f"{slug(meta)}{path.suffix}", conditional=True)

    @app.get("/api/sessions/<sid>/transcript.txt")
    def transcript_txt(sid):
        meta, err = get_or_404(sid)
        if err:
            return err
        text = store.read_text(sid, "transcript.txt")
        if text is None:
            return jsonify({"error": "No transcript yet."}), 404
        return Response(text, mimetype="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{slug(meta)}-transcript.txt"'})

    @app.get("/api/sessions/<sid>/report.md")
    def report_md(sid):
        meta, err = get_or_404(sid)
        if err:
            return err
        text = store.read_text(sid, "report.md")
        if text is None:
            return jsonify({"error": "No report yet."}), 404
        return Response(text, mimetype="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{slug(meta)}-report.md"'})

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"error": "That file is too large."}), 413

    return app


app = create_app() if os.environ.get("KIKAO_NO_AUTOAPP") != "1" else None

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="127.0.0.1", port=port, debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
