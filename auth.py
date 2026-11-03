"""Authentication helpers for the Nusrah API: password hashing, JWT tokens, route guards, rate limiting."""
import hmac
import os
import time
from collections import defaultdict, deque

import bcrypt
import jwt
from database import supabase
from fastapi import Depends, HTTPException, Request

JWT_SECRET = os.getenv("JWT_SECRET", "")
if len(JWT_SECRET) < 32:
  # Fail at startup rather than silently signing tokens with a guessable key
  raise RuntimeError("JWT_SECRET must be set to a random string of at least 32 characters")
JWT_ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_HOURS", "8")) * 3600

# Columns that must never leave the server
SENSITIVE_FIELDS = ("password", "reset_otp", "otp_expires_at")

# ---------- passwords ----------

def _pw_bytes(plain: str) -> bytes:
  return plain.encode("utf-8")[:72] # bcrypt only uses the first 72 bytes; truncating keeps hash and check consistent

def hash_password(plain: str) -> str:
  return bcrypt.hashpw(_pw_bytes(plain), bcrypt.gensalt(rounds=12)).decode("utf-8")

def is_hashed(stored) -> bool:
  return isinstance(stored, str) and len(stored) == 60 and stored.startswith(("$2a$", "$2b$", "$2y$"))

def verify_password(plain: str, stored) -> bool:
  if not stored:
    return False
  if is_hashed(stored):
    try:
      return bcrypt.checkpw(_pw_bytes(plain), stored.encode("utf-8"))
    except ValueError:
      return False
  # Legacy plaintext row that has not been migrated yet: constant-time compare, and the login route re-hashes it on success
  return hmac.compare_digest(plain.encode("utf-8"), str(stored).encode("utf-8"))

def public_profile(p):
  """Strip password / OTP columns from a profile row (or list of rows) before it goes into a response."""
  if isinstance(p, list):
    return [public_profile(x) for x in p]
  if isinstance(p, dict):
    return {k: v for k, v in p.items() if k not in SENSITIVE_FIELDS}
  return p

# ---------- tokens ----------

def create_access_token(profile: dict) -> str:
  now = int(time.time())
  claims = {"sub": str(profile["id"]), "role": (profile.get("role") or "").lower(), "iat": now, "exp": now + TOKEN_TTL_SECONDS}
  return jwt.encode(claims, JWT_SECRET, algorithm=JWT_ALGORITHM)

def _unauthorized(detail: str) -> HTTPException:
  return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})

def get_current_user(request: Request) -> dict:
  """Verifies the Bearer token and loads the user fresh from the database, so deleted or demoted users lose access immediately."""
  scheme, _, token = request.headers.get("Authorization", "").partition(" ")
  if scheme.lower() != "bearer" or not token.strip():
    raise _unauthorized("Not authenticated")
  try:
    claims = jwt.decode(token.strip(), JWT_SECRET, algorithms=[JWT_ALGORITHM], options={"require": ["exp", "sub"]})
  except jwt.PyJWTError:
    raise _unauthorized("Invalid or expired token")
  rows = supabase.table("profiles").select("id, email, role, approval_status").eq("id", claims["sub"]).execute().data
  if not rows:
    raise _unauthorized("Account no longer exists")
  user = rows[0]
  user["role"] = (user.get("role") or "").lower()
  if user["role"] == "volunteer" and (user.get("approval_status") or "").lower() == "pending":
    raise HTTPException(status_code=403, detail="Your volunteer registration has not been approved yet.")
  return user

def require_admin(user: dict = Depends(get_current_user)) -> dict:
  if user["role"] != "admin":
    raise HTTPException(status_code=403, detail="Admin access required")
  return user

# ---------- rate limiting (in-memory: per process, resets on restart) ----------

_attempts = defaultdict(deque)

def client_ip(request: Request) -> str:
  return request.client.host if request.client else "unknown"

def is_limited(key: str, limit: int, window_seconds: int) -> bool:
  q = _attempts.get(key)
  if not q:
    return False
  cutoff = time.time() - window_seconds
  while q and q[0] < cutoff:
    q.popleft()
  return len(q) >= limit

def record_attempt(key: str) -> None:
  if len(_attempts) > 20000: # keep memory bounded
    cutoff = time.time() - 3600
    for k in [k for k, q in _attempts.items() if not q or q[-1] < cutoff]:
      del _attempts[k]
  _attempts[key].append(time.time())

def clear_attempts(key: str) -> None:
  _attempts.pop(key, None)