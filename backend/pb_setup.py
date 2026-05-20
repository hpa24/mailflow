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
        smtp_servers_id = await _ensure_collection(client, headers, existing, _smtp_servers_schema())

        # 7. triage_rules (depends on accounts)
        await _ensure_collection(client, headers, existing, _triage_rules_schema(accounts_id))

        # 8. response_patterns (depends on accounts)
        await _ensure_collection(client, headers, existing, _response_patterns_schema(accounts_id))

        # 9. spam_rules (depends on accounts)
        await _ensure_collection(client, headers, existing, _spam_rules_schema(accounts_id))

        # 10. email_variables (no dependencies) — globale Variablen für Vorlagen-Rendering
        await _ensure_collection(client, headers, existing, _email_variables_schema())

        # 11. email_snippets (no dependencies) — wiederverwendbare HTML-Blöcke
        await _ensure_collection(client, headers, existing, _email_snippets_schema())

        # 12. email_templates (no dependencies) — Vorlagen für Versand
        await _ensure_collection(client, headers, existing, _email_templates_schema())

        # 13. contact_groups (no dependencies) — Sets von Kontakten für Gruppen-Versand
        contact_groups_id = await _ensure_collection(client, headers, existing, _contact_groups_schema())

        # 14. bulk_sends (depends on accounts) — Historie der Massenversände + Empfänger-Status
        await _ensure_collection(client, headers, existing, _bulk_sends_schema(accounts_id))

        # 15. webhooks (depends on smtp_servers + accounts) — externe Send-Endpoints
        webhooks_id = await _ensure_collection(
            client, headers, existing, _webhooks_schema(smtp_servers_id, accounts_id)
        )

        # 16. webhook_logs (depends on webhooks) — Audit-Trail pro Webhook-Aufruf
        await _ensure_collection(client, headers, existing, _webhook_logs_schema(webhooks_id))

        # Migrations: add fields to existing collections if missing
        if "accounts" in existing:
            await _add_missing_fields(client, headers, "accounts", existing["accounts"], [
                _field("signature", "text"),
                _field("reply_to_email", "text"),
            ])
            # A11 Phase 2: listRule/viewRule auf "any authenticated user" patchen,
            # damit GET /accounts mit User-Token funktioniert (statt Admin-Bypass).
            await _ensure_rules(client, headers, "accounts", existing["accounts"], {
                "listRule": '@request.auth.id != ""',
                "viewRule": '@request.auth.id != ""',
            })

        # A11 Phase 3a — Vorlagen-Cluster: full User-CRUD auf email_variables,
        # email_snippets, email_templates. Reine User-Daten, kein Backend-Job schreibt rein.
        # A11 Phase 3b — Kontakte-Cluster: contacts und contact_groups dito. Admin-Pfade
        # (Import via X-Import-Key, IMAP-Sync-Upsert) nutzen Admin-Token und sind von
        # Rules nicht betroffen (Superuser-Bypass).
        # A11 Phase 3c — Kleinkram-Cluster: folders, smtp_servers, triage_rules, spam_rules,
        # response_patterns. Backend-Schreiber (imap_sync, spam_filter, smtp_sender,
        # cleanup_folders) nutzen Admin-Token.
        _cluster_rules = {
            "listRule": '@request.auth.id != ""',
            "viewRule": '@request.auth.id != ""',
            "createRule": '@request.auth.id != ""',
            "updateRule": '@request.auth.id != ""',
            "deleteRule": '@request.auth.id != ""',
        }
        # A11 Phase 3d — emails + attachments. IMAP-Sync, spam_filter, scheduler usw.
        # schreiben weiterhin als Admin; Frontend nutzt User-Token für Reads/Marks/Moves.
        # A11 Phase 3e — Audit/Bulk-Cluster: bulk_sends, webhooks, webhook_logs.
        # User-CRUD via UI; Backend-Schreiber (webhook_send mit X-Webhook-Key,
        # _do_send_job, _bulk_record_recipient_result) nutzen Admin.
        for _name in (
            "email_variables", "email_snippets", "email_templates",
            "contacts", "contact_groups",
            "folders", "smtp_servers", "triage_rules", "spam_rules", "response_patterns",
            "emails", "attachments",
            "bulk_sends", "webhooks", "webhook_logs",
        ):
            if _name in existing:
                await _ensure_rules(client, headers, _name, existing[_name], _cluster_rules)
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
        if "emails" in existing:
            await _add_missing_fields(client, headers, "emails", existing["emails"], [
                _field("spam_score", "number"),
                _field("spam_suggested", "bool"),
                _field("spam_rule_match", "text"),
            ])
        if "emails" in existing and "webhooks" in existing:
            await _add_missing_fields(client, headers, "emails", existing["emails"], [
                _field("webhook", "relation",
                       collectionId=existing["webhooks"], maxSelect=1, cascadeDelete=False),
            ])
        if "contacts" in existing:
            await _add_missing_fields(client, headers, "contacts", existing["contacts"], [
                _field("xano_context", "text", max=MAX_UNLIMITED),
                _field("xano_synced_at", "date"),
            ])
            await _add_missing_fields(client, headers, "contacts", existing["contacts"], [
                _field("groups", "relation",
                       collectionId=contact_groups_id, maxSelect=999, cascadeDelete=False),
                _field("unsubscribed", "bool"),
            ])
        if "emails" in existing:
            await _ensure_indexes(client, headers, "emails", existing["emails"], [
                "CREATE INDEX IF NOT EXISTS idx_emails_account_folder_date ON emails (account, folder, date_sent DESC)",
                "CREATE INDEX IF NOT EXISTS idx_emails_account_folder_read_date ON emails (account, folder, is_read, date_sent DESC)",
            ])

        # B15: has_attachments + is_done für Worker (Resume-Logik + Poll-Filter).
        if "bulk_sends" in existing:
            await _add_missing_fields(client, headers, "bulk_sends", existing["bulk_sends"], [
                _field("has_attachments", "bool"),
                _field("is_done", "bool"),
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


async def _ensure_rules(
    client: httpx.AsyncClient, headers: dict,
    collection_name: str, collection_id: str, rules: dict
) -> None:
    """PATCHt list/view/create/update/deleteRule auf existierender Collection,
    wenn die aktuellen Werte nicht mit `rules` übereinstimmen. Idempotent —
    sorgt dafür, dass Code-Schema = Source of Truth für PB-Rules ist.
    """
    resp = await client.get(f"/api/collections/{collection_id}", headers=headers)
    if not resp.is_success:
        logger.warning(f"Could not fetch collection '{collection_name}': {resp.text[:200]}")
        return
    current = resp.json()
    changes = {k: v for k, v in rules.items() if current.get(k) != v}
    if not changes:
        return
    patch = await client.patch(f"/api/collections/{collection_id}", headers=headers, json=changes)
    if patch.is_success:
        logger.info(f"Updated rules for '{collection_name}': {changes}")
    else:
        logger.warning(f"Failed to update rules on '{collection_name}': {patch.text[:300]}")


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
        # listRule/viewRule: A11 Phase 2 — jeder eingeloggte User darf lesen.
        # createRule/updateRule/deleteRule bleiben admin-only (kein User-Self-Service).
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
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
            _field("reply_to_email", "text"),
        ],
    }


