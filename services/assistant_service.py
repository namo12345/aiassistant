import base64
import html
import json
import mimetypes
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from uuid import uuid4

import dateparser
import docx
import fitz
import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from dateparser.search import search_dates
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from services.reinforcement_service import attach_trace, get_prompt_variant, select_strategy
from services.supabase_client import get_supabase
from utils.deep_research_agent import search_web
from utils.llm_util import chat_completion, polish_message, summarize_text
from utils.nlu_agent import parse_intent_with_llm

load_dotenv()

GREETING_PATTERNS = re.compile(
    r"^\s*(hi|hello|hey|good\s*(morning|evening|afternoon|night)|what'?s?\s*up|yo|howdy|greetings|thanks?|thank\s*you)\b",
    re.IGNORECASE,
)

GMAIL_READONLY_SCOPE = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_SEND_SCOPE = ["https://www.googleapis.com/auth/gmail.send"]
CALENDAR_SCOPE = ["https://www.googleapis.com/auth/calendar.events"]

MAX_SUMMARY_CHARS = 12000
MAX_RESEARCH_CHARS = 12000
MAX_EMAIL_BODY_CHARS = 1800
MAX_DOCUMENT_BODY_CHARS = 9000
_DATA_ROOT = Path("/tmp") if os.getenv("VERCEL") else Path(__file__).resolve().parent.parent / "data"
LOCAL_REMINDER_LOG_PATH = _DATA_ROOT / "local_reminders.jsonl"

EMAIL_BOILERPLATE_MARKERS = (
    "unsubscribe",
    "manage preferences",
    "view in browser",
    "copyright",
    "privacy policy",
    "terms of service",
    "powered by stripe",
    "follow us on",
    "download invoice",
    "download receipt",
    "you are receiving this email because",
    "all rights reserved",
)


def _success(response_type, title, text, items=None, meta=None, sources=None, export_text=None):
    response = {
        "type": response_type,
        "title": title,
        "text": text,
        "items": items or [],
        "meta": meta or {},
        "sources": sources or [],
        "export_text": export_text or f"{title}\n\n{text}",
    }
    return {"ok": True, "response": response}


def _error(message, title="Request failed", response_type="error", meta=None):
    response = {
        "type": response_type,
        "title": title,
        "text": message,
        "items": [],
        "meta": meta or {},
        "sources": [],
        "export_text": f"{title}\n\n{message}",
    }
    return {"ok": False, "response": response}


def _limit_text(text, limit):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].strip()
    return f"{clipped}..."


def _looks_like_html(text):
    return bool(re.search(r"<(html|body|div|table|p|br|span|a|img|td|tr)\b", text or "", re.IGNORECASE))


def _clean_extracted_text(text, max_chars):
    if not text:
        return ""

    cleaned = html.unescape(text).replace("\xa0", " ")
    cleaned = re.sub(r"[\u034f\u200b-\u200f\u2060\ufeff]", "", cleaned)
    if _looks_like_html(cleaned):
        soup = BeautifulSoup(cleaned, "html.parser")
        for tag in soup(["script", "style", "head", "title", "meta", "link", "svg", "img"]):
            tag.decompose()
        cleaned = soup.get_text("\n")

    lines = []
    seen = set()
    total_chars = 0

    for raw_line in cleaned.splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue

        lowered = line.lower()
        if lowered in seen:
            continue
        if any(marker in lowered for marker in EMAIL_BOILERPLATE_MARKERS):
            continue
        if re.fullmatch(r"[_=\-•·\s]{4,}", line):
            continue

        line = re.sub(r"https?://\S+", "", line).strip()
        line = re.sub(r"\b(?:cid|mailto):\S+", "", line).strip()
        if not line:
            continue

        alpha_ratio = sum(char.isalpha() for char in line) / max(len(line), 1)
        if len(line) > 240 and alpha_ratio < 0.45:
            continue

        lines.append(line)
        seen.add(lowered)
        total_chars += len(line) + 1
        if total_chars >= max_chars:
            break

    return "\n".join(lines)


def _collect_email_bodies(payload, plain_parts, html_parts):
    if not payload:
        return

    mime_type = payload.get("mimeType", "")
    body_text = _decode_base64_text(payload.get("body", {}).get("data", ""))
    if body_text.strip():
        if mime_type == "text/plain":
            plain_parts.append(body_text)
        elif mime_type == "text/html":
            html_parts.append(body_text)

    for part in payload.get("parts", []):
        _collect_email_bodies(part, plain_parts, html_parts)


