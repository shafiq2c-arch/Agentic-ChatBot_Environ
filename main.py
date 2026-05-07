"""
Environ Property Services — Agentic Chatbot
Booking-focused with Google Calendar + Twilio WhatsApp
"""
import os
import json
import datetime
import pytz
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
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv

load_dotenv()

# ── Constants ──────────────────────────────────────
CHROMA_PATH        = "./chroma_db"
COLLECTION_NAME    = "environ_knowledge"
CALENDAR_ID        = "shafiq2c2c@gmail.com"
NOTIFY_WA_NUMBER   = "+923365579579"
TWILIO_FROM        = "whatsapp:+14155238886"
LONDON_TZ          = pytz.timezone("Europe/London")
WORK_START_H       = 9   # 9 AM
WORK_END_H         = 18  # 6 PM
SLOT_DURATION_H    = 1

GOOGLE_CREDS_FILE  = "google_credentials.json"

# ── System prompt ──────────────────────────────────
SYSTEM_PROMPT_TEMPLATE = """You are Alex, a friendly property specialist for Environ Property Services, London.

RESPONSE STYLE:
- Be CONCISE — 2-3 sentences for most answers
- Use bullet points for lists, never long paragraphs
- Give detail only when the user explicitly asks "tell me more" or similar

PRIMARY GOAL: Help users understand their property issue, then guide them toward booking a FREE site inspection.

BOOKING APPROACH:
- After 1-2 questions, naturally suggest: "This sounds like something worth inspecting in person — I can check our availability for a free site visit if you'd like?"
- When they agree: ask which day suits them, then call check_availability
- Present available slots clearly, ask them to pick one
- Then collect: full name, phone number, email address (ask naturally, not all at once)
- Once you have all three, call book_appointment
- Confirm warmly: "✅ You're booked! See you on [date] at [time], [name]."

TOOLS AVAILABLE:
- check_availability(date): checks free 1-hour slots between 9 AM–6 PM Monday–Saturday
- book_appointment(date, time, name, phone, email): creates calendar event and sends WhatsApp confirmation

SERVICES: Damp (rising/penetrating/lateral/condensation), mould removal & remediation, dry/wet rot, repointing, brick cleaning, heritage restoration, roofing, drainage, sash windows, pest control.

COMPANY: Environ Property Services — family-run, London-based, 15+ years experience, 65+ specialists, PCA-accredited, TrustMark registered, SPAB-affiliated director (Terry Clark).

Today: {today}
Working hours: Monday–Saturday, 9 AM–6 PM (London time). No Sundays."""

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
                    "date": {
                        "type": "string",
                        "description": "Date to check in YYYY-MM-DD format"
                    }
                },
                "required": ["date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book a 1-hour property inspection on Google Calendar and send a WhatsApp notification.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date":  {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "time":  {"type": "string", "description": "Start time in HH:MM 24-hour format"},
                    "name":  {"type": "string", "description": "Customer full name"},
                    "phone": {"type": "string", "description": "Customer phone number"},
                    "email": {"type": "string", "description": "Customer email address"}
                },
                "required": ["date", "time", "name", "phone", "email"]
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
    try:
        dt    = datetime.datetime.strptime(f"{args['date']} {args['time']}", "%Y-%m-%d %H:%M")
        start = LONDON_TZ.localize(dt)
        end   = start + datetime.timedelta(hours=SLOT_DURATION_H)

        service = get_calendar_service()
        event = service.events().insert(
            calendarId=CALENDAR_ID,
            body={
                "summary":     f"Property Inspection – {args['name']}",
                "description": f"Customer: {args['name']}\nPhone: {args['phone']}\nEmail: {args['email']}\n\nBooked via Environ chatbot.",
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

        _send_whatsapp(args["name"], args["phone"], args["email"], start)

        return {
            "success": True,
            "formatted_date": start.strftime("%A, %d %B %Y"),
            "formatted_time": start.strftime("%I:%M %p"),
            "name": args["name"]
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _send_whatsapp(name: str, phone: str, email: str, dt: datetime.datetime):
    body = (
        f"🏠 *New Inspection Booking – Environ Property Services*\n\n"
        f"👤 Name: {name}\n"
        f"📱 Phone: {phone}\n"
        f"📧 Email: {email}\n\n"
        f"📅 Date: {dt.strftime('%A, %d %B %Y')}\n"
        f"⏰ Time: {dt.strftime('%I:%M %p')}\n\n"
        f"Booked via website chatbot."
    )
    TwilioClient(
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN")
    ).messages.create(
        from_=TWILIO_FROM,
        to=f"whatsapp:{NOTIFY_WA_NUMBER}",
        body=body
    )


def execute_tool(name: str, args: dict) -> dict:
    if name == "check_availability":
        return check_calendar_availability(args["date"])
    if name == "book_appointment":
        return create_calendar_booking(args)
    return {"error": "Unknown tool"}


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


def build_messages(req: ChatRequest) -> tuple[list, str]:
    today   = datetime.date.today().strftime("%A, %d %B %Y")
    system  = SYSTEM_PROMPT_TEMPLATE.format(today=today)
    context = retrieve_context(req.message)

    messages = [{"role": "system", "content": system}]
    for m in req.history[-6:]:
        messages.append({"role": m.role, "content": m.content})

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
        model = "gpt-4o"          # vision needed
    else:
        user_content = user_text
        model = "gpt-4o-mini"     # 10× cheaper for text

    messages.append({"role": "user", "content": user_content})
    return messages, model


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
            for tc in msg.tool_calls:
                result = execute_tool(tc.function.name, json.loads(tc.function.arguments))
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

            # Step 3: stream final response after tool execution
            stream = openai_client.chat.completions.create(
                model=model,
                messages=follow_up,
                max_tokens=350,
                temperature=0.5,
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield f"data: {json.dumps({'token': delta})}\n\n"

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
