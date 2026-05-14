"""
Environ Property Services — Agentic Chatbot
Booking-focused with Google Calendar + Gmail notifications
"""
import os
import json
import re
import datetime
import pytz
import socket as _socket
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
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
WORK_START_H       = 9    # 9 AM
WORK_END_H         = 18   # 6 PM
SLOT_DURATION_H    = 1
GOOGLE_CREDS_FILE  = "google_credentials.json"
NOTIFY_EMAIL       = "aiagentsautomation87@gmail.com"
_GCAL_TIMEOUT      = 10   # seconds — hard socket timeout for all Google API calls

# Date-asking trigger phrases (used to show the date picker)
_DATE_ASK_TRIGGERS = [
    'which day', 'what day', 'when would', 'day works', 'day suits',
    'preferred date', 'preferred day', 'works for you', 'what date',
    'which date', 'choose a date', 'pick a date', 'when suits',
    'what day works', 'what day suits', 'pick a day',
]

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
COMPLAINTS: If the customer expresses frustration, dissatisfaction, or a complaint (e.g. "this is unacceptable", "last time was terrible", "I'm really upset"), acknowledge their feelings FIRST before anything else. Say something like: "I'm really sorry to hear that — that's not the experience we want you to have at all." Then offer to help resolve the situation. Never be defensive or dismissive.

━━━ COMPETITOR MENTIONS — MANDATORY RULE ━━━
TRIGGER: Whenever the customer mentions another company, a quote from a different company, a competitor's price, or indicates they are comparing services (e.g. "another company quoted me", "I got a quote from X", "comparing quotes", "someone else said", "I found a cheaper option").
ACTION REQUIRED — NO EXCEPTIONS: Your reply MUST begin with this exact phrase as the very first words: "That's great that you're comparing options! 😊"
Do NOT start with anything else. Do NOT skip this line. Do NOT rephrase it. This rule overrides all other instructions about how to start a reply.
After that opening line, highlight Environ's strengths: FREE initial inspection with no obligation, 15+ years experience, PCA-accredited, TrustMark registered, family-run business. Then answer their question.
Never criticise competitors or mention them by name.

━━━ BOOKING — ONLY WHEN THE CUSTOMER WANTS IT ━━━
NEVER push or force the booking. Only enter the booking flow when the customer clearly expresses interest (e.g. "I'd like to book", "can I make an appointment", "how do I book", "can someone come out").
At the end of relevant answers, you may add ONE soft line like: "If you'd like a specialist to take a look, I can arrange a free inspection — just let me know! 😊"

━━━ URGENCY & EMERGENCIES — MANDATORY RULE ━━━
TRIGGER: Customer expresses urgency ("it's urgent", "emergency", "water is coming in now", "it's getting worse", "it's really bad", "it's leaking", "flooding").
ACTION REQUIRED — NO EXCEPTIONS: Your reply MUST begin with this exact phrase as the very first words: "I understand — let's get this sorted as quickly as possible! 🏠"
Do NOT start with "I'm sorry", "It sounds like", bullet points, tips, potential causes, or any other text first. This rule overrides all other instructions about how to start a reply.
After that opening line: immediately move into the booking flow — confirm the service (or ask if unclear) and proceed to collect details. Do NOT give general advice or a support response first.

When the customer does want to book, follow these steps one at a time:
STEP 1 — Service: Ask "What service do you need?" (if not already known). If you recommended a service and the user accepted it, treat that as the confirmed service — do NOT ask again.
FAST-TRACK: If the customer's opening message already contains service, date, issues, or any combination — do NOT ask for them again. Jump to the FIRST missing field. Examples:
  • "Book a damp survey for Monday, I have rising damp, no other issues" → service ✅ issue ✅ issues_complete ✅ date ✅ → ask name (STEP 5)
  • "I'd like mould removal, I have mould on the ceiling" → service ✅ issue ✅ → ask for more issues (STEP 2 continuation)
  • "I have damp and mould, can someone come out?" → issues ✅ → ask service (STEP 1) then skip issues
  NEVER re-ask for anything the customer already stated — even if mentioned in passing.
STEP 2 — Issues (multi-issue collection):
  • If a photo was shared ([Photo attached] in history): you already know the issue from the image — do NOT ask the customer to describe it again. Still ask "Are you also facing any other issues I should include?"
  • If no photo: ask "Could you describe the issue(s) you are facing?"
  • VAGUE DESCRIPTIONS: If the customer's reply is too vague (e.g. "a problem", "something wrong", "an issue with my house"), ask one short clarifying question: "Could you tell me a bit more about what you're seeing? For example, is it damp, mould, a crack, or something else?" — accept whatever they reply next as the issue, no matter how brief.
  • After receiving the first issue description, ALWAYS ask: "Are you facing any other issues as well? I can include everything in a single inspection — just let me know! 😊"
  • When the user mentions more issues using phrases like "also", "another issue", "one more thing", "and also", "plus", "as well", "additionally", "there's also" — collect each one and ask again.
  • IMPORTANT: If the customer uses a trigger phrase AND includes an issue description in the SAME message (e.g. "also there's mould", "one more thing — the roof is leaking"), accept the issue immediately — do NOT ask them to describe it again. Just acknowledge it and ask if there are any more.
  • Only stop when the user confirms no more issues: "no", "nope", "that's all", "no more", "nothing else", "just that", "that's it", "done".
  • NEVER start a new booking for each individual issue — ALL issues go into ONE single booking.
  • Once all issues are collected and confirmed, proceed to STEP 3.

STEP 3 — Date: Ask "Which day works for you? We're available Monday to Saturday — just let me know what suits!" — do NOT call check_availability yet. Wait for the user to reply with a date first.
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
  DATE RULE FOR SUMMARY: Always use the EXACT "formatted_date" returned by the check_availability tool (e.g. "Thursday, 14 May 2026") — never the date as the customer typed it.
STEP 9 — Book: Call book_appointment only AFTER confirmation. Pass ALL issues combined as the issue field. Then say "✅ You're booked! See you on [date] at [time], [name]. Our team will be in touch to confirm your visit. Is there anything else I can help you with? 😊"

