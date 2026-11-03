import os
import secrets
import smtplib
import time
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from inspect import iscoroutinefunction
from typing import List, Optional

import razorpay
from database import supabase
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from auth import (
    TOKEN_TTL_SECONDS, clear_attempts, client_ip, create_access_token, get_current_user, hash_password,
    is_hashed, is_limited, public_profile, record_attempt, require_admin, verify_password,
)
from chatbot import router as chatbot_router

app = FastAPI()

# Wildcard origins can't be used with credentials, so origins come from the comma-separated ALLOWED_ORIGINS env var (local Angular ports as fallback)
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()] or ["http://localhost:4200", "http://127.0.0.1:4200"]
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Nusrah chatbot (POST /api/chat)
app.include_router(chatbot_router)

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, os.getenv("RAZORPAY_KEY_SECRET")))

class LoginRequest(BaseModel):
  email: str
  password: str

class SignupRequest(BaseModel):
  name: Optional[str] = None
  full_name: Optional[str] = None
  email: str
  password: str
  phone: Optional[str] = None
  city: Optional[str] = None
  bio: Optional[str] = None
  photo_url: Optional[str] = None

class ForgotPasswordRequest(BaseModel):
  email: str

class ResetPasswordRequest(BaseModel):
  email: str
  otp: str
  new_password: str

class ChangePasswordRequest(BaseModel):
  current_password: str
  new_password: str

class ProgressRequest(BaseModel):
  raised: float

class DeleteDocRequest(BaseModel):
  file_url: Optional[str] = None
  url: Optional[str] = None

class CreateOrderRequest(BaseModel):
  amount: float
  campaign_id: str

class VerifyPaymentRequest(BaseModel):
  razorpay_order_id: str
  razorpay_payment_id: str
  razorpay_signature: str
  campaign_id: str
  amount: float
  donor_email: Optional[str] = None
  message: Optional[str] = None

class CreateQRRequest(BaseModel):
  amount: float
  campaign_id: str
  name: Optional[str] = "Campaign Donation"
  description: Optional[str] = "Donation for campaign"

CAMPAIGN_FIELDS = ("title", "location", "description", "information", "category", "goal", "raised", "status", "is_featured", "featured_highlights", "additional_images", "bank_account_name", "bank_account_number", "bank_name", "upi_id")
CAMPAIGN_ALIASES = (("document_proofs", ("document_proofs", "verification_docs")), ("bank_ifsc_code", ("bank_ifsc_code", "ifsc_code")), ("upi_qr_code_url", ("upi_qr_code_url", "qr_code_url")))
BANK_FIELDS = ("bank_account_number", "bank_ifsc_code", "bank_name")
PAYMENT_FIELDS = ("bank_account_name", "bank_account_number", "bank_name", "bank_ifsc_code", "upi_id", "upi_qr_code_url")
ADMIN_ONLY_CAMPAIGN_FIELDS = ("status", "is_featured", "raised") # only admins may change these
MIN_PASSWORD_LENGTH = 8

def _safe(fn):
  # Turns unexpected errors into HTTP 500 and lets HTTPExceptions through (keeps sync/async so FastAPI runs it the same way)
  def to_http(e):
    return e if isinstance(e, HTTPException) else HTTPException(status_code=500, detail=str(e))

  if iscoroutinefunction(fn):
    @wraps(fn)
    async def wrapper(*args, **kwargs):
      try:
        return await fn(*args, **kwargs)
      except Exception as e:
        raise to_http(e)
  else:
    @wraps(fn)
    def wrapper(*args, **kwargs):
      try:
        return fn(*args, **kwargs)
      except Exception as e:
        raise to_http(e)
  return wrapper

def _campaign_has_bank_details(*fields) -> bool:
  return all((f or "").strip() for f in fields)

def _count(query):
  res = query.execute()
  return res.count if res.count is not None else len(res.data or [])

def _sync_docs(c):
  if "document_proofs" in c and "verification_docs" not in c:
    c["verification_docs"] = c["document_proofs"]
  elif "verification_docs" in c and "document_proofs" not in c:
    c["document_proofs"] = c["verification_docs"]

def _sync_name(p):
  if "full_name" in p and not p.get("name"):
    p["name"] = p["full_name"]
  elif "name" in p and not p.get("full_name"):
    p["full_name"] = p["name"]

def _campaigns(query):
  campaigns = query.execute().data or []
  for c in campaigns:
    _sync_docs(c)
  return campaigns

def _profile_update(data, fields):
  u = {k: data.get(k) for k in fields if k in data}
  if "name" in data or "full_name" in data:
    u["full_name"] = data.get("name") or data.get("full_name")
  return u

