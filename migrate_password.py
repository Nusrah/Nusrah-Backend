"""One-time migration: replace plaintext passwords in `profiles` with bcrypt hashes.

Run from the backend folder (same env vars as the API, SUPABASE_URL / SUPABASE_KEY / JWT_SECRET):
    python migrate_passwords.py --dry-run   # shows how many rows would change
    python migrate_passwords.py             # does it
Safe to re-run: rows that are already hashed are skipped.
"""
import sys

from auth import hash_password, is_hashed
from database import supabase

PAGE = 500
dry_run = "--dry-run" in sys.argv

start, migrated, skipped, empty = 0, 0, 0, 0
while True:
  rows = supabase.table("profiles").select("id, password").order("id").range(start, start + PAGE - 1).execute().data or []
  if not rows:
    break
  for row in rows:
    pw = row.get("password")
    if not pw:
      empty += 1
    elif is_hashed(pw):
      skipped += 1
    else:
      migrated += 1
      if not dry_run:
        supabase.table("profiles").update({"password": hash_password(pw)}).eq("id", row["id"]).execute()
  start += PAGE

print(f"{'Would hash' if dry_run else 'Hashed'}: {migrated} | already hashed: {skipped} | no password: {empty}")