def _emails_schema(accounts_id: str) -> dict:
    return {
        "name": "emails",
        "type": "base",
        # A11 Phase 3d — heißester Code-Pfad. IMAP-Sync schreibt als Admin (Backend);
        # Frontend liest/markiert/verschiebt/löscht als User. Signed-URL-Endpoints
        # (/emails/{id}/inline, /attachments/{id}/download) bleiben Admin.
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
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
            _field("is_answered", "bool"),
            _field("is_new", "bool"),
            _field("has_attachments", "bool"),
            _field("reply_to", "text"),
            {
                "name": "ai_category",
                "type": "select",
                "required": False,
                "values": ["focus", "quick-reply", "office", "info-trash"],
                "maxSelect": 1,
            },
            _field("spam_score", "number"),
            _field("spam_suggested", "bool"),
            _field("spam_rule_match", "text"),
        ],
    }


def _attachments_schema(emails_id: str) -> dict:
    return {
        "name": "attachments",
        "type": "base",
        # A11 Phase 3d — siehe emails-Schema. IMAP-Sync schreibt Admin; Frontend lädt
        # Anhänge per signed URL (bleibt Admin) und listet sie per Bearer (User).
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
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
        # A11 Phase 3c — Kleinkram-Cluster. IMAP-Sync schreibt folders als Admin (Backend).
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
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
        # A11 Phase 3c — Kleinkram-Cluster. smtp_sender liest als Admin (Backend-Versand).
        # GET /smtp-servers reicht nur id/name/is_default ans Frontend durch (fields-Whitelist
        # in main.py), damit `password` nicht leakt.
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
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
        # A11 Phase 3c — Kleinkram-Cluster.
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
        "fields": [
            _field("account", "relation", required=True,
                   collectionId=accounts_id, maxSelect=1, cascadeDelete=True),
            _field("category_slug", "text", required=True),
            _field("rule_text", "text", max=MAX_UNLIMITED),
        ],
    }