async def _upload(f: UploadFile, prefix="", ext="jpg", ctype="image/jpeg"):
  name = f"{prefix}{uuid.uuid4()}.{f.filename.split('.')[-1] if f.filename else ext}"
  bucket = supabase.storage.from_("campaign-images")
  bucket.upload(name, await f.read(), file_options={"content-type": f.content_type or ctype})
  return bucket.get_public_url(name)

def _clean_password(raw: str) -> str:
  pw = (raw or "").strip()
  if len(pw) < MIN_PASSWORD_LENGTH:
    raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")
  return pw

def _require_self_or_admin(user, identifier):
  # identifier may be a profile id or an email
  ident = (identifier or "").strip().lower()
  if user["role"] == "admin" or ident == str(user["id"]).lower() or ident == (user.get("email") or "").strip().lower():
    return
  raise HTTPException(status_code=403, detail="You can only access your own account")

def _require_campaign_manager(user, campaign_id):
  # Admins manage everything; a volunteer only manages campaigns they created or are assigned to. Returns the campaign row.
  if user["role"] == "admin":
    return {}
  rows = supabase.table("campaigns").select("user_id, status, " + ", ".join(PAYMENT_FIELDS)).eq("id", campaign_id).execute().data
  if not rows:
    raise HTTPException(status_code=404, detail="Campaign not found")
  uid = str(user["id"])
  if str(rows[0].get("user_id")) == uid:
    return rows[0]
  assigned = supabase.table("campaign_volunteers").select("campaign_id").eq("campaign_id", campaign_id).eq("volunteer_id", uid).execute().data
  if assigned:
    return rows[0]
  raise HTTPException(status_code=403, detail="You do not have access to this campaign")

def _require_payment_edit(user, camp):
  # Once a campaign is live, where the money goes can only be changed by an admin
  if user["role"] != "admin" and camp.get("status") not in ("pending", "pending_bank_details"):
    raise HTTPException(status_code=403, detail="Payment details of a live campaign can only be changed by an admin")

@app.get("/")
def read_root():
  return {"message": "Welcome to Nusrah API!"}

@app.post("/api/create-order")
@_safe
def create_razorpay_order(data: CreateOrderRequest):
  order = razorpay_client.order.create(data={"amount": int(data.amount * 100), "currency": "INR", "payment_capture": 1})
  return {"success": True, "order_id": order["id"], "amount": order["amount"], "currency": order["currency"], "key_id": RAZORPAY_KEY_ID}

@app.post("/api/create-qr-code")
@_safe
def create_razorpay_qr_code(data: CreateQRRequest):
  qr = razorpay_client.qrcode.create({
      "type": "upi_qr",
      "name": data.name,
      "usage": "single_use",
      "fixed_amount": True,
      "payment_amount": int(data.amount * 100),
      "description": data.description,
      "close_by": int(time.time()) + 15 * 60,
      "notes": {"campaign_id": data.campaign_id},
})
  return {k: qr.get(k) for k in ("id", "image_url", "payment_amount", "status", "close_by")} | {"success": True} if False else {
      "success": True,
      "qr_code_id": qr.get("id"),
      "image_url": qr.get("image_url"),
      "payment_amount": qr.get("payment_amount"),
      "status": qr.get("status"),
      "close_by": qr.get("close_by"),
}

@app.post("/api/verify-payment")
def verify_razorpay_payment(data: VerifyPaymentRequest):
  try:
    razorpay_client.utility.verify_payment_signature({
        "razorpay_order_id": data.razorpay_order_id,
        "razorpay_payment_id": data.razorpay_payment_id,
        "razorpay_signature": data.razorpay_signature,
})
    camp = supabase.table("campaigns").select("raised, goal, reach").eq("id", data.campaign_id).execute()
    if not camp.data:
      raise HTTPException(status_code=404, detail="Campaign not found")
    new_raised = float(camp.data[0].get("raised") or 0) + float(data.amount)
    new_reach = int(camp.data[0].get("reach") or 0) + 1 # reach = completed donations (donor count shown on the donate page)
    supabase.table("campaigns").update({"raised": new_raised, "reach": new_reach}).eq("id", data.campaign_id).execute()
    supabase.table("donations").insert({"campaign_id": data.campaign_id, "amount": data.amount, "message": data.message or ""}).execute()
    return {"success": True, "message": "Payment verified and recorded successfully", "new_raised": new_raised, "new_reach": new_reach}
  except razorpay.errors.SignatureVerificationError:
    raise HTTPException(status_code=400, detail="Payment signature verification failed")
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stats")
@_safe
def get_stats():
  table = supabase.table
  return {
      "active_campaigns": _count(table("campaigns").select("*", count="exact").ilike("status", "active")),
      "active_volunteers": _count(table("profiles").select("*", count="exact").ilike("role", "volunteer").ilike("approval_status", "approved")),
      "finished_campaigns": _count(table("campaigns").select("*", count="exact").ilike("status", "finished")),
}

