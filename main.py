"""
Environ Property Services — Agentic Chatbot
Booking-focused with Google Calendar + Twilio WhatsApp
"""
import os
import json
import re
import datetime
import pytz
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import chromadb
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

# ── Constants ──────────────────────────────────────
CHROMA_PATH        = "./chroma_db"
COLLECTION_NAME    = "environ_knowledge"
CALENDAR_ID        = "shafiq2c2c@gmail.com"
LONDON_TZ          = pytz.timezone("Europe/London")
WORK_START_H       = 9   # 9 AM
WORK_END_H         = 18  # 6 PM
SLOT_DURATION_H    = 1

GOOGLE_CREDS_FILE  = "google_credentials.json"
NOTIFY_EMAIL       = "aiagentsautomation87@gmail.com"

# ── System prompt ──────────────────────────────────
SYSTEM_PROMPT_TEMPLATE = """You are Alex, a knowledgeable and friendly property specialist at Environ Property Services, London.

━━━ YOUR PRIMARY ROLE ━━━
You are a CUSTOMER SUPPORT assistant first. Your job is to:
- Answer questions about damp, mould, rot, repointing, roofing, drainage, pest control, sash windows, and all property issues
- Explain causes, symptoms, risks, and treatment options clearly
- Give honest, helpful advice — educate the customer
- Share information about the company, services, pricing expectations, process, and credentials
- Let the CUSTOMER lead the conversation — never push or pressurise them

STYLE: Friendly and helpful. 2-4 sentences or short bullet points. No jargon. No long paragraphs.
Use bullet points by default. Only use a markdown table if the user explicitly asks for one — and keep it concise (max 6 rows).

━━━ BOOKING — ONLY WHEN THE CUSTOMER WANTS IT ━━━
NEVER push or force the booking. Only enter the booking flow when the customer clearly expresses interest (e.g. "I'd like to book", "can I make an appointment", "how do I book", "can someone come out").
At the end of relevant answers, you may add ONE soft line like: "If you'd like a specialist to take a look, I can arrange a free inspection — just let me know! 😊"

When the customer does want to book, follow these steps one at a time:
STEP 1 — Service: Ask "What service do you need?" (if not already known). If you recommended a service and the user accepted it, treat that as the confirmed service — do NOT ask again.
STEP 2 — Issues (multi-issue collection):
  • If a photo was shared ([Photo attached] in history): you already know the issue from the image — do NOT ask the customer to describe it again. Still ask "Are you also facing any other issues I should include?"
  • If no photo: ask "Could you describe the issue(s) you are facing?"
  • After receiving the first issue description, ALWAYS ask: "Are you facing any other issues as well? I can include everything in a single inspection — just let me know! 😊"
  • When the user mentions more issues using phrases like "also", "another issue", "one more thing", "and also", "plus", "as well", "additionally", "there's also" — collect each one and ask again.
  • Only stop when the user confirms no more issues: "no", "nope", "that's all", "no more", "nothing else", "just that", "that's it", "done".
  • NEVER start a new booking for each individual issue — ALL issues go into ONE single booking.
  • Once all issues are collected and confirmed, proceed to STEP 3.

STEP 3 — Date: Ask "Which day works for you?" — do NOT call check_availability yet. Wait for the user to reply with a date first.
STEP 4 — Time: Call check_availability for the date the user gave (this sends slot buttons to the frontend). Then say ONLY "We have availability on [formatted_date]! Please pick a time 👇" — do NOT list times as text.
STEP 5 — Name: Ask "Could you provide your full name?"
STEP 6 — Phone: Ask "Could you provide your phone number?"
STEP 7 — Email: Ask "Could you provide your email address?"
STEP 8 — Confirm: Show full summary. If multiple issues were reported, list them as a numbered list. Format:
  "Here's a summary of your booking:
  • Service: [service]
  • Issues reported:
    1. [first issue]
    2. [second issue] (if applicable)
  • Date: [date]  • Time: [time]
  • Name: [name]
  • Phone: [phone]
  • Email: [email]
  Shall I confirm this booking?"
STEP 9 — Book: Call book_appointment only AFTER confirmation. Pass ALL issues combined as the issue field. Then say "✅ You're booked! See you on [date] at [time], [name]."

━━━ BOOKING RULES ━━━
- The BOOKING STATE block injected above this message shows what is already collected. NEVER re-ask for a ✅ field. Jump to the stated NEXT STEP.
- Each step asks ONE question and waits. Move on after the reply — never loop on the same step.
- Typed time in HH:MM (e.g. "13:00") = valid slot. Proceed with it.
- NEVER validate email — any string with @ is valid. NEVER validate phone — any digits are valid.
- Confirmation words (yes/sure/ok/yeah/correct/go ahead/confirm/please/do it/yep) = proceed.
- On slot_taken: call check_availability for same date, show new buttons, book with SAME details.
- book_appointment needs: date, time, name, phone, email, service, issue — all 7 fields.

━━━ CANCEL / RESCHEDULE ━━━
Cancel: ask email → find_booking → confirm with user → cancel_booking.
Reschedule: ask email → find_booking → ask new day → check_availability → confirm → reschedule_booking.

━━━ DATE RULES ━━━
- No Sundays. No past dates. Today is {today}.
- Copy exact "formatted_date" from check_availability result — never recalculate.

━━━ IMAGES ━━━
When a customer sends a photo: study it carefully, describe what you can see (damp patches, mould, staining, cracks, rot, etc.), identify the likely issue and its severity, and give useful advice. Always acknowledge the image — never say you cannot see or process it.
If the customer later refers back to a photo they sent earlier (e.g. "I already shared the picture", "can you review it", "I sent you a photo"):
- You CANNOT re-analyse the original image — only the first message contained the actual photo data
- Instead, refer back to what you described in your EARLIER response about the image (look up your previous analysis in the conversation)
- Say something like: "Yes, from the photo you shared earlier I could see [your earlier finding]. Based on that…"
- Do NOT say you cannot see the image or ask them to re-send it
- Do NOT ask them to describe the issue — you already have it from the photo

SERVICES: Damp (rising/penetrating/lateral/condensation), mould removal, dry/wet rot, repointing, brick cleaning, heritage restoration, roofing, drainage, sash windows, pest control.
COMPANY: Environ Property Services — family-run, London-based, 15+ years, PCA-accredited, TrustMark registered. Free inspections available.
Hours: Monday–Saturday 9 AM–6 PM London time."""