━━━ BOOKING RULES ━━━
- The BOOKING STATE block injected above this message shows what is already collected. NEVER re-ask for a ✅ field. Jump to the stated NEXT STEP.
- Each step asks ONE question and waits. Move on after the reply — never loop on the same step.
- Typed time in HH:MM (e.g. "13:00") = valid slot. Proceed with it.
- NEVER validate email — any string with @ is valid. NEVER validate phone — any digits are valid.
- Confirmation words (yes/sure/ok/yeah/correct/go ahead/confirm/please/do it/yep) = proceed.
- On slot_taken: call check_availability for same date, show new buttons, book with SAME details.
- book_appointment needs: date, time, name, phone, email, service, issue — all 7 fields.
- SERVICE CHANGE during new booking: if the customer changes the service mid-booking (e.g. "actually, make it a damp survey", "change the service to X"), simply update the service and continue the new booking flow — do NOT treat this as a cancel or reschedule request.
- HESITATION: If the customer hesitates mid-booking ("I'll think about it", "let me check my diary", "maybe later", "I'm not sure yet"), do NOT push or repeat the question. Respond warmly: "Of course, take your time! Just come back whenever you're ready and we'll pick up right where we left off 😊" — then wait.
- NATURAL TIME FORMATS: Accept all natural time expressions ("3pm", "3 o'clock", "half past 2", "2:30 pm") and convert them to HH:MM 24h format internally. Never ask the customer to retype a time in a different format.

━━━ CANCEL / RESCHEDULE ━━━
CRITICAL: When the customer uses words like "cancel", "reschedule", "move my appointment", "change my booking" — enter the cancel/reschedule flow IMMEDIATELY. Do NOT ask "What service do you need?" or start a new booking flow.

CANCEL RULES (follow strictly — no shortcuts):
1. Detect cancel intent from: "cancel", "cancel my booking", "cancel my appointment", "I want to cancel".
2. Ask for the customer's email address if not already provided.
3. Call find_booking(email) immediately — never skip this step, never ask for service/issues.
4. If find_booking returns found:false → say "I couldn't find a booking for [email]. Could you double-check the email or try a different one?" Stay in the cancel flow — do NOT start a new booking.
5. If find_booking returns found:true → show the booking details and ask: "Shall I go ahead and cancel your [service] on [date] at [time]?"
6. Call cancel_booking(event_id) only after the customer confirms. Then confirm the cancellation.

RESCHEDULE RULES (follow strictly — no shortcuts):
1. Detect reschedule intent from: "reschedule", "move my appointment", "change the date", "move it to", "rebook".
2. Do NOT treat a reschedule request as a new booking — NEVER ask "What service do you need?" or "Could you describe the issue(s)".
3. Ask for the customer's email address if not already provided.
4. You MUST call find_booking(email) FIRST before anything else. Never skip this step.
5. After find_booking succeeds, tell the user: "I found your booking: [service] on [formatted_date] at [formatted_time]. I'll reschedule that for you."
6. IMPORTANT — date carry-forward: if the customer already mentioned a new date and/or time (even in their very first message, before find_booking was called), use it directly after find_booking — do NOT ask for the date again. Call check_availability for that date immediately.
7. If no new date was mentioned anywhere in the conversation, ask: "What new date would you like to move it to?"
8. Store the event_id from find_booking in your memory — you MUST pass it to reschedule_booking.
9. Once you have event_id + new_date + new_time: call check_availability, show slot buttons, get confirmation, then call reschedule_booking(event_id, new_date, new_time).
10. Stay in the reschedule flow for the entire conversation until reschedule_booking succeeds or the user explicitly abandons it. Do NOT fall back to the new-booking flow at any point.
11. If reschedule_booking returns success:false — handle by failure type: (a) if error is "slot taken", call check_availability for the same date and show fresh slot buttons; (b) for any other error, tell the customer clearly what went wrong (use the message from the tool) and ask "Would you like to try a different date or time?" — do NOT ask for the email again.

