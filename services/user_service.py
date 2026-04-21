from datetime import datetime, timezone

from services.supabase_client import get_supabase


def upsert_user(google_sub, email, name, picture_url, refresh_token):
    client = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "google_sub": google_sub,
        "email": email,
        "name": name,
        "picture_url": picture_url,
        "refresh_token": refresh_token,
        "updated_at": now_iso,
    }
    response = (
        client.table("users")
        .upsert(payload, on_conflict="google_sub")
        .execute()
    )
    if not response.data:
        raise RuntimeError("Failed to upsert user.")
    return response.data[0]


def get_user(user_id):
    if not user_id:
        return None
    client = get_supabase()
    response = client.table("users").select("*").eq("id", user_id).limit(1).execute()
    if not response.data:
        return None
    return response.data[0]


def get_user_by_sub(google_sub):
    client = get_supabase()
    response = client.table("users").select("*").eq("google_sub", google_sub).limit(1).execute()
    if not response.data:
        return None
    return response.data[0]


def update_refresh_token(user_id, refresh_token):
    client = get_supabase()
    client.table("users").update(
        {"refresh_token": refresh_token, "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", user_id).execute()


def get_user_memory(user_id):
    client = get_supabase()
    response = (
        client.table("user_memory")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if response.data:
        return response.data[0]
    # Create empty memory row on first access
    client.table("user_memory").insert({"user_id": user_id}).execute()
    return {"user_id": user_id, "preferences": {}, "contacts": {}}


def update_user_memory(user_id, preferences=None, contacts=None):
    client = get_supabase()
    patch = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if preferences is not None:
        patch["preferences"] = preferences
    if contacts is not None:
        patch["contacts"] = contacts
    client.table("user_memory").upsert({"user_id": user_id, **patch}).execute()


def merge_contact_hint(user_id, contact_email, tone=None, sign_off=None):
    """Update per-contact memory incrementally."""
    if not contact_email:
        return
    memory = get_user_memory(user_id)
    contacts = memory.get("contacts") or {}
    entry = contacts.get(contact_email, {})
    if tone:
        entry["tone"] = tone
    if sign_off:
        entry["sign_off"] = sign_off
    contacts[contact_email] = entry
    update_user_memory(user_id, contacts=contacts)