# ── OpenAI tools ───────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Check free 1-hour appointment slots on a given date between 9 AM and 6 PM Monday to Saturday (London time).",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date to check in YYYY-MM-DD format"}
                },
                "required": ["date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book a 1-hour property inspection on Google Calendar and send an email notification.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date":    {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "time":    {"type": "string", "description": "Start time in HH:MM 24-hour format"},
                    "name":    {"type": "string", "description": "Customer full name"},
                    "phone":   {"type": "string", "description": "Customer phone number"},
                    "email":   {"type": "string", "description": "Customer email address"},
                    "service": {"type": "string", "description": "Service requested e.g. damp survey, mould removal, roofing"},
                    "issue":   {"type": "string", "description": "Brief description of the property issue the customer is facing"}
                },
                "required": ["date", "time", "name", "phone", "email", "service", "issue"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_booking",
            "description": "Find a customer's upcoming booking by their email address.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Customer email address"}
                },
                "required": ["email"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_booking",
            "description": "Cancel an existing booking by its Google Calendar event ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "Google Calendar event ID from find_booking"}
                },
                "required": ["event_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_booking",
            "description": "Reschedule an existing booking to a new date and time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "Google Calendar event ID from find_booking"},
                    "new_date": {"type": "string", "description": "New date in YYYY-MM-DD format"},
                    "new_time": {"type": "string", "description": "New start time in HH:MM 24-hour format"}
                },
                "required": ["event_id", "new_date", "new_time"]
            }
        }
    }
]

# ── Global clients ─────────────────────────────────
openai_client: OpenAI       = None
chroma_collection           = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global openai_client, chroma_collection
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    chroma = chromadb.PersistentClient(path=CHROMA_PATH)
    chroma_collection = chroma.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    print(f"ChromaDB ready — {chroma_collection.count()} chunks")
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ── Google Calendar helpers ────────────────────────
def get_calendar_service():
    with open(GOOGLE_CREDS_FILE) as f:
        info = json.load(f)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)


def check_calendar_availability(date_str: str) -> dict:
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        if dt.weekday() == 6:  # Sunday
            return {"available": False, "message": "We don't work on Sundays. Please choose Monday–Saturday.", "slots": []}
        if dt.date() < datetime.date.today():
            return {"available": False, "message": "That date is in the past. Please choose a future date.", "slots": []}

        service = get_calendar_service()
        day_start = LONDON_TZ.localize(dt.replace(hour=WORK_START_H, minute=0, second=0, microsecond=0))
        day_end   = LONDON_TZ.localize(dt.replace(hour=WORK_END_H,   minute=0, second=0, microsecond=0))

        result = service.freebusy().query(body={
            "timeMin": day_start.isoformat(),
            "timeMax": day_end.isoformat(),
            "items":   [{"id": CALENDAR_ID}]
        }).execute()

        busy_periods = result.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", [])

        free_slots = []
        cursor = day_start
        while cursor < day_end:
            slot_end = cursor + datetime.timedelta(hours=SLOT_DURATION_H)
            is_busy = any(
                cursor < pytz.utc.localize(datetime.datetime.fromisoformat(b["end"].replace("Z", ""))).astimezone(LONDON_TZ) and
                slot_end > pytz.utc.localize(datetime.datetime.fromisoformat(b["start"].replace("Z", ""))).astimezone(LONDON_TZ)
                for b in busy_periods
            )
            if not is_busy:
                free_slots.append(cursor.strftime("%H:%M"))
            cursor = slot_end

        return {
            "date": date_str,
            "formatted_date": dt.strftime("%A, %d %B %Y"),
            "available": len(free_slots) > 0,
            "slots": free_slots,
            "message": "" if free_slots else "No free slots on this date. Please try another day."
        }
    except Exception as e:
        return {"available": False, "slots": [], "message": f"Calendar error: {e}"}


def create_calendar_booking(args: dict) -> dict:
    # ── Basic sanity checks only ──────────────────────
    email = args.get("email", "").strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        return {"success": False, "error": "invalid_email",
                "message": "The email address must contain @ and a domain (e.g. name@gmail.com)."}

    phone = args.get("phone", "").strip()
    digits_only = re.sub(r"\D", "", phone)
    if len(digits_only) < 6:
        return {"success": False, "error": "invalid_phone",
                "message": "Please provide a valid phone number."}

    try:
        dt    = datetime.datetime.strptime(f"{args['date']} {args['time']}", "%Y-%m-%d %H:%M")
        start = LONDON_TZ.localize(dt)
        end   = start + datetime.timedelta(hours=SLOT_DURATION_H)

        service = get_calendar_service()

        # Guard against double-booking: check the slot is still free right now
        busy_check = service.freebusy().query(body={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "items":   [{"id": CALENDAR_ID}]
        }).execute()
        busy_now = busy_check.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", [])
        if busy_now:
            return {"success": False, "error": "slot_taken",
                    "message": f"The {args['time']} slot on {args['date']} was just taken. Please check availability again and pick another slot."}

        event = service.events().insert(
            calendarId=CALENDAR_ID,
            body={
                "summary":     f"{args.get('service','Property Inspection')} – {args['name']}",
                "description": (
                    f"Customer: {args['name']}\n"
                    f"Phone: {args['phone']}\n"
                    f"Email: {args['email']}\n"
                    f"Service: {args.get('service','N/A')}\n"
                    f"Issue: {args.get('issue','N/A')}\n\n"
                    f"Booked via Environ chatbot."
                ),
                "start": {"dateTime": start.isoformat(), "timeZone": "Europe/London"},
                "end":   {"dateTime": end.isoformat(),   "timeZone": "Europe/London"},
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "email",  "minutes": 60},
                        {"method": "popup",  "minutes": 30}
                    ]
                }
            }
        ).execute()

        # Email notification is best-effort — never block the booking if it fails
        notes = []
        svc   = args.get("service", "Property Inspection")
        issue = args.get("issue", "N/A")

        try:
            _send_email_notification(args["name"], args["phone"], args["email"], start, svc, issue)
        except Exception as em_err:
            print(f"[EMAIL ERROR] {em_err}", flush=True)
            notes.append(f"Email failed: {em_err}")

        return {
            "success": True,
            "formatted_date": start.strftime("%A, %d %B %Y"),
            "formatted_time": start.strftime("%I:%M %p"),
            "name": args["name"],
            "note": "; ".join(notes)
        }
    except Exception as e:
        print(f"[BOOKING ERROR] {e}", flush=True)
        return {"success": False, "error": str(e), "message": f"Booking failed: {e}"}


