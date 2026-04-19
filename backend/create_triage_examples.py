"""
Einmalig ausführen, um die triage_examples-Collection in PocketBase anzulegen.

Aufruf (im Coolify-Terminal):
  python create_triage_examples.py
"""
import asyncio
import httpx
from config import settings


async def main():
    async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=30) as client:
        resp = await client.post(
            "/api/collections/_superusers/auth-with-password",
            json={"identity": settings.PB_ADMIN_EMAIL, "password": settings.PB_ADMIN_PASSWORD},
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        payload = {
            "name": "triage_examples",
            "type": "base",
            "fields": [
                {"name": "account",      "type": "text",     "required": True},
                {"name": "from_email",   "type": "text",     "required": True},
                {"name": "subject",      "type": "text",     "required": False},
                {"name": "body_snippet", "type": "text",     "required": False},
                {"name": "category",     "type": "text",     "required": True},
            ],
        }

        resp = await client.post("/api/collections", json=payload, headers=headers)
        if resp.status_code == 200:
            print("Collection 'triage_examples' erfolgreich angelegt.")
        elif resp.status_code == 400 and "already exists" in resp.text:
            print("Collection 'triage_examples' existiert bereits.")
        else:
            print(f"Fehler: {resp.status_code} {resp.text}")


asyncio.run(main())