━━━ DATE RULES ━━━
- No Sundays. No past dates. Today is {today}.
- Copy exact "formatted_date" from check_availability result — never recalculate.
- OUT-OF-HOURS: If the customer contacts outside Monday–Saturday 9 AM–6 PM London time, acknowledge it warmly: "Our team is currently offline, but I can take your booking now and they'll be in touch first thing when we reopen! 😊" Then continue the booking flow as normal.

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
Hours: Monday–Saturday 9 AM–6 PM London time.
PRICING: When asked about cost or price, explain that Environ offers FREE initial property inspections — no charge for the visit. A detailed written quote is provided on-site after the inspection, based on the scope of work. Never invent or quote a specific price. Example reply: "We offer a free initial inspection with no obligation — our specialist will assess everything on-site and provide a detailed quote. There's no charge for the visit itself!"
COVERAGE: Environ covers Greater London and surrounding areas. If asked about a specific location, confirm we cover the London area and invite them to book — the team will confirm coverage when they get in touch. Never turn a customer away based on location.
HUMAN HANDOFF: If the customer asks to speak to a person, requests a phone call, or says they'd rather not use the chat (e.g. "can I speak to someone?", "I'd rather call", "give me a number"), respond warmly: "Of course! You can reach our team directly at 📞 0203 935 1596 or 📧 service@environpropertyservices.co.uk — they'll be happy to help. Is there anything else I can assist you with in the meantime?"
Specialist lines: Roofing → 020 3971 1901 | Drainage → 020 3875 8207 | Pest Control → 0203 875 8225 | Restoration/Sash Windows → 0203 903 6919. Route the customer to the relevant number if their query is clearly one of these specialisms."""

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
openai_client: OpenAI = None   # OpenRouter — used for ALL LLM calls (DeepSeek)
chroma_collection     = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global openai_client, chroma_collection
    # All LLM calls → OpenRouter (DeepSeek only — no OpenAI API key required)
    openai_client = OpenAI(
        api_key=os.getenv("OPENROUTER_API_KEY"),
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://chatbot.app.digitalsgalaxy.com",
            "X-Title": "Environ Property Services Chatbot",
        },
    )
    chroma = chromadb.PersistentClient(path=CHROMA_PATH)
    # No embedding_function specified → ChromaDB uses its built-in local model
    # (all-MiniLM-L6-v2 via ONNX). No external API key needed.
    chroma_collection = chroma.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    print(f"ChromaDB ready — {chroma_collection.count()} chunks", flush=True)
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
    # cache_discovery=False avoids file-lock issues in multi-threaded containers
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def check_calendar_availability(date_str: str) -> dict:
    _prev = _socket.getdefaulttimeout()
    _socket.setdefaulttimeout(_GCAL_TIMEOUT)
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        if dt.weekday() == 6:
            return {"available": False, "slots": [],
                    "message": "We don't work on Sundays. Please choose Monday–Saturday."}
        if dt.date() < datetime.date.today():
            return {"available": False, "slots": [],
                    "message": "That date is in the past. Please choose a future date."}

        service   = get_calendar_service()
        day_start = LONDON_TZ.localize(dt.replace(hour=WORK_START_H, minute=0, second=0, microsecond=0))
        day_end   = LONDON_TZ.localize(dt.replace(hour=WORK_END_H,   minute=0, second=0, microsecond=0))

        result = service.freebusy().query(body={
            "timeMin": day_start.isoformat(),
            "timeMax": day_end.isoformat(),
            "items":   [{"id": CALENDAR_ID}]
        }).execute()

        busy_periods = result.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", [])
        free_slots, cursor = [], day_start
        while cursor < day_end:
            slot_end = cursor + datetime.timedelta(hours=SLOT_DURATION_H)
            is_busy = any(
                cursor < pytz.utc.localize(
                    datetime.datetime.fromisoformat(b["end"].replace("Z", ""))
                ).astimezone(LONDON_TZ) and
                slot_end > pytz.utc.localize(
                    datetime.datetime.fromisoformat(b["start"].replace("Z", ""))
                ).astimezone(LONDON_TZ)
                for b in busy_periods
            )
            if not is_busy:
                free_slots.append(cursor.strftime("%H:%M"))
            cursor = slot_end

        return {
            "date":           date_str,
            "formatted_date": dt.strftime("%A, %d %B %Y"),
            "available":      len(free_slots) > 0,
            "slots":          free_slots,
            "message":        "" if free_slots else "No free slots on this date. Please try another day."
        }
    except Exception as e:
        print(f"[CALENDAR ERROR] check_availability: {e}", flush=True)
        # Fallback: return all standard slots so the bot can still show time buttons.
        # The booking step does a live double-check before creating the calendar event.
        _dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return {
            "date":           date_str,
            "formatted_date": _dt.strftime("%A, %d %B %Y"),
            "available":      True,
            "slots":          [f"{h:02d}:00" for h in range(WORK_START_H, WORK_END_H)],
            "message":        "",
            "_fallback":      True,
        }
    finally:
        _socket.setdefaulttimeout(_prev)


def create_calendar_booking(args: dict) -> dict:
    email = args.get("email", "").strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        return {"success": False, "error": "invalid_email",
                "message": "The email address must contain @ and a domain (e.g. name@gmail.com)."}

    phone = args.get("phone", "").strip()
    if len(re.sub(r"\D", "", phone)) < 6:
        return {"success": False, "error": "invalid_phone",
                "message": "Please provide a valid phone number."}

    _prev = _socket.getdefaulttimeout()
    _socket.setdefaulttimeout(_GCAL_TIMEOUT)
    try:
        dt    = datetime.datetime.strptime(f"{args['date']} {args['time']}", "%Y-%m-%d %H:%M")
        start = LONDON_TZ.localize(dt)
        end   = start + datetime.timedelta(hours=SLOT_DURATION_H)
        service = get_calendar_service()

        # Guard against double-booking
        busy_check = service.freebusy().query(body={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "items":   [{"id": CALENDAR_ID}]
        }).execute()
        if busy_check.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", []):
            return {"success": False, "error": "slot_taken",
                    "message": f"The {args['time']} slot on {args['date']} was just taken. "
                               "Please check availability again and pick another slot."}

        service.events().insert(
            calendarId=CALENDAR_ID,
            body={
                "summary":     f"{args.get('service', 'Property Inspection')} – {args['name']}",
                "description": (
                    f"Customer: {args['name']}\n"
                    f"Phone: {args['phone']}\n"
                    f"Email: {args['email']}\n"
                    f"Service: {args.get('service', 'N/A')}\n"
                    f"Issue: {args.get('issue', 'N/A')}\n\n"
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

        try:
            _send_email_notification(
                args["name"], args["phone"], args["email"], start,
                args.get("service", "Property Inspection"), args.get("issue", "N/A")
            )
        except Exception as em_err:
            print(f"[EMAIL ERROR] {em_err}", flush=True)

        return {
            "success":        True,
            "formatted_date": start.strftime("%A, %d %B %Y"),
            "formatted_time": start.strftime("%I:%M %p"),
            "name":           args["name"],
        }
    except Exception as e:
        print(f"[BOOKING ERROR] {e}", flush=True)
        return {"success": False, "error": str(e), "message": f"Booking failed: {e}"}
    finally:
        _socket.setdefaulttimeout(_prev)


def find_customer_booking(email: str) -> dict:
    _prev = _socket.getdefaulttimeout()
    _socket.setdefaulttimeout(_GCAL_TIMEOUT)
    try:
        service = get_calendar_service()
        _now    = datetime.datetime.now(datetime.timezone.utc)
        now     = _now.strftime("%Y-%m-%dT%H:%M:%SZ")
        future  = (_now + datetime.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result  = service.events().list(
            calendarId=CALENDAR_ID, timeMin=now, timeMax=future,
            q=email, singleEvents=True, orderBy="startTime"
        ).execute()
        events = result.get("items", [])
        if not events:
            return {"found": False,
                    "message": f"No upcoming bookings found for {email}. Please double-check the email address."}

        event     = events[0]
        start_raw = event["start"].get("dateTime", event["start"].get("date"))
        dt        = datetime.datetime.fromisoformat(
                        start_raw.replace("Z", "+00:00")
                    ).astimezone(LONDON_TZ)

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
    finally:
        _socket.setdefaulttimeout(_prev)


def cancel_customer_booking(event_id: str) -> dict:
    _prev = _socket.getdefaulttimeout()
    _socket.setdefaulttimeout(_GCAL_TIMEOUT)
    try:
        service   = get_calendar_service()
        event     = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
        start_raw = event["start"].get("dateTime", event["start"].get("date"))
        dt        = datetime.datetime.fromisoformat(
                        start_raw.replace("Z", "+00:00")
                    ).astimezone(LONDON_TZ)

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
            "success": True,
            "message": f"Booking on {dt.strftime('%A, %d %B %Y')} at {dt.strftime('%I:%M %p')} "
                       "has been cancelled successfully."
        }
    except Exception as e:
        print(f"[CANCEL ERROR] {e}", flush=True)
        return {"success": False, "message": f"Cancellation failed: {e}"}
    finally:
        _socket.setdefaulttimeout(_prev)


def reschedule_customer_booking(event_id: str, new_date: str, new_time: str) -> dict:
    if not event_id or event_id.strip() == "":
        return {"success": False,
                "message": "No event_id provided. You must call find_booking first to get the event_id before rescheduling."}
    _prev = _socket.getdefaulttimeout()
    _socket.setdefaulttimeout(_GCAL_TIMEOUT)
    try:
        service   = get_calendar_service()
        try:
            event = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
        except Exception:
            return {"success": False,
                    "message": "Could not find that booking in the calendar — it may have already been cancelled or modified. Please ask the customer to verify their booking details."}

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
            return {"success": False,
                    "message": f"The {new_time} slot on {new_date} is already taken. "
                               "Please check availability and pick another slot."}

        old_raw = event["start"].get("dateTime", event["start"].get("date"))
        old_dt  = datetime.datetime.fromisoformat(
                      old_raw.replace("Z", "+00:00")
                  ).astimezone(LONDON_TZ)

        name = ""
        for line in event.get("description", "").split("\n"):
            if line.startswith("Customer:"):
                name = line.replace("Customer:", "").strip()
                break

        # Create new event FIRST — so if insert fails the original booking is preserved
        service.events().insert(
            calendarId=CALENDAR_ID,
            body={
                "summary":     event.get("summary", "Property Inspection"),
                "description": event.get("description", "") +
                               f"\n\nRescheduled from {old_dt.strftime('%A, %d %B %Y at %I:%M %p')}",
                "start": {"dateTime": new_start.isoformat(), "timeZone": "Europe/London"},
                "end":   {"dateTime": new_end.isoformat(),   "timeZone": "Europe/London"},
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "email", "minutes": 60},
                        {"method": "popup", "minutes": 30}
                    ]
                }
            }
        ).execute()
        # Only delete old event after new one is confirmed created
        service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()

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
    finally:
        _socket.setdefaulttimeout(_prev)


# ── Email helpers ──────────────────────────────────

def _send_email_notification(name: str, phone: str, email: str, dt: datetime.datetime,
                              service: str = "Property Inspection", issue: str = "N/A"):
    gmail_user = os.getenv("GMAIL_SENDER")
    gmail_pw   = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_pw:
        raise ValueError("GMAIL_SENDER or GMAIL_APP_PASSWORD not set")

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
      <div style="padding:12px 24px;background:#e8f5ee;font-size:13px;color:#555;">Booked via the Environ website chatbot.</div>
    </div>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📅 New Booking – {name} | {dt.strftime('%d %b %Y %I:%M %p')}"
    msg["From"]    = gmail_user
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(gmail_user, gmail_pw)
        s.sendmail(gmail_user, NOTIFY_EMAIL, msg.as_string())


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
    msg["From"] = gmail_user
    msg["To"]   = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(gmail_user, gmail_pw)
        s.sendmail(gmail_user, NOTIFY_EMAIL, msg.as_string())


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
    msg["From"] = gmail_user
    msg["To"]   = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(gmail_user, gmail_pw)
        s.sendmail(gmail_user, NOTIFY_EMAIL, msg.as_string())


# ── Tool executor ──────────────────────────────────
_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_TOOL_TIMEOUT  = 15  # seconds

def execute_tool(name: str, args: dict) -> dict:
    def _run():
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

    try:
        return _TOOL_EXECUTOR.submit(_run).result(timeout=_TOOL_TIMEOUT)
    except FuturesTimeoutError:
        print(f"[TOOL TIMEOUT] {name} exceeded {_TOOL_TIMEOUT}s", flush=True)
        return {"available": False, "slots": [], "success": False,
                "message": "The calendar service is taking too long. Please try again in a moment."}
    except Exception as e:
        print(f"[TOOL ERROR] {name}: {e}", flush=True)
        return {"error": str(e), "available": False, "slots": []}


# ── RAG ────────────────────────────────────────────
def retrieve_context(query: str, n: int = 3) -> str:
    """Retrieve relevant knowledge-base chunks using ChromaDB's local embedding model."""
    if not chroma_collection or chroma_collection.count() == 0:
        return ""
    try:
        # query_texts lets ChromaDB embed the query using its built-in local model —
        # no external API call required.
        docs = chroma_collection.query(
            query_texts=[query], n_results=n
        ).get("documents", [[]])[0]
        return "\n\n---\n\n".join(docs)
    except Exception as e:
        print(f"[RAG ERROR] {e}", flush=True)
        return ""