def _send_email_notification(name: str, phone: str, email: str, dt: datetime.datetime,
                              service: str = "Property Inspection", issue: str = "N/A"):
    gmail_user     = os.getenv("GMAIL_SENDER")       # your gmail address used to send
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")  # 16-char app password (not your login password)
    if not gmail_user or not gmail_password:
        raise ValueError("GMAIL_SENDER or GMAIL_APP_PASSWORD not set in environment")

    subject = f"📅 New Booking – {name} | {dt.strftime('%d %b %Y %I:%M %p')}"

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;border:1px solid #e0e0e0;border-radius:8px;overflow:hidden;">
      <div style="background:#1a6b3c;padding:20px 24px;">
        <h2 style="color:#fff;margin:0">🏠 New Booking – Environ Property Services</h2>
      </div>
      <div style="padding:24px;background:#f9f9f9;">
        <table style="width:100%;border-collapse:collapse;">
          <tr><td style="padding:8px 0;color:#555;width:120px;">👤 Name</td><td style="padding:8px 0;font-weight:bold;">{name}</td></tr>
          <tr><td style="padding:8px 0;color:#555;">📱 Phone</td><td style="padding:8px 0;">{phone}</td></tr>
          <tr><td style="padding:8px 0;color:#555;">📧 Email</td><td style="padding:8px 0;">{email}</td></tr>
          <tr><td style="padding:8px 0;color:#555;">🔧 Service</td><td style="padding:8px 0;">{service}</td></tr>
          <tr><td style="padding:8px 0;color:#555;">⚠️ Issue</td><td style="padding:8px 0;">{issue}</td></tr>
          <tr><td style="padding:8px 0;color:#555;">📅 Date</td><td style="padding:8px 0;font-weight:bold;">{dt.strftime('%A, %d %B %Y')}</td></tr>
          <tr><td style="padding:8px 0;color:#555;">⏰ Time</td><td style="padding:8px 0;font-weight:bold;">{dt.strftime('%I:%M %p')}</td></tr>
        </table>
      </div>
      <div style="padding:12px 24px;background:#e8f5ee;font-size:13px;color:#555;">
        Booked via the Environ website chatbot.
      </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_user
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, NOTIFY_EMAIL, msg.as_string())


