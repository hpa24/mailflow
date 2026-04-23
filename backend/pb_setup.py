import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)


async def setup_pocketbase_schema(token: str) -> None:
    """Create all Mailflow collections in PocketBase if they don't exist."""
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=30) as client:
        # Get existing collections
        resp = await client.get("/api/collections", headers=headers, params={"perPage": 200})
        resp.raise_for_status()
        existing = {c["name"]: c["id"] for c in resp.json().get("items", [])}
        logger.info(f"Existing collections: {list(existing.keys())}")

        # 1. accounts (no dependencies)
        accounts_id = await _ensure_collection(client, headers, existing, _accounts_schema())

        # 2. contacts (no dependencies)
        await _ensure_collection(client, headers, existing, _contacts_schema())

        # 3. emails (depends on accounts)
        emails_id = await _ensure_collection(client, headers, existing, _emails_schema(accounts_id))

        # 4. attachments (depends on emails)
        await _ensure_collection(client, headers, existing, _attachments_schema(emails_id))

        # 5. folders (depends on accounts)
        await _ensure_collection(client, headers, existing, _folders_schema(accounts_id))

        # 6. smtp_servers (no dependencies)
        await _ensure_collection(client, headers, existing, _smtp_servers_schema())

        # 7. triage_rules (depends on accounts)
        await _ensure_collection(client, headers, existing, _triage_rules_schema(accounts_id))

        # Migrations: add fields to existing collections if missing
        if "accounts" in existing:
            await _add_missing_fields(client, headers, "accounts", existing["accounts"], [
                _field("signature", "text"),
                _field("reply_to_email", "text"),
            ])
        if "emails" in existing:
            await _add_missing_fields(client, headers, "emails", existing["emails"], [
                _field("reply_to", "text"),
                _field("is_answered", "bool"),
                _field("body_html", "text", max=0),
                _field("is_new", "bool"),
            ])
            # body_html darf kein Zeichenlimit haben (Standard-PATCH setzt max=5000)
            await _fix_text_field_max(client, headers, "emails", existing["emails"], "body_html")
        if "smtp_servers" in existing:
            await _add_missing_fields(client, headers, "smtp_servers", existing["smtp_servers"], [
                _field("is_default", "bool"),
            ])
        if "folders" in existing:
            await _add_missing_fields(client, headers, "folders", existing["folders"], [
                _field("email_folder", "text"),  # Normierter Ordnername für emails.folder-Abfragen
                _field("no_select", "bool"),     # \NoSelect: Ordner ist reiner Namensraum-Container
            ])
        if "emails" in existing:
            await _add_missing_fields(client, headers, "emails", existing["emails"], [
                {
                    "name": "ai_category",
                    "type": "select",
                    "required": False,
                    "values": ["focus", "quick-reply", "office", "info-trash"],
                    "maxSelect": 1,
                },
            ])
        if "contacts" in existing:
            await _add_missing_fields(client, headers, "contacts", existing["contacts"], [
                _field("xano_context", "text", max=MAX_UNLIMITED),
                _field("xano_synced_at", "date"),
            ])
        if "emails" in existing:
            await _ensure_indexes(client, headers, "emails", existing["emails"], [
                "CREATE INDEX IF NOT EXISTS idx_emails_account_folder_date ON emails (account, folder, date_sent DESC)",
                "CREATE INDEX IF NOT EXISTS idx_emails_account_folder_read_date ON emails (account, folder, is_read, date_sent DESC)",
            ])

    logger.info("PocketBase schema setup complete")