@app.get("/api/home/campaigns")
@_safe
def get_home_campaigns():
  return {"campaigns": _campaigns(supabase.table("campaigns").select("*").eq("is_featured", True).ilike("status", "active"))}

@app.get("/api/campaigns")
@_safe
def get_all_campaigns():
  return {"campaigns": _campaigns(supabase.table("campaigns").select("*").ilike("status", "active"))}

@app.get("/api/admin/campaigns", dependencies=[Depends(require_admin)])
@_safe
def get_admin_campaigns():
  campaigns = supabase.table("campaigns").select("*").execute().data or []
  try:
    vol_rels = supabase.table("campaign_volunteers").select("*").execute().data or []
  except Exception:
    vol_rels = []
  for camp in campaigns:
    _sync_docs(camp)
    camp_id = str(camp.get("id", "")).strip().lower()
    vols = [v["volunteer_id"] for v in vol_rels if str(v.get("campaign_id", "")).strip().lower() == camp_id]
    camp["assigned_volunteers_count"] = len(vols)
    camp["volunteer_ids"] = vols
  return {"campaigns": campaigns}

@app.get("/api/admin/campaign-volunteers", dependencies=[Depends(require_admin)])
def get_admin_campaign_volunteers():
  try:
    rows = supabase.table("campaign_volunteers").select("*").execute().data or []
  except Exception:
    rows = []
  return {"assignments": rows, "campaign_volunteers": rows}

@app.get("/api/volunteers/profile")
@_safe
def get_volunteer_profile_by_query(identifier: str, user: dict = Depends(get_current_user)):
  _require_self_or_admin(user, identifier)
  res = supabase.table("profiles").select("*").eq("email" if "@" in identifier else "id", identifier).execute()
  if not res.data:
    raise HTTPException(status_code=404, detail="Profile not found")
  _sync_name(res.data[0])
  return {"profile": public_profile(res.data[0])}

@app.get("/api/volunteers/{volunteer_identifier}/campaigns")
def get_volunteer_campaigns(volunteer_identifier: str, user: dict = Depends(get_current_user)):
  _require_self_or_admin(user, volunteer_identifier)
  try:
    vol_id = volunteer_identifier
    if "@" in volunteer_identifier:
      prof = supabase.table("profiles").select("id").eq("email", volunteer_identifier).execute()
      if not prof.data:
        return {"campaigns": []}
      vol_id = prof.data[0].get("id")
    junction = supabase.table("campaign_volunteers").select("campaign_id").eq("volunteer_id", vol_id).execute()
    if not junction.data:
      return {"campaigns": []}
    return {"campaigns": _campaigns(supabase.table("campaigns").select("*").in_("id", [i["campaign_id"] for i in junction.data]))}
  except Exception:
    return {"campaigns": []}

@app.post("/api/campaigns")
@_safe
async def create_campaign(
    title: str = Form(...),
    location: str = Form(...),
    description: str = Form(...),
    goal: float = Form(...),
    user_email: Optional[str] = Form(default=None), # ignored: the user is taken from the login token
    file: UploadFile = File(...),
    additional_files: List[UploadFile] = File(default=[]),
    verification_docs: List[UploadFile] = File(default=[]),
    bank_account_name: Optional[str] = Form(default=None),
    bank_account_number: Optional[str] = Form(default=None),
    bank_ifsc_code: Optional[str] = Form(default=None),
    bank_name: Optional[str] = Form(default=None),
    upi_id: Optional[str] = Form(default=None),
    information: Optional[str] = Form(default=None),
    category: Optional[str] = Form(default=None),
    end_date: Optional[str] = Form(default=None),
    upi_qr_code: Optional[UploadFile] = File(default=None),
    user: dict = Depends(get_current_user),
):
  profile = user
  if profile.get("role") == "admin": # admins skip the approval queue but still need bank details to go live
    status = "active" if _campaign_has_bank_details(bank_account_number, bank_ifsc_code, bank_name) else "pending_bank_details"
  else:
    status = "pending"
  image_url = await _upload(file)
  additional_images = [await _upload(f) for f in additional_files[:10]]
  doc_urls = [await _upload(f, "proof-", "bin", "application/octet-stream") for f in verification_docs]
  qr_url = await _upload(upi_qr_code, "qr-") if upi_qr_code is not None and upi_qr_code.filename else None
  res = supabase.table("campaigns").insert({
      "title": title,
      "location": location,
      "description": description,
      "goal": goal,
      "raised": 0,
      "status": status,
      "is_featured": False,
      "user_id": profile.get("id"),
      "image_url": image_url,
      "additional_images": additional_images,
      "document_proofs": doc_urls,
      "bank_account_name": bank_account_name,
      "bank_account_number": bank_account_number,
      "bank_ifsc_code": bank_ifsc_code,
      "bank_name": bank_name,
      "upi_id": upi_id,
      "upi_qr_code_url": qr_url,
      "information": information,
      "category": category,
      "end_date": (end_date or "").strip() or None,
}).execute()
  return {"message": "Campaign submitted successfully", "campaign": res.data}