def _extract_email_body(payload):
    plain_parts = []
    html_parts = []
    _collect_email_bodies(payload, plain_parts, html_parts)

    for candidates in (plain_parts, html_parts):
        combined = "\n".join(
            _clean_extracted_text(candidate, MAX_EMAIL_BODY_CHARS)
            for candidate in candidates
            if candidate.strip()
        ).strip()
        if combined:
            return _limit_text(combined, MAX_EMAIL_BODY_CHARS)

    raw_text = _decode_base64_text(payload.get("body", {}).get("data", ""))
    return _limit_text(_clean_extracted_text(raw_text, MAX_EMAIL_BODY_CHARS), MAX_EMAIL_BODY_CHARS)


def _sender_name(sender):
    if not sender:
        return "Unknown sender"
    match = re.match(r'"?([^"<]+)"?\s*<', sender)
    if match:
        return match.group(1).strip()
    return sender.strip()


def _first_sentence(text, fallback="No preview available."):
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return fallback
    sentence = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0].strip()
    return _limit_text(sentence or cleaned, 180)


def _extract_amount(text):
    preferred_patterns = (
        r"(?i)(?:amount paid|amount due|total(?: excluding tax)?|paid)\D{0,20}((?:rs\.?|inr|\$)\s?[\d,]+(?:\.\d{2})?)",
        r"(?i)((?:rs\.?|inr|\$)\s?[\d,]+(?:\.\d{2})?)",
    )
    for pattern in preferred_patterns:
        match = re.search(pattern, text or "")
        if match:
            return match.group(1)
    return ""


def _extract_date(text):
    preferred_patterns = (
        r"(?i)(?:date paid|paid|date of issue|due)\D{0,20}((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{4})",
        r"(?i)((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{4})",
        r"(?i)(\d{2}[-/]\d{2}[-/]\d{2,4})",
    )
    for pattern in preferred_patterns:
        match = re.search(pattern, text or "")
        if match:
            return match.group(1)
    return ""


def _find_action_hint(text):
    cleaned = " ".join((text or "").split())
    for sentence in re.split(r"(?<=[.!?])\s+", cleaned):
        lowered = sentence.lower()
        if re.search(r"\baction required\b", lowered) or re.search(
            r"\b(?:please|check|review|confirm|verify|respond|reply)\b",
            lowered,
        ):
            return _limit_text(sentence.strip(), 160)
    return ""


def _summary_looks_noisy(summary, max_chars=280):
    text = (summary or "").strip()
    if not text:
        return True
    if len(text) > max_chars:
        return True
    if any(marker in text.lower() for marker in ("<html", "page 1 of 1", "invoice number", "receipt number", "http://", "https://")):
        return True
    return False


def _fallback_email_summary(subject, sender, snippet, body_text):
    sender_label = _sender_name(sender)
    cleaned_subject = _clean_extracted_text(subject, 120) or "No subject"
    preview = _clean_extracted_text(snippet, 280) or _clean_extracted_text(body_text, 280)
    combined = " ".join([cleaned_subject, sender_label, preview, body_text]).lower()
    amount = _extract_amount(" ".join([subject, snippet, body_text]))
    date_label = _extract_date(" ".join([subject, snippet, body_text]))
    action_hint = _find_action_hint(" ".join([snippet, body_text]))

    if any(token in combined for token in ("receipt", "invoice", "paid", "payment method", "amount due")):
        details = []
        if amount:
            details.append(amount)
        if date_label:
            details.append(date_label)
        detail_text = f" ({', '.join(details)})" if details else ""
        return f"Billing email from {sender_label} about {cleaned_subject}{detail_text}. {'No clear action required.' if not action_hint else action_hint}"

    if any(token in combined for token in ("signed in", "new device", "unrecognized device", "security alert")):
        return f"Security alert from {sender_label}: {_first_sentence(preview, cleaned_subject)} Review it if the activity was not yours."

    if any(token in combined for token in ("deploy", "deployment", "build failed", "production deployment", "vercel")):
        return f"Deployment alert from {sender_label}: {_first_sentence(preview, cleaned_subject)} Check the deployment details."

    if any(token in combined for token in ("debited", "credited", "upi", "transaction reference", "bank")):
        return f"Bank alert from {sender_label}: {_first_sentence(preview, cleaned_subject)}"

    if any(token in combined for token in ("offer", "sale", "coupon", "subscribe", "frames for every")):
        return f"Promotional email from {sender_label}: {_first_sentence(preview, cleaned_subject)}"

    base = _first_sentence(preview, cleaned_subject)
    return f"Email from {sender_label}: {base}"


def _build_email_note(snippet, summary):
    preview = _clean_extracted_text(snippet, 180)
    if not preview:
        return ""
    if preview.lower() in (summary or "").lower():
        return ""
    return preview


