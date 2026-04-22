import json
import re

from dateparser.search import search_dates

from utils.llm_util import chat_completion
from utils.intent_parser import parse_intent

PROMPT_VARIANTS = {
    "strict_json": """
You are the intent router for a productivity assistant with many real tools.
Return ONLY a JSON object. No markdown, no prose, no explanation.

Intents:
- summarize_mails     -> the user wants their Gmail inbox summarized.
- search_inbox        -> the user wants to find specific emails matching a query.
                         Examples: "find emails from vercel", "any mails from sana last week",
                         "search inbox for invoice". Extract: query (Gmail-compatible search
                         string — use `from:`, `subject:`, `after:` hints when clear).
- send_email          -> the user wants to send a NEW email. Extract: email, subject (optional), message.
- reply_email         -> the user wants to REPLY to an existing email thread. Extract:
                         query (subject or sender hint to find the thread), message (the reply text),
                         optional: message_id, subject.
                         Examples: "reply to the last email from sana saying I'll be there",
                         "send a reply to the vercel email telling them it's resolved".
- set_reminder        -> the user wants a calendar reminder. Extract: task, time, description (optional).
- list_events         -> the user wants to see upcoming events / their schedule.
                         Examples: "what's on my schedule", "my calendar this week". Optional: days_ahead.
- reschedule_event    -> the user wants to MOVE an existing event to a new time. Extract:
                         query (event title/keyword), time (new time), optional: event_id, duration_minutes.
                         Examples: "move the standup to 4pm tomorrow", "reschedule my dentist to friday 11am".
- cancel_event        -> the user wants to DELETE an event. Extract: query (event title/keyword).
                         Examples: "cancel the 3pm meeting", "delete tomorrow's standup".
- daily_briefing      -> the user wants a one-shot digest of today: inbox + calendar + reminders.
                         Examples: "give me my briefing", "what's my day look like", "morning summary",
                         "what should I know today".
- do_research         -> ANY question that needs external/world knowledge to answer well:
                         facts, people, places, concepts, news, career advice, how-to, suggestions,
                         comparisons, current trends, recommendations, explanations. Extract: topic.
- summarize_attachments -> the user mentions a file/PDF/doc/attachment they want summarized.
- general_chat        -> pure social chat: greetings, thanks, small talk, acknowledgements,
                         meta-questions about the assistant itself ("who are you?", "what can you do?").

When in doubt between do_research and general_chat, prefer do_research — answering from memory
is worse than fetching real sources. Short confirmations like "yes", "sure", "go ahead" should
reuse the topic/action from the prior assistant message if one is shown in context.

For do_research, the "topic" should be a concise search query (3-10 words), stripped of filler
like "please tell me", "I want to know", "can you". Examples:
- "I want to know about cats please tell me" -> topic: "cats"
- "what is RAG" -> topic: "retrieval augmented generation"

Disambiguation rules:
- "send an email to X saying Y" => send_email
- "reply to X saying Y" / "respond to the email from X" => reply_email
- "find / search mails" => search_inbox (do NOT use summarize_mails unless the user just wants an overview)
- "move / reschedule / push X to <time>" => reschedule_event
- "cancel / delete / drop X" => cancel_event
- "briefing / my day / what's up today" => daily_briefing

Context handling: if a "Previous assistant message" is provided and the current user message
is a short confirmation ("yes", "sure", "please do", "go ahead"), infer the intent and topic
from what the assistant proposed. Do NOT answer with general_chat in that case.

Output shape:
{"intent": "<one of the above>", ...extracted fields}
""",
    "workflow_json": """
Route the user's message to one of these tools for a browser productivity assistant.
Return ONLY one JSON object.

Tools:
- summarize_mails: inbox summary overview.
- search_inbox: search Gmail (field: query — Gmail search syntax OK).
- send_email: send a NEW email (fields: email, subject, message).
- reply_email: reply to an existing thread (fields: query, message; optional message_id/subject).
- set_reminder: create a calendar reminder (fields: task, time, description).
- list_events: show upcoming calendar events (optional field: days_ahead).
- reschedule_event: move an existing event (fields: query, time; optional duration_minutes/event_id).
- cancel_event: delete an event (fields: query or event_id).
- daily_briefing: combined digest of today's inbox, calendar, reminders — for "my day" questions.
- do_research: anything requiring external knowledge (field: topic).
- summarize_attachments: user referenced a file/doc/PDF to summarize.
- general_chat: greetings, thanks, or meta questions about the assistant itself.

Bias: when the user asks ANY factual or advice question, prefer do_research. Only use
general_chat for pure social/conversational messages.

Prefer reply_email over send_email when the user refers to an existing email/thread/conversation.
Prefer reschedule_event / cancel_event over set_reminder when the user references an existing event.

For short acknowledgements ("yes", "please do", "go ahead") when a Previous assistant message
is provided, inherit the action the assistant proposed.
""",
}