def _spam_rules_schema(accounts_id: str) -> dict:
    return {
        "name": "spam_rules",
        "type": "base",
        # A11 Phase 3c — Kleinkram-Cluster. spam_filter liest+schreibt als Admin (Backend).
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
        "indexes": [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_spam_rules_account_pattern ON spam_rules (account, match_type, pattern)",
        ],
        "fields": [
            _field("account", "relation", required=True,
                   collectionId=accounts_id, maxSelect=1, cascadeDelete=True),
            _field("match_type", "select", required=True,
                   values=["email", "domain"], maxSelect=1),
            _field("pattern", "text", required=True),
            _field("hits", "number"),
            _field("last_hit", "date"),
        ],
    }


def _response_patterns_schema(accounts_id: str) -> dict:
    return {
        "name": "response_patterns",
        "type": "base",
        # A11 Phase 3c — Kleinkram-Cluster.
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
        "fields": [
            _field("account", "relation", required=True,
                   collectionId=accounts_id, maxSelect=1, cascadeDelete=True),
            _field("element_text", "text", max=MAX_UNLIMITED),
            _field("action", "text"),
            _field("draft_text", "text", max=MAX_UNLIMITED),
            _field("was_edited", "bool"),
        ],
    }


def _contacts_schema() -> dict:
    return {
        "name": "contacts",
        "type": "base",
        # A11 Phase 3b — Kontakte-Cluster: full User-CRUD via PB-Rules.
        # /contacts/import nutzt weiterhin Admin-Token (X-Import-Key-Pfad, kein User).
        # IMAP-Sync upsertet ebenfalls als Admin (Backend-Job, kein User-Kontext).
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
        "indexes": [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_email ON contacts (email)",
        ],
        "fields": [
            _field("email", "text", required=True),
            _field("name", "text"),
            _field("email_count", "number"),
            _field("last_contact", "date"),
            _field("notes", "text"),
            _field("xano_context", "text", max=MAX_UNLIMITED),
            _field("xano_synced_at", "date"),
        ],
    }


def _email_variables_schema() -> dict:
    return {
        "name": "email_variables",
        "type": "base",
        # A11 Phase 3a — Vorlagen-Cluster: full User-CRUD via PB-Rules.
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
        "indexes": [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_email_variables_name ON email_variables (name)",
        ],
        "fields": [
            _field("name", "text", required=True),
            _field("value", "text", max=MAX_UNLIMITED),
        ],
    }


def _email_snippets_schema() -> dict:
    return {
        "name": "email_snippets",
        "type": "base",
        # A11 Phase 3a — Vorlagen-Cluster: full User-CRUD via PB-Rules.
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
        "indexes": [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_email_snippets_name ON email_snippets (name)",
        ],
        "fields": [
            _field("name", "text", required=True),
            _field("html", "text", max=MAX_UNLIMITED),
        ],
    }


def _email_templates_schema() -> dict:
    return {
        "name": "email_templates",
        "type": "base",
        # A11 Phase 3a — Vorlagen-Cluster: full User-CRUD via PB-Rules.
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
        "indexes": [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_email_templates_prefix_name ON email_templates (prefix, name)",
            "CREATE INDEX IF NOT EXISTS idx_email_templates_prefix ON email_templates (prefix)",
        ],
        "fields": [
            _field("prefix", "text"),
            _field("name", "text", required=True),
            _field("subject", "text"),
            _field("html_body", "text", max=MAX_UNLIMITED),
            _field("text_body", "text", max=MAX_UNLIMITED),
        ],
    }


