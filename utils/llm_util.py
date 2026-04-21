import os
import re
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("LLM_API_KEY")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b:free")
DEFAULT_FALLBACK_MODELS = [
    "openai/gpt-oss-120b:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-3-27b-it:free",
    "z-ai/glm-4.5-air:free",
    "openai/gpt-oss-20b:free",
]

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant")

MAX_RETRIES_PER_MODEL = 2
RETRY_BACKOFF_SECONDS = 2


def _fallback_models():
    configured = [model.strip() for model in os.getenv("LLM_FALLBACK_MODELS", "").split(",") if model.strip()]
    if configured:
        return configured
    return DEFAULT_FALLBACK_MODELS


def _extractive_summary(text, max_sentences=3):
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return "No content was available to summarize."

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    selected = [sentence.strip() for sentence in sentences if sentence.strip()][:max_sentences]
    summary = " ".join(selected).strip()

    action_signals = []
    for sentence in sentences:
        lowered = sentence.lower()
        if any(keyword in lowered for keyword in ("todo", "action", "deadline", "by ", "tomorrow", "next week", "follow up", "finish", "confirm", "prepare")):
            action_signals.append(sentence.strip())
        if len(action_signals) == 2:
            break

    if action_signals:
        summary += "\n\nAction items: " + " ".join(action_signals)

    return summary


def _call_openai_compatible(url, api_key, model, system_prompt, user_prompt, timeout, extra_headers=None):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    return response


def _try_openrouter(system_prompt, user_prompt, timeout):
    if not OPENROUTER_API_KEY:
        return None, "LLM_API_KEY is missing from the environment."

    models_to_try = []
    for model in [OPENROUTER_MODEL, *_fallback_models()]:
        if model and model not in models_to_try:
            models_to_try.append(model)

    last_error = None
    failures = []
    extra = {"HTTP-Referer": "https://autopilot.local", "X-Title": "AutoPilotAI-BrowserUI"}

    for model in models_to_try:
        for attempt in range(MAX_RETRIES_PER_MODEL):
            try:
                response = _call_openai_compatible(
                    OPENROUTER_BASE_URL, OPENROUTER_API_KEY, model,
                    system_prompt, user_prompt, timeout, extra_headers=extra,
                )
                if response.status_code == 404:
                    last_error = f"Model {model} is unavailable (404)"
                    break
                if response.status_code == 429:
                    retry_after = int(response.headers.get("retry-after", RETRY_BACKOFF_SECONDS))
                    last_error = f"Model {model} rate-limited (429)"
                    if attempt < MAX_RETRIES_PER_MODEL - 1:
                        time.sleep(min(retry_after, 5))
                        continue
                    break
                if response.status_code == 503:
                    last_error = f"Model {model} unavailable (503)"
                    if attempt < MAX_RETRIES_PER_MODEL - 1:
                        time.sleep(RETRY_BACKOFF_SECONDS)
                        continue
                    break

                response.raise_for_status()
                data = response.json()
                choices = data.get("choices")
                if not choices or not choices[0].get("message", {}).get("content"):
                    last_error = f"Model {model} returned empty response"
                    break
                return choices[0]["message"]["content"].strip(), None
            except httpx.TimeoutException:
                last_error = f"Model {model} timed out"
                break
            except httpx.HTTPStatusError as exc:
                last_error = f"Model {model} HTTP error: {exc.response.status_code}"
                break
        if last_error:
            failures.append(last_error)

    if failures and all("rate-limited" in reason or "429" in reason for reason in failures):
        return None, "All OpenRouter models are rate-limited."
    return None, last_error or "OpenRouter unavailable."


def _try_gemini(system_prompt, user_prompt, timeout):
    if not GEMINI_API_KEY:
        return None, "GEMINI_API_KEY not set."

    for attempt in range(MAX_RETRIES_PER_MODEL):
        try:
            response = _call_openai_compatible(
                GEMINI_BASE_URL, GEMINI_API_KEY, GEMINI_MODEL,
                system_prompt, user_prompt, timeout,
            )
            if response.status_code == 429:
                if attempt < MAX_RETRIES_PER_MODEL - 1:
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                return None, "Gemini rate-limited (429)."
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            if not choices or not choices[0].get("message", {}).get("content"):
                return None, "Gemini returned empty response."
            return choices[0]["message"]["content"].strip(), None
        except httpx.TimeoutException:
            return None, "Gemini request timed out."
        except httpx.HTTPStatusError as exc:
            return None, f"Gemini HTTP error: {exc.response.status_code}"
        except Exception as exc:
            return None, f"Gemini request failed: {exc}"

    return None, "Gemini unavailable."