def find_customer_booking(email: str) -> dict:
    try:
        service  = get_calendar_service()
        _now    = datetime.datetime.now(datetime.timezone.utc)
        now     = _now.strftime("%Y-%m-%dT%H:%M:%SZ")
        future  = (_now + datetime.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result   = service.events().list(
            calendarId=CALENDAR_ID, timeMin=now, timeMax=future,
            q=email, singleEvents=True, orderBy="startTime"
        ).execute()
        events = result.get("items", [])
        if not events:
            return {"found": False, "message": f"No upcoming bookings found for {email}. Please double-check the email address."}

        event = events[0]
        start_raw = event["start"].get("dateTime", event["start"].get("date"))
        dt = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(LONDON_TZ)

        # Parse customer name from description
        name = ""
        for line in event.get("description", "").split("\n"):
            if line.startswith("Customer:"):
                name = line.replace("Customer:", "").strip()
                break
        if not name and " – " in event.get("summary", ""):
            name = event["summary"].split(" – ", 1)[1].strip()

        return {
            "found":          True,
            "event_id":       event["id"],
            "summary":        event.get("summary", "Property Inspection"),
            "customer_name":  name,
            "formatted_date": dt.strftime("%A, %d %B %Y"),
            "formatted_time": dt.strftime("%I:%M %p"),
            "date":           dt.strftime("%Y-%m-%d"),
            "time":           dt.strftime("%H:%M"),
        }
    except Exception as e:
        print(f"[FIND BOOKING ERROR] {e}", flush=True)
        return {"found": False, "message": f"Error searching bookings: {e}"}


def cancel_customer_booking(event_id: str) -> dict:
    try:
        service = get_calendar_service()
        event   = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
        start_raw = event["start"].get("dateTime", event["start"].get("date"))
        dt = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(LONDON_TZ)

        # Parse name
        name = ""
        for line in event.get("description", "").split("\n"):
            if line.startswith("Customer:"):
                name = line.replace("Customer:", "").strip()
                break

        service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()

        try:
            _send_cancel_email(name, dt, event.get("summary", "Property Inspection"))
        except Exception as em_err:
            print(f"[CANCEL EMAIL ERROR] {em_err}", flush=True)

        return {
            "success":  True,
            "message":  f"Booking on {dt.strftime('%A, %d %B %Y')} at {dt.strftime('%I:%M %p')} has been cancelled successfully."
        }
    except Exception as e:
        print(f"[CANCEL ERROR] {e}", flush=True)
        return {"success": False, "message": f"Cancellation failed: {e}"}


def reschedule_customer_booking(event_id: str, new_date: str, new_time: str) -> dict:
    try:
        service = get_calendar_service()
        event   = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()

        new_dt    = datetime.datetime.strptime(f"{new_date} {new_time}", "%Y-%m-%d %H:%M")
        new_start = LONDON_TZ.localize(new_dt)
        new_end   = new_start + datetime.timedelta(hours=SLOT_DURATION_H)

        # Guard: check new slot is free
        busy_check = service.freebusy().query(body={
            "timeMin": new_start.isoformat(),
            "timeMax": new_end.isoformat(),
            "items":   [{"id": CALENDAR_ID}]
        }).execute()
        if busy_check.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", []):
            return {"success": False, "message": f"The {new_time} slot on {new_date} is already taken. Please check availability and pick another slot."}

        # Get old datetime for email
        old_raw = event["start"].get("dateTime", event["start"].get("date"))
        old_dt  = datetime.datetime.fromisoformat(old_raw.replace("Z", "+00:00")).astimezone(LONDON_TZ)

        # Parse name
        name = ""
        for line in event.get("description", "").split("\n"):
            if line.startswith("Customer:"):
                name = line.replace("Customer:", "").strip()
                break

        # Delete old, create new
        service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
        service.events().insert(
            calendarId=CALENDAR_ID,
            body={
                "summary":     event.get("summary", "Property Inspection"),
                "description": event.get("description", "") + f"\n\nRescheduled from {old_dt.strftime('%A, %d %B %Y at %I:%M %p')}",
                "start": {"dateTime": new_start.isoformat(), "timeZone": "Europe/London"},
                "end":   {"dateTime": new_end.isoformat(),   "timeZone": "Europe/London"},
                "reminders": {
                    "useDefault": False,
                    "overrides": [{"method": "email", "minutes": 60}, {"method": "popup", "minutes": 30}]
                }
            }
        ).execute()

        try:
            _send_reschedule_email(name, old_dt, new_start)
        except Exception as em_err:
            print(f"[RESCHEDULE EMAIL ERROR] {em_err}", flush=True)

        return {
            "success":        True,
            "formatted_date": new_start.strftime("%A, %d %B %Y"),
            "formatted_time": new_start.strftime("%I:%M %p"),
            "name":           name
        }
    except Exception as e:
        print(f"[RESCHEDULE ERROR] {e}", flush=True)
        return {"success": False, "message": f"Reschedule failed: {e}"}


def _send_cancel_email(name: str, dt: datetime.datetime, summary: str = "Property Inspection"):
    gmail_user, gmail_pw = os.getenv("GMAIL_SENDER"), os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_pw:
        raise ValueError("GMAIL credentials not set")
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;border:1px solid #e0e0e0;border-radius:8px;overflow:hidden;">
      <div style="background:#dc2626;padding:20px 24px;"><h2 style="color:#fff;margin:0">❌ Booking Cancelled</h2></div>
      <div style="padding:24px;background:#f9f9f9;">
        <table style="width:100%;border-collapse:collapse;">
          <tr><td style="padding:8px 0;color:#555;width:120px;">👤 Name</td><td style="padding:8px 0;font-weight:bold;">{name or 'N/A'}</td></tr>
          <tr><td style="padding:8px 0;color:#555;">🔧 Service</td><td style="padding:8px 0;">{summary}</td></tr>
          <tr><td style="padding:8px 0;color:#555;">📅 Was booked</td><td style="padding:8px 0;">{dt.strftime('%A, %d %B %Y')}</td></tr>
          <tr><td style="padding:8px 0;color:#555;">⏰ Time</td><td style="padding:8px 0;">{dt.strftime('%I:%M %p')}</td></tr>
        </table>
      </div>
      <div style="padding:12px 24px;background:#fee2e2;font-size:13px;color:#555;">Cancelled via Environ website chatbot.</div>
    </div>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"❌ Booking Cancelled – {name or 'Customer'} | {dt.strftime('%d %b %Y')}"
    msg["From"] = gmail_user; msg["To"] = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(gmail_user, gmail_pw); s.sendmail(gmail_user, NOTIFY_EMAIL, msg.as_string())


def _send_reschedule_email(name: str, old_dt: datetime.datetime, new_dt: datetime.datetime):
    gmail_user, gmail_pw = os.getenv("GMAIL_SENDER"), os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_pw:
        raise ValueError("GMAIL credentials not set")
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;border:1px solid #e0e0e0;border-radius:8px;overflow:hidden;">
      <div style="background:#d97706;padding:20px 24px;"><h2 style="color:#fff;margin:0">🔄 Booking Rescheduled</h2></div>
      <div style="padding:24px;background:#f9f9f9;">
        <table style="width:100%;border-collapse:collapse;">
          <tr><td style="padding:8px 0;color:#555;width:140px;">👤 Name</td><td style="padding:8px 0;font-weight:bold;">{name or 'N/A'}</td></tr>
          <tr><td style="padding:8px 0;color:#555;">❌ Was</td><td style="padding:8px 0;text-decoration:line-through;color:#9ca3af;">{old_dt.strftime('%A, %d %B %Y at %I:%M %p')}</td></tr>
          <tr><td style="padding:8px 0;color:#555;">✅ Now</td><td style="padding:8px 0;font-weight:bold;color:#15803d;">{new_dt.strftime('%A, %d %B %Y at %I:%M %p')}</td></tr>
        </table>
      </div>
      <div style="padding:12px 24px;background:#fef3c7;font-size:13px;color:#555;">Rescheduled via Environ website chatbot.</div>
    </div>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔄 Booking Rescheduled – {name or 'Customer'} | {new_dt.strftime('%d %b %Y')}"
    msg["From"] = gmail_user; msg["To"] = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(gmail_user, gmail_pw); s.sendmail(gmail_user, NOTIFY_EMAIL, msg.as_string())


def execute_tool(name: str, args: dict) -> dict:
    try:
        if name == "check_availability":
            return check_calendar_availability(args["date"])
        if name == "book_appointment":
            return create_calendar_booking(args)
        if name == "find_booking":
            return find_customer_booking(args["email"])
        if name == "cancel_booking":
            return cancel_customer_booking(args["event_id"])
        if name == "reschedule_booking":
            return reschedule_customer_booking(args["event_id"], args["new_date"], args["new_time"])
        return {"error": "Unknown tool"}
    except Exception as e:
        return {"error": str(e), "available": False, "slots": []}


# ── RAG ────────────────────────────────────────────
def embed_query(text: str) -> list:
    return openai_client.embeddings.create(
        model="text-embedding-3-small", input=[text]
    ).data[0].embedding


def retrieve_context(query: str, n: int = 3) -> str:
    if not chroma_collection or chroma_collection.count() == 0:
        return ""
    docs = chroma_collection.query(
        query_embeddings=[embed_query(query)], n_results=n
    ).get("documents", [[]])[0]
    return "\n\n---\n\n".join(docs)


# ── Request model ──────────────────────────────────
class HistoryMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    image_base64: Optional[str] = None
    image_mime_type: Optional[str] = "image/jpeg"
    history: list[HistoryMessage] = []
    session_id: Optional[str] = None   # for future logging/tracking


CONFIRM_WORDS = {"yes","sure","ok","okay","yeah","correct","go ahead","confirm",
                 "confirmed","please","do it","book it","yep","yup","done"}

# ── Guardrails ─────────────────────────────────────

INPUT_MAX_CHARS = 600   # hard cap on user message length

# Days that cannot exist in certain months
_MONTH_MAX_DAYS = {
    "feb": 29, "february": 29,
    "apr": 30, "april": 30,
    "jun": 30, "june": 30,
    "sep": 30, "september": 30,
    "nov": 30, "november": 30,
}

def validate_input(message: str) -> dict:
    """
    Run all input guardrails. Returns {"ok": True} or {"ok": False, "reply": "..."}
    """
    s = message.strip()

    # 1. Empty
    if not s:
        return {"ok": False, "reply": "Please type a message and I'll be happy to help! 😊"}

    # 2. Max length
    if len(s) > INPUT_MAX_CHARS:
        return {"ok": False, "reply": (
            f"Your message is too long ({len(s)} characters — max {INPUT_MAX_CHARS}). "
            "Could you shorten it, or split it across a couple of messages?"
        )}

    # 3. Non-English script (Arabic, Chinese, Cyrillic, Hindi, etc.)
    #    Count chars above Unicode 591 that are letters — high ratio = non-Latin script
    non_latin = sum(1 for c in s if c.isalpha() and ord(c) > 591)
    if len(s) > 8 and non_latin / max(len(s), 1) > 0.25:
        return {"ok": False, "reply": (
            "I can only assist in English. Please write your message in English "
            "and I'll be glad to help! 😊"
        )}

    # 4. Spam / gibberish — same character repeated 10+ times
    if re.search(r'(.)\1{9,}', s):
        return {"ok": False, "reply": (
            "That doesn't look like a valid message. "
            "Please describe your property issue and I'll help you out!"
        )}

    # 5. Invalid date: day number that cannot exist
    s_lower = s.lower()
    # Day > 31 for any month
    m = re.search(
        r'\b(3[2-9]|[4-9]\d|\d{3,})\s*(?:st|nd|rd|th)?\s+'
        r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
        r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b',
        s_lower
    )
    if m:
        return {"ok": False, "reply": (
            f"Hmm, that date doesn't seem right — there's no **{m.group(0)}**. "
            "Could you double-check and let me know the correct date?"
        )}
    # Day 0
    if re.search(
        r'\b0+\s*(?:st|nd|rd|th)?\s+'
        r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b', s_lower
    ):
        return {"ok": False, "reply": (
            "Day 0 doesn't exist! Could you double-check the date you meant?"
        )}
    # Month-specific overflows (e.g. Feb 30, April 31)
    m2 = re.search(
        r'\b(3[01]|29|30)\s*(?:st|nd|rd|th)?\s+'
        r'(feb(?:ruary)?|apr(?:il)?|jun(?:e)?|sep(?:tember)?|nov(?:ember)?)\b',
        s_lower
    )
    if m2:
        day, mon = int(m2.group(1)), m2.group(2)[:3]
        if day > _MONTH_MAX_DAYS.get(mon, 31):
            return {"ok": False, "reply": (
                f"That date isn't valid — **{m2.group(0)}** doesn't exist. "
                "Please pick a real date and I'll check availability for you!"
            )}
    # Month name given as number > 12
    m3 = re.search(r'\b(1[3-9]|[2-9]\d)\s*/\s*\d{4}\b', s_lower)
    if m3:
        return {"ok": False, "reply": (
            f"Month {m3.group(1)} doesn't exist — there are only 12 months. "
            "Could you double-check the date?"
        )}

    # 6. Prompt injection attempts
    _INJECT = [
        r'ignore\s+(your|all|previous)\s+(instructions?|rules?|prompt)',
        r'you\s+are\s+now\b',
        r'forget\s+(everything|all|your)',
        r'(new|override)\s+instructions?\s*:',
        r'pretend\s+(you\s+are|to\s+be)',
        r'\bjailbreak\b',
        r'act\s+as\s+(?:if\s+)?(?:you\s+(?:are|were)|a\s+)',
        r'disregard\s+(all|your|previous)',
    ]
    for pat in _INJECT:
        if re.search(pat, s_lower):
            return {"ok": False, "reply": (
                "I'm here to help with property and home-related questions only! "
                "What can I help you with today? 😊"
            )}

    # 7. Format-forcing / data-format demands (not relevant to a property chatbot)
    _FORMAT_PATTERNS = [
        r'\b(?:as|in|using)\s+(?:json|xml|csv|html|yaml|code|markdown|latex)\b',
        r'\bformat(?:ted)?\s+(?:as|in)\b',
        r'\bwrite\s+(?:a\s+)?(?:script|code|program|function)\b',
        r'\bshow\s+(?:me\s+)?(?:the\s+)?(?:source|html|css|code)\b',
        r'\bexport\s+(?:as|to)\b',
        r'\bprint\s+(?:in|as)\s+(?:json|xml|csv)\b',
    ]
    if any(re.search(p, s_lower) for p in _FORMAT_PATTERNS):
        return {"ok": False, "reply": (
            "I'm a property support assistant — I can't output data in technical formats like JSON, XML or code. "
            "I'm happy to explain services, causes, treatments, or help you book an inspection. What would you like to know? 😊"
        )}

    # 8. Profanity / abusive language
    _PROFANITY = [
        r'\bf[\*u][c\*]k', r'\bs[h\*][i\*]t\b', r'\bb[i\*]tch\b',
        r'\bc[u\*]nt\b',   r'\bwanker\b',        r'\btwat\b',
        r'\bdickhead\b',   r'\bprick\b',          r'\barshole\b',
        r'\bcocksucker',   r'\bmotherfuck',       r'\bfucking\b',
        r'\bshitting\b',   r'\bbullshit\b',
    ]
    if any(re.search(p, s_lower) for p in _PROFANITY):
        return {"ok": False, "reply": (
            "Please keep the conversation respectful and I'll be happy to help "
            "with your property needs! 😊"
        )}

    # 8. URL / link injection (prevent phishing links in chat)
    if re.search(r'https?://', s_lower):
        return {"ok": False, "reply": (
            "Please don't include web links in your message. Just describe your "
            "property issue in your own words and I'll help you out!"
        )}

    # 9. Excessive symbols / special-character spam
    #    If more than 55% of characters are non-alphanumeric (excl. spaces & common punctuation)
    symbols = sum(1 for c in s if not c.isalnum() and c not in " .,!?'-@:/()")
    if len(s) > 10 and symbols / max(len(s), 1) > 0.55:
        return {"ok": False, "reply": (
            "That message contains too many special characters. "
            "Please describe your property issue in plain English and I'll help you out!"
        )}

    return {"ok": True}


def validate_output(text: str) -> str:
    """
    Run output guardrails on the fully assembled bot response.
    Returns the original text, a sanitised version, or a safe fallback.
    """
    if not text:
        return text

    # 0. Strip literal HTML tags that GPT-4o sometimes outputs (e.g. <br> inside table cells)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(
        r'</?(?:b|i|u|em|strong|span|div|p|table|tr|td|th|thead|tbody|hr)\b[^>]*>',
        '', text, flags=re.IGNORECASE
    )

    # 1. Non-English output — GPT-4o should never do this, but safety net
    non_latin = sum(1 for c in text if c.isalpha() and ord(c) > 591)
    if len(text) > 30 and non_latin / max(len(text), 1) > 0.25:
        return ("I'm sorry, something went wrong with my response. "
                "Could you rephrase your question and I'll try again?")

    # 2. Internal Python error strings leaking into the response
    _LEAK = ['traceback (most recent', 'file "/', 'line ', 'syntaxerror',
             'valueerror', 'keyerror', 'attributeerror']
    tl = text.lower()
    if any(p in tl for p in _LEAK[:2]):   # only the most obvious leaks
        return ("I ran into a small technical issue. "
                "Please try again or contact us directly — we're happy to help!")

    # 3. Excessive length safety valve — trim gracefully at sentence boundary
    MAX_OUTPUT_CHARS = 1200
    if len(text) > MAX_OUTPUT_CHARS:
        cut = text[:MAX_OUTPUT_CHARS]
        last_stop = max(cut.rfind('. '), cut.rfind('.\n'), cut.rfind('! '), cut.rfind('? '))
        if last_stop > MAX_OUTPUT_CHARS * 0.6:
            text = cut[:last_stop + 1] + "\n\n*…Feel free to ask if you'd like more detail!*"
        else:
            text = cut.rstrip() + "…"

    return text