def _summarize_email_message(subject, sender, snippet, body_text, strategy):
    cleaned_snippet = _clean_extracted_text(snippet, 280)
    cleaned_body = _clean_extracted_text(body_text, MAX_EMAIL_BODY_CHARS)
    prompt = (
        get_prompt_variant("inbox_summary", strategy)
        + " Ignore HTML tags, template filler, legal boilerplate, tracking links, repeated invoice blocks, and promotional clutter. "
        + "State what happened, whether action is required, and keep the summary under 2 sentences."
    )
    summary_input = "\n".join(
        [
            f"Subject: {_clean_extracted_text(subject, 160)}",
            f"From: {_sender_name(sender)}",
            f"Preview: {cleaned_snippet or 'No preview available.'}",
            "Body excerpt:",
            cleaned_body or "No readable body content.",
        ]
    )

    try:
        summary = chat_completion(prompt, summary_input[:MAX_SUMMARY_CHARS])
        if _summary_looks_noisy(summary, max_chars=240):
            return _fallback_email_summary(subject, sender, cleaned_snippet, cleaned_body)
        return summary
    except Exception:
        return _fallback_email_summary(subject, sender, cleaned_snippet, cleaned_body)


def _fallback_document_summary(filename, text):
    cleaned_name = filename or "document"
    cleaned_text = _clean_extracted_text(text, MAX_DOCUMENT_BODY_CHARS)
    lowered = cleaned_text.lower()
    amount = _extract_amount(cleaned_text)
    date_label = _extract_date(cleaned_text)
    action_hint = _find_action_hint(cleaned_text)

    if any(token in lowered for token in ("invoice", "receipt", "amount due", "subtotal", "payment history")):
        details = []
        if amount:
            details.append(amount)
        if date_label:
            details.append(date_label)
        suffix = f" ({', '.join(details)})" if details else ""
        return f"{cleaned_name} appears to be a billing document{suffix}. {'No clear action required.' if not action_hint else action_hint}"

    if any(token in lowered for token in ("bank", "upi", "debited", "credited", "transaction reference")):
        return f"{cleaned_name} appears to be a banking or payment document. {_first_sentence(cleaned_text)}"

    return _first_sentence(cleaned_text, f"{cleaned_name} contains readable text but no concise summary could be generated.")


def _parse_reminder_datetime(when_text):
    settings = {
        "TIMEZONE": "Asia/Kolkata",
        "RETURN_AS_TIMEZONE_AWARE": False,
        "PREFER_DATES_FROM": "future",
    }
    parsed_time = dateparser.parse(when_text, settings=settings)
    if parsed_time:
        return parsed_time

    matches = search_dates(when_text, settings=settings) or []
    if not matches:
        return None

    if len(matches) >= 2:
        first_phrase, first_time = matches[0]
        second_phrase, second_time = matches[1]
        first_has_time = bool(re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", first_phrase, re.IGNORECASE))
        second_has_time = bool(re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", second_phrase, re.IGNORECASE))
        if not first_has_time and second_has_time and first_time.date() == second_time.date():
            return second_time

    return matches[0][1]


def _build_google_service(api_name, version, client_id, client_secret, refresh_token, scopes):
    if not client_id or not client_secret or not refresh_token:
        raise ValueError(
            f"Missing Google credentials for {api_name}. Check your .env file."
        )

    credentials = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )
    try:
        credentials.refresh(Request())
    except Exception as exc:
        if "invalid_grant" in str(exc):
            raise ValueError(
                f"Google {api_name} authentication failed with invalid_grant. "
                "The refresh token or OAuth client credentials in .env need to be regenerated."
            ) from exc
        raise
    return build(api_name, version, credentials=credentials, cache_discovery=False)


def _user_refresh_token(user):
    token = (user or {}).get("refresh_token")
    if not token:
        raise ValueError(
            "You are not connected to Google. Please sign in again to grant access."
        )
    return token


def _get_gmail_service(user, scopes):
    return _build_google_service(
        "gmail",
        "v1",
        os.getenv("GOOGLE_CLIENT_ID"),
        os.getenv("GOOGLE_CLIENT_SECRET"),
        _user_refresh_token(user),
        scopes,
    )


def _get_gmail_account_email(user):
    email = (user or {}).get("email", "")
    if email:
        return email.strip()

    try:
        service = _get_gmail_service(user, GMAIL_READONLY_SCOPE)
        profile = service.users().getProfile(userId="me").execute()
        return (profile.get("emailAddress") or "").strip()
    except Exception:
        return ""


def _send_reminder_to_gmail(user, title, when_label, when_iso, description, duration_minutes):
    recipient = _get_gmail_account_email(user)
    if not recipient:
        raise ValueError("No Gmail account email is available for reminder fallback.")

    subject = f"Reminder fallback: {title}"
    body_lines = [
        "Google Calendar was unavailable, so this reminder was saved in email.",
        "",
        f"Title: {title}",
        f"When: {when_label}",
        f"When (ISO): {when_iso}",
        f"Duration: {duration_minutes} minutes",
        f"Description: {description or 'No description'}",
    ]
    body = "\n".join(body_lines)

    service = _get_gmail_service(user, GMAIL_SEND_SCOPE)
    gmail_message = _build_gmail_message(recipient, subject, body)
    result = service.users().messages().send(userId="me", body=gmail_message).execute()
    return {
        "recipient": recipient,
        "subject": subject,
        "message_id": result.get("id", ""),
        "body": body,
    }


def _get_calendar_service(user):
    return _build_google_service(
        "calendar",
        "v3",
        os.getenv("GOOGLE_CLIENT_ID"),
        os.getenv("GOOGLE_CLIENT_SECRET"),
        _user_refresh_token(user),
        CALENDAR_SCOPE,
    )


def _header_value(headers, name):
    return next((header.get("value", "") for header in headers if header.get("name") == name), "")


def _decode_base64_text(data):
    if not data:
        return ""

    padding = len(data) % 4
    if padding:
        data += "=" * (4 - padding)

    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="ignore")