# ── Request model ──────────────────────────────────
class HistoryMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    image_base64: Optional[str] = None
    image_mime_type: Optional[str] = "image/jpeg"
    history: list[HistoryMessage] = []
    session_id: Optional[str] = None


# ── Guardrails ─────────────────────────────────────
INPUT_MAX_CHARS = 600

_MONTH_MAX_DAYS = {
    "feb": 29, "february": 29,
    "apr": 30, "april": 30,
    "jun": 30, "june": 30,
    "sep": 30, "september": 30,
    "nov": 30, "november": 30,
}

def validate_input(message: str) -> dict:
    s = message.strip()

    if not s:
        return {"ok": False, "reply": "Please type a message and I'll be happy to help! 😊"}

    if len(s) > INPUT_MAX_CHARS:
        return {"ok": False, "reply": (
            f"Your message is too long ({len(s)} characters — max {INPUT_MAX_CHARS}). "
            "Could you shorten it, or split it across a couple of messages?"
        )}

    non_latin = sum(1 for c in s if c.isalpha() and ord(c) > 591)
    if len(s) > 8 and non_latin / max(len(s), 1) > 0.25:
        return {"ok": False, "reply": (
            "I can only assist in English. Please write your message in English and I'll be glad to help! 😊"
        )}

    if re.search(r'(.)\1{9,}', s):
        return {"ok": False, "reply": (
            "That doesn't look like a valid message. Please describe your property issue and I'll help you out!"
        )}

    s_lower = s.lower()
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
    if re.search(
        r'\b0+\s*(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b', s_lower
    ):
        return {"ok": False, "reply": "Day 0 doesn't exist! Could you double-check the date you meant?"}

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

    m3 = re.search(r'\b(1[3-9]|[2-9]\d)\s*/\s*\d{4}\b', s_lower)
    if m3:
        return {"ok": False, "reply": (
            f"Month {m3.group(1)} doesn't exist — there are only 12 months. Could you double-check the date?"
        )}

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
                "I'm here to help with property and home-related questions only! What can I help you with today? 😊"
            )}

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
            "I'm a property support assistant — I can't output data in technical formats. "
            "I'm happy to explain services, causes, treatments, or help you book an inspection. What would you like to know? 😊"
        )}

    _PROFANITY = [
        r'\bf[\*u][c\*]k', r'\bs[h\*][i\*]t\b', r'\bb[i\*]tch\b',
        r'\bc[u\*]nt\b',   r'\bwanker\b',        r'\btwat\b',
        r'\bdickhead\b',   r'\bprick\b',          r'\barshole\b',
        r'\bcocksucker',   r'\bmotherfuck',       r'\bfucking\b',
        r'\bshitting\b',   r'\bbullshit\b',
    ]
    if any(re.search(p, s_lower) for p in _PROFANITY):
        return {"ok": False, "reply": (
            "Please keep the conversation respectful and I'll be happy to help with your property needs! 😊"
        )}

    if re.search(r'https?://', s_lower):
        return {"ok": False, "reply": (
            "Please don't include web links in your message. Just describe your "
            "property issue in your own words and I'll help you out!"
        )}

    symbols = sum(1 for c in s if not c.isalnum() and c not in " .,!?'-@:/()")
    if len(s) > 10 and symbols / max(len(s), 1) > 0.55:
        return {"ok": False, "reply": (
            "That message contains too many special characters. "
            "Please describe your property issue in plain English and I'll help you out!"
        )}

    return {"ok": True}


