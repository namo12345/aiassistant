import os
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from services.assistant_service import (
    cancel_event,
    daily_briefing,
    handle_command,
    list_upcoming_events,
    reply_to_email,
    research_topic,
    reschedule_event,
    search_inbox,
    send_email_message,
    set_reminder,
    summarize_inbox,
    summarize_uploaded_file,
)
from services.auth_service import (
    build_authorize_url,
    complete_oauth,
    new_state_token,
)
from services.reinforcement_service import get_learning_status, record_feedback
from services.user_service import get_user

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.secret_key = os.getenv("FLASK_SECRET_KEY") or "dev-only-change-me"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("VERCEL") == "1"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def _payload_response(payload):
    status_code = 200 if payload.get("ok") else 400
    return jsonify(payload), status_code


def _current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_user(user_id)


def require_auth(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = _current_user()
        if not user:
            return jsonify({"ok": False, "error": "unauthenticated"}), 401
        request.current_user = user
        return fn(*args, **kwargs)

    return wrapped


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "app": "autopilot-ai-assistant"})


# ===== AUTH =====

@app.get("/login")
def login():
    state = new_state_token()
    session["oauth_state"] = state
    return redirect(build_authorize_url(state))


@app.get("/oauth/callback")
def oauth_callback():
    received_state = request.args.get("state", "")
    expected_state = session.pop("oauth_state", None)
    if not received_state or received_state != expected_state:
        return render_template("index.html", oauth_error="Invalid OAuth state. Please try signing in again."), 400

    code = request.args.get("code", "")
    if not code:
        return render_template("index.html", oauth_error="No authorization code received."), 400

    try:
        user = complete_oauth(code)
    except Exception as exc:
        return render_template("index.html", oauth_error=f"Sign-in failed: {exc}"), 400

    session["user_id"] = user["id"]
    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    user = _current_user()
    if not user:
        return jsonify({"ok": False, "authenticated": False}), 401
    return jsonify(
        {
            "ok": True,
            "authenticated": True,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "name": user.get("name") or user["email"],
                "picture_url": user.get("picture_url") or "",
            },
        }
    )


# ===== CORE FEATURES (all require auth) =====

@app.get("/api/mail/summary")
@require_auth
def mail_summary():
    limit = request.args.get("limit", default=5, type=int)
    return _payload_response(summarize_inbox(request.current_user, limit))


@app.post("/api/email/send")
@require_auth
def email_send():
    payload = request.get_json(silent=True) or {}
    return _payload_response(
        send_email_message(
            request.current_user,
            payload.get("recipient", ""),
            payload.get("subject", ""),
            payload.get("message", ""),
            polish=bool(payload.get("polish", True)),
        )
    )


@app.post("/api/reminder/create")
@require_auth
def reminder_create():
    payload = request.get_json(silent=True) or {}
    return _payload_response(
        set_reminder(
            request.current_user,
            payload.get("title", ""),
            payload.get("when", ""),
            payload.get("description", ""),
            payload.get("duration_minutes", 60),
        )
    )


@app.get("/api/events/upcoming")
@require_auth
def events_upcoming():
    days = request.args.get("days", default=7, type=int)
    return _payload_response(list_upcoming_events(request.current_user, days_ahead=days))


@app.post("/api/events/reschedule")
@require_auth
def events_reschedule():
    payload = request.get_json(silent=True) or {}
    return _payload_response(
        reschedule_event(
            request.current_user,
            new_when_text=payload.get("when", "") or payload.get("time", ""),
            event_id=payload.get("event_id", ""),
            query=payload.get("query", ""),
            duration_minutes=payload.get("duration_minutes"),
        )
    )


@app.post("/api/events/cancel")
@require_auth
def events_cancel():
    payload = request.get_json(silent=True) or {}
    return _payload_response(
        cancel_event(
            request.current_user,
            event_id=payload.get("event_id", ""),
            query=payload.get("query", ""),
        )
    )


@app.post("/api/email/reply")
@require_auth
def email_reply():
    payload = request.get_json(silent=True) or {}
    return _payload_response(
        reply_to_email(
            request.current_user,
            body=payload.get("message", "") or payload.get("body", ""),
            message_id=payload.get("message_id", ""),
            query=payload.get("query", ""),
            subject_hint=payload.get("subject", ""),
            polish=bool(payload.get("polish", True)),
        )
    )


@app.get("/api/mail/search")
@require_auth
def mail_search():
    query = request.args.get("q", default="", type=str)
    limit = request.args.get("limit", default=5, type=int)
    return _payload_response(search_inbox(request.current_user, query, limit=limit))


@app.get("/api/briefing")
@require_auth
def briefing():
    return _payload_response(daily_briefing(request.current_user))


@app.post("/api/research")
@require_auth
def research():
    payload = request.get_json(silent=True) or {}
    return _payload_response(
        research_topic(request.current_user, payload.get("topic", ""))
    )


@app.post("/api/attachment/summarize")
@require_auth
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

    return _payload_response(
        summarize_uploaded_file(request.current_user, upload.filename, upload.read())
    )


@app.post("/api/command")
@require_auth
def command():
    payload = request.get_json(silent=True) or {}
    return _payload_response(
        handle_command(
            request.current_user,
            payload.get("message", ""),
            context=payload.get("context") or [],
        )
    )


@app.post("/api/feedback")
@require_auth
def feedback():
    payload = request.get_json(silent=True) or {}
    result = record_feedback(
        request.current_user["id"],
        payload.get("trace_id", ""),
        payload.get("reward", 0),
        label=payload.get("label", ""),
        comment=payload.get("comment", ""),
    )
    return jsonify(result), (200 if result.get("ok") else 404)


@app.get("/api/learning/status")
@require_auth
def learning_status():
    return jsonify(get_learning_status(request.current_user["id"]))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    host = os.getenv("HOST", "0.0.0.0")
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