def _extract_plain_text(payload):
    if not payload:
        return ""

    mime_type = payload.get("mimeType", "")
    body_text = _decode_base64_text(payload.get("body", {}).get("data", ""))
    if mime_type == "text/plain" and body_text.strip():
        return body_text

    for part in payload.get("parts", []):
        nested = _extract_plain_text(part)
        if nested.strip():
            return nested

    return body_text


def _iter_attachment_parts(payload):
    if not payload:
        return

    for part in payload.get("parts", []):
        filename = part.get("filename", "")
        attachment_id = part.get("body", {}).get("attachmentId")
        if filename and attachment_id:
            yield part
        yield from _iter_attachment_parts(part)


def _read_text_from_path(file_path):
    suffix = Path(file_path).suffix.lower()

    if suffix == ".pdf":
        pdf_doc = fitz.open(file_path)
        try:
            pages = []
            for page in pdf_doc:
                page_text = page.get_text("text", sort=True).strip()
                if len(page_text) < 80:
                    block_text = "\n".join(
                        block[4].strip()
                        for block in page.get_text("blocks")
                        if len(block) >= 5 and block[4].strip()
                    )
                    if len(block_text) > len(page_text):
                        page_text = block_text.strip()
                pages.append(page_text)
            return "\n".join(part for part in pages if part)
        finally:
            pdf_doc.close()

    if suffix == ".docx":
        document = docx.Document(file_path)
        return "\n".join(paragraph.text for paragraph in document.paragraphs)

    if suffix == ".csv":
        frame = pd.read_csv(file_path)
        return frame.to_string(index=False)

    if suffix in {".txt", ".md", ".json", ".py", ".html", ".css", ".js"}:
        return Path(file_path).read_text(encoding="utf-8", errors="ignore")

    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type and mime_type.startswith("text/"):
        return Path(file_path).read_text(encoding="utf-8", errors="ignore")

    raise ValueError("Unsupported file type. Upload PDF, DOCX, CSV, TXT, or other text files.")


def _summarize_file_bytes(filename, file_bytes, instruction=None):
    if not file_bytes:
        raise ValueError("The uploaded file is empty.")

    suffix = Path(filename).suffix or ".txt"
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(file_bytes)
            temp_path = temp_file.name

        text = _read_text_from_path(temp_path).strip()
        if not text:
            raise ValueError("No readable text was found in the file.")

        suffix = Path(filename).suffix.lower()
        cleaned_text = _clean_extracted_text(text, MAX_DOCUMENT_BODY_CHARS)
        if len(cleaned_text) < 60:
            preview = _limit_text(cleaned_text or text, 180) or "No readable text was detected."
            if suffix == ".pdf":
                return (
                    f"I could extract only limited text from {filename}. "
                    f"Extracted text: {preview} "
                    "This usually means the PDF is image-based/scanned. Upload a text-based PDF or DOCX for a reliable summary."
                )
            return (
                f"I could extract only limited text from {filename}. "
                f"Extracted text: {preview}"
            )

        summary_instruction = instruction or (
            "You are summarizing a document the user uploaded. "
            "Write a clear, helpful summary in this exact structure:\n"
            "1) One opening paragraph (2-4 sentences) explaining what the document is and its main point.\n"
            "2) A short bulleted list titled 'Key points:' with 3-6 bullets capturing the most important facts, numbers, or arguments.\n"
            "3) If the document contains tasks, deadlines, or asks, add a section 'Action items:' with bullets. "
            "Skip this section if there are none.\n"
            "Do NOT just repeat headings or the title. Do NOT output HTML. "
            "Ignore boilerplate, page numbers, and duplicated text."
        )

        try:
            summary = chat_completion(summary_instruction, cleaned_text[:MAX_SUMMARY_CHARS])
            if _summary_looks_noisy(summary, max_chars=4000):
                return _fallback_document_summary(filename, cleaned_text)
            return summary
        except Exception:
            return _fallback_document_summary(filename, cleaned_text)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def _build_gmail_message(recipient, subject, body_text):
    message = MIMEText(body_text)
    message["to"] = recipient
    message["subject"] = subject
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return {"raw": encoded}