def parse_time_to_hhmm(text: str):
    """
    Convert a time expression to HH:MM 24-hour string, or return None.
    Handles: "14:00", "2pm", "2 pm", "2:30pm", "9am", "9 am", "9:00am"
    """
    t = text.strip().lower()
    # Already exact HH:MM
    if re.match(r"^\d{1,2}:\d{2}$", t):
        h, m = t.split(":")
        return f"{int(h):02d}:{m}"
    # 12-hour with am/pm, optional minutes: "2pm", "2 pm", "2:30pm", "14pm" etc.
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if m:
        h    = int(m.group(1))
        mins = m.group(2) or "00"
        ampm = m.group(3)
        if ampm == "pm" and h != 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        if 0 <= h <= 23:
            return f"{h:02d}:{mins}"
    return None

def extract_booking_state(history: list) -> str:
    """
    AI-powered booking state extractor.
    Uses GPT-4o-mini to read the conversation and return a clean JSON state,
    then formats it into a structured block injected into the main prompt.
    Replaces the old 500-line regex machine — robust to any phrasing.
    """
    if not history or not openai_client:
        return ""

    today = datetime.date.today().strftime("%A, %d %B %Y")

    # Build compact transcript (last 30 messages)
    transcript_lines = []
    for m in history[-30:]:
        role_label = "Customer" if m.role == "user" else "Agent"
        text = m.content.strip()[:400]
        transcript_lines.append(f"{role_label}: {text}")
    transcript = "\n".join(transcript_lines)

    extraction_prompt = f"""You are a booking state extractor for a property inspection chatbot.
Read the conversation below and extract ONLY what has been explicitly provided or clearly confirmed.
Be conservative — when uncertain, use null. Today is {today}.

Return ONLY a valid JSON object with these fields:
{{
  "booking_intent": true or false,
  "service": "service name or null",
  "issues": ["list of distinct property issues mentioned — deduplicated, no meta-comments like 'its an issue' or 'same thing'"],
  "issues_complete": true or false,
  "pending_more_issue": true or false,
  "date": "date as the customer said it, or null",
  "time": "HH:MM 24h format or null",
  "name": "full name or null",
  "phone": "phone number or null",
  "email": "email address or null"
}}

Field rules:
- booking_intent: true if customer expressed intent to book/schedule/arrange an inspection
- service: extract from customer message OR from agent recommendation the customer accepted
- issues: list every distinct property issue the customer mentioned. Deduplicate. Ignore frustrated meta-replies like "its an issue", "i already said", "same thing", "that's the issue"
- issues_complete: true ONLY if customer explicitly said no more issues ("no", "nope", "that's all", "nothing else", "done", "just that one") OR if date/time are already chosen (past that stage)
- pending_more_issue: true if the LAST exchange was agent asking "any other issues?" and customer replied "yes/yeah/sure" WITHOUT describing a new issue
- date: only if customer gave a specific date (e.g. "Monday 19 May 2026") or clicked a date button
- time: only if customer selected a specific time slot or typed a time — convert to HH:MM 24h
- name/phone/email: only if customer explicitly provided these

Conversation:
{transcript}"""

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": extraction_prompt}],
            response_format={"type": "json_object"},
            max_tokens=350,
            temperature=0,
        )
        state = json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[STATE EXTRACTOR ERROR] {e}", flush=True)
        return ""

    # ── Nothing collected yet → no state block needed ──
    if not state.get("booking_intent") and not any([
        state.get("service"), state.get("issues"), state.get("name"),
        state.get("date"), state.get("time"), state.get("email"),
    ]):
        return ""

    issues_list = state.get("issues") or []
    issues_text = "\n".join(f"    {i+1}. {iss}" for i, iss in enumerate(issues_list))

    # ── Build structured state block ──────────────────
    lines = ["━━━ BOOKING STATE (injected by system — highest priority) ━━━"]

    if state.get("service"):
        lines.append(f"  ✅ 🔧 Service: {state['service']}")
    if issues_list:
        lines.append(f"  ✅ ⚠️  Issues:\n{issues_text}")
    if state.get("issues_complete"):
        lines.append("  ✅ ✔️  Issues finalised: Yes — proceed to date")
    if state.get("date"):
        lines.append(f"  ✅ 📅 Date: {state['date']}")
    if state.get("time"):
        lines.append(f"  ✅ ⏰ Time: {state['time']}")
    if state.get("name"):
        lines.append(f"  ✅ 👤 Name: {state['name']}")
    if state.get("phone"):
        lines.append(f"  ✅ 📱 Phone: {state['phone']}")
    if state.get("email"):
        lines.append(f"  ✅ 📧 Email: {state['email']}")

    # ── Determine NEXT STEP ────────────────────────────
    _booking_active = state.get("booking_intent") or len([
        f for f in ["service", "date", "time", "name", "phone", "email"]
        if state.get(f)
    ]) >= 1 or bool(issues_list)

    if _booking_active:
        if not state.get("service"):
            next_step = 'Ask: "What service do you need?"'
        elif not issues_list:
            next_step = ('Ask: "Could you describe the issue(s) you are facing?" '
                         '— accept the VERY NEXT reply as-is.')
        elif not state.get("issues_complete"):
            if state.get("pending_more_issue"):
                next_step = ('User said yes to having more issues. Ask: '
                             '"Please describe that issue briefly." '
                             '— accept the VERY NEXT reply as the issue description.')
            else:
                noted = issues_text.strip()
                next_step = (
                    f'Issues noted:\n{noted}\n'
                    'Confirm to the user which issue(s) you have noted (so they know you heard them), '
                    'then ask: "Are there any other issues I should include? If not, just say no 😊" '
                    '— if user says no/done/that\'s all → immediately move to date step.'
                )
        elif not state.get("date") and not state.get("time"):
            next_step = ('All issues collected ✅. Ask: "Which day works for you?" '
                         '— do NOT call check_availability yet, wait for the user to give a date first.')
        elif not state.get("time"):
            date_ref = state.get("date", "the chosen date")
            next_step = (
                f'MUST call check_availability for "{date_ref}" (convert to YYYY-MM-DD). '
                'This is mandatory — it sends the slot buttons to the frontend. '
                'After the tool returns, say ONLY: '
                '"We have availability on [formatted_date]! Please pick a time 👇" '
                '— do NOT list times as text, do NOT skip the tool call.'
            )
        elif not state.get("name"):
            next_step = 'Ask: "Could you provide your full name?"'
        elif not state.get("phone"):
            next_step = 'Ask: "Could you provide your phone number?"'
        elif not state.get("email"):
            next_step = 'Ask: "Could you provide your email address?"'
        else:
            next_step = ('Show full confirmation summary — list ALL issues as a numbered list — '
                         'and ask "Shall I confirm this booking?"')
        lines.append(f"\n  ⏭  NEXT STEP: {next_step}")
        lines.append("  🚫 DO NOT re-ask for any ✅ field above — they are final.")
    else:
        lines.append("\n  ℹ️  User is in support/info mode — answer their question freely.")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def build_messages(req: ChatRequest) -> tuple[list, str]:
    today  = datetime.date.today().strftime("%A, %d %B %Y")
    system = SYSTEM_PROMPT_TEMPLATE.format(today=today)
    context = retrieve_context(req.message)

    messages = [{"role": "system", "content": system}]
    for m in req.history[-20:]:
        messages.append({"role": m.role, "content": m.content})

    # Inject booking state as a SYSTEM message immediately before the user's
    # current message — this position gets maximum attention from the model.
    # Include current user message in extraction so the state already reflects
    # the answer the user just gave (e.g. name, phone) and skips to next step.
    temp_history = list(req.history) + [HistoryMessage(role="user", content=req.message)]
    session_state = extract_booking_state(temp_history)
    if session_state:
        messages.append({"role": "system", "content": session_state})

    user_text = (
        f"Knowledge base context:\n{context}\n\n---\n\nUser: {req.message}"
        if context else req.message
    )

    if req.image_base64:
        user_content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {
                "url": f"data:{req.image_mime_type};base64,{req.image_base64}",
                "detail": "high"
            }}
        ]
    else:
        user_content = user_text

    messages.append({"role": "user", "content": user_content})
    return messages, "gpt-4o"   # gpt-4o for all — much better context tracking