def _try_groq(system_prompt, user_prompt, timeout):
    if not GROQ_API_KEY:
        return None, "GROQ_API_KEY not set."

    models_to_try = []
    for model in [GROQ_MODEL, GROQ_FALLBACK_MODEL]:
        if model and model not in models_to_try:
            models_to_try.append(model)

    last_error = None
    for model in models_to_try:
        for attempt in range(MAX_RETRIES_PER_MODEL):
            try:
                response = _call_openai_compatible(
                    GROQ_BASE_URL, GROQ_API_KEY, model,
                    system_prompt, user_prompt, timeout,
                )
                if response.status_code == 404:
                    last_error = f"Groq model {model} unavailable (404)"
                    break
                if response.status_code == 429:
                    last_error = f"Groq model {model} rate-limited (429)"
                    if attempt < MAX_RETRIES_PER_MODEL - 1:
                        time.sleep(RETRY_BACKOFF_SECONDS)
                        continue
                    break
                response.raise_for_status()
                data = response.json()
                choices = data.get("choices") or []
                if not choices or not choices[0].get("message", {}).get("content"):
                    last_error = f"Groq model {model} returned empty response"
                    break
                return choices[0]["message"]["content"].strip(), None
            except httpx.TimeoutException:
                last_error = f"Groq model {model} timed out"
                break
            except httpx.HTTPStatusError as exc:
                last_error = f"Groq model {model} HTTP error: {exc.response.status_code}"
                break
            except Exception as exc:
                last_error = f"Groq model {model} failed: {exc}"
                break

    return None, last_error or "Groq unavailable."


def chat_completion(system_prompt, user_prompt, timeout=60):
    errors = []

    if GEMINI_API_KEY:
        result, error = _try_gemini(system_prompt, user_prompt, timeout)
        if result:
            return result
        if error:
            errors.append(f"Gemini: {error}")

    if GROQ_API_KEY:
        result, error = _try_groq(system_prompt, user_prompt, timeout)
        if result:
            return result
        if error:
            errors.append(f"Groq: {error}")

    if OPENROUTER_API_KEY:
        result, error = _try_openrouter(system_prompt, user_prompt, timeout)
        if result:
            return result
        if error:
            errors.append(f"OpenRouter: {error}")

    if not OPENROUTER_API_KEY and not GEMINI_API_KEY and not GROQ_API_KEY:
        raise ValueError("No LLM provider configured. Set GEMINI_API_KEY, GROQ_API_KEY, or LLM_API_KEY (OpenRouter) in .env.")

    combined = " | ".join(errors) or "unknown error"
    if all("rate-limited" in e.lower() for e in errors):
        raise ValueError(f"All LLM providers are rate-limited right now. Wait a minute and try again. ({combined})")
    raise ValueError(f"LLM is unavailable. {combined}")


def summarize_text(text, instruction=None):
    summary_prompt = instruction or (
        "Summarize the following text in 2 to 3 sentences and call out any clear action items."
    )
    return chat_completion(summary_prompt, text)


def polish_message(raw_message, subject="", instruction=None, sender_name="", recipient_email="", recipient_name_hint=""):
    system_prompt = instruction or (
        "Rewrite the user's draft as a complete, natural-sounding email. "
        "Rules: "
        "(1) Infer the recipient's first name from their email address (e.g., 'sanashaaista@gmail.com' -> 'Sana', "
        "'bob.smith@x.com' -> 'Bob'). If the email looks like a company/no-reply/newsletter address, use 'Hi there,'. "
        "(2) Open with 'Hi <FirstName>,' on its own line. "
        "(3) Keep the message concise — 2-4 short paragraphs maximum. "
        "(4) Do NOT invent facts, meeting times, dates, or details not present in the draft. "
        "(5) Close with a friendly sign-off ('Best regards,' or 'Thanks,') followed by the sender's first name on the next line. "
        "(6) Match the tone of the draft — casual if casual, professional if formal. "
        "(7) Return only the email body text. No subject line, no markdown code fences, no 'Subject:' prefix, no explanation."
    )
    user_prompt_parts = [
        f"Subject: {subject or 'No subject provided'}",
        f"Sender's full name: {sender_name or 'not provided'}",
        f"Recipient email: {recipient_email or 'not provided'}",
    ]
    if recipient_name_hint:
        user_prompt_parts.append(f"Recipient name hint (from email local-part): {recipient_name_hint}")
    user_prompt_parts.extend(["", "Draft:", raw_message])
    return chat_completion(system_prompt, "\n".join(user_prompt_parts))
