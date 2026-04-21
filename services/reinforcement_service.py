"""Per-user UCB1 bandit over prompt variants, backed by Supabase.

Each user has their own learning policy. Feedback on their responses updates
only their own bandit state — learning is isolated per-user.
"""

import json
import math
import uuid
from datetime import datetime, timezone

from services.supabase_client import get_supabase


POLICY_VARIANTS = {
    "intent_routing": ("strict_json", "workflow_json"),
    "inbox_summary": ("action_first", "priority_first"),
    "research_brief": ("takeaway_first", "structured_brief"),
    "attachment_summary": ("overview", "action_checklist"),
    "email_polish": ("professional", "friendly"),
}

PROMPT_VARIANTS = {
    "intent_routing": {
        "strict_json": (
            "You are an intent parser for a productivity assistant. "
            "Return exactly one JSON object with the fields needed for the chosen action."
        ),
        "workflow_json": (
            "You route browser productivity tasks. "
            "Return exactly one JSON object for email, reminder, research, file summary, or inbox summary."
        ),
    },
    "inbox_summary": {
        "action_first": (
            "Summarize this email in 2 to 3 sentences. "
            "Lead with action items, deadlines, and the sender's ask."
        ),
        "priority_first": (
            "Summarize this email with urgency first. "
            "State whether it is important, what the user should do next, and the main details."
        ),
    },
    "research_brief": {
        "takeaway_first": (
            "Create a compact research brief with the main idea, "
            "3 to 5 useful takeaways, and a plain-language conclusion."
        ),
        "structured_brief": (
            "Create a short research brief with these sections: Overview, Key Findings, and Recommended Next Step."
        ),
    },
    "attachment_summary": {
        "overview": (
            "Summarize the uploaded document in a concise way. "
            "Highlight the main idea and any useful action items."
        ),
        "action_checklist": (
            "Summarize the uploaded document and extract the most useful action items, decisions, or deadlines."
        ),
    },
    "email_polish": {
        "professional": (
            "Rewrite the user's draft as a complete email. Preserve the intent and keep it concise. "
            "Use a professional tone."
        ),
        "friendly": (
            "Rewrite the user's draft as a complete email. Preserve the intent, keep it concise, "
            "and make it warm but still professional."
        ),
    },
}


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def get_prompt_variant(skill, strategy):
    return PROMPT_VARIANTS[skill][strategy]


def _load_user_skill_state(user_id, skill):
    """Return dict variant -> {count, total_reward}, initializing rows on first call."""
    client = get_supabase()
    response = (
        client.table("bandit_state")
        .select("variant, count, total_reward")
        .eq("user_id", user_id)
        .eq("skill", skill)
        .execute()
    )

    by_variant = {row["variant"]: row for row in (response.data or [])}
    variants = POLICY_VARIANTS.get(skill, ())

    missing = [v for v in variants if v not in by_variant]
    if missing:
        client.table("bandit_state").insert(
            [
                {"user_id": user_id, "skill": skill, "variant": v, "count": 0, "total_reward": 0}
                for v in missing
            ]
        ).execute()
        for v in missing:
            by_variant[v] = {"variant": v, "count": 0, "total_reward": 0}

    return by_variant


def select_strategy(user_id, skill):
    """UCB1: try each variant once, then pick argmax(avg_reward + sqrt(2 ln N / n))."""
    variants = POLICY_VARIANTS.get(skill, ())
    if not variants:
        return ""

    try:
        state = _load_user_skill_state(user_id, skill)
    except Exception:
        # DB unavailable — fall back to first variant
        return variants[0]

    for variant in variants:
        if state.get(variant, {}).get("count", 0) == 0:
            return variant

    total_count = sum(state[v].get("count", 0) for v in variants)
    best_variant = variants[0]
    best_score = float("-inf")
    for variant in variants:
        stats = state[variant]
        count = stats.get("count", 0) or 1
        average_reward = (stats.get("total_reward", 0.0) or 0.0) / count
        exploration_bonus = math.sqrt(2 * math.log(max(total_count, 1)) / count)
        score = average_reward + exploration_bonus
        if score > best_score:
            best_score = score
            best_variant = variant
    return best_variant


def attach_trace(payload, user_id, skill, strategy, request_payload):
    if not payload.get("ok"):
        return payload

    try:
        client = get_supabase()
        trace_id = str(uuid.uuid4())
        response_text = payload["response"].get("export_text") or payload["response"].get("text") or ""
        client.table("traces").insert(
            {
                "id": trace_id,
                "user_id": user_id,
                "skill": skill,
                "strategy": strategy,
                "request": request_payload,
                "response_text": response_text,
            }
        ).execute()

        payload["response"]["meta"]["trace_id"] = trace_id
        payload["response"]["meta"]["skill"] = skill
        payload["response"]["meta"]["strategy"] = strategy
        payload["response"]["meta"]["learning_mode"] = "ucb_bandit_per_user"
    except Exception:
        # Trace failure must not break the user's response
        pass

    return payload


def _find_trace(user_id, trace_id):
    client = get_supabase()
    response = (
        client.table("traces")
        .select("*")
        .eq("id", trace_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not response.data:
        return None
    return response.data[0]


def record_feedback(user_id, trace_id, reward, label=None, comment=""):
    if not trace_id:
        return {"ok": False, "message": "trace_id is required."}

    try:
        reward_value = float(reward)
    except (TypeError, ValueError):
        reward_value = 0.0

    client = get_supabase()
    trace = _find_trace(user_id, trace_id)
    if trace is None:
        return {"ok": False, "message": "Trace not found for feedback."}

    skill = trace["skill"]
    strategy = trace["strategy"]

    # Read current bandit row, then upsert incremented values
    state = _load_user_skill_state(user_id, skill)
    stats = state.get(strategy, {"count": 0, "total_reward": 0.0})
    new_count = (stats.get("count", 0) or 0) + 1
    new_total = (stats.get("total_reward", 0.0) or 0.0) + reward_value

    client.table("bandit_state").upsert(
        {
            "user_id": user_id,
            "skill": skill,
            "variant": strategy,
            "count": new_count,
            "total_reward": new_total,
            "updated_at": _timestamp(),
        },
        on_conflict="user_id,skill,variant",
    ).execute()

    client.table("feedback").insert(
        {
            "trace_id": trace_id,
            "user_id": user_id,
            "reward": reward_value,
            "label": label or ("positive" if reward_value >= 0.5 else "negative"),
            "comment": comment or "",
        }
    ).execute()

    return {
        "ok": True,
        "message": "Feedback recorded.",
        "trace_id": trace_id,
        "skill": skill,
        "strategy": strategy,
        "average_reward": round(new_total / new_count, 4) if new_count else 0.0,
        "count": new_count,
    }


def get_learning_status(user_id):
    client = get_supabase()
    response = (
        client.table("bandit_state")
        .select("skill, variant, count, total_reward, updated_at")
        .eq("user_id", user_id)
        .execute()
    )

    summary = {}
    latest_update = None
    for row in response.data or []:
        skill = row["skill"]
        variant = row["variant"]
        count = row.get("count", 0) or 0
        total_reward = row.get("total_reward", 0.0) or 0.0
        summary.setdefault(skill, {})[variant] = {
            "count": count,
            "average_reward": round(total_reward / count, 4) if count else 0.0,
        }
        if row.get("updated_at") and (not latest_update or row["updated_at"] > latest_update):
            latest_update = row["updated_at"]

    return {
        "ok": True,
        "updated_at": latest_update or _timestamp(),
        "policy": summary,
    }