def validate_output(text: str) -> str:
    if not text:
        return text

    # Strip HTML tags GPT-4o sometimes outputs
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(
        r'</?(?:b|i|u|em|strong|span|div|p|table|tr|td|th|thead|tbody|hr)\b[^>]*>',
        '', text, flags=re.IGNORECASE
    )

    non_latin = sum(1 for c in text if c.isalpha() and ord(c) > 591)
    if len(text) > 30 and non_latin / max(len(text), 1) > 0.25:
        return ("I'm sorry, something went wrong with my response. "
                "Could you rephrase your question and I'll try again?")

    tl = text.lower()
    if any(p in tl for p in ['traceback (most recent', 'file "/']):
        return ("I ran into a small technical issue. "
                "Please try again or contact us directly — we're happy to help!")

    MAX_OUTPUT_CHARS = 1200
    if len(text) > MAX_OUTPUT_CHARS:
        cut = text[:MAX_OUTPUT_CHARS]
        last_stop = max(cut.rfind('. '), cut.rfind('.\n'), cut.rfind('! '), cut.rfind('? '))
        if last_stop > MAX_OUTPUT_CHARS * 0.6:
            text = cut[:last_stop + 1] + "\n\n*…Feel free to ask if you'd like more detail!*"
        else:
            text = cut.rstrip() + "…"

    return text


# ── Booking state extractor ────────────────────────

