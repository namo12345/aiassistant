import json
import re

from dateparser.search import search_dates

from utils.llm_util import chat_completion
from utils.intent_parser import parse_intent

PROMPT_VARIANTS = {
    "strict_json": """
You are an intent parser for a productivity assistant.
Return only one JSON object and no extra text.

Supported intents:
- summarize_mails
- summarize_attachments
- set_reminder
- send_email
- do_research
- research
- get_weather
- general_chat
- unknown

Rules:
- For send_email include "email", optional "subject", and "message".
- For set_reminder include "task", "time", and optional "description".
- For research include "topic".
- If the user mentions files, PDFs, attachments, or documents, use summarize_attachments.
- If the user is making casual conversation, asking general questions, or greeting, use general_chat.
- If the request does not match a supported tool, use general_chat (not unknown).
""",
    "workflow_json": """
You route browser productivity tasks for a single-user assistant.
Return only one JSON object and no extra text.

Supported intents:
- summarize_mails
- summarize_attachments
- set_reminder
- send_email
- do_research
- research
- get_weather
- general_chat
- unknown

Rules:
- Extract email, subject, and message for send_email if possible.
- Extract task, time, and optional description for reminders.
- Extract a research topic for research requests.
- Use summarize_attachments for files, PDFs, docs, attachments, and uploads.
- If the user is chatting casually, greeting, or asking general questions, use general_chat.
- If the request is ambiguous, prefer the most likely productivity action instead of explaining it.
""",
}

EMAIL_PATTERN = re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _clean_email_message_hint(text):
    message = (text or "").strip(" \t\r\n,.;:-")
    if not message:
        return ""

    message = re.sub(r"(?i)^(?:that\s+)?(?:says?|saying|message|about|regarding)\s+", "", message).strip()
    message = re.sub(r"(?i)^to\s+(?=ask\b|request\b|let\b|inform\b|invite\b|schedule\b|confirm\b|check\b|follow\s*up\b|see\b|join\b|meet\b|share\b|review\b|discuss\b)", "", message).strip()
    message = re.sub(r"(?i)^please\s+", "", message).strip()
    return message


def _extract_email_fields(user_input):
    text = (user_input or "").strip()
    email_match = EMAIL_PATTERN.search(text)
    email = email_match.group(1) if email_match else ""

    message = ""
    if email_match:
        after_email = _clean_email_message_hint(text[email_match.end() :])
        if after_email:
            message = after_email

    if not message:
        patterns = (
            r"(?i)\b(?:saying|that|message|about|regarding)\s+(.+)$",
            r"(?i)\bto\s+(.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                message = _clean_email_message_hint(match.group(1))
                if message:
                    break

    if not message:
        without_email = EMAIL_PATTERN.sub(" ", text, count=1).strip() if email else text
        without_command = re.sub(
            r"(?i)^\s*(?:you\s+)?(?:please\s+)?(?:can you\s+|could you\s+)?(?:send(?:\s+an?)?\s+)?(?:mail|email)\b(?:\s+to)?\s*",
            "",
            without_email,
        ).strip()
        message = _clean_email_message_hint(without_command)

    return {"email": email, "message": message}


def _extract_json_block(content):
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in LLM response.")

    return json.loads(match.group(0))


def parse_intent_with_llm(user_input, strategy="strict_json"):
    if not user_input or not user_input.strip():
        return {}

    try:
        content = chat_completion(PROMPT_VARIANTS.get(strategy, PROMPT_VARIANTS["strict_json"]), user_input)
        parsed = _extract_json_block(content)
    except Exception:
        parsed = _fallback_parse(user_input)

    return _normalize_result(user_input, parsed)


def _normalize_result(user_input, parsed):
    if not isinstance(parsed, dict):
        return _fallback_parse(user_input)

    normalized = dict(parsed)
    lowered = user_input.strip().lower()
    intent = normalized.get("intent", "unknown")

    if intent in {"do_research", "research"} and not _has_research_signal(lowered):
        return {"intent": "unknown"}

    if intent == "set_reminder":
        reminder_fields = _extract_reminder_fields(user_input)
        if reminder_fields["time"]:
            normalized["time"] = reminder_fields["time"]
        if reminder_fields["task"] and (
            not normalized.get("task")
            or normalized.get("task", "").strip().lower() in {"reminder", "set reminder"}
        ):
            normalized["task"] = reminder_fields["task"]
    elif intent == "send_email":
        email_fields = _extract_email_fields(user_input)
        if email_fields["email"] and not normalized.get("email"):
            normalized["email"] = email_fields["email"]
        if email_fields["message"] and not normalized.get("message"):
            normalized["message"] = email_fields["message"]
        if not normalized.get("subject") and normalized.get("message"):
            if re.search(r"(?i)\b(meeting|meet|schedule|call)\b", normalized["message"]):
                normalized["subject"] = "Meeting request"

    return normalized


def _has_research_signal(lowered):
    return any(
        signal in lowered
        for signal in (
            "research",
            "deep research",
            "tell me about",
            "what is",
            "who is",
            "explain",
            "learn about",
            "find information",
        )
    )


def _extract_reminder_fields(user_input):
    cleaned = re.sub(r"(?i)^\s*(remind me|set a reminder|set reminder|create a reminder)\s*", "", user_input).strip()
    matches = search_dates(
        cleaned,
        settings={
            "TIMEZONE": "Asia/Kolkata",
            "RETURN_AS_TIMEZONE_AWARE": False,
            "PREFER_DATES_FROM": "future",
        },
    ) or []

    if not matches:
        task_match = re.search(r"(?:to|for)\s+(.+)", cleaned, flags=re.IGNORECASE)
        return {
            "task": task_match.group(1).strip() if task_match else "Reminder",
            "time": "",
        }

    time_phrase, _ = matches[0]
    task = cleaned.replace(time_phrase, " ", 1)
    task = re.sub(r"(?i)\b(to|for)\b", " ", task)
    task = re.sub(r"\s+", " ", task).strip(" ,.-")

    return {
        "task": task or "Reminder",
        "time": time_phrase.strip(),
    }


def _fallback_parse(user_input):
    text = user_input.strip()
    lowered = text.lower()
    if "research" in lowered or "deep research" in lowered:
        intent = "deepresearch"
    elif "remind me" in lowered or "reminder" in lowered:
        intent = "reminder"
    elif re.search(r"\bsend\b.*\b(mail|email)\b", lowered) or "email " in lowered:
        intent = "sendmail"
    elif "summary" in lowered or "summarize" in lowered:
        intent = "summary"
    elif "weather" in lowered:
        intent = "weather"
    else:
        intent = parse_intent(text)

    email_fields = _extract_email_fields(text)

    if intent == "summary":
        if any(word in lowered for word in ("attachment", "attachments", "document", "pdf", "doc", "file", "upload")):
            return {"intent": "summarize_attachments"}
        return {"intent": "summarize_mails"}

    if intent == "sendmail":
        return {
            "intent": "send_email",
            "email": email_fields["email"],
            "subject": "",
            "message": email_fields["message"],
        }

    if intent == "reminder":
        reminder_fields = _extract_reminder_fields(text)
        return {"intent": "set_reminder", **reminder_fields}

    if intent == "deepresearch":
        topic = re.sub(r"(?i)^(do )?(deep )?research( about| on)?", "", text).strip()
        return {"intent": "do_research", "topic": topic or text}

    if intent == "weather":
        return {"intent": "get_weather"}

    return {"intent": "unknown"}