@app.put("/api/admin/campaigns/{campaign_id}/approve", dependencies=[Depends(require_admin)])
@_safe
def approve_campaign(campaign_id: str):
  res = supabase.table("campaigns").select(", ".join(BANK_FIELDS)).eq("id", campaign_id).execute()
  if not res.data:
    raise HTTPException(status_code=404, detail="Campaign not found")
  status = "active" if _campaign_has_bank_details(*(res.data[0].get(k) for k in BANK_FIELDS)) else "pending_bank_details"
  db_res = supabase.table("campaigns").update({"status": status}).eq("id", campaign_id).execute()
  return {
      "message": "Campaign approved successfully" if status == "active" else "Campaign approved, but it needs bank details before it can go live",
      "status": status,
      "campaign": db_res.data,
}

@app.api_route("/api/admin/campaigns/{campaign_id}/assign-volunteers", methods=["PUT", "POST"], dependencies=[Depends(require_admin)])
@_safe
async def assign_campaign_volunteer(campaign_id: str, request: Request):
  try:
    body = await request.json()
  except Exception:
    body = {}
  if not isinstance(body, dict):
    body = {}
  raw = body.get("volunteer_ids") or body.get("volunteer_id") or body.get("ids") or []
  if isinstance(raw, str):
    raw = [raw]
  elif not isinstance(raw, list):
    raw = [raw] if raw else []
  ids = [str(v).strip() for v in raw if v]
  supabase.table("campaign_volunteers").delete().eq("campaign_id", campaign_id).execute()
  assigned = []
  if ids:
    assigned = supabase.table("campaign_volunteers").insert([{"campaign_id": campaign_id, "volunteer_id": v} for v in ids]).execute().data or []
  return {"success": True, "message": "Campaign volunteers assigned successfully", "assignments": assigned}

@app.put("/api/admin/campaigns/{campaign_id}", dependencies=[Depends(require_admin)])
@app.put("/api/campaigns/{campaign_id}")
@_safe
def update_campaign(campaign_id: str, data: dict, user: dict = Depends(get_current_user)):
  is_admin = user["role"] == "admin"
  camp = _require_campaign_manager(user, campaign_id)
  u = {k: data.get(k) for k in CAMPAIGN_FIELDS if k in data}
  if "end_date" in data:
    end_date = data.get("end_date")
    u["end_date"] = end_date.strip() if isinstance(end_date, str) and end_date.strip() else None
  for dest, sources in CAMPAIGN_ALIASES:
    src = next((s for s in sources if s in data), None)
    if src:
      u[dest] = data.get(src)
  if not is_admin:
    for k in ADMIN_ONLY_CAMPAIGN_FIELDS:
      u.pop(k, None)
    # The dashboard re-sends the whole campaign on every edit, so only treat a payment field as an edit if its value changed
    for k in PAYMENT_FIELDS:
      if k in u and str(u[k] or "") == str(camp.get(k) or ""):
        u.pop(k)
    if any(k in u for k in PAYMENT_FIELDS):
      _require_payment_edit(user, camp)
  # Re-check bank details whenever one is touched so a campaign can never be active without them
  if any(k in data for k in BANK_FIELDS):
    cur = supabase.table("campaigns").select("status, " + ", ".join(BANK_FIELDS)).eq("id", campaign_id).execute().data
    if cur:
      cur = cur[0]
      has_bank = _campaign_has_bank_details(*(u.get(k, cur.get(k)) for k in BANK_FIELDS))
      status = u.get("status", cur.get("status"))
      if status == "pending_bank_details" and has_bank:
        u["status"] = "active"
      elif status == "active" and not has_bank:
        u["status"] = "pending_bank_details"
  res = supabase.table("campaigns").update(u).eq("id", campaign_id).execute()
  return {"message": "Campaign updated successfully", "campaign": res.data}

@app.put("/api/campaigns/{campaign_id}/progress")
@_safe
def update_campaign_progress(campaign_id: str, data: ProgressRequest, user: dict = Depends(get_current_user)):
  _require_campaign_manager(user, campaign_id)
  res = supabase.table("campaigns").update({"raised": data.raised}).eq("id", campaign_id).execute()
  return {"message": "Progress updated successfully", "campaign": res.data}

