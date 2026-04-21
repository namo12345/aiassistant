import json
import math
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path("/tmp") if os.getenv("VERCEL") else Path(__file__).resolve().parent.parent / "data"
POLICY_PATH = DATA_DIR / "policy_state.json"
TRACE_LOG_PATH = DATA_DIR / "interaction_traces.jsonl"
FEEDBACK_LOG_PATH = DATA_DIR / "feedback_events.jsonl"
AGENT_LIGHTNING_EXPORT_PATH = DATA_DIR / "agent_lightning_events.jsonl"

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

_LOCK = threading.Lock()
_AGENT_LIGHTNING_STATUS = None


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _default_policy_state():
    return {
        "updated_at": _timestamp(),
        "skills": {
            skill: {
                variant: {"count": 0, "total_reward": 0.0}
                for variant in variants
            }
            for skill, variants in POLICY_VARIANTS.items()
        },
    }


def _ensure_storage():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not POLICY_PATH.exists():
        POLICY_PATH.write_text(json.dumps(_default_policy_state(), indent=2), encoding="utf-8")
    for path in (TRACE_LOG_PATH, FEEDBACK_LOG_PATH, AGENT_LIGHTNING_EXPORT_PATH):
        if not path.exists():
            path.write_text("", encoding="utf-8")


def _load_policy_state():
    _ensure_storage()
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def _save_policy_state(state):
    state["updated_at"] = _timestamp()
    POLICY_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _append_jsonl(path, payload):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def get_prompt_variant(skill, strategy):
    return PROMPT_VARIANTS[skill][strategy]


def select_strategy(skill):
    with _LOCK:
        state = _load_policy_state()
        skill_state = state["skills"].setdefault(
            skill,
            {variant: {"count": 0, "total_reward": 0.0} for variant in POLICY_VARIANTS[skill]},
        )

        for variant, stats in skill_state.items():
            if stats["count"] == 0:
                return variant

        total_count = sum(stats["count"] for stats in skill_state.values())
        scores = {}
        for variant, stats in skill_state.items():
            average_reward = stats["total_reward"] / stats["count"]
            exploration_bonus = math.sqrt(2 * math.log(total_count) / stats["count"])
            scores[variant] = average_reward + exploration_bonus

        return max(scores, key=scores.get)


def _agent_lightning_status():
    global _AGENT_LIGHTNING_STATUS

    if _AGENT_LIGHTNING_STATUS is not None:
        return _AGENT_LIGHTNING_STATUS

    try:
        import agentlightning  # noqa: F401

        _AGENT_LIGHTNING_STATUS = {"available": True, "mode": "native"}
    except Exception as exc:
        _AGENT_LIGHTNING_STATUS = {
            "available": False,
            "mode": "compat-export",
            "reason": str(exc),
        }

    return _AGENT_LIGHTNING_STATUS


def attach_trace(payload, skill, strategy, request_payload):
    if not payload.get("ok"):
        return payload

    _ensure_storage()
    trace_id = uuid.uuid4().hex
    trace = {
        "trace_id": trace_id,
        "timestamp": _timestamp(),
        "skill": skill,
        "strategy": strategy,
        "request": request_payload,
        "response": payload["response"].get("export_text", payload["response"].get("text", "")),
    }

    with _LOCK:
        _append_jsonl(TRACE_LOG_PATH, trace)

    payload["response"]["meta"]["trace_id"] = trace_id
    payload["response"]["meta"]["skill"] = skill
    payload["response"]["meta"]["strategy"] = strategy
    payload["response"]["meta"]["learning_mode"] = "ucb_bandit"
    payload["response"]["meta"]["agent_lightning_mode"] = _agent_lightning_status()["mode"]
    return payload


def _find_trace(trace_id):
    if not TRACE_LOG_PATH.exists():
        return None

    with TRACE_LOG_PATH.open("r", encoding="utf-8") as handle:
        for line in reversed(handle.readlines()):
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get("trace_id") == trace_id:
                return payload

    return None


def record_feedback(trace_id, reward, label=None, comment=""):
    reward_value = float(reward)
    trace = _find_trace(trace_id)
    if trace is None:
        return {
            "ok": False,
            "message": "Trace not found for feedback.",
        }

    with _LOCK:
        state = _load_policy_state()
        skill_state = state["skills"][trace["skill"]][trace["strategy"]]
        skill_state["count"] += 1
        skill_state["total_reward"] += reward_value
        average_reward = skill_state["total_reward"] / skill_state["count"]
        _save_policy_state(state)

        feedback_event = {
            "trace_id": trace_id,
            "timestamp": _timestamp(),
            "skill": trace["skill"],
            "strategy": trace["strategy"],
            "reward": reward_value,
            "label": label or ("positive" if reward_value >= 0.5 else "negative"),
            "comment": comment,
        }
        _append_jsonl(FEEDBACK_LOG_PATH, feedback_event)
        _append_jsonl(
            AGENT_LIGHTNING_EXPORT_PATH,
            {
                "timestamp": _timestamp(),
                "trace": trace,
                "feedback": feedback_event,
                "agent_lightning": _agent_lightning_status(),
            },
        )

    return {
        "ok": True,
        "message": "Feedback recorded.",
        "trace_id": trace_id,
        "skill": trace["skill"],
        "strategy": trace["strategy"],
        "average_reward": round(average_reward, 4),
        "count": skill_state["count"],
        "agent_lightning": _agent_lightning_status(),
    }


def get_learning_status():
    with _LOCK:
        state = _load_policy_state()

    summary = {}
    for skill, variants in state["skills"].items():
        summary[skill] = {}
        for variant, stats in variants.items():
            count = stats["count"]
            total_reward = stats["total_reward"]
            summary[skill][variant] = {
                "count": count,
                "average_reward": round(total_reward / count, 4) if count else 0.0,
            }

    return {
        "ok": True,
        "updated_at": state["updated_at"],
        "agent_lightning": _agent_lightning_status(),
        "policy": summary,
    }