def _contact_groups_schema() -> dict:
    return {
        "name": "contact_groups",
        "type": "base",
        # A11 Phase 3b — Kontakte-Cluster: full User-CRUD via PB-Rules.
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
        "indexes": [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_groups_name ON contact_groups (name)",
        ],
        "fields": [
            _field("name", "text", required=True),
            _field("description", "text"),
        ],
    }


def _bulk_sends_schema(accounts_id: str) -> dict:
    return {
        "name": "bulk_sends",
        "type": "base",
        # A11 Phase 3e — Audit-Collection. User darf lesen/löschen; Backend (bulk_send_endpoint,
        # _do_send_job, _bulk_record_recipient_result) schreibt als Admin.
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_bulk_sends_sent_at ON bulk_sends (sent_at DESC)",
        ],
        "fields": [
            _field("subject", "text"),
            _field("from_account", "relation",
                   collectionId=accounts_id, maxSelect=1, cascadeDelete=False),
            _field("from_account_email", "text"),
            _field("smtp_server", "text"),
            _field("body_html", "text", max=MAX_UNLIMITED),
            _field("body_text", "text", max=MAX_UNLIMITED),
            _field("sent_at", "date"),
            _field("delay_seconds", "number"),
            # recipients: [{email, name, raw, status, message_id, error, sent_at, next_attempt_at}]
            # status ∈ queued|sent|error|bounced
            # next_attempt_at: ISO-Datum, ab wann der Worker diesen Empfänger versenden darf (B15).
            _field("recipients", "json", maxSize=5_000_000),
            _field("total_count", "number"),
            _field("sent_count", "number"),
            _field("error_count", "number"),
            _field("bounced_count", "number"),
            # B15: True, wenn der Versand Anhänge hatte. Bei Backend-Restart räumt der
            # Worker offene Empfänger solcher Aussendungen auf (Anhänge sind in-memory).
            _field("has_attachments", "bool"),
            # B15: True, wenn alle Empfänger terminal (sent/error/bounced) sind.
            # Worker filtert is_done!=true und überspringt fertige Aussendungen.
            _field("is_done", "bool"),
        ],
    }


def _webhooks_schema(smtp_servers_id: str, accounts_id: str) -> dict:
    return {
        "name": "webhooks",
        "type": "base",
        # A11 Phase 3e — User-CRUD via UI. _webhook_by_slug nutzt Admin
        # (externer /webhooks/{slug}/send-Pfad mit X-Webhook-Key, kein User-Token).
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
        "indexes": [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_webhooks_slug ON webhooks (slug)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_webhooks_api_key ON webhooks (api_key)",
        ],
        "fields": [
            _field("name", "text", required=True),
            _field("slug", "text", required=True),
            _field("smtp_server", "relation", required=True,
                   collectionId=smtp_servers_id, maxSelect=1, cascadeDelete=False),
            _field("from_account", "relation", required=True,
                   collectionId=accounts_id, maxSelect=1, cascadeDelete=False),
            _field("default_to", "text"),
            _field("from_name_override", "text"),
            _field("allow_to_override", "bool"),
            _field("allow_reply_to", "bool"),
            _field("allow_cc", "bool"),
            _field("is_active", "bool"),
            _field("api_key", "text", required=True),
        ],
    }


def _webhook_logs_schema(webhooks_id: str) -> dict:
    return {
        "name": "webhook_logs",
        "type": "base",
        # A11 Phase 3e — Audit-Collection. User darf lesen; webhook_send schreibt als Admin.
        "listRule": '@request.auth.id != ""',
        "viewRule": '@request.auth.id != ""',
        "createRule": '@request.auth.id != ""',
        "updateRule": '@request.auth.id != ""',
        "deleteRule": '@request.auth.id != ""',
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_webhook_logs_webhook_created ON webhook_logs (webhook, created DESC)",
        ],
        "fields": [
            _field("webhook", "relation", required=True,
                   collectionId=webhooks_id, maxSelect=1, cascadeDelete=True),
            _field("ip", "text"),
            _field("status", "text"),
            _field("to", "text"),
            _field("subject", "text"),
            _field("message_id", "text"),
            _field("error", "text", max=MAX_UNLIMITED),
        ],
    }