EMAIL_PATTERN = re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
REMINDER_PARSE_SETTINGS = {
    "TIMEZONE": "Asia/Kolkata",
    "RETURN_AS_TIMEZONE_AWARE": False,
    "PREFER_DATES_FROM": "future",
}
REMINDER_NOISE_TOKENS = {"to", "for", "on", "at", "in", "me", "a", "an"}
REMINDER_MONTH_PATTERN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
REMINDER_EXPLICIT_DAY_MONTH_PATTERN = re.compile(
    rf"(?i)\b(\d{{1,2}}(?:st|nd|rd|th)?\s+{REMINDER_MONTH_PATTERN}(?:\s+\d{{4}})?)\b"
)
REMINDER_EXPLICIT_MONTH_DAY_PATTERN = re.compile(
    rf"(?i)\b({REMINDER_MONTH_PATTERN}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s+\d{{4}})?)\b"
)
REMINDER_EXPLICIT_TIME_PATTERN = re.compile(
    r"(?i)\b("
    r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|"
    r"noon|midnight"
    r")\b"
)
REMINDER_TIME_COMPONENT_PATTERN = re.compile(
    r"(?i)\b("
    r"\d{1,2}(:\d{2})?\s*(a\.?m\.?|p\.?m\.?)|"
    r"\d{1,2}:\d{2}|"
    r"noon|midnight"
    r")\b"
)
REMINDER_DATE_COMPONENT_PATTERN = re.compile(
    r"(?i)\b("
    r"today|tomorrow|tonight|next|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"\d{1,2}(st|nd|rd|th)\b|"
    r"\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b|"
    r"\d{1,2}\s+"
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b|"
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}\b"
    r")\b"
)
REMINDER_SIGNAL_PATTERN = re.compile(
    r"(?i)("
    r"\d|am|pm|noon|midnight|"
    r"today|tomorrow|tonight|next|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
    r")"
)


def _has_time_component(text):
    return bool(REMINDER_TIME_COMPONENT_PATTERN.search(text or ""))


def _has_date_component(text):
    return bool(REMINDER_DATE_COMPONENT_PATTERN.search(text or ""))


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


def parse_intent_with_llm(user_input, strategy="strict_json", context=None):
    if not user_input or not user_input.strip():
        return {}

    context_block = _format_context(context)
    user_message = user_input.strip()
    if context_block:
        llm_user_prompt = f"{context_block}\n\nCurrent user message: {user_message}"
    else:
        llm_user_prompt = user_message

    try:
        content = chat_completion(
            PROMPT_VARIANTS.get(strategy, PROMPT_VARIANTS["strict_json"]),
            llm_user_prompt,
        )
        parsed = _extract_json_block(content)
    except Exception:
        parsed = _fallback_parse(user_input)

    return _normalize_result(user_input, parsed)


def _format_context(context):
    if not context:
        return ""
    turns = []
    for entry in context[-4:]:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role") or ""
        text = (entry.get("text") or entry.get("content") or "").strip()
        if not text:
            continue
        label = "Assistant" if role == "assistant" else "User"
        turns.append(f"{label}: {text[:400]}")
    if not turns:
        return ""
    return "Recent conversation (most recent last):\n" + "\n".join(turns)


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
    return bool(lowered and lowered.strip())