def _is_calendar_auth_error(exc):
    lowered = str(exc).lower()
    return (
        "invalid_grant" in lowered
        or "missing google credentials" in lowered
        or "google calendar authentication failed" in lowered
    )


def _store_local_reminder(user_id, title, when_iso, description, duration_minutes, error_text):
    try:
        client = get_supabase()
        response = (
            client.table("reminders")
            .insert(
                {
                    "user_id": user_id,
                    "title": title,
                    "when_iso": when_iso,
                    "description": description,
                    "duration_minutes": duration_minutes,
                    "source": "fallback",
                }
            )
            .execute()
        )
        if response.data:
            return {"id": response.data[0]["id"]}
    except Exception:
        pass

    # Best-effort local JSONL fallback if Supabase is unreachable
    LOCAL_REMINDER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    reminder_id = uuid4().hex
    payload = {
        "id": reminder_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "when": when_iso,
        "description": description,
        "duration_minutes": duration_minutes,
        "error": error_text,
    }
    with LOCAL_REMINDER_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
    return payload


def _derive_name_from_email(email_addr):
    if not email_addr:
        return ""
    local = email_addr.split("@", 1)[0]
    if not local:
        return ""
    local = re.sub(r"\d+", "", local)
    parts = re.split(r"[._+-]+", local)
    cleaned_parts = [part for part in parts if part]
    if not cleaned_parts:
        return ""
    if len(cleaned_parts) == 1:
        chunk = cleaned_parts[0]
        split = re.findall(r"[A-Z][a-z]+|[a-z]+", chunk)
        if len(split) >= 2:
            cleaned_parts = split
        else:
            return chunk.capitalize()
    return " ".join(part.capitalize() for part in cleaned_parts)


def _infer_email_subject(subject, message):
    cleaned_subject = (subject or "").strip()
    if cleaned_subject:
        return cleaned_subject

    lowered = (message or "").lower()
    if re.search(r"\b(meeting|meet|schedule|call)\b", lowered):
        return "Meeting request"
    if re.search(r"\b(follow up|follow-up)\b", lowered):
        return "Follow-up"
    if re.search(r"\b(update|status)\b", lowered):
        return "Quick update"
    return "Message from AutoPilot AI"


def _default_email_draft(subject):
    lowered = (subject or "").lower()
    if re.search(r"\b(meeting|meet|schedule|call)\b", lowered):
        return (
            "Hi,\n\n"
            "I hope you're doing well. Could we schedule a meeting tomorrow morning? "
            "Please share a time that works for you.\n\n"
            "Best regards,"
        )

    return (
        "Hi,\n\n"
        "I hope you're doing well. Please let me know a convenient time to connect.\n\n"
        "Best regards,"
    )


def _attachment_summaries(service, message_id, payload):
    summaries = []
    for part in _iter_attachment_parts(payload):
        filename = part.get("filename", "")
        attachment_id = part.get("body", {}).get("attachmentId")
        if not filename or not attachment_id:
            continue

        attachment = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        try:
            summary = _summarize_file_bytes(
                filename,
                base64.urlsafe_b64decode(attachment["data"].encode("utf-8")),
            )
        except Exception as exc:
            summary = f"Attachment summary failed: {exc}"

        summaries.append({"filename": filename, "summary": summary})

    return summaries


