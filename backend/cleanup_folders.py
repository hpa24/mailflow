"""
Einmalig ausführen, um doppelte Folder-Einträge in PocketBase zu bereinigen.

Aufruf (auf dem Server):
  docker compose exec backend python cleanup_folders.py
"""
import asyncio
import httpx
from config import settings


async def main():
    # Authentifizieren
    async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=30) as client:
        resp = await client.post(
            "/api/collections/_superusers/auth-with-password",
            json={"identity": settings.PB_ADMIN_EMAIL, "password": settings.PB_ADMIN_PASSWORD},
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Alle Folder laden (max. 2000)
        resp = await client.get(
            "/api/collections/folders/records",
            params={"perPage": 2000, "page": 1},
            headers=headers,
        )
        resp.raise_for_status()
        all_folders = resp.json().get("items", [])
        print(f"Gesamt Folder-Einträge: {len(all_folders)}")

        # Gruppieren nach (account, imap_path)
        seen: dict[tuple, str] = {}   # (account, imap_path) → erster id (wird behalten)
        to_delete: list[str] = []

        for f in all_folders:
            key = (f["account"], f["imap_path"])
            if key in seen:
                to_delete.append(f["id"])
            else:
                seen[key] = f["id"]

        print(f"Duplikate gefunden: {len(to_delete)}")
        if not to_delete:
            print("Nichts zu tun.")
            return

        # Duplikate löschen
        deleted = 0
        for fid in to_delete:
            del_resp = await client.delete(
                f"/api/collections/folders/records/{fid}",
                headers=headers,
            )
            if del_resp.status_code in (200, 204):
                deleted += 1
            else:
                print(f"  FEHLER beim Löschen {fid}: {del_resp.status_code} {del_resp.text}")

        print(f"Gelöscht: {deleted} Einträge")


asyncio.run(main())