async def _ensure_indexes(
    client: httpx.AsyncClient, headers: dict,
    collection_name: str, collection_id: str, new_indexes: list
) -> None:
    """Fügt fehlende Indizes zu einer bestehenden Collection hinzu."""
    resp = await client.get(f"/api/collections/{collection_id}", headers=headers)
    if not resp.is_success:
        logger.warning(f"Could not fetch collection '{collection_name}': {resp.text}")
        return
    coll = resp.json()
    existing_indexes = set(coll.get("indexes") or [])
    to_add = [idx for idx in new_indexes if idx not in existing_indexes]
    if not to_add:
        return
    coll["indexes"] = list(existing_indexes) + to_add
    patch = await client.patch(f"/api/collections/{collection_id}", headers=headers, json=coll)
    if patch.is_success:
        logger.info(f"Added {len(to_add)} index(es) to '{collection_name}'")
    else:
        logger.warning(f"Failed to add indexes to '{collection_name}': {patch.text[:300]}")


async def _add_missing_fields(
    client: httpx.AsyncClient, headers: dict,
    collection_name: str, collection_id: str, new_fields: list
) -> None:
    """Add new fields to an existing collection without touching existing ones."""
    resp = await client.get(f"/api/collections/{collection_id}", headers=headers)
    if not resp.is_success:
        logger.warning(f"Could not fetch collection '{collection_name}': {resp.text}")
        return
    coll = resp.json()
    existing_names = {f["name"] for f in coll.get("fields", [])}
    to_add = [f for f in new_fields if f["name"] not in existing_names]
    if not to_add:
        return
    coll["fields"] = coll.get("fields", []) + to_add
    patch = await client.patch(f"/api/collections/{collection_id}", headers=headers, json=coll)
    if patch.is_success:
        logger.info(f"Added fields to '{collection_name}': {[f['name'] for f in to_add]}")
    else:
        logger.warning(f"Failed to add fields to '{collection_name}': {patch.text[:300]}")


MAX_UNLIMITED = 999_999_999_999_999  # PocketBase: max=0 bedeutet "default" (5000), nicht unbegrenzt


async def _fix_text_field_max(
    client: httpx.AsyncClient, headers: dict,
    collection_name: str, collection_id: str, field_name: str
) -> None:
    """Setzt max=MAX_UNLIMITED auf einem text-Feld um das Standard-Limit (5000) zu umgehen."""
    resp = await client.get(f"/api/collections/{collection_id}", headers=headers)
    if not resp.is_success:
        return
    coll = resp.json()
    changed = False
    for f in coll.get("fields", []):
        if f.get("name") == field_name and f.get("type") == "text":
            if f.get("max", 0) != MAX_UNLIMITED:
                f["max"] = MAX_UNLIMITED
                changed = True
    if not changed:
        return
    patch = await client.patch(f"/api/collections/{collection_id}", headers=headers, json=coll)
    if patch.is_success:
        logger.info(f"Zeichenlimit entfernt: '{collection_name}'.'{field_name}'")
    else:
        logger.warning(f"Limit-Korrektur fehlgeschlagen für '{collection_name}'.'{field_name}': {patch.text[:200]}")


async def _ensure_collection(
    client: httpx.AsyncClient, headers: dict, existing: dict, schema: dict
) -> str:
    name = schema["name"]
    if name in existing:
        logger.info(f"Collection '{name}' already exists (id: {existing[name]})")
        return existing[name]

    resp = await client.post("/api/collections", headers=headers, json=schema)
    if resp.status_code in (200, 204):
        coll_id = resp.json()["id"]
        logger.info(f"Created collection '{name}' (id: {coll_id})")
        return coll_id
    else:
        logger.error(f"Failed to create '{name}': {resp.status_code} {resp.text}")
        raise RuntimeError(f"Failed to create collection '{name}'")


def _field(name: str, type_: str, required: bool = False, **kwargs) -> dict:
    f: dict = {"name": name, "type": type_, "required": required}
    f.update(kwargs)
    return f