def summarize_inbox(user, limit=5):
    if limit < 1:
        limit = 1
    user_id = user["id"]
    strategy = select_strategy(user_id, "inbox_summary")

    try:
        service = _get_gmail_service(user, GMAIL_READONLY_SCOPE)
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=min(limit, 10))
            .execute()
        )
        messages = result.get("messages", [])
        if not messages:
            return _success(
                "mail_summary",
                "Inbox summary",
                "No emails were found in your inbox.",
                export_text="Inbox summary\n\nNo emails were found in your inbox.",
            )

        items = []
        export_sections = []

        for message in messages:
            message_data = (
                service.users().messages().get(userId="me", id=message["id"], format="full").execute()
            )
            payload = message_data.get("payload", {})
            headers = payload.get("headers", [])
            subject = _header_value(headers, "Subject") or "No Subject"
            sender = _header_value(headers, "From") or "Unknown sender"
            snippet = message_data.get("snippet", "").strip()
            body_text = _extract_email_body(payload).strip() or _clean_extracted_text(snippet, 280)
            summary = _summarize_email_message(subject, sender, snippet, body_text, strategy)
            note = _build_email_note(snippet, summary)
            attachments = _attachment_summaries(service, message["id"], payload)

            items.append(
                {
                    "title": subject,
                    "subtitle": f"From: {sender}",
                    "body": summary,
                    "note": "",
                    "attachments": attachments,
                }
            )

            attachment_text = ""
            if attachments:
                attachment_text = "\nAttachments:\n" + "\n".join(
                    f"- {attachment['filename']}: {attachment['summary']}"
                    for attachment in attachments
                )

            export_sections.append(
                "\n".join(
                    [
                        subject,
                        f"From: {sender}",
                        f"Summary: {summary}",
                        f"Snippet: {note or 'No snippet shown.'}",
                    ]
                )
                + attachment_text
            )

        return attach_trace(
            _success(
                "mail_summary",
                "Inbox summary",
                f"Summarized {len(items)} emails from your inbox.",
                items=items,
                meta={"count": len(items)},
                export_text="\n\n".join(export_sections),
            ),
            user_id,
            "inbox_summary",
            strategy,
            {"limit": limit},
        )
    except Exception as exc:
        return _error(str(exc), title="Could not summarize inbox")


def send_email_message(user, recipient, subject, message, polish=True):
    recipient = (recipient or "").strip()
    subject = (subject or "").strip()
    message = (message or "").strip()
    user_id = user["id"]
    strategy = select_strategy(user_id, "email_polish")

    if not recipient:
        return _error(
            "Recipient is required before sending email.",
            title="Missing email fields",
        )

    try:
        service = _get_gmail_service(user, GMAIL_SEND_SCOPE)
        draft_message = message or _default_email_draft(subject)
        final_subject = _infer_email_subject(subject, draft_message)
        sender_name = (user.get("name") or "").strip() or _derive_name_from_email(user.get("email", ""))
        recipient_name = _derive_name_from_email(recipient)
        polish_skipped_reason = ""
        if polish:
            try:
                final_body = polish_message(
                    draft_message,
                    subject=final_subject,
                    instruction=get_prompt_variant("email_polish", strategy),
                    sender_name=sender_name,
                    recipient_email=recipient,
                    recipient_name_hint=recipient_name,
                )
            except Exception as polish_exc:
                final_body = draft_message
                polish_skipped_reason = str(polish_exc)
        else:
            final_body = draft_message
        gmail_message = _build_gmail_message(recipient, final_subject, final_body)
        service.users().messages().send(userId="me", body=gmail_message).execute()

        status_note = ""
        if polish_skipped_reason:
            status_note = f" Note: AI polishing was skipped ({polish_skipped_reason}) — original draft was sent."

        export_text = "\n".join(
            [
                "Email sent",
                f"To: {recipient}",
                f"Subject: {final_subject}",
                "",
                final_body,
            ]
        )

        return attach_trace(
            _success(
                "email_send",
                "Email sent",
                f"Delivered to {recipient}.{status_note}",
                items=[
                    {
                        "title": final_subject,
                        "subtitle": f"To: {recipient}",
                        "body": final_body,
                    }
                ],
                meta={
                    "recipient": recipient,
                    "polish_skipped": bool(polish_skipped_reason),
                },
                export_text=export_text,
            ),
            user_id,
            "email_polish",
            strategy,
            {"recipient": recipient, "subject": final_subject, "polish": polish and not polish_skipped_reason},
        )
    except Exception as exc:
        return _error(str(exc), title="Could not send email")