@app.post("/api/campaigns/{campaign_id}/upload-photo")
@_safe
async def upload_campaign_photo(campaign_id: str, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
  _require_campaign_manager(user, campaign_id)
  image_url = await _upload(file)
  res = supabase.table("campaigns").update({"image_url": image_url}).eq("id", campaign_id).execute()
  return {"message": "Photo updated successfully", "image_url": image_url, "campaign": res.data}

@app.post("/api/campaigns/{campaign_id}/upload-qr-code")
@_safe
async def upload_campaign_qr_code(campaign_id: str, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
  _require_payment_edit(user, _require_campaign_manager(user, campaign_id))
  qr_url = await _upload(file, "qr-")
  res = supabase.table("campaigns").update({"upi_qr_code_url": qr_url}).eq("id", campaign_id).execute()
  return {"message": "QR code uploaded successfully", "upi_qr_code_url": qr_url, "campaign": res.data}

@app.post("/api/campaigns/{campaign_id}/upload-gallery")
@_safe
async def upload_campaign_gallery(campaign_id: str, files: List[UploadFile] = File(...), user: dict = Depends(get_current_user)):
  _require_campaign_manager(user, campaign_id)
  res = supabase.table("campaigns").select("additional_images").eq("id", campaign_id).execute()
  current = (res.data[0].get("additional_images") if res.data else None) or []
  new_urls = []
  for f in files:
    if len(current) + len(new_urls) >= 10:
      break
    new_urls.append(await _upload(f))
  images = current + new_urls
  db_res = supabase.table("campaigns").update({"additional_images": images}).eq("id", campaign_id).execute()
  return {"message": "Gallery updated successfully", "additional_images": images, "campaign": db_res.data}

@app.post("/api/campaigns/{campaign_id}/upload-verification-docs")
@app.post("/api/campaigns/{campaign_id}/upload-documents")
@app.post("/api/admin/campaigns/{campaign_id}/upload-documents", dependencies=[Depends(require_admin)])
@_safe
async def upload_verification_docs(
    campaign_id: str,
    request: Request,
    verification_docs: Optional[List[UploadFile]] = File(default=None),
    files: Optional[List[UploadFile]] = File(default=None),
    file: Optional[UploadFile] = File(default=None),
    documents: Optional[List[UploadFile]] = File(default=None),
    document: Optional[UploadFile] = File(default=None),
    user: dict = Depends(get_current_user),
):
  _require_campaign_manager(user, campaign_id)
  all_files = [*(verification_docs or []), *(files or []), *([file] if file else []), *(documents or []), *([document] if document else [])]
  if not all_files:
    try:
      all_files = [v for _, v in (await request.form()).multi_items() if isinstance(v, UploadFile)]
    except Exception:
      pass
  if not all_files:
    raise HTTPException(status_code=422, detail="No files provided for upload")
  res = supabase.table("campaigns").select("id, document_proofs").eq("id", campaign_id).execute()
  if not res.data:
    raise HTTPException(status_code=404, detail="Campaign not found")
  docs = (res.data[0].get("document_proofs") or []) + [await _upload(f, "proof-", "bin", "application/octet-stream") for f in all_files]
  db_res = supabase.table("campaigns").update({"document_proofs": docs}).eq("id", campaign_id).execute()
  return {"message": "Verification documents uploaded successfully", "verification_docs": docs, "campaign": db_res.data}

@app.api_route("/api/campaigns/{campaign_id}/delete-verification-doc", methods=["POST", "DELETE", "PUT"])
@app.api_route("/api/admin/campaigns/{campaign_id}/delete-verification-doc", methods=["POST", "DELETE", "PUT"], dependencies=[Depends(require_admin)])
@_safe
async def delete_verification_doc(campaign_id: str, request: Request, data: Optional[DeleteDocRequest] = None, user: dict = Depends(get_current_user)):
  _require_campaign_manager(user, campaign_id)
  file_url = None
  try:
    body = await request.json()
    if isinstance(body, dict):
      file_url = body.get("file_url") or body.get("url") or body.get("file") or body.get("path")
  except Exception:
    pass
  if not file_url and data:
    file_url = data.file_url or data.url
  params = request.query_params
  file_url = file_url or params.get("file_url") or params.get("url") or params.get("file")
  res = supabase.table("campaigns").select("id, document_proofs").eq("id", campaign_id).execute()
  if not res.data:
    raise HTTPException(status_code=404, detail="Campaign not found")
  docs = res.data[0].get("document_proofs") or []
  if file_url:
    docs = [d for d in docs if d != file_url and file_url not in d and d not in file_url]
  db_res = supabase.table("campaigns").update({"document_proofs": docs}).eq("id", campaign_id).execute()
  return {"success": True, "message": "Verification document deleted successfully", "verification_docs": docs, "campaign": db_res.data}

@app.delete("/api/campaigns/{campaign_id}", dependencies=[Depends(require_admin)])
@_safe
def delete_campaign(campaign_id: str):
  supabase.table("campaigns").delete().eq("id", campaign_id).execute()
  return {"message": "Campaign deleted successfully"}

@app.get("/api/admin/volunteers", dependencies=[Depends(require_admin)])
@_safe
def get_volunteers():
  volunteers = supabase.table("profiles").select("*").ilike("role", "volunteer").execute().data or []
  for v in volunteers:
    _sync_name(v)
  return {"volunteers": public_profile(volunteers)}

@app.post("/api/admin/volunteers", dependencies=[Depends(require_admin)])
@_safe
def create_volunteer(data: dict):
  res = supabase.table("profiles").insert({
      "full_name": data.get("name") or data.get("full_name"),
      "email": (data.get("email") or "").strip().lower(),
      "password": hash_password(_clean_password(data.get("password"))),
      "phone": data.get("phone"),
      "city": data.get("city"),
      "bio": data.get("bio"),
      "photo_url": data.get("photo_url"),
      "role": "volunteer",
      "approval_status": data.get("approval_status", "approved"),
}).execute()
  return {"message": "Volunteer added successfully", "volunteer": public_profile(res.data)}

@app.put("/api/admin/volunteers/{volunteer_id}/approve", dependencies=[Depends(require_admin)])
@_safe
def approve_volunteer(volunteer_id: str):
  res = supabase.table("profiles").update({"approval_status": "approved"}).eq("id", volunteer_id).execute()
  return {"message": "Volunteer approved successfully", "volunteer": public_profile(res.data)}

@app.put("/api/volunteers/{volunteer_id}")
@_safe
def update_volunteer_profile(volunteer_id: str, data: dict, user: dict = Depends(get_current_user)):
  _require_self_or_admin(user, volunteer_id)
  res = supabase.table("profiles").update(_profile_update(data, ("phone", "city", "bio", "photo_url"))).eq("id", volunteer_id).execute()
  return {"message": "Profile updated successfully", "volunteer": public_profile(res.data)}

@app.put("/api/admin/volunteers/{volunteer_id}", dependencies=[Depends(require_admin)])
@_safe
def update_volunteer(volunteer_id: str, data: dict):
  u = _profile_update(data, ("email", "phone", "city", "bio", "photo_url", "approval_status"))
  if u.get("email"):
    u["email"] = u["email"].strip().lower()
  if data.get("password"):
    u["password"] = hash_password(_clean_password(data["password"]))
  res = supabase.table("profiles").update(u).eq("id", volunteer_id).execute()
  return {"message": "Volunteer updated successfully", "volunteer": public_profile(res.data)}

@app.post("/api/admin/volunteers/{volunteer_id}/upload-photo", dependencies=[Depends(require_admin)])
@app.post("/api/volunteers/{volunteer_id}/upload-photo")
@_safe
async def upload_volunteer_photo(volunteer_id: str, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
  _require_self_or_admin(user, volunteer_id)
  photo_url = await _upload(file, "volunteer-")
  res = supabase.table("profiles").update({"photo_url": photo_url}).eq("id", volunteer_id).execute()
  return {"message": "Volunteer photo uploaded successfully", "photo_url": photo_url, "volunteer": public_profile(res.data)}

@app.delete("/api/admin/volunteers/{volunteer_id}", dependencies=[Depends(require_admin)])
@_safe
def delete_volunteer(volunteer_id: str):
  supabase.table("profiles").delete().eq("id", volunteer_id).execute()
  return {"message": "Volunteer deleted successfully"}

@app.get("/api/volunteer/me")
@_safe
def volunteer_me(user: dict = Depends(get_current_user)):
  res = supabase.table("profiles").select("*").eq("id", user["id"]).execute()
  if not res.data:
    raise HTTPException(status_code=404, detail="Profile not found")
  _sync_name(res.data[0])
  return {"profile": public_profile(res.data[0])}

@app.get("/api/volunteer/campaigns")
@_safe
def volunteer_campaigns(user: dict = Depends(get_current_user)):
  # Campaigns assigned to this volunteer plus the ones they created themselves (pending / needs bank details included)
  junction = supabase.table("campaign_volunteers").select("campaign_id").eq("volunteer_id", user["id"]).execute().data or []
  ids = [i["campaign_id"] for i in junction]
  mine = _campaigns(supabase.table("campaigns").select("*").eq("user_id", user["id"]))
  assigned = _campaigns(supabase.table("campaigns").select("*").in_("id", ids)) if ids else []
  seen, campaigns = set(), []
  for c in mine + assigned:
    if c.get("id") not in seen:
      seen.add(c.get("id"))
      campaigns.append(c)
  return {"campaigns": campaigns}

@app.post("/api/auth/login")
@_safe
def login(data: LoginRequest, request: Request):
  raw_email = data.email.strip()
  email = raw_email.lower()
  ip_key, email_key = f"login-ip:{client_ip(request)}", f"login-email:{email}"
  if is_limited(ip_key, 20, 900) or is_limited(email_key, 5, 900):
    raise HTTPException(status_code=429, detail="Too many failed login attempts. Please try again in 15 minutes.")
  rows = supabase.table("profiles").select("*").in_("email", list({raw_email, email})).execute().data or []
  profile = next((r for r in rows if (r.get("email") or "").strip().lower() == email), None)
  password = data.password.strip()
  if not profile or not verify_password(password, profile.get("password")):
    record_attempt(ip_key)
    record_attempt(email_key)
    raise HTTPException(status_code=401, detail="Invalid email or password")
  clear_attempts(email_key)
  if not is_hashed(profile.get("password")): # legacy plaintext row: upgrade it on first successful login
    supabase.table("profiles").update({"password": hash_password(password)}).eq("id", profile["id"]).execute()
  _sync_name(profile)
  if profile.get("role", "").lower() == "volunteer" and profile.get("approval_status", "").lower() == "pending":
    raise HTTPException(status_code=403, detail="Kindly wait until the admin has approved your volunteer registration request.")
  return {
      "message": "Login successful",
      "access_token": create_access_token(profile),
      "token_type": "bearer",
      "expires_in": TOKEN_TTL_SECONDS,
      "role": profile["role"],
      "profile": public_profile(profile),
}

@app.post("/api/auth/change-password")
@_safe
def change_password(data: ChangePasswordRequest, user: dict = Depends(get_current_user)):
  key = f"change-password:{user['id']}"
  if is_limited(key, 5, 900):
    raise HTTPException(status_code=429, detail="Too many incorrect attempts. Please try again in 15 minutes.")
  rows = supabase.table("profiles").select("password").eq("id", user["id"]).execute().data
  current = data.current_password.strip()
  if not rows or not verify_password(current, rows[0].get("password")):
    record_attempt(key)
    raise HTTPException(status_code=400, detail="Current password is incorrect.") # 400, not 401, so the frontend does not treat it as an expired session
  new_password = _clean_password(data.new_password)
  if new_password == current:
    raise HTTPException(status_code=400, detail="New password must be different from the current one.")
  supabase.table("profiles").update({"password": hash_password(new_password)}).eq("id", user["id"]).execute()
  clear_attempts(key)
  return {"success": True, "message": "Password changed successfully."}

def _send_otp_email(clean_email: str, otp_code: str):
  # Runs as a background task so the API responds instantly instead of waiting on Gmail's SMTP handshake
  sender = "nusrah.support@gmail.com"
  app_password = os.getenv("GMAIL_APP_PASSWORD")
  if not app_password:
    print("[forgot-password] GMAIL_APP_PASSWORD is not set — OTP email cannot be sent.")
    return
  msg = MIMEMultipart("alternative")
  msg["Subject"] = "Your Nusrah verification code"
  msg["From"] = f"Nusrah Portal <{sender}>"
  msg["To"] = clean_email
  msg.attach(MIMEText(
      "Hello,\n\n"
      "We received a request to reset your password for your Nusrah account.\n\n"
      "Your verification code is:\n\n"
      f" {otp_code}\n\n"
      "This code will expire in 10 minutes. If you didn't request this, you can ignore this email.\n\n"
      "Nusrah Team",
      "plain",
))
  # Boxed digits use table cells because most email clients strip flexbox/grid
  digit_cells = "".join(
      f"""<td style="width: 44px; height: 54px; background: #ffffff; border: 2px solid #16a34a;
border-radius: 10px; text-align: center; vertical-align: middle;
font-family: 'Courier New', monospace; font-size: 26px; font-weight: bold;
color: #16a34a; padding: 0 4px;">{digit}</td>
<td style="width: 8px;"></td>"""
      for digit in otp_code
)
  msg.attach(MIMEText(f"""
<div style="font-family: Arial, sans-serif; padding: 24px; color: #333; max-width: 480px; margin: 0 auto;">
<h2 style="margin-bottom: 4px;">Password reset request</h2>
<p>Hello,</p>
<p>We received a request to reset your password for your Nusrah account. Use the verification code below to continue.</p>
<div style="background: #f6f7f5; border-radius: 16px; padding: 24px 16px; margin: 20px 0; text-align: center;">
<p style="margin: 0 0 14px; font-size: 13px; color: #6b7280; text-transform: uppercase; letter-spacing: 1px;">Your verification code</p>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin: 0 auto;">
<tr>{digit_cells}</tr>
</table>
<p style="margin: 18px 0 6px; font-size: 12px; color: #6b7280;">Tap and hold the code below to select and copy it</p>
<p style="margin: 0; font-family: 'Courier New', monospace; font-size: 28px; font-weight: bold;
letter-spacing: 8px; color: #16a34a; user-select: all;">{otp_code}</p>
</div>
<p>This code will expire in 10 minutes. If you didn't request this, you can safely ignore this email.</p>
<hr style="border: none; border-top: 1px solid #eee;" />
<p style="font-size: 12px; color: #777;">Nusrah Team</p>
</div>
""", "html"))
  try:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server: # timeout stops a stalled connection from hanging forever
      server.login(sender, app_password)
      server.sendmail(sender, clean_email, msg.as_string())
  except Exception as e:
    print(f"[forgot-password] Failed to send OTP email to {clean_email}: {e}")

@app.post("/api/auth/forgot-password")
def forgot_password(data: ForgotPasswordRequest, background_tasks: BackgroundTasks, request: Request):
  try:
    email = data.email.strip().lower()
    ip_key, email_key = f"forgot-ip:{client_ip(request)}", f"forgot-email:{email}"
    if is_limited(ip_key, 10, 900) or is_limited(email_key, 3, 900):
      raise HTTPException(status_code=429, detail="Too many requests. Please try again in 15 minutes.")
    record_attempt(ip_key)
    record_attempt(email_key)
    if not supabase.table("profiles").select("*").eq("email", email).execute().data:
      raise HTTPException(status_code=404, detail="Email not registered.") # frontend checks this 404 to show the register screen
    otp_code = str(100000 + secrets.randbelow(900000))
    supabase.table("profiles").update({"reset_otp": otp_code, "otp_expires_at": int(time.time()) + 600}).eq("email", email).execute()
    background_tasks.add_task(_send_otp_email, email, otp_code)
    return {"success": True, "message": "OTP sent successfully to your email."}
  except HTTPException:
    raise
  except Exception as e:
    raise HTTPException(status_code=500, detail=f"Failed to process OTP request: {str(e)}")

@app.post("/api/auth/reset-password")
@_safe
def reset_password(data: ResetPasswordRequest):
  email = data.email.strip().lower()
  fail_key = f"reset-email:{email}"
  if is_limited(fail_key, 5, 900):
    raise HTTPException(status_code=429, detail="Too many incorrect codes. Please request a new code in 15 minutes.")
  new_password = _clean_password(data.new_password)
  res = supabase.table("profiles").select("*").eq("email", email).execute()
  if not res.data:
    raise HTTPException(status_code=404, detail="User not found.")
  user = res.data[0]
  stored_otp = str(user.get("reset_otp", ""))
  expires_at = int(user.get("otp_expires_at") or 0)
  if not stored_otp or not secrets.compare_digest(stored_otp.encode(), data.otp.strip().encode()):
    record_attempt(fail_key)
    raise HTTPException(status_code=400, detail="Invalid OTP code.")
  if time.time() > expires_at:
    raise HTTPException(status_code=400, detail="OTP code has expired.")
  supabase.table("profiles").update({"password": hash_password(new_password), "reset_otp": None, "otp_expires_at": None}).eq("email", email).execute()
  clear_attempts(fail_key)
  return {"success": True, "message": "Password reset successfully. You can now log in."}

@app.post("/api/auth/signup")
@app.post("/api/register")
@app.post("/api/auth/register")
@_safe
def signup(data: SignupRequest):
  email = data.email.strip().lower()
  if supabase.table("profiles").select("*").eq("email", email).execute().data:
    raise HTTPException(status_code=400, detail="Email already registered")
  res = supabase.table("profiles").insert({
      "full_name": data.full_name or data.name,
      "email": email,
      "password": hash_password(_clean_password(data.password)),
      "phone": data.phone,
      "city": data.city,
      "bio": data.bio or "",
      "photo_url": data.photo_url or "",
      "role": "volunteer",
      "approval_status": "pending",
}).execute()
  return {"message": "Account created successfully", "profile": public_profile(res.data)}

def _assert_admin_routes_protected():
  for r in app.routes:
    path = getattr(r, "path", "")
    if path.startswith("/api/admin") and not any(d.call is require_admin for d in r.dependant.dependencies):
      raise RuntimeError(f"Admin route is missing the admin guard: {path}")

_assert_admin_routes_protected()