def _extract_reminder_fields(user_input):
    cleaned = re.sub(
        r"(?i)^\s*(remind me|set a reminder|set reminder|create a reminder)\s*(?:to\s+)?",
        "",
        user_input,
    ).strip()
    matches = search_dates(cleaned, settings=REMINDER_PARSE_SETTINGS) or []

    candidates = []
    for phrase, parsed in matches:
        candidate = " ".join((phrase or "").split()).strip(" ,.-")
        lowered = candidate.lower()
        if not candidate or lowered in REMINDER_NOISE_TOKENS:
            continue
        if not REMINDER_SIGNAL_PATTERN.search(candidate):
            continue
        candidates.append((candidate, parsed))

    explicit_date_match = (
        REMINDER_EXPLICIT_DAY_MONTH_PATTERN.search(cleaned)
        or REMINDER_EXPLICIT_MONTH_DAY_PATTERN.search(cleaned)
    )
    explicit_time_match = REMINDER_EXPLICIT_TIME_PATTERN.search(cleaned)

    chosen_phrase = ""
    if explicit_date_match and explicit_time_match:
        explicit_date = " ".join(explicit_date_match.group(1).split())
        explicit_time = " ".join(explicit_time_match.group(1).split())
        chosen_phrase = f"{explicit_time} on {explicit_date}"

    time_candidate = ""
    date_candidate = ""

    for candidate, _ in candidates:
        if chosen_phrase:
            break
        has_time = _has_time_component(candidate)
        has_date = _has_date_component(candidate)
        if has_time and not time_candidate:
            time_candidate = candidate
        if has_date and not date_candidate:
            date_candidate = candidate
        if has_time and has_date:
            chosen_phrase = candidate
            break

    if not chosen_phrase and time_candidate and date_candidate and time_candidate != date_candidate:
        ampm_match = re.search(
            rf"(?i)\b{re.escape(time_candidate)}\s*(a\.?m\.?|p\.?m\.?)\b",
            cleaned,
        )
        time_with_meridian = time_candidate
        if ampm_match:
            time_with_meridian = f"{time_candidate} {ampm_match.group(1)}"

        clean_date = re.sub(r"(?i)^\s*(on|at|by|for)\s+", "", date_candidate).strip()
        chosen_phrase = f"{time_with_meridian} on {clean_date}".strip()

    if not chosen_phrase:
        direct_phrase_match = re.search(
            r"(?i)\b(?:on|at|by|for)\s+(.+)$",
            cleaned,
        )
        if direct_phrase_match:
            direct_phrase = direct_phrase_match.group(1).strip(" ,.-")
            if direct_phrase and REMINDER_SIGNAL_PATTERN.search(direct_phrase):
                chosen_phrase = direct_phrase

    if not chosen_phrase and candidates:
        def _score(candidate_text):
            score = 0
            if _has_time_component(candidate_text):
                score += 2
            if _has_date_component(candidate_text):
                score += 2
            score += len(candidate_text)
            return score

        chosen_phrase = max((candidate for candidate, _ in candidates), key=_score)

    if not chosen_phrase:
        task_match = re.search(r"(?:to|for)\s+(.+)", cleaned, flags=re.IGNORECASE)
        return {
            "task": task_match.group(1).strip() if task_match else "Reminder",
            "time": "",
        }

    task = cleaned
    for candidate, _ in candidates:
        task = re.sub(re.escape(candidate), " ", task, flags=re.IGNORECASE)
    task = re.sub(r"(?i)\b(a\.?m\.?|p\.?m\.?)\b", " ", task)
    task = re.sub(re.escape(chosen_phrase), " ", task, flags=re.IGNORECASE)
    task = re.sub(r"(?i)\b(to|for|on|at|by)\b", " ", task)
    task = re.sub(r"\s+", " ", task).strip(" ,.-")

    return {
        "task": task or "Reminder",
        "time": chosen_phrase,
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
