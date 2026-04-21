import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from services.assistant_service import (
    handle_command,
    research_topic,
    send_email_message,
    set_reminder,
    summarize_inbox,
    summarize_uploaded_file,
)
from services.reinforcement_service import get_learning_status, record_feedback

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


def _payload_response(payload):
    status_code = 200 if payload.get("ok") else 400
    return jsonify(payload), status_code


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "app": "autopilot-ai-assistant"})


@app.get("/api/mail/summary")
def mail_summary():
    limit = request.args.get("limit", default=5, type=int)
    return _payload_response(summarize_inbox(limit))


@app.post("/api/email/send")
def email_send():
    payload = request.get_json(silent=True) or {}
    return _payload_response(
        send_email_message(
            payload.get("recipient", ""),
            payload.get("subject", ""),
            payload.get("message", ""),
            polish=bool(payload.get("polish", True)),
        )
    )


@app.post("/api/reminder/create")
def reminder_create():
    payload = request.get_json(silent=True) or {}
    return _payload_response(
        set_reminder(
            payload.get("title", ""),
            payload.get("when", ""),
            payload.get("description", ""),
            payload.get("duration_minutes", 60),
        )
    )


@app.post("/api/research")
def research():
    payload = request.get_json(silent=True) or {}
    return _payload_response(research_topic(payload.get("topic", "")))


@app.post("/api/attachment/summarize")
def attachment_summarize():
    upload = request.files.get("file")
    if upload is None:
        return _payload_response(
            {
                "ok": False,
                "response": {
                    "type": "error",
                    "title": "Missing file",
                    "text": "Choose a file before submitting.",
                    "items": [],
                    "meta": {},
                    "sources": [],
                    "export_text": "Missing file\n\nChoose a file before submitting.",
                },
            }
        )

    return _payload_response(summarize_uploaded_file(upload.filename, upload.read()))


@app.post("/api/command")
def command():
    payload = request.get_json(silent=True) or {}
    return _payload_response(
        handle_command(
            payload.get("message", ""),
            context=payload.get("context") or [],
        )
    )


@app.post("/api/feedback")
def feedback():
    payload = request.get_json(silent=True) or {}
    result = record_feedback(
        payload.get("trace_id", ""),
        payload.get("reward", 0),
        label=payload.get("label", ""),
        comment=payload.get("comment", ""),
    )
    return jsonify(result), (200 if result.get("ok") else 404)


@app.get("/api/learning/status")
def learning_status():
    return jsonify(get_learning_status())


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    host = os.getenv("HOST", "0.0.0.0")
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