def set_reminder(user, title, when_text, description="", duration_minutes=60):
    title = (title or "").strip()
    when_text = (when_text or "").strip()
    description = (description or "").strip()
    user_id = user["id"]

    if not title or not when_text:
        return _error(
            "Reminder title and time are both required.",
            title="Missing reminder fields",
        )

    try:
        parsed_time = _parse_reminder_datetime(when_text)
        if not parsed_time:
            return _error(
                "Could not understand the reminder time. Try a clearer value like 'tomorrow 8pm'.",
                title="Invalid reminder time",
            )

        duration_minutes = max(15, min(int(duration_minutes or 60), 720))
        end_time = parsed_time + timedelta(minutes=duration_minutes)
        when_label = parsed_time.strftime("%d %b %Y, %I:%M %p")

        event_body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": parsed_time.isoformat(), "timeZone": "Asia/Kolkata"},
            "end": {"dateTime": end_time.isoformat(), "timeZone": "Asia/Kolkata"},
        }

        try:
            service = _get_calendar_service(user)
            created_event = service.events().insert(calendarId="primary", body=event_body).execute()
            event_link = created_event.get("htmlLink", "")
            calendar_email = (
                created_event.get("organizer", {}).get("email")
                or created_event.get("creator", {}).get("email")
                or ""
            )
            export_text = "\n".join(
                [
                    "Reminder set",
                    f"Title: {title}",
                    f"When: {when_label}",
                    f"Duration: {duration_minutes} minutes",
                    f"Description: {description or 'No description'}",
                    f"Calendar: {calendar_email or 'primary'}",
                    f"Open: {event_link or 'N/A'}",
                    "Backend: Google Calendar",
                ]
            )
            body_text = description or "No additional description."
            if calendar_email:
                body_text += f"\nCalendar: {calendar_email}"
            if event_link:
                body_text += f"\nOpen: {event_link}"

            return _success(
                "reminder",
                "Reminder set",
                f'"{title}" is scheduled for {when_label} on {calendar_email or "your primary calendar"}.',
                items=[
                    {
                        "title": title,
                        "subtitle": when_label,
                        "body": body_text,
                    }
                ],
                meta={
                    "event_link": event_link,
                    "calendar_email": calendar_email,
                    "reminder_backend": "google_calendar",
                },
                sources=(
                    [{"title": "Open in Google Calendar", "url": event_link}]
                    if event_link
                    else []
                ),
                export_text=export_text,
            )
        except Exception as calendar_exc:
            if not _is_calendar_auth_error(calendar_exc):
                raise

            try:
                fallback_email = _send_reminder_to_gmail(
                    user,
                    title,
                    when_label,
                    parsed_time.isoformat(),
                    description,
                    duration_minutes,
                )
                export_text = "\n".join(
                    [
                        "Reminder saved to Gmail",
                        f"Title: {title}",
                        f"When: {when_label}",
                        f"Duration: {duration_minutes} minutes",
                        f"Description: {description or 'No description'}",
                        f"Saved in mailbox: {fallback_email['recipient']}",
                        f"Reason: {calendar_exc}",
                    ]
                )
                return _success(
                    "reminder",
                    "Reminder saved to Gmail",
                    f'"{title}" is saved for {when_label}. Google Calendar was unavailable, so I saved it in {fallback_email["recipient"]} mail.',
                    items=[
                        {
                            "title": title,
                            "subtitle": when_label,
                            "body": (
                                (description or "No additional description.")
                                + f"\nSaved in mail: {fallback_email['recipient']}"
                            ),
                        }
                    ],
                    meta={
                        "event_link": "",
                        "reminder_backend": "gmail_fallback",
                        "fallback_mailbox": fallback_email["recipient"],
                        "fallback_message_id": fallback_email["message_id"],
                    },
                    export_text=export_text,
                )
            except Exception:
                pass

            local_reminder = _store_local_reminder(
                user_id,
                title,
                parsed_time.isoformat(),
                description,
                duration_minutes,
                str(calendar_exc),
            )
            export_text = "\n".join(
                [
                    "Reminder saved locally",
                    f"Title: {title}",
                    f"When: {when_label}",
                    f"Duration: {duration_minutes} minutes",
                    f"Description: {description or 'No description'}",
                    "Backend: Local fallback",
                    "Reason: Google Calendar is unavailable right now.",
                ]
            )

            return _success(
                "reminder",
                "Reminder saved locally",
                f'"{title}" is saved for {when_label}. Google Calendar is unavailable right now.',
                items=[
                    {
                        "title": title,
                        "subtitle": when_label,
                        "body": (description or "No additional description.") + " (Stored locally)",
                    }
                ],
                meta={
                    "event_link": "",
                    "reminder_backend": "local_fallback",
                    "local_id": local_reminder["id"],
                },
                export_text=export_text,
            )
    except Exception as exc:
        return _error(str(exc), title="Could not create reminder")