def extract_booking_state(history: list) -> str:
    """
    AI-powered booking state extractor. Uses GPT-4o-mini to read the conversation
    and return a structured BOOKING STATE block injected into the main prompt.
    """
    if not history or not openai_client:
        return ""

    today = datetime.date.today().strftime("%A, %d %B %Y")
    transcript_lines = []
    for m in history[-30:]:
        role_label = "Customer" if m.role == "user" else "Agent"
        transcript_lines.append(f"{role_label}: {m.content.strip()[:400]}")
    transcript = "\n".join(transcript_lines)

    extraction_prompt = f"""You are a booking state extractor for a property inspection chatbot.
Read the conversation below and extract ONLY what has been explicitly provided or clearly confirmed.
Be conservative — when uncertain, use null. Today is {today}.

Return ONLY a valid JSON object with these fields:
{{
  "intent": "new_booking" or "reschedule" or "cancel" or "support",
  "service": "service name or null",
  "issues": ["list of distinct property issues — deduplicated, no meta-comments like 'its an issue'"],
  "issues_complete": true or false,
  "pending_more_issue": true or false,
  "date": "date as the customer said it (for NEW bookings only), or null",
  "time": "HH:MM 24h format (for NEW bookings only), or null",
  "name": "full name or null",
  "phone": "phone number or null",
  "email": "email address or null",
  "reschedule_email": "MOST RECENT email the customer gave for reschedule/cancel lookup, or null",
  "reschedule_new_date": "new date the customer wants for reschedule (as stated), or null",
  "reschedule_new_time": "HH:MM 24h format of new time for reschedule, or null",
  "find_booking_done": true or false,
  "find_booking_failed": true or false,
  "find_booking_failed_email": "the specific email address that find_booking failed on, or null"
}}

Field rules:
- intent:
    "new_booking" = customer wants a NEW inspection/appointment
    "reschedule" = customer wants to MOVE/CHANGE/RESCHEDULE an existing booking
    "cancel" = customer wants to CANCEL an existing booking
    "support" = questions only, no booking action
    CRITICAL: Only set "reschedule" or "cancel" if the customer explicitly wants to act on an ALREADY EXISTING booking (keywords: "cancel my appointment", "reschedule my booking", "move my existing appointment"). If the customer is modifying details of a NEW booking currently in progress (e.g. "change the service", "update the date"), keep intent as "new_booking". If reschedule/cancel intent appeared earlier but a NEW booking is now clearly in progress (service+issues+date+time+name all collected), keep intent as "new_booking".
- service: extract from customer message OR from agent recommendation the customer accepted — for NEW bookings only
- issues: every distinct property issue the customer mentioned. Deduplicate. Ignore meta-replies like "its an issue" or "a problem". Include issues from ANY message including the first one. If the customer described an issue AND immediately confirmed no more ("I have damp, that's it" / "just mould, nothing else"), include the issue AND set issues_complete to true in the same turn.
- issues_complete: true if ANY of these: (a) customer explicitly said no more issues ("no", "nope", "that's all", "nothing else", "done", "just that", "that's it", "no more", "only that", "that's everything"), (b) conversation has moved past issues stage — date, time, name, phone, or email have been provided, (c) customer gave a clear single issue AND the agent has already acknowledged it and asked for more AND customer replied with "no" or equivalent. Set false only if issues are still actively being collected and customer has NOT yet confirmed they're done.
- pending_more_issue: Use this simple two-step test:
    STEP A — Does the customer's LAST message contain ANY property/issue word? Check for: damp, mould, mold, mold, leak, leaking, crack, rot, pest, roof, drain, window, wall, ceiling, floor, water, stain, smell, smell, damp, condensation, rising, penetrating, wet, dry, structural, tiles, brick, render. If YES → pending_more_issue = false. Full stop. No further checks needed.
    STEP B — Only if STEP A is false (no property word found): set pending_more_issue = true ONLY if ALL of: (a) the customer's last message is a short affirmative only — "yes", "yeah", "yep", "sure", "ok", "one more", "also", "another" — with no other content; AND (b) the issues list already has at least one entry before this turn.
    DEFAULT: when in doubt, set pending_more_issue = false.
- date: ONLY for new bookings — specific date given or date button clicked
- time: ONLY for new bookings — time slot selected or typed, converted to HH:MM 24h
- name/phone/email: ONLY if customer explicitly provided these for a NEW booking
- reschedule_email: the MOST RECENT email the customer gave for reschedule/cancel lookup (if they gave multiple emails, use the latest one)
- reschedule_new_date: new date customer wants for reschedule (look through ALL messages, including early ones)
- reschedule_new_time: new time for reschedule in HH:MM 24h format
- find_booking_done: true if agent said "I found your booking" or similar success message
- find_booking_failed: true if agent said "no booking found", "couldn't find a booking", or similar failure
- find_booking_failed_email: the exact email address that was used when find_booking failed (extract from agent's error message like "couldn't find a booking for [email]")

Conversation:
{transcript}"""

    try:
        resp = openai_client.chat.completions.create(
            model="deepseek/deepseek-chat",
            messages=[{"role": "user", "content": extraction_prompt}],
            response_format={"type": "json_object"},
            max_tokens=400,
            temperature=0,
        )
        state = json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[STATE EXTRACTOR ERROR] {e}", flush=True)
        return ""

    intent = state.get("intent", "support")

    # ── RESCHEDULE STATE ──────────────────────────────
    if intent == "reschedule":
        lines = ["━━━ RESCHEDULE STATE (injected by system — highest priority) ━━━"]
        lines.append("  ⚠️  RESCHEDULE request — NOT a new booking. Follow RESCHEDULE RULES strictly.")
        lines.append("  🚫 DO NOT ask for service, issues, or start the new-booking flow.")

        r_email      = state.get("reschedule_email")
        r_date       = state.get("reschedule_new_date")
        r_time       = state.get("reschedule_new_time")
        fb_done      = state.get("find_booking_done", False)
        fb_failed    = state.get("find_booking_failed", False)
        fb_fail_email = state.get("find_booking_failed_email")

        if r_email:
            lines.append(f"  ✅ 📧 Customer email: {r_email}")
        if r_date:
            lines.append(f"  ✅ 📅 Requested new date: {r_date}")
        if r_time:
            lines.append(f"  ✅ ⏰ Requested new time: {r_time}")
        if fb_done:
            lines.append("  ✅ 🔍 find_booking: already called successfully — event_id is in conversation history")
        if fb_failed and fb_fail_email:
            lines.append(f"  ⚠️  find_booking: failed for {fb_fail_email} — customer may have provided a new email")

        # Determine if customer gave a NEW email after the failed attempt
        _email_retried = fb_failed and fb_fail_email and r_email and (r_email.lower() != fb_fail_email.lower())

        if not r_email:
            next_step = 'Ask: "Could you provide the email address linked to your booking?"'
        elif fb_failed and not _email_retried:
            # Same email that already failed — ask for a different one
            next_step = (f'No booking was found for "{r_email}". '
                         'Tell the customer: "I couldn\'t find a booking for that email address. '
                         'Could you double-check it or try a different one?"')
        elif not fb_done:
            # First attempt or new email given after failure — call find_booking now
            next_step = (f'IMMEDIATELY call find_booking("{r_email}") — '
                         'do this NOW before writing any response text.')
        elif r_date and r_time:
            next_step = (
                f'find_booking succeeded. Customer already stated new date "{r_date}" and time "{r_time}". '
                f'Call check_availability for "{r_date}" (convert to YYYY-MM-DD). '
                'Show slot buttons. Once user confirms, call reschedule_booking(event_id, new_date, new_time).'
            )
        elif r_date and not r_time:
            next_step = (
                f'find_booking succeeded. Customer already stated new date "{r_date}". '
                f'Call check_availability for "{r_date}" (convert to YYYY-MM-DD) to show available slots. '
                'Say "We have availability on [formatted_date]! Please pick a time 👇"'
            )
        else:
            next_step = 'find_booking succeeded. Ask: "What new date would you like to move your appointment to?"'

        lines.append(f"\n  ⏭  NEXT STEP: {next_step}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    # ── CANCEL STATE ──────────────────────────────────
    if intent == "cancel":
        lines = ["━━━ CANCEL STATE (injected by system — highest priority) ━━━"]
        lines.append("  ⚠️  CANCEL request — NOT a new booking. Follow CANCEL RULES strictly.")
        lines.append("  🚫 DO NOT ask for service, issues, or start the new-booking flow.")

        c_email       = state.get("reschedule_email")
        fb_done       = state.get("find_booking_done", False)
        fb_failed     = state.get("find_booking_failed", False)
        fb_fail_email = state.get("find_booking_failed_email")

        if c_email:
            lines.append(f"  ✅ 📧 Customer email: {c_email}")
        if fb_done:
            lines.append("  ✅ 🔍 find_booking: already called successfully — event_id is in conversation history")
        if fb_failed and fb_fail_email:
            lines.append(f"  ⚠️  find_booking: failed for {fb_fail_email} — customer may have provided a new email")

        _email_retried = fb_failed and fb_fail_email and c_email and (c_email.lower() != fb_fail_email.lower())

        if not c_email:
            next_step = 'Ask: "Could you provide the email address linked to your booking?"'
        elif fb_failed and not _email_retried:
            next_step = (f'No booking was found for "{c_email}". '
                         'Tell the customer: "I couldn\'t find a booking for that email address. '
                         'Could you double-check it or try a different one?"')
        elif not fb_done:
            next_step = (f'IMMEDIATELY call find_booking("{c_email}") — '
                         'do this NOW before writing any response text.')
        else:
            next_step = ('find_booking succeeded. Show the booking details and ask: '
                         '"Shall I go ahead and cancel your [service] on [date] at [time]?" '
                         'Then call cancel_booking(event_id) after confirmation.')

        lines.append(f"\n  ⏭  NEXT STEP: {next_step}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    # ── BOOKING STATE (new bookings only) ─────────────
    booking_active = intent == "new_booking" or any([
        state.get("service"), state.get("issues"), state.get("name"),
        state.get("date"), state.get("time"), state.get("email"),
    ])

    if not booking_active:
        return ""

    issues_list = state.get("issues") or []
    issues_text = "\n".join(f"    {i+1}. {iss}" for i, iss in enumerate(issues_list))

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

    if booking_active:
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
                         'and ask "Shall I confirm this booking?" '
                         'IMPORTANT: Use the EXACT formatted_date from the check_availability tool result for the Date field.')
        lines.append(f"\n  ⏭  NEXT STEP: {next_step}")
        lines.append("  🚫 DO NOT re-ask for any ✅ field above — they are final.")
    else:
        lines.append("\n  ℹ️  User is in support/info mode — answer their question freely.")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def _parse_date_str_to_iso(date_raw: str) -> str:
    """Convert natural date strings like 'Monday 18 May' or '18 May 2026' to YYYY-MM-DD."""
    today = datetime.date.today()
    date_raw = date_raw.strip()
    for fmt in ["%A %d %B %Y", "%A, %d %B %Y", "%d %B %Y", "%d %B", "%A %d %B", "%A, %d %B"]:
        try:
            dt = datetime.datetime.strptime(date_raw, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=today.year)
                if dt.date() < today:
                    dt = dt.replace(year=today.year + 1)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def build_messages(req: ChatRequest) -> tuple[list, str]:
    today   = datetime.date.today().strftime("%A, %d %B %Y")
    system  = SYSTEM_PROMPT_TEMPLATE.format(today=today)
    context = retrieve_context(req.message)

    messages = [{"role": "system", "content": system}]
    for m in req.history[-20:]:
        messages.append({"role": m.role, "content": m.content})

    # Include current message so the state extractor already reflects
    # the answer the user just gave (name/phone/etc.) and skips to next step.
    temp_history  = list(req.history) + [HistoryMessage(role="user", content=req.message)]
    session_state = extract_booking_state(temp_history)
    if session_state:
        messages.append({"role": "system", "content": session_state})

    # ── Dynamic mandatory-phrase injection ─────────────────────────────────
    # Detect competitor / urgency triggers and inject a pinned system note so
    # GPT-4o cannot paraphrase the required opening phrase.
    _msg_lower = req.message.lower()
    _competitor_kw = [
        "another company", "other company", "another firm", "different company",
        "quoted me", "got a quote", "i got a quote", "quote from", "price from",
        "comparing", "comparing options", "someone else quoted",
        "cheaper elsewhere", "found a cheaper", "found cheaper",
    ]
    _urgency_kw = [
        "urgent", "emergency", "water is coming", "water coming",
        "flooding", "flood", "getting worse", "right now", "coming through my roof",
        "coming in now", "leaking now", "it's leaking", "asap",
    ]
    if any(kw in _msg_lower for kw in _competitor_kw):
        messages.append({"role": "system", "content": (
            "⚠️ COMPETITOR KEYWORD DETECTED in the customer's message.\n"
            "Your reply MUST begin with these exact words — no exceptions, no variations:\n"
            "\"That's great that you're comparing options! 😊\"\n"
            "Do NOT open with 'At Environ', 'Great question', 'I understand', or anything else. "
            "Those four words above are your first words, full stop."
        )})
    elif any(kw in _msg_lower for kw in _urgency_kw):
        messages.append({"role": "system", "content": (
            "⚠️ URGENCY KEYWORD DETECTED in the customer's message.\n"
            "Your reply MUST begin with these exact words — no exceptions, no variations:\n"
            "\"I understand — let's get this sorted as quickly as possible! 🏠\"\n"
            "Do NOT open with 'I'm sorry', bullet points, tips, or anything else. "
            "Those words above are your first words. "
            "After that line, immediately move into the booking flow."
        )})

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
    return messages, "deepseek/deepseek-v4-pro"


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

    def generate():
        # build_messages runs here (inside the thread) so it never blocks the async event loop.
        # This includes the gpt-4o-mini extract_booking_state call and the ChromaDB embedding.
        messages, model = build_messages(req)

        # ── Detect booking stage ────────────────────────────────────────────
        # If date is collected but time is not (new booking only), FORCE check_availability
        # so slot buttons always appear — never rely on GPT-4o choosing the tool.
        _state_text = " ".join(
            m["content"] for m in messages
            if isinstance(m.get("content"), str) and any(
                kw in m.get("content", "")
                for kw in ("BOOKING STATE", "RESCHEDULE STATE", "CANCEL STATE")
            )
        )
        _has_date             = "📅 Date:" in _state_text
        _has_time             = "⏰ Time:" in _state_text
        _has_issues_complete  = "Issues finalised: Yes" in _state_text
        _in_reschedule_cancel = "RESCHEDULE STATE" in _state_text or "CANCEL STATE" in _state_text
        # Only force check_availability for NEW bookings, after issues are confirmed,
        # and only when date is known but time is not yet selected.
        _forced_tool_choice = (
            {"type": "function", "function": {"name": "check_availability"}}
            if _has_date and not _has_time and _has_issues_complete and not _in_reschedule_cancel
            else "auto"
        )

        # Step 1: non-streaming call — detects tool calls
        response = openai_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice=_forced_tool_choice,
            max_tokens=500,
            temperature=0.4,
        )
        choice = response.choices[0]

        # Check tool_calls directly — GPT-4o sometimes returns finish_reason="stop"
        # even for forced tool calls; the tool_calls field is the reliable indicator.
        if choice.message.tool_calls:
            msg          = choice.message
            tool_results = []
            ui_events    = []

            for tc in msg.tool_calls:
                args   = json.loads(tc.function.arguments)
                result = execute_tool(tc.function.name, args)

                if tc.function.name == "check_availability" and result.get("slots"):
                    ui_events.append({
                        "ui":             "slots",
                        "date":           result["date"],
                        "formatted_date": result.get("formatted_date", result["date"]),
                        "slots":          result["slots"]
                    })

                # Slot just taken — re-check via execute_tool (has timeout protection)
                if tc.function.name == "book_appointment" and result.get("error") == "slot_taken":
                    avail = execute_tool("check_availability", {"date": args["date"]})
                    if avail.get("slots"):
                        ui_events.append({
                            "ui":             "slots",
                            "date":           avail["date"],
                            "formatted_date": avail.get("formatted_date", avail["date"]),
                            "slots":          avail["slots"]
                        })
                        result["new_availability"] = avail

                tool_results.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      json.dumps(result)
                })

            # ── Auto-chain: find_booking succeeded in reschedule → emit slot buttons ──
            # After find_booking returns in the reschedule flow, we can't make a second
            # tool call in the same turn. Instead, auto-call check_availability and inject
            # the result as a system note so the follow-up text is correct.
            _auto_avail_ctx = ""
            if _in_reschedule_cancel and not any(e.get("ui") == "slots" for e in ui_events):
                for _tc in msg.tool_calls:
                    if _tc.function.name == "find_booking":
                        _find_res = next(
                            (json.loads(r["content"]) for r in tool_results if r["tool_call_id"] == _tc.id),
                            {}
                        )
                        if _find_res.get("found"):
                            _dm = re.search(r'Requested new date: ([^\n]+)', _state_text)
                            if _dm:
                                _new_iso = _parse_date_str_to_iso(_dm.group(1).strip())
                                if _new_iso:
                                    _avail = execute_tool("check_availability", {"date": _new_iso})
                                    if _avail.get("slots"):
                                        ui_events.insert(0, {
                                            "ui":             "slots",
                                            "date":           _avail["date"],
                                            "formatted_date": _avail.get("formatted_date", _new_iso),
                                            "slots":          _avail["slots"]
                                        })
                                        _auto_avail_ctx = (
                                            f"\n[SYSTEM: check_availability was automatically run for {_new_iso}. "
                                            f"Result: available slots on {_avail.get('formatted_date', _new_iso)}: {_avail['slots']}. "
                                            f"Slot buttons have been sent to the frontend. "
                                            f"Say: 'We have availability on {_avail.get('formatted_date', _new_iso)}! "
                                            f"Please pick a time 👇' — do NOT list slots as text.]"
                                        )
                                    else:
                                        _auto_avail_ctx = (
                                            f"\n[SYSTEM: check_availability was run for {_new_iso}. "
                                            f"No slots available. Tell the customer and ask for a different date.]"
                                        )

            _asst_msg = {
                "role":       "assistant",
                "content":    msg.content,
                "tool_calls": [
                    {
                        "id":       tc.id,
                        "type":     "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    }
                    for tc in msg.tool_calls
                ]
            }
            follow_up = messages + [_asst_msg] + tool_results
            # Inject auto-availability context as a system note after tool results
            if _auto_avail_ctx:
                follow_up.append({"role": "system", "content": _auto_avail_ctx})

            # Step 2: follow-up streaming response
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
            for i, w in enumerate(safe_text.split(" ")):
                yield f"data: {json.dumps({'token': w + (' ' if i < len(safe_text.split(' '))-1 else '')})}\n\n"

            # Emit slot / datepicker UI events after the text
            for evt in ui_events:
                yield f"data: {json.dumps(evt)}\n\n"

            _has_slot_ui = any(e.get("ui") == "slots" for e in ui_events)
            if not _has_slot_ui and any(t in safe_text.lower() for t in _DATE_ASK_TRIGGERS):
                yield f"data: {json.dumps({'ui': 'datepicker'})}\n\n"

        else:
            # No tool call — stream the direct response
            content = validate_output(choice.message.content or "")
            for i, word in enumerate(content.split(" ")):
                token = word + (" " if i < len(content.split(" ")) - 1 else "")
                yield f"data: {json.dumps({'token': token})}\n\n"

            if any(t in content.lower() for t in _DATE_ASK_TRIGGERS):
                yield f"data: {json.dumps({'ui': 'datepicker'})}\n\n"

        yield "data: [DONE]\n\n"

    def safe_generate():
        """Wraps generate() so any uncaught exception still sends [DONE] to the client."""
        try:
            yield from generate()
        except Exception as err:
            print(f"[GENERATE ERROR] {err}", flush=True)
            import traceback; traceback.print_exc()
            yield f"data: {json.dumps({'token': 'I ran into a technical issue. Please try again or contact us directly!'})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        safe_generate(),
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
