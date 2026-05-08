import os
import json
import requests

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection


# -----------------------------
# CONFIG
# -----------------------------
PROCESS_NAME = "BrugAarhusRefresh"
CREDENTIAL_NAME = "BrugAarhusAPI"   # contains base_url in username and token in password

# Optional: if you want deterministic ordering for the fields in Deskpro UI:
DISPLAY_ORDER_START = 8850          # april example was 8840, so we'll continue after that
DISPLAY_ORDER_STEP = 10

MONTHS_TO_CREATE = [
    "maj",
    "juni",
    "juli",
    "august",
    "september",
    "oktober",
    "november",
    "december",
]

# Prefix used in agent/internal "title"
TITLE_PREFIX = "TEST Udeservering - Serveringsareal"
# User-facing title (translation user_title)
USER_TITLE_PREFIX = "Serveringsareal"

# Deskpro handler class for number fields
HANDLER_CLASS = "Application\\DeskPRO\\CustomFields\\Handler\\Number"


# -----------------------------
# ORCHESTRATOR CONNECTION
# -----------------------------
orchestrator_connection = OrchestratorConnection(
    PROCESS_NAME,
    os.getenv("OpenOrchestratorSQL"),
    os.getenv("OpenOrchestratorKey"),
    None,
)


def build_payload(month_name: str, display_order: float) -> dict:
    """
    Build Deskpro custom field payload matching your April example.
    """
    full_title = f"{TITLE_PREFIX} {month_name}"
    user_title = f"{USER_TITLE_PREFIX} {month_name}"

    return {
        "title": full_title,
        "description": "",
        "handler_class": HANDLER_CLASS,
        "parent_id": None,
        "default_value": None,
        "is_enabled": True,
        "is_user_enabled": True,
        "is_agent_field": False,
        "display_order": display_order,
        "aliases": [],
        # Deskpro expects options as an object (it is listed as string in docs,
        # but the API typically accepts JSON objects for options)
        "options": {
            "custom_css_classname": "",
            "required": True,
            "agent_required": True,
            "min": 1,
            "max": 1000,
            "is_select": False,
        }
    }


def create_custom_field(base_url: str, token: str, payload: dict) -> dict:
    url = f"{base_url}/api/v2/ticket_custom_fields"

    headers = {
        "Authorization": token,
        "Cookie": "dp_last_lang=da",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)

    # If Deskpro returns validation error, show full details
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to create field '{payload.get('title')}'. "
            f"Status: {resp.status_code}. Response: {resp.text}"
        )

    return resp.json()


def main():
    print("Creating remaining month number fields in Deskpro...")

    # Fetch API credentials
    cred = orchestrator_connection.get_credential(CREDENTIAL_NAME)
    base_url = cred.username.rstrip("/")
    token = cred.password

    created = []
    failed = []

    display_order = DISPLAY_ORDER_START

    for month in MONTHS_TO_CREATE:
        payload = build_payload(month, display_order)

        try:
            result = create_custom_field(base_url, token, payload)
            field_id = result.get("data", {}).get("id")
            print(
                f"✅ Created: {payload['title']} (id={field_id}, display_order={display_order})"
            )
            created.append({"month": month, "id": field_id, "title": payload["title"]})
        except Exception as e:
            print(f"❌ Failed: {payload['title']} — {e}")
            failed.append({"month": month, "title": payload["title"], "error": str(e)})

        display_order += DISPLAY_ORDER_STEP

    print(
        f"Done. Created={len(created)}, Failed={len(failed)}"
    )

    # Print a JSON summary (handy for logs)
    summary = {"created": created, "failed": failed}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