def research_topic(user, topic):
    topic = (topic or "").strip()
    user_id = user["id"]
    strategy = select_strategy(user_id, "research_brief")
    if not topic:
        return _error("Enter a topic before starting research.", title="Missing topic")

    try:
        results = search_web(topic, max_results=5)
        combined_content = "\n\n".join(
            result.get("content", "") for result in results if result.get("content")
        )
        if not combined_content.strip():
            return _error(
                "No useful content came back from the search provider.",
                title="No research results",
            )

        summary = summarize_text(
            combined_content[:MAX_RESEARCH_CHARS],
            instruction=get_prompt_variant("research_brief", strategy),
        )
        sources = [
            {
                "title": result.get("title") or result.get("url") or "Source",
                "url": result.get("url", ""),
            }
            for result in results
        ]
        export_text = "\n".join(
            [
                f"Research brief: {topic}",
                "",
                summary,
                "",
                "Sources:",
                "\n".join(source["url"] or source["title"] for source in sources) or "None",
            ]
        )

        return attach_trace(
            _success(
                "research",
                "Research brief",
                f'Created a research brief for "{topic}".',
                items=[{"title": topic, "body": summary}],
                meta={"source_count": len(sources)},
                sources=sources,
                export_text=export_text,
            ),
            user_id,
            "research_brief",
            strategy,
            {"topic": topic},
        )
    except Exception as exc:
        return _error(str(exc), title="Could not complete research")


def summarize_uploaded_file(user, filename, file_bytes):
    filename = (filename or "").strip()
    user_id = user["id"]
    strategy = select_strategy(user_id, "attachment_summary")
    if not filename:
        return _error("Choose a file to summarize.", title="Missing file")

    try:
        summary = _summarize_file_bytes(
            filename,
            file_bytes,
            instruction=get_prompt_variant("attachment_summary", strategy),
        )
        export_text = "\n".join(
            [
                f"Attachment summary: {filename}",
                "",
                summary,
            ]
        )
        payload = _success(
            "attachment_summary",
            "Attachment summary",
            f"Summarized {filename}.",
            items=[{"title": filename, "body": summary}],
            meta={"filename": filename},
            export_text=export_text,
        )
        return attach_trace(
            payload,
            user_id,
            "attachment_summary",
            strategy,
            {"filename": filename},
        )
    except Exception as exc:
        return _error(str(exc), title="Could not summarize attachment")


def _general_chat(message):
    system_prompt = (
        "You are AutoPilot AI, a friendly productivity assistant in a browser workspace. "
        "You can help with: email summaries, sending emails, setting reminders, "
        "researching topics via web search, and summarizing uploaded documents. "
        "Answer the user concisely. If their request maps to one of your tools, suggest it."
    )
    try:
        reply = chat_completion(system_prompt, message)
        return _success("chat", "AutoPilot AI", reply)
    except Exception as exc:
        return _error(str(exc), title="Could not respond")


def handle_command(user, message, context=None):
    message = (message or "").strip()
    if not message:
        return _error("Type a request before sending it.", title="Empty message")

    user_id = user["id"]

    has_context = bool(context)
    if GREETING_PATTERNS.match(message) and not has_context:
        response = _general_chat(message)
        response["response"]["meta"]["intent"] = "greeting"
        response["response"]["meta"]["routing_strategy"] = "direct"
        return response

    routing_strategy = select_strategy(user_id, "intent_routing")
    parsed = parse_intent_with_llm(message, strategy=routing_strategy, context=context)
    intent = parsed.get("intent", "unknown")

    if intent == "summarize_mails":
        response = summarize_inbox(user)
    elif intent == "send_email":
        response = send_email_message(
            user,
            parsed.get("email", ""),
            parsed.get("subject", ""),
            parsed.get("message", ""),
            polish=True,
        )
    elif intent == "set_reminder":
        response = set_reminder(
            user,
            parsed.get("task", "Reminder"),
            parsed.get("time", ""),
            parsed.get("description", ""),
        )
    elif intent in {"do_research", "research"}:
        response = research_topic(user, parsed.get("topic") or message)
    elif intent == "summarize_attachments":
        response = _error(
            "Use the file upload tool to summarize attachments in the browser app.",
            title="File upload required",
            response_type="attachment_hint",
        )
    elif intent == "get_weather":
        response = _error(
            "Weather is not implemented in the browser app yet.",
            title="Feature not available",
            response_type="unsupported",
        )
    elif intent == "general_chat":
        response = _general_chat(message)
    else:
        response = _general_chat(message)
        intent = "general_chat"

    response["response"]["meta"]["intent"] = intent
    response["response"]["meta"]["routing_strategy"] = routing_strategy
    return response