def _accounts_schema() -> dict:
    return {
        "name": "accounts",
        "type": "base",
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "fields": [
            _field("name", "text", required=True),
            _field("from_name", "text"),
            _field("from_email", "text", required=True),
            _field("color_tag", "text"),
            _field("is_default", "bool"),
            _field("imap_host", "text"),
            _field("imap_port", "number"),
            _field("imap_user", "text"),
            _field("imap_pass", "text"),
            _field("smtp_host", "text"),
            _field("smtp_port", "number"),
            _field("smtp_user", "text"),
            _field("smtp_pass", "text"),
            _field("signature", "text"),
        ],
    }


def _emails_schema(accounts_id: str) -> dict:
    return {
        "name": "emails",
        "type": "base",
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "indexes": [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_emails_message_id ON emails (message_id)",
            "CREATE INDEX IF NOT EXISTS idx_emails_account_uid ON emails (account, imap_uid)",
            "CREATE INDEX IF NOT EXISTS idx_emails_account_folder_date ON emails (account, folder, date_sent DESC)",
            "CREATE INDEX IF NOT EXISTS idx_emails_account_folder_read_date ON emails (account, folder, is_read, date_sent DESC)",
        ],
        "fields": [
            _field("account", "relation", required=True,
                   collectionId=accounts_id, maxSelect=1, cascadeDelete=False),
            _field("imap_uid", "number"),
            _field("uidvalidity", "number"),
            _field("folder", "text"),
            _field("message_id", "text"),
            _field("in_reply_to", "text"),
            _field("from_email", "text"),
            _field("from_name", "text"),
            _field("to_emails", "json"),
            _field("cc_emails", "json"),
            _field("subject", "text"),
            _field("body_plain", "text", max=MAX_UNLIMITED),
            _field("body_html", "text", max=MAX_UNLIMITED),
            _field("snippet", "text"),
            _field("date_sent", "date"),
            _field("is_read", "bool"),
            _field("is_flagged", "bool"),
            _field("has_attachments", "bool"),
        ],
    }


def _attachments_schema(emails_id: str) -> dict:
    return {
        "name": "attachments",
        "type": "base",
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "fields": [
            _field("email", "relation", required=True,
                   collectionId=emails_id, maxSelect=1, cascadeDelete=True),
            _field("filename", "text"),
            _field("mime_type", "text"),
            _field("size_bytes", "number"),
            _field("part_id", "text"),
        ],
    }


def _folders_schema(accounts_id: str) -> dict:
    return {
        "name": "folders",
        "type": "base",
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "fields": [
            _field("account", "relation", required=True,
                   collectionId=accounts_id, maxSelect=1, cascadeDelete=True),
            _field("imap_path", "text"),
            _field("display_name", "text"),
            _field("email_folder", "text"),  # Normierter Name für emails.folder-Abfragen
            _field("no_select", "bool"),     # \NoSelect: reiner Namensraum-Container
            _field("unread_count", "number"),
            _field("last_sync_uid", "number"),
            _field("uidvalidity", "number"),
        ],
    }


def _smtp_servers_schema() -> dict:
    return {
        "name": "smtp_servers",
        "type": "base",
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "fields": [
            _field("name", "text", required=True),
            _field("host", "text", required=True),
            _field("port", "number"),
            _field("user", "text"),
            _field("password", "text"),
            _field("use_tls", "bool"),
            _field("use_starttls", "bool"),
            _field("is_default", "bool"),
        ],
    }


def _triage_rules_schema(accounts_id: str) -> dict:
    return {
        "name": "triage_rules",
        "type": "base",
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "fields": [
            _field("account", "relation", required=True,
                   collectionId=accounts_id, maxSelect=1, cascadeDelete=True),
            _field("category_slug", "text", required=True),
            _field("rule_text", "text", max=MAX_UNLIMITED),
        ],
    }


def _contacts_schema() -> dict:
    return {
        "name": "contacts",
        "type": "base",
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "indexes": [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_email ON contacts (email)",
        ],
        "fields": [
            _field("email", "text", required=True),
            _field("name", "text"),
            _field("email_count", "number"),
            _field("last_contact", "date"),
            _field("notes", "text"),
        ],
    }