# ── Chat endpoint ──────────────────────────────────
@app.post("/api/chat")
async def chat(req: ChatRequest):
    # ── Input guardrail ────────────────────────────
    guard = validate_input(req.message)
    if not guard["ok"]:
        reply = guard["reply"]
        def _blocked():
            words = reply.split(" ")
            for i, w in enumerate(words):
                yield f"data: {json.dumps({'token': w + (' ' if i < len(words)-1 else '')})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_blocked(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    messages, model = build_messages(req)

    def generate():
        # ── Detect booking stage from injected BOOKING STATE block ──────────
        # If date is collected but time slot is not, FORCE check_availability
        # so slot buttons always appear — never rely on GPT-4o choosing the tool.
        _state_text = " ".join(
            m["content"] for m in messages
            if isinstance(m.get("content"), str) and "BOOKING STATE" in m.get("content", "")
        )
        _has_date   = "📅 Date:" in _state_text
        _has_time   = "⏰ Time:" in _state_text
        _forced_tool_choice = (
            {"type": "function", "function": {"name": "check_availability"}}
            if _has_date and not _has_time
            else "auto"
        )

        # Step 1: non-streaming call (supports tool detection)
        response = openai_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice=_forced_tool_choice,
            max_tokens=500,
            temperature=0.4,
        )
        choice = response.choices[0]

        if choice.finish_reason == "tool_calls":
            # Step 2: execute every tool call
            msg = choice.message
            tool_results = []
            ui_events    = []   # deferred UI events sent after text stream

            for tc in msg.tool_calls:
                args   = json.loads(tc.function.arguments)
                result = execute_tool(tc.function.name, args)

                # Queue a slot-picker UI event for the frontend
                if tc.function.name == "check_availability" and result.get("slots"):
                    ui_events.append({
                        "ui":             "slots",
                        "date":           result["date"],
                        "formatted_date": result.get("formatted_date", result["date"]),
                        "slots":          result["slots"]
                    })

                # If slot just taken — auto re-check availability so buttons appear
                if tc.function.name == "book_appointment" and result.get("error") == "slot_taken":
                    avail = check_calendar_availability(args["date"])
                    if avail.get("slots"):
                        ui_events.append({
                            "ui":             "slots",
                            "date":           avail["date"],
                            "formatted_date": avail.get("formatted_date", avail["date"]),
                            "slots":          avail["slots"]
                        })
                        result["new_availability"] = avail  # give AI the fresh slots in its context

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result)
                })

            # Build follow-up messages including assistant + tool results
            follow_up = messages + [
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                        }
                        for tc in msg.tool_calls
                    ]
                }
            ] + tool_results

            # Step 3: buffer streaming response then apply output guardrail
            stream = openai_client.chat.completions.create(
                model=model,
                messages=follow_up,
                max_tokens=600,
                temperature=0.4,
                stream=True,
            )
            raw_text = ""
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    raw_text += delta
            safe_text = validate_output(raw_text)
            words = safe_text.split(" ")
            for i, w in enumerate(words):
                yield f"data: {json.dumps({'token': w + (' ' if i < len(words)-1 else '')})}\n\n"

            # Step 4: after text, emit UI events so buttons appear below the bubble
            for evt in ui_events:
                yield f"data: {json.dumps(evt)}\n\n"

            # Emit datepicker event if bot is asking for a date (and no slot UI already queued)
            _DATE_ASK_TRIGGERS = [
                'which day', 'what day', 'when would', 'day works', 'day suits',
                'preferred date', 'preferred day', 'works for you', 'what date',
                'which date', 'choose a date', 'pick a date', 'when suits',
                'what day works', 'what day suits', 'pick a day',
            ]
            _has_slot_ui = any(e.get("ui") == "slots" for e in ui_events)
            if not _has_slot_ui and any(t in safe_text.lower() for t in _DATE_ASK_TRIGGERS):
                yield f"data: {json.dumps({'ui': 'datepicker'})}\n\n"

        else:
            # No tool call — apply output guardrail then stream word-by-word
            content = validate_output(choice.message.content or "")
            words = content.split(" ")
            for i, word in enumerate(words):
                token = word + (" " if i < len(words) - 1 else "")
                yield f"data: {json.dumps({'token': token})}\n\n"

            # Emit datepicker event if bot is asking for a date
            _DATE_ASK_TRIGGERS = [
                'which day', 'what day', 'when would', 'day works', 'day suits',
                'preferred date', 'preferred day', 'works for you', 'what date',
                'which date', 'choose a date', 'pick a date', 'when suits',
                'what day works', 'what day suits', 'pick a day',
            ]
            if any(t in content.lower() for t in _DATE_ASK_TRIGGERS):
                yield f"data: {json.dumps({'ui': 'datepicker'})}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    )


@app.get("/api/status")
def status():
    return {"status": "ok", "indexed_chunks": chroma_collection.count() if chroma_collection else 0}

@app.get("/")
def index():
    return FileResponse("frontend/index.html")

app.mount("/static", StaticFiles(directory="frontend"), name="static")
