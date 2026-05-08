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
SYSTEM_PROMPT_TEMPLATE = """You are Alex, a friendly property specialist at Environ Property Services, London.

STYLE: Concise. 2-3 sentences max. Bullet points for lists. No long paragraphs.

━━━ BOOKING FLOW (follow exactly, one step at a time) ━━━
STEP 1 — Service: Ask "What service do you need?" (damp survey, mould removal, rot treatment, repointing, roofing, drainage, pest control, etc.)
STEP 2 — Issue: Ask "Could you briefly describe the issue?" → Accept the VERY NEXT reply as-is, NO MATTER WHAT IT IS. One word like "mould" is fine. If they say "no", "skip", "not sure", "I'll tell you later", "rather not say" or anything negative/evasive — say "No problem, we can discuss it on the day!" and set the issue as "Will discuss on site". NEVER ask twice.
STEP 3 — Date: Say "Which day works for you?" then call check_availability.
STEP 4 — Time: Say ONLY "We have availability on [formatted_date]! Please pick a time 👇" — time buttons appear automatically, do NOT list times as text.
STEP 5 — Name: Ask "Could you provide your full name?" → Accept anything. Move on.
STEP 6 — Phone: Ask "Could you provide your phone number?" → Accept any digits. Move on.
STEP 7 — Email: Ask "Could you provide your email address?" → Accept anything with @. Move on.
STEP 8 — Confirm: Show summary and ask "Shall I confirm this booking?"
STEP 9 — Book: Call book_appointment only AFTER user confirms. Then say "✅ You're booked! See you on [date] at [time], [name]."

━━━ ABSOLUTE RULES ━━━
- The BOOKING STATE block injected above this message shows what is already collected. NEVER re-ask for a ✅ field. Jump straight to the stated NEXT STEP.
- Each step asks ONE question and waits. After the user replies, move to the next step — NEVER stay on the same step.
- If user is chatting about a property issue and shows interest in booking, guide them into the flow naturally.
- If already mid-flow (service + issue known), do NOT give general property advice — stay on the flow.
- Typed time in HH:MM (e.g. "13:00") = valid slot selection. Proceed with it.
- NEVER validate email — any string with @ is valid. NEVER validate phone — any digits are valid.
- Confirmation words (yes/sure/ok/yeah/correct/go ahead/confirm/please/do it/yep) = proceed.
- On slot_taken: call check_availability for same date, show new buttons, then book with SAME details.
- book_appointment needs: date, time, name, phone, email, service, issue — all 7 fields.

━━━ CANCEL / RESCHEDULE ━━━
Cancel: ask email → find_booking → confirm with user → cancel_booking.
Reschedule: ask email → find_booking → ask new day → check_availability → confirm → reschedule_booking.

━━━ DATE RULES ━━━
- No Sundays. No past dates. Today is {today}.
- Copy exact "formatted_date" from check_availability result — never recalculate.

SERVICES: Damp (rising/penetrating/lateral/condensation), mould removal, dry/wet rot, repointing, brick cleaning, heritage restoration, roofing, drainage, sash windows, pest control.
COMPANY: Environ Property Services — family-run, London-based, 15+ years, PCA-accredited, TrustMark registered.
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
        now      = datetime.datetime.utcnow().isoformat() + "Z"
        future   = (datetime.datetime.utcnow() + datetime.timedelta(days=90)).isoformat() + "Z"
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
    Scan conversation history, extract collected booking fields, determine
    the next required step, and return a structured state block to inject
    as a high-priority system message immediately before the user's message.
    """
    collected = {}
    msgs = [(m.role, m.content.strip()) for m in history]

    for i in range(len(msgs) - 1):
        role, content = msgs[i]
        if role != "assistant":
            continue
        next_role, next_val = msgs[i + 1]
        if next_role != "user" or not next_val or len(next_val) > 300:
            continue

        lc = content.lower()

        # Service
        if ("which service" in lc or "what service" in lc or "what service do you need" in lc):
            if len(next_val.split()) <= 8 and next_val.lower().strip() not in CONFIRM_WORDS:
                collected["service"] = next_val

        # Issue — accept ANY answer after asking for description.
        # If user skips / refuses, store a placeholder so we never ask again.
        SKIP_WORDS = {"no","nope","skip","pass","later","idk","don't know","not sure",
                      "n/a","na","nothing","private","rather not","prefer not","no thanks",
                      "i'll tell you","tell you later","tell you when","explain later"}
        if ("describe the issue" in lc or "briefly describe" in lc or
                "what issue" in lc or "what's the issue" in lc or "facing with" in lc):
            if next_val.lower().strip() not in CONFIRM_WORDS:
                nv_lower = next_val.lower().strip()
                # Negative / skip response → use placeholder
                if any(nv_lower == s or nv_lower.startswith(s) for s in SKIP_WORDS):
                    collected["issue"] = "Will discuss on site"
                else:
                    collected["issue"] = next_val

        # Name — 1-4 words, no digits, no @
        if ("your name" in lc or "full name" in lc or "provide your name" in lc) and "last name" not in lc:
            words = next_val.split()
            if ("@" not in next_val and 1 <= len(words) <= 4
                    and not any(c.isdigit() for c in next_val)):
                collected["name"] = next_val

        # Last name — append
        if "last name" in lc:
            if "@" not in next_val and not any(c.isdigit() for c in next_val):
                first = collected.get("name", "")
                if first and next_val.lower() not in first.lower():
                    collected["name"] = f"{first} {next_val}".strip()
                elif not first:
                    collected["name"] = next_val

        # Phone — ≥7 digits, digits ≥50% of string
        if ("phone" in lc or "mobile" in lc or "contact number" in lc) and "email" not in lc:
            digits_only = re.sub(r"\D", "", next_val)
            if (len(digits_only) >= 7
                    and len(digits_only) / max(len(next_val), 1) >= 0.5
                    and len(next_val) <= 20):
                collected["phone"] = next_val

        # Email
        if "email" in lc and "@" in next_val:
            collected["email"] = next_val

        # Date chosen
        if ("which day" in lc or "what day" in lc or "what date" in lc or "when would" in lc):
            if len(next_val) <= 50 and next_val.lower().strip() not in CONFIRM_WORDS:
                collected["date_chosen"] = next_val

        # Time slot — exact HH:MM or natural language (2pm, 9am, etc.)
        # Only capture when bot's message was about picking a time
        if ("pick a time" in lc or "please pick" in lc or "which time" in lc
                or re.match(r"^\d{1,2}:\d{2}$", next_val.strip())):
            parsed = parse_time_to_hhmm(next_val)
            if parsed:
                collected["time_slot"] = parsed

    # ── FALLBACK: extract service from any user message ──
    # Handles the case where user mentions service upfront (e.g. "I want to book a damp survey")
    # without the bot ever asking "What service do you need?"
    if "service" not in collected:
        SERVICE_KEYWORDS = [
            "damp survey", "damp proofing", "damp inspection", "damp treatment", "damp check",
            "mould removal", "mould treatment", "mould survey", "mold removal", "mold treatment",
            "dry rot", "wet rot", "rot treatment", "rot survey",
            "repointing", "brick cleaning", "pointing",
            "roofing", "roof repair", "roof inspection", "roof survey",
            "drainage", "drain survey", "drain inspection",
            "sash window", "window repair", "window survey",
            "pest control", "pest inspection",
            "waterproofing", "basement conversion",
            "condensation survey", "rising damp", "penetrating damp", "lateral damp",
            "property inspection", "property survey", "free inspection", "free survey",
        ]
        for msg_role, msg_content in msgs:
            if msg_role != "user":
                continue
            content_lower = msg_content.lower()
            for kw in SERVICE_KEYWORDS:
                if kw in content_lower:
                    collected["service"] = kw.title()
                    break
            if "service" in collected:
                break

    # ── FALLBACK: extract time slot from user message after a time-picker prompt ──
    # Handles "11:00" button clicks AND natural language like "2pm", "9 am"
    if "time_slot" not in collected:
        for i in range(len(msgs) - 1):
            role, content = msgs[i]
            if role != "assistant":
                continue
            lc2 = content.lower()
            if "pick a time" in lc2 or "please pick" in lc2 or "availability" in lc2:
                next_role2, next_val2 = msgs[i + 1]
                if next_role2 == "user":
                    parsed = parse_time_to_hhmm(next_val2)
                    if parsed:
                        collected["time_slot"] = parsed
                        break

    # ── FALLBACK: extract date from any user message ──
    # Handles date button clicks like "Monday, 11 May 2026"
    if "date_chosen" not in collected:
        DATE_PATTERN = re.compile(
            r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday)"
            r"[,\s]+\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\S*\s+\d{4}\b",
            re.IGNORECASE
        )
        for msg_role, msg_content in msgs:
            if msg_role == "user" and DATE_PATTERN.search(msg_content):
                collected["date_chosen"] = msg_content.strip()
                break

    if not collected:
        return ""

    # ── Build structured state block ──────────────────
    label_map = {
        "service":      "🔧 Service",
        "issue":        "⚠️  Issue",
        "date_chosen":  "📅 Date",
        "time_slot":    "⏰ Time",
        "name":         "👤 Name",
        "phone":        "📱 Phone",
        "email":        "📧 Email",
    }
    ORDERED = ["service", "issue", "date_chosen", "time_slot", "name", "phone", "email"]

    lines = ["━━━ BOOKING STATE (injected by system — highest priority) ━━━"]
    for key in ORDERED:
        if key in collected:
            lines.append(f"  ✅ {label_map[key]}: {collected[key]}")

    # Determine exactly what to do next
    if "service" not in collected:
        next_step = 'Ask: "What service do you need?"'
    elif "issue" not in collected:
        next_step = ('Ask: "Could you briefly describe the issue?" — '
                     'then accept the VERY NEXT reply as-is, even one word. Move on immediately.')
    elif "date_chosen" not in collected and "time_slot" not in collected:
        next_step = 'Ask which day works and call check_availability. Do NOT ask for issue again.'
    elif "time_slot" not in collected:
        next_step = 'Wait for user to pick a time slot from the buttons.'
    elif "name" not in collected:
        next_step = 'Ask: "Could you provide your full name?"'
    elif "phone" not in collected:
        next_step = 'Ask: "Could you provide your phone number?"'
    elif "email" not in collected:
        next_step = 'Ask: "Could you provide your email address?"'
    else:
        next_step = 'Show full confirmation summary and ask "Shall I confirm this booking?"'

    lines.append(f"\n  ⏭  NEXT STEP: {next_step}")
    lines.append("  🚫 DO NOT re-ask for any ✅ field above — they are final.")
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
    messages, model = build_messages(req)

    def generate():
        # Step 1: non-streaming call (supports tool detection)
        response = openai_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=350,
            temperature=0.5,
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

            # Step 3: stream text response first
            stream = openai_client.chat.completions.create(
                model=model,
                messages=follow_up,
                max_tokens=600,
                temperature=0.5,
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield f"data: {json.dumps({'token': delta})}\n\n"

            # Step 4: after text, emit UI events so buttons appear below the bubble
            for evt in ui_events:
                yield f"data: {json.dumps(evt)}\n\n"

        else:
            # No tool call — stream the response we already have word-by-word
            content = choice.message.content or ""
            words = content.split(" ")
            for i, word in enumerate(words):
                token = word + (" " if i < len(words) - 1 else "")
                yield f"data: {json.dumps({'token': token})}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.get("/api/status")
def status():
    return {"status": "ok", "indexed_chunks": chroma_collection.count() if chroma_collection else 0}

@app.get("/")
def index():
    return FileResponse("frontend/index.html")

app.mount("/static", StaticFiles(directory="frontend"), name="static")
