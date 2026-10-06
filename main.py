from typing import List, Optional
import uuid
import os
import time
import random
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import razorpay
from database import supabase
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

# allow_origins=["*"] combined with allow_credentials=True is actually
# invalid per the CORS spec — browsers refuse to honor credentialed
# requests against a wildcard origin, so it never truly worked as intended.
# ALLOWED_ORIGINS is a comma-separated env var, e.g.:
#   ALLOWED_ORIGINS=https://nusrah.in,https://www.nusrah.in
# Locally, it falls back to common Angular dev server ports so nothing
# breaks if the env var isn't set yet.
_allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = (
    [origin.strip() for origin in _allowed_origins_env.split(",") if origin.strip()]
    or ["http://localhost:4200", "http://127.0.0.1:4200"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Razorpay Configuration ---
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


# --- Pydantic Models ---
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


class VolunteerBase(BaseModel):
  name: Optional[str] = None
  full_name: Optional[str] = None
  email: Optional[str] = None
  phone: Optional[str] = None
  city: Optional[str] = None
  bio: Optional[str] = None
  photo_url: Optional[str] = None
  password: Optional[str] = None
  approval_status: Optional[str] = "pending"


class ForgotPasswordRequest(BaseModel):
  email: str


class ResetPasswordRequest(BaseModel):
  email: str
  otp: str
  new_password: str


class ProgressRequest(BaseModel):
  raised: float


class CampaignAssignmentRequest(BaseModel):
  volunteer_ids: Optional[List[str]] = []


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


# --- API Endpoints ---


@app.get("/")
def read_root():
  return {"message": "Welcome to Nusrah API!"}


def _campaign_has_bank_details(bank_account_number, bank_ifsc_code, bank_name) -> bool:
  """
  A campaign is only allowed to be 'active' (live and visible to donors) once
  its actual bank transfer details are on file — this is separate from the
  Razorpay checkout, and exists so the charity's own records always have a
  verified settlement account for every live campaign.
  """
  return bool(
      (bank_account_number or "").strip()
      and (bank_ifsc_code or "").strip()
      and (bank_name or "").strip()
  )


@app.post("/api/create-order")
def create_razorpay_order(data: CreateOrderRequest):
  try:
    amount_in_paise = int(data.amount * 100)
    
    order_data = {
        "amount": amount_in_paise,
        "currency": "INR",
        "payment_capture": 1
    }
    
    order = razorpay_client.order.create(data=order_data)
    
    return {
        "success": True,
        "order_id": order["id"],
        "amount": order["amount"],
        "currency": order["currency"],
        "key_id": RAZORPAY_KEY_ID
    }
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/create-qr-code")
def create_razorpay_qr_code(data: CreateQRRequest):
  try:
    amount_in_paise = int(data.amount * 100)
    close_by_timestamp = int(time.time()) + (15 * 60)

    qr_data = {
        "type": "upi_qr",
        "name": data.name,
        "usage": "single_use",
        "fixed_amount": True,
        "payment_amount": amount_in_paise,
        "description": data.description,
        "close_by": close_by_timestamp,
        "notes": {
            "campaign_id": data.campaign_id
        }
    }

    qr_response = razorpay_client.qrcode.create(qr_data)

    return {
        "success": True,
        "qr_code_id": qr_response.get("id"),
        "image_url": qr_response.get("image_url"),
        "payment_amount": qr_response.get("payment_amount"),
        "status": qr_response.get("status"),
        "close_by": qr_response.get("close_by")
    }
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/verify-payment")
def verify_razorpay_payment(data: VerifyPaymentRequest):
  try:
    razorpay_client.utility.verify_payment_signature({
        'razorpay_order_id': data.razorpay_order_id,
        'razorpay_payment_id': data.razorpay_payment_id,
        'razorpay_signature': data.razorpay_signature
    })

    camp_res = supabase.table("campaigns").select("raised, goal, reach").eq("id", data.campaign_id).execute()
    if not camp_res.data:
      raise HTTPException(status_code=404, detail="Campaign not found")
    
    current_raised = float(camp_res.data[0].get("raised") or 0)
    new_raised = current_raised + float(data.amount)

    # "reach" tracks the number of completed donations for this campaign (shown
    # to donors on the donate page as the donor count). Incremented by 1 per
    # successful payment, same as raised is incremented by the payment amount.
    current_reach = int(camp_res.data[0].get("reach") or 0)
    new_reach = current_reach + 1

    supabase.table("campaigns").update({"raised": new_raised, "reach": new_reach}).eq("id", data.campaign_id).execute()

    donation_data = {
        "campaign_id": data.campaign_id,
        "amount": data.amount,
        "message": data.message or "",
    }
    
    supabase.table("donations").insert(donation_data).execute()

    return {
        "success": True,
        "message": "Payment verified and recorded successfully",
        "new_raised": new_raised,
        "new_reach": new_reach
    }
  except razorpay.errors.SignatureVerificationError:
    raise HTTPException(status_code=400, detail="Payment signature verification failed")
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stats")
def get_stats():
  try:
    active_campaigns_res = (
        supabase.table("campaigns")
        .select("*", count="exact")
        .ilike("status", "active")
        .execute()
    )
    active_campaigns_count = (
        active_campaigns_res.count
        if active_campaigns_res.count is not None
        else len(active_campaigns_res.data or [])
    )

    finished_campaigns_res = (
        supabase.table("campaigns")
        .select("*", count="exact")
        .ilike("status", "finished")
        .execute()
    )
    finished_campaigns_count = (
        finished_campaigns_res.count
        if finished_campaigns_res.count is not None
        else len(finished_campaigns_res.data or [])
    )

    volunteers_res = (
        supabase.table("profiles")
        .select("*", count="exact")
        .ilike("role", "volunteer")
        .ilike("approval_status", "approved")
        .execute()
    )
    volunteers_count = (
        volunteers_res.count
        if volunteers_res.count is not None
        else len(volunteers_res.data or [])
    )

    return {
        "active_campaigns": active_campaigns_count,
        "active_volunteers": volunteers_count,
        "finished_campaigns": finished_campaigns_count,
    }
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/home/campaigns")
def get_home_campaigns():
  try:
    response = (
        supabase.table("campaigns")
        .select("*")
        .eq("is_featured", True)
        .ilike("status", "active")
        .execute()
    )
    campaigns = response.data or []

    for c in campaigns:
      if "document_proofs" in c and "verification_docs" not in c:
        c["verification_docs"] = c["document_proofs"]
      elif "verification_docs" in c and "document_proofs" not in c:
        c["document_proofs"] = c["verification_docs"]

    return {"campaigns": campaigns}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/campaigns")
def get_all_campaigns():
  try:
    response = (
        supabase.table("campaigns")
        .select("*")
        .ilike("status", "active")
        .execute()
    )
    
    campaigns = response.data or []
    for c in campaigns:
      if "document_proofs" in c and "verification_docs" not in c:
        c["verification_docs"] = c["document_proofs"]
      elif "verification_docs" in c and "document_proofs" not in c:
        c["document_proofs"] = c["verification_docs"]

    return {"campaigns": campaigns}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/campaigns")
def get_admin_campaigns():
  try:
    campaigns_res = supabase.table("campaigns").select("*").execute()
    campaigns = campaigns_res.data or []

    try:
      vol_rels_res = supabase.table("campaign_volunteers").select("*").execute()
      vol_rels = vol_rels_res.data or []
    except Exception:
      vol_rels = []

    for camp in campaigns:
      if "document_proofs" in camp and "verification_docs" not in camp:
        camp["verification_docs"] = camp["document_proofs"]
      elif "verification_docs" in camp and "document_proofs" not in camp:
        camp["document_proofs"] = camp["verification_docs"]

      camp_id = str(camp.get("id", "")).strip().lower()
      matched_vols = [
          v["volunteer_id"]
          for v in vol_rels
          if str(v.get("campaign_id", "")).strip().lower() == camp_id
      ]
      camp["assigned_volunteers_count"] = len(matched_vols)
      camp["volunteer_ids"] = matched_vols

    return {"campaigns": campaigns}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/campaign-volunteers")
def get_admin_campaign_volunteers():
  try:
    response = supabase.table("campaign_volunteers").select("*").execute()
    return {
        "assignments": response.data or [],
        "campaign_volunteers": response.data or [],
    }
  except Exception as e:
    return {"assignments": [], "campaign_volunteers": []}


@app.get("/api/volunteers/profile")
def get_volunteer_profile_by_query(identifier: str):
  try:
    query = supabase.table("profiles").select("*")
    if "@" in identifier:
      res = query.eq("email", identifier).execute()
    else:
      res = query.eq("id", identifier).execute()

    if not res.data:
      raise HTTPException(status_code=404, detail="Profile not found")
    
    profile = res.data[0]
    if "full_name" in profile and not profile.get("name"):
      profile["name"] = profile["full_name"]
    elif "name" in profile and not profile.get("full_name"):
      profile["full_name"] = profile["name"]

    return {"profile": profile}
  except HTTPException as he:
    raise he
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/volunteers/{volunteer_identifier}/campaigns")
def get_volunteer_campaigns(volunteer_identifier: str):
  try:
    vol_id = volunteer_identifier
    if "@" in volunteer_identifier:
      prof_res = (
          supabase.table("profiles")
          .select("id")
          .eq("email", volunteer_identifier)
          .execute()
      )
      if prof_res.data:
        vol_id = prof_res.data[0].get("id")
      else:
        return {"campaigns": []}

    junction_res = (
        supabase.table("campaign_volunteers")
        .select("campaign_id")
        .eq("volunteer_id", vol_id)
        .execute()
    )
    if not junction_res.data:
      return {"campaigns": []}

    campaign_ids = [item["campaign_id"] for item in junction_res.data]
    campaigns_res = (
        supabase.table("campaigns").select("*").in_("id", campaign_ids).execute()
    )

    campaigns = campaigns_res.data or []
    for c in campaigns:
      if "document_proofs" in c and "verification_docs" not in c:
        c["verification_docs"] = c["document_proofs"]
      elif "verification_docs" in c and "document_proofs" not in c:
        c["document_proofs"] = c["verification_docs"]

    return {"campaigns": campaigns}
  except Exception as e:
    return {"campaigns": []}


@app.post("/api/campaigns")
async def create_campaign(
    title: str = Form(...),
    location: str = Form(...),
    description: str = Form(...),
    goal: float = Form(...),
    user_email: str = Form(...),
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
):
  try:
    profile_res = (
        supabase.table("profiles")
        .select("id, role")
        .eq("email", user_email)
        .execute()
    )
    if not profile_res.data:
      raise HTTPException(status_code=403, detail="Unauthorized user")

    user_profile = profile_res.data[0]
    user_id = user_profile.get("id")
    user_role = user_profile.get("role", "").lower()

    if user_role == "admin":
      # Admin submissions skip the approval queue, but still can't go live
      # without bank details on file.
      campaign_status = (
          "active"
          if _campaign_has_bank_details(bank_account_number, bank_ifsc_code, bank_name)
          else "pending_bank_details"
      )
    else:
      # Volunteer submissions always need admin approval first, regardless of
      # whether bank details were filled in at submission time.
      campaign_status = "pending"

    clean_end_date = end_date.strip() if end_date and end_date.strip() else None

    file_bytes = await file.read()
    file_ext = file.filename.split(".")[-1] if file.filename else "jpg"
    file_name = f"{uuid.uuid4()}.{file_ext}"

    supabase.storage.from_("campaign-images").upload(
        file_name, file_bytes, file_options={"content-type": file.content_type or "image/jpeg"}
    )
    image_url = supabase.storage.from_("campaign-images").get_public_url(
        file_name
    )

    additional_images_urls = []
    for add_file in additional_files[:10]:
      add_bytes = await add_file.read()
      add_ext = add_file.filename.split(".")[-1] if add_file.filename else "jpg"
      add_name = f"{uuid.uuid4()}.{add_ext}"
      supabase.storage.from_("campaign-images").upload(
          add_name, add_bytes, file_options={"content-type": add_file.content_type or "image/jpeg"}
      )
      add_url = supabase.storage.from_("campaign-images").get_public_url(
          add_name
      )
      additional_images_urls.append(add_url)

    doc_urls = []
    for doc_file in verification_docs:
      doc_bytes = await doc_file.read()
      doc_ext = doc_file.filename.split(".")[-1] if doc_file.filename else "bin"
      doc_name = f"proof-{uuid.uuid4()}.{doc_ext}"
      supabase.storage.from_("campaign-images").upload(
          doc_name, doc_bytes, file_options={"content-type": doc_file.content_type or "application/octet-stream"}
      )
      doc_url = supabase.storage.from_("campaign-images").get_public_url(
          doc_name
      )
      doc_urls.append(doc_url)

    qr_code_url = None
    if upi_qr_code is not None and upi_qr_code.filename:
      qr_bytes = await upi_qr_code.read()
      qr_ext = upi_qr_code.filename.split(".")[-1] if upi_qr_code.filename else "jpg"
      qr_name = f"qr-{uuid.uuid4()}.{qr_ext}"
      supabase.storage.from_("campaign-images").upload(
          qr_name, qr_bytes, file_options={"content-type": upi_qr_code.content_type or "image/jpeg"}
      )
      qr_code_url = supabase.storage.from_("campaign-images").get_public_url(
          qr_name
      )

    campaign_data = {
        "title": title,
        "location": location,
        "description": description,
        "goal": goal,
        "raised": 0,
        "status": campaign_status,
        "is_featured": False,
        "user_id": user_id,
        "image_url": image_url,
        "additional_images": additional_images_urls,
        "document_proofs": doc_urls,
        "bank_account_name": bank_account_name,
        "bank_account_number": bank_account_number,
        "bank_ifsc_code": bank_ifsc_code,
        "bank_name": bank_name,
        "upi_id": upi_id,
        "upi_qr_code_url": qr_code_url,
        "information": information,
        "category": category,
        "end_date": clean_end_date,
    }

    db_res = supabase.table("campaigns").insert(campaign_data).execute()
    return {"message": "Campaign submitted successfully", "campaign": db_res.data}
  except HTTPException as he:
    raise he
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/admin/campaigns/{campaign_id}/approve")
def approve_campaign(campaign_id: str):
  try:
    camp_res = (
        supabase.table("campaigns")
        .select("bank_account_number, bank_ifsc_code, bank_name")
        .eq("id", campaign_id)
        .execute()
    )
    if not camp_res.data:
      raise HTTPException(status_code=404, detail="Campaign not found")

    camp = camp_res.data[0]
    has_bank = _campaign_has_bank_details(
        camp.get("bank_account_number"), camp.get("bank_ifsc_code"), camp.get("bank_name")
    )
    new_status = "active" if has_bank else "pending_bank_details"

    db_res = (
        supabase.table("campaigns")
        .update({"status": new_status})
        .eq("id", campaign_id)
        .execute()
    )
    return {
        "message": (
            "Campaign approved successfully"
            if new_status == "active"
            else "Campaign approved, but it needs bank details before it can go live"
        ),
        "status": new_status,
        "campaign": db_res.data,
    }
  except HTTPException as he:
    raise he
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.api_route(
    "/api/admin/campaigns/{campaign_id}/assign-volunteers",
    methods=["PUT", "POST"],
)
async def assign_campaign_volunteer(campaign_id: str, request: Request):
  try:
    try:
      body = await request.json()
    except Exception:
      body = {}

    if not isinstance(body, dict):
      body = {}

    raw_ids = (
        body.get("volunteer_ids")
        or body.get("volunteer_id")
        or body.get("ids")
        or []
    )

    if isinstance(raw_ids, str):
      volunteer_ids = [raw_ids]
    elif isinstance(raw_ids, list):
      volunteer_ids = raw_ids
    else:
      volunteer_ids = [str(raw_ids)] if raw_ids else []

    cleaned_volunteer_ids = [str(v).strip() for v in volunteer_ids if v]

    (
        supabase.table("campaign_volunteers")
        .delete()
        .eq("campaign_id", campaign_id)
        .execute()
    )

    insertion_records = [
        {"campaign_id": campaign_id, "volunteer_id": vol_id}
        for vol_id in cleaned_volunteer_ids
    ]

    assigned_data = []
    if insertion_records:
      insert_res = (
          supabase.table("campaign_volunteers")
          .insert(insertion_records)
          .execute()
      )
      assigned_data = insert_res.data or []

    return {
        "success": True,
        "message": "Campaign volunteers assigned successfully",
        "assignments": assigned_data,
    }
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/admin/campaigns/{campaign_id}")
@app.put("/api/campaigns/{campaign_id}")
def update_campaign(campaign_id: str, data: dict):
  try:
    update_data = {}
    if "title" in data:
      update_data["title"] = data.get("title")
    if "location" in data:
      update_data["location"] = data.get("location")
    if "description" in data:
      update_data["description"] = data.get("description")
    if "information" in data:
      update_data["information"] = data.get("information")
    if "category" in data:
      update_data["category"] = data.get("category")
    if "end_date" in data:
      raw_end_date = data.get("end_date")
      update_data["end_date"] = raw_end_date.strip() if isinstance(raw_end_date, str) and raw_end_date.strip() else None
    if "goal" in data:
      update_data["goal"] = data.get("goal")
    if "raised" in data:
      update_data["raised"] = data.get("raised")
    if "status" in data:
      update_data["status"] = data.get("status")
    if "is_featured" in data:
      update_data["is_featured"] = data.get("is_featured")
    if "featured_highlights" in data:
      update_data["featured_highlights"] = data.get("featured_highlights")

    if "document_proofs" in data:
      update_data["document_proofs"] = data.get("document_proofs")
    elif "verification_docs" in data:
      update_data["document_proofs"] = data.get("verification_docs")

    if "additional_images" in data:
      update_data["additional_images"] = data.get("additional_images")

    if "bank_account_name" in data:
      update_data["bank_account_name"] = data.get("bank_account_name")
    if "bank_account_number" in data:
      update_data["bank_account_number"] = data.get("bank_account_number")
    if "bank_ifsc_code" in data:
      update_data["bank_ifsc_code"] = data.get("bank_ifsc_code")
    elif "ifsc_code" in data:
      update_data["bank_ifsc_code"] = data.get("ifsc_code")
    if "bank_name" in data:
      update_data["bank_name"] = data.get("bank_name")
    if "upi_id" in data:
      update_data["upi_id"] = data.get("upi_id")
    if "upi_qr_code_url" in data:
      update_data["upi_qr_code_url"] = data.get("upi_qr_code_url")
    elif "qr_code_url" in data:
      update_data["upi_qr_code_url"] = data.get("qr_code_url")

    # Whenever any bank-detail field is touched, re-check whether the campaign
    # (as it will exist after this update) has complete bank details, and
    # auto-correct its status to match:
    #   - pending_bank_details -> active     (details just became complete)
    #   - active -> pending_bank_details     (details were just cleared/removed)
    # This runs regardless of whether the client also sent an explicit
    # "status" field, so the campaign can never end up active without bank
    # details on file.
    bank_fields_touched = any(
        k in data for k in ("bank_account_number", "bank_ifsc_code", "bank_name")
    )
    if bank_fields_touched:
      current_res = (
          supabase.table("campaigns")
          .select("status, bank_account_number, bank_ifsc_code, bank_name")
          .eq("id", campaign_id)
          .execute()
      )
      if current_res.data:
        current = current_res.data[0]
        merged_account_number = update_data.get("bank_account_number", current.get("bank_account_number"))
        merged_ifsc_code = update_data.get("bank_ifsc_code", current.get("bank_ifsc_code"))
        merged_bank_name = update_data.get("bank_name", current.get("bank_name"))
        has_bank = _campaign_has_bank_details(merged_account_number, merged_ifsc_code, merged_bank_name)

        effective_status = update_data.get("status", current.get("status"))
        if effective_status == "pending_bank_details" and has_bank:
          update_data["status"] = "active"
        elif effective_status == "active" and not has_bank:
          update_data["status"] = "pending_bank_details"

    res = (
        supabase.table("campaigns")
        .update(update_data)
        .eq("id", campaign_id)
        .execute()
    )
    return {"message": "Campaign updated successfully", "campaign": res.data}
  except HTTPException as he:
    raise he
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/campaigns/{campaign_id}/progress")
def update_campaign_progress(campaign_id: str, data: ProgressRequest):
  try:
    db_res = (
        supabase.table("campaigns")
        .update({"raised": data.raised})
        .eq("id", campaign_id)
        .execute()
    )
    return {"message": "Progress updated successfully", "campaign": db_res.data}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/campaigns/{campaign_id}/upload-photo")
async def upload_campaign_photo(campaign_id: str, file: UploadFile = File(...)):
  try:
    file_bytes = await file.read()
    file_ext = file.filename.split(".")[-1] if file.filename else "jpg"
    file_name = f"{uuid.uuid4()}.{file_ext}"

    supabase.storage.from_("campaign-images").upload(
        file_name, file_bytes, file_options={"content-type": file.content_type or "image/jpeg"}
    )

    image_url = supabase.storage.from_("campaign-images").get_public_url(
        file_name
    )
    db_res = (
        supabase.table("campaigns")
        .update({"image_url": image_url})
        .eq("id", campaign_id)
        .execute()
    )
    return {
        "message": "Photo updated successfully",
        "image_url": image_url,
        "campaign": db_res.data,
    }
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/campaigns/{campaign_id}/upload-qr-code")
async def upload_campaign_qr_code(campaign_id: str, file: UploadFile = File(...)):
  try:
    file_bytes = await file.read()
    file_ext = file.filename.split(".")[-1] if file.filename else "jpg"
    file_name = f"qr-{uuid.uuid4()}.{file_ext}"

    supabase.storage.from_("campaign-images").upload(
        file_name, file_bytes, file_options={"content-type": file.content_type or "image/jpeg"}
    )

    qr_code_url = supabase.storage.from_("campaign-images").get_public_url(
        file_name
    )
    db_res = (
        supabase.table("campaigns")
        .update({"upi_qr_code_url": qr_code_url})
        .eq("id", campaign_id)
        .execute()
    )
    return {
        "message": "QR code uploaded successfully",
        "upi_qr_code_url": qr_code_url,
        "campaign": db_res.data,
    }
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/campaigns/{campaign_id}/upload-gallery")
async def upload_campaign_gallery(
    campaign_id: str, files: List[UploadFile] = File(...)
):
  try:
    camp_res = (
        supabase.table("campaigns")
        .select("additional_images")
        .eq("id", campaign_id)
        .execute()
    )
    current_images = []
    if camp_res.data and camp_res.data[0].get("additional_images"):
      current_images = camp_res.data[0].get("additional_images")

    new_urls = []
    for file in files:
      if len(current_images) + len(new_urls) >= 10:
        break
      file_bytes = await file.read()
      file_ext = file.filename.split(".")[-1] if file.filename else "jpg"
      file_name = f"{uuid.uuid4()}.{file_ext}"
      supabase.storage.from_("campaign-images").upload(
          file_name, file_bytes, file_options={"content-type": file.content_type or "image/jpeg"}
      )
      url = supabase.storage.from_("campaign-images").get_public_url(file_name)
      new_urls.append(url)

    updated_list = current_images + new_urls
    db_res = (
        supabase.table("campaigns")
        .update({"additional_images": updated_list})
        .eq("id", campaign_id)
        .execute()
    )
    return {
        "message": "Gallery updated successfully",
        "additional_images": updated_list,
        "campaign": db_res.data,
    }
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/campaigns/{campaign_id}/upload-verification-docs")
@app.post("/api/campaigns/{campaign_id}/upload-documents")
@app.post("/api/admin/campaigns/{campaign_id}/upload-documents")
async def upload_verification_docs(
    campaign_id: str,
    request: Request,
    verification_docs: Optional[List[UploadFile]] = File(default=None),
    files: Optional[List[UploadFile]] = File(default=None),
    file: Optional[UploadFile] = File(default=None),
    documents: Optional[List[UploadFile]] = File(default=None),
    document: Optional[UploadFile] = File(default=None),
):
  try:
    all_files = []
    if verification_docs:
      all_files.extend(verification_docs)
    if files:
      all_files.extend(files)
    if file:
      all_files.append(file)
    if documents:
      all_files.extend(documents)
    if document:
      all_files.append(document)

    if not all_files:
      try:
        form = await request.form()
        for key, val in form.multi_items():
          if isinstance(val, UploadFile):
            all_files.append(val)
      except Exception:
        pass

    if not all_files:
      raise HTTPException(status_code=422, detail="No files provided for upload")

    camp_res = (
        supabase.table("campaigns")
        .select("id, document_proofs")
        .eq("id", campaign_id)
        .execute()
    )
    if not camp_res.data:
      raise HTTPException(status_code=404, detail="Campaign not found")

    current_docs = camp_res.data[0].get("document_proofs") or []
    new_doc_urls = []

    for uploaded_file in all_files:
      file_bytes = await uploaded_file.read()
      file_ext = (
          uploaded_file.filename.split(".")[-1]
          if uploaded_file.filename
          else "bin"
      )
      file_name = f"proof-{uuid.uuid4()}.{file_ext}"

      supabase.storage.from_("campaign-images").upload(
          file_name,
          file_bytes,
          file_options={"content-type": uploaded_file.content_type or "application/octet-stream"},
      )
      file_url = supabase.storage.from_("campaign-images").get_public_url(
          file_name
      )
      new_doc_urls.append(file_url)

    updated_docs = current_docs + new_doc_urls

    db_res = (
        supabase.table("campaigns")
        .update({"document_proofs": updated_docs})
        .eq("id", campaign_id)
        .execute()
    )

    return {
        "message": "Verification documents uploaded successfully",
        "verification_docs": updated_docs,
        "campaign": db_res.data,
    }
  except HTTPException as he:
    raise he
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.api_route(
    "/api/campaigns/{campaign_id}/delete-verification-doc",
    methods=["POST", "DELETE", "PUT"],
)
@app.api_route(
    "/api/admin/campaigns/{campaign_id}/delete-verification-doc",
    methods=["POST", "DELETE", "PUT"],
)
async def delete_verification_doc(
    campaign_id: str, request: Request, data: Optional[DeleteDocRequest] = None
):
  try:
    file_url = None
    try:
      body = await request.json()
      if isinstance(body, dict):
        file_url = (
            body.get("file_url")
            or body.get("url")
            or body.get("file")
            or body.get("path")
        )
    except Exception:
      pass

    if not file_url and data:
      file_url = data.file_url or data.url

    if not file_url:
      file_url = (
          request.query_params.get("file_url")
          or request.query_params.get("url")
          or request.query_params.get("file")
      )

    camp_res = (
        supabase.table("campaigns")
        .select("id, document_proofs")
        .eq("id", campaign_id)
        .execute()
    )
    if not camp_res.data:
      raise HTTPException(status_code=404, detail="Campaign not found")

    current_docs = camp_res.data[0].get("document_proofs") or []

    if file_url:
      updated_docs = [
          doc
          for doc in current_docs
          if doc != file_url and file_url not in doc and doc not in file_url
      ]
    else:
      updated_docs = current_docs

    db_res = (
        supabase.table("campaigns")
        .update({"document_proofs": updated_docs})
        .eq("id", campaign_id)
        .execute()
    )

    return {
        "success": True,
        "message": "Verification document deleted successfully",
        "verification_docs": updated_docs,
        "campaign": db_res.data,
    }
  except HTTPException as he:
    raise he
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/campaigns/{campaign_id}")
def delete_campaign(campaign_id: str):
  try:
    supabase.table("campaigns").delete().eq("id", campaign_id).execute()
    return {"message": "Campaign deleted successfully"}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/volunteers")
def get_volunteers():
  try:
    response = (
        supabase.table("profiles")
        .select("*")
        .ilike("role", "volunteer")
        .execute()
    )
    volunteers = response.data or []
    for v in volunteers:
      if "full_name" in v and not v.get("name"):
        v["name"] = v["full_name"]
      elif "name" in v and not v.get("full_name"):
        v["full_name"] = v["name"]
    return {"volunteers": volunteers}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/volunteers")
def create_volunteer(data: dict):
  try:
    volunteer_data = {
        "full_name": data.get("name") or data.get("full_name"),
        "email": data.get("email"),
        "password": data.get("password"),
        "phone": data.get("phone"),
        "city": data.get("city"),
        "bio": data.get("bio"),
        "photo_url": data.get("photo_url"),
        "role": "volunteer",
        "approval_status": data.get("approval_status", "approved"),
    }
    res = supabase.table("profiles").insert(volunteer_data).execute()
    return {"message": "Volunteer added successfully", "volunteer": res.data}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/admin/volunteers/{volunteer_id}/approve")
def approve_volunteer(volunteer_id: str):
  try:
    db_res = (
        supabase.table("profiles")
        .update({"approval_status": "approved"})
        .eq("id", volunteer_id)
        .execute()
    )
    return {
        "message": "Volunteer approved successfully",
        "volunteer": db_res.data,
    }
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/volunteers/{volunteer_id}")
def update_volunteer_profile(volunteer_id: str, data: dict):
  try:
    update_data = {}
    if "name" in data or "full_name" in data:
      update_data["full_name"] = data.get("name") or data.get("full_name")
    if "phone" in data:
      update_data["phone"] = data.get("phone")
    if "city" in data:
      update_data["city"] = data.get("city")
    if "bio" in data:
      update_data["bio"] = data.get("bio")
    if "photo_url" in data:
      update_data["photo_url"] = data.get("photo_url")

    res = (
        supabase.table("profiles")
        .update(update_data)
        .eq("id", volunteer_id)
        .execute()
    )
    return {"message": "Profile updated successfully", "volunteer": res.data}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/admin/volunteers/{volunteer_id}")
def update_volunteer(volunteer_id: str, data: dict):
  try:
    update_data = {}
    if "name" in data or "full_name" in data:
      update_data["full_name"] = data.get("name") or data.get("full_name")
    if "email" in data:
      update_data["email"] = data.get("email")
    if "phone" in data:
      update_data["phone"] = data.get("phone")
    if "city" in data:
      update_data["city"] = data.get("city")
    if "bio" in data:
      update_data["bio"] = data.get("bio")
    if "photo_url" in data:
      update_data["photo_url"] = data.get("photo_url")
    if "approval_status" in data:
      update_data["approval_status"] = data.get("approval_status")
    if "password" in data and data.get("password"):
      update_data["password"] = data.get("password")

    res = (
        supabase.table("profiles")
        .update(update_data)
        .eq("id", volunteer_id)
        .execute()
    )
    return {"message": "Volunteer updated successfully", "volunteer": res.data}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/volunteers/{volunteer_id}/upload-photo")
async def upload_volunteer_photo(volunteer_id: str, file: UploadFile = File(...)):
  try:
    file_bytes = await file.read()
    file_ext = file.filename.split(".")[-1] if file.filename else "jpg"
    file_name = f"volunteer-{uuid.uuid4()}.{file_ext}"

    supabase.storage.from_("campaign-images").upload(
        file_name, file_bytes, file_options={"content-type": file.content_type or "image/jpeg"}
    )

    photo_url = supabase.storage.from_("campaign-images").get_public_url(
        file_name
    )
    db_res = (
        supabase.table("profiles")
        .update({"photo_url": photo_url})
        .eq("id", volunteer_id)
        .execute()
    )
    return {
        "message": "Volunteer photo uploaded successfully",
        "photo_url": photo_url,
        "volunteer": db_res.data,
    }
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/volunteers/{volunteer_id}")
def delete_volunteer(volunteer_id: str):
  try:
    supabase.table("profiles").delete().eq("id", volunteer_id).execute()
    return {"message": "Volunteer deleted successfully"}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/auth/login")
def login(data: LoginRequest):
  try:
    clean_email = data.email.strip()
    clean_password = data.password.strip()

    response = (
        supabase.table("profiles")
        .select("*")
        .eq("email", clean_email)
        .eq("password", clean_password)
        .execute()
    )

    if not response.data or len(response.data) == 0:
      raise HTTPException(status_code=401, detail="Invalid email or password")

    user_profile = response.data[0]
    if "full_name" in user_profile and not user_profile.get("name"):
      user_profile["name"] = user_profile["full_name"]
    elif "name" in user_profile and not user_profile.get("full_name"):
      user_profile["full_name"] = user_profile["name"]

    if (
        user_profile.get("role", "").lower() == "volunteer"
        and user_profile.get("approval_status", "").lower() == "pending"
    ):
      raise HTTPException(
          status_code=403,
          detail=(
              "Kindly wait until the admin has approved your volunteer"
              " registration request."
          ),
      )

    # Issue a simple session token. This is a lightweight opaque token
    # (not a verified/expiring JWT) — good enough to unblock any code
    # downstream that checks "is a token present in localStorage",
    # but you should replace this with real JWT-based auth before
    # going to production.
    session_token = uuid.uuid4().hex

    return {
        "message": "Login successful",
        "access_token": session_token,
        "role": user_profile["role"],
        "profile": user_profile,
    }
  except HTTPException as he:
    raise he
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


# --- Password Reset Endpoints (Using Gmail SMTP) ---

def _send_otp_email(clean_email: str, otp_code: str):
  """
  Runs in a background task AFTER the HTTP response has already been sent
  back to the frontend. This is what makes the OTP screen appear instantly
  instead of the button hanging on 'Sending Code...' until Gmail's SMTP
  handshake finishes (which can take anywhere from 1 to 30+ seconds, and
  can hang indefinitely if the network stalls).
  """
  sender_email = "nusrah.support@gmail.com"

  # Gmail app password is read from the environment, never hardcoded in
  # source. Set GMAIL_APP_PASSWORD in your local .env file, and in your
  # hosting platform's environment variables once deployed.
  app_password = os.getenv("GMAIL_APP_PASSWORD")
  if not app_password:
    print("[forgot-password] GMAIL_APP_PASSWORD is not set — OTP email cannot be sent.")
    return

  msg = MIMEMultipart("alternative")
  msg["Subject"] = "Your Nusrah verification code"
  msg["From"] = f"Nusrah Portal <{sender_email}>"
  msg["To"] = clean_email

  # Plain-text version. Sending HTML-only mail is one of the small signals
  # spam filters use against automated senders, so we include a plain-text
  # alternative alongside the HTML one below.
  text_content = (
      f"Hello,\n\n"
      f"We received a request to reset your password for your Nusrah account.\n\n"
      f"Your verification code is:\n\n"
      f"    {otp_code}\n\n"
      f"This code will expire in 10 minutes. If you didn't request this, you can ignore this email.\n\n"
      f"Nusrah Team"
  )
  msg.attach(MIMEText(text_content, "plain"))

  # Individual boxed digits, built as <td> cells in a <table> rather than flexbox/grid,
  # since most email clients (Outlook, older Gmail renderers) strip modern CSS layout
  # but render basic HTML tables reliably.
  digit_cells = "".join(
      f"""<td style="width: 44px; height: 54px; background: #ffffff; border: 2px solid #16a34a;
               border-radius: 10px; text-align: center; vertical-align: middle;
               font-family: 'Courier New', monospace; font-size: 26px; font-weight: bold;
               color: #16a34a; padding: 0 4px;">{digit}</td>
          <td style="width: 8px;"></td>"""
      for digit in otp_code
  )

  html_content = f"""
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
  """
  msg.attach(MIMEText(html_content, "html"))

  try:
    # timeout=15 is critical: without it, a stalled network connection to
    # Gmail can leave this call hanging forever with no error ever raised.
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
      server.login(sender_email, app_password)
      server.sendmail(sender_email, clean_email, msg.as_string())
  except Exception as e:
    # Background task exceptions don't propagate to the client (the
    # response is already gone), so just log it server-side.
    print(f"[forgot-password] Failed to send OTP email to {clean_email}: {e}")


@app.post("/api/auth/forgot-password")
def forgot_password(data: ForgotPasswordRequest, background_tasks: BackgroundTasks):
  try:
    clean_email = data.email.strip().lower()

    user_res = supabase.table("profiles").select("*").eq("email", clean_email).execute()
    if not user_res.data:
      # Frontend checks specifically for a 404 to show the
      # "you haven't registered yet" screen with a Register button.
      raise HTTPException(status_code=404, detail="Email not registered.")

    otp_code = str(random.randint(100000, 999999))
    expires_at = int(time.time()) + 600

    supabase.table("profiles").update({
        "reset_otp": otp_code,
        "otp_expires_at": expires_at
    }).eq("email", clean_email).execute()

    # OTP is generated and stored FIRST. The email is queued as a
    # background task so the response returns immediately - the frontend
    # can move to the "enter OTP" screen right away instead of waiting on
    # the SMTP round trip.
    background_tasks.add_task(_send_otp_email, clean_email, otp_code)

    return {"success": True, "message": "OTP sent successfully to your email."}
  except HTTPException as he:
    raise he
  except Exception as e:
    raise HTTPException(status_code=500, detail=f"Failed to process OTP request: {str(e)}")


@app.post("/api/auth/reset-password")
def reset_password(data: ResetPasswordRequest):
  try:
    clean_email = data.email.strip().lower()
    clean_otp = data.otp.strip()
    clean_new_password = data.new_password.strip()

    user_res = supabase.table("profiles").select("*").eq("email", clean_email).execute()
    if not user_res.data:
      raise HTTPException(status_code=404, detail="User not found.")

    user = user_res.data[0]
    stored_otp = str(user.get("reset_otp", ""))
    otp_expires_at = int(user.get("otp_expires_at", 0))

    if not stored_otp or stored_otp != clean_otp:
      raise HTTPException(status_code=400, detail="Invalid OTP code.")

    if time.time() > otp_expires_at:
      raise HTTPException(status_code=400, detail="OTP code has expired.")

    supabase.table("profiles").update({
        "password": clean_new_password,
        "reset_otp": None,
        "otp_expires_at": None
    }).eq("email", clean_email).execute()

    return {"success": True, "message": "Password reset successfully. You can now log in."}
  except HTTPException as he:
    raise he
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


def handle_signup(data: SignupRequest):
  try:
    clean_email = data.email.strip().lower()

    existing = (
        supabase.table("profiles").select("*").eq("email", clean_email).execute()
    )
    if existing.data and len(existing.data) > 0:
      raise HTTPException(status_code=400, detail="Email already registered")

    user_data = {
        "full_name": data.full_name or data.name,
        "email": clean_email,
        "password": data.password.strip(),
        "phone": data.phone,
        "city": data.city,
        "bio": data.bio or "",
        "photo_url": data.photo_url or "",
        "role": "volunteer",
        "approval_status": "pending",
    }

    res = supabase.table("profiles").insert(user_data).execute()
    return {"message": "Account created successfully", "profile": res.data}
  except HTTPException as he:
    raise he
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/auth/signup")
def signup_auth(data: SignupRequest):
  return handle_signup(data)


@app.post("/api/register")
def signup_register(data: SignupRequest):
  return handle_signup(data)


@app.post("/api/auth/register")
def signup_register_auth(data: SignupRequest):
  return handle_signup(data)