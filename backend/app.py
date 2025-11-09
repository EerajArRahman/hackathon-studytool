from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Session, create_engine, select
from datetime import datetime
from typing import List, Optional, Dict
from pydantic import BaseModel
from pypdf import PdfReader

from models import Deck, Card, next_due_time

DB_URL = "sqlite:///db.sqlite3"
engine = create_engine(DB_URL, echo=False)
SQLModel.metadata.create_all(engine)

app = FastAPI(title="StudyTool API", version="0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

# ---------- Pydantic I/O ----------

class DeckIn(BaseModel):
    name: str
    description: Optional[str] = None

class CardIn(BaseModel):
    deck_id: int
    tag: str
    question: str
    answer: str

class ReviewResultIn(BaseModel):
    card_id: int
    result: str  # "again" | "good" | "easy"

# ---------- Deck routes ----------

@app.post("/decks", response_model=Deck)
def create_deck(deck: DeckIn):
    with Session(engine) as sess:
        d = Deck(name=deck.name, description=deck.description)
        sess.add(d); sess.commit(); sess.refresh(d)
        return d

@app.get("/decks", response_model=List[Deck])
def list_decks():
    with Session(engine) as sess:
        return sess.exec(select(Deck)).all()

# ---------- Card routes ----------

@app.post("/cards", response_model=Card)
def create_card(card: CardIn):
    with Session(engine) as sess:
        # ensure deck exists
        deck = sess.get(Deck, card.deck_id)
        if not deck:
            raise HTTPException(404, "Deck not found")
        c = Card(deck_id=card.deck_id, tag=card.tag,
                 question=card.question, answer=card.answer)
        sess.add(c); sess.commit(); sess.refresh(c)
        return c

@app.get("/cards", response_model=List[Card])
def list_cards(deck_id: Optional[int] = None, tag: Optional[str] = None):
    with Session(engine) as sess:
        stmt = select(Card)
        if deck_id is not None:
            stmt = stmt.where(Card.deck_id == deck_id)
        if tag:
            stmt = stmt.where(Card.tag == tag)
        return sess.exec(stmt).all()

# Next card due (by deck + optional tag)
@app.get("/review/next", response_model=Optional[Card])
def next_card(deck_id: int, tag: Optional[str] = None):
    with Session(engine) as sess:
        stmt = select(Card).where(Card.deck_id == deck_id)
        if tag:
            stmt = stmt.where(Card.tag == tag)
        # due now or earlier
        stmt = stmt.where(Card.due_at <= datetime.utcnow())
        stmt = stmt.order_by(Card.due_at)
        card = sess.exec(stmt).first()
        return card  # may be None

# Submit a review result
@app.post("/review/submit", response_model=Card)
def submit_result(payload: ReviewResultIn):
    result = payload.result
    if result not in {"again", "good", "easy"}:
        raise HTTPException(400, "Invalid result")

    with Session(engine) as sess:
        card = sess.get(Card, payload.card_id)
        if not card:
            raise HTTPException(404, "Card not found")

        # simple SRS
        card.last_result = result
        if result == "again":
            card.wrong_count += 1
        else:
            card.right_count += 1
        card.due_at = next_due_time(result)
        sess.add(card); sess.commit(); sess.refresh(card)
        return card

# ---------- Reflect (stats) ----------

@app.get("/reflect/stats")
def reflect(deck_id: Optional[int] = None):
    with Session(engine) as sess:
        stmt = select(Card)
        if deck_id is not None:
            stmt = stmt.where(Card.deck_id == deck_id)
        cards = sess.exec(stmt).all()

    total = len(cards)
    hard = sum(c.wrong_count > c.right_count for c in cards)
    medium = sum(c.wrong_count == c.right_count and (c.right_count + c.wrong_count) > 0 for c in cards)
    easy = sum(c.right_count > c.wrong_count for c in cards)
    never = sum((c.right_count + c.wrong_count) == 0 for c in cards)

    return {
        "total": total,
        "buckets": {
            "red_hard": hard,
            "orange_medium": medium,
            "green_easy": easy,
            "gray_never": never
        }
    }

# ---------- PDF Q-Gen (heuristic baseline) ----------

def naive_qgen(text: str, max_q: int = 8):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # simple “term: definition” split, or question from long lines
    qa = []
    for ln in lines:
        if ":" in ln and len(qa) < max_q:
            term, definition = ln.split(":", 1)
            qa.append({"q": f"What is {term.strip()}?",
                       "a": definition.strip()})
        elif len(ln.split()) > 7 and len(qa) < max_q:
            words = ln.split()
            head = " ".join(words[:6])
            qa.append({"q": f"Explain: {head} ...", "a": ln})
        if len(qa) >= max_q:
            break
    return qa if qa else [{"q": "Summarize the main idea.", "a": text[:300] + ("..." if len(text)>300 else "")}]

@app.post("/ingest/pdf")
async def ingest_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Upload a PDF")
    data = await file.read()
    try:
        from io import BytesIO
        reader = PdfReader(BytesIO(data))
        pages = []
        for i, p in enumerate(reader.pages):
            txt = p.extract_text() or ""
            pages.append(txt)
        full = "\n".join(pages)
        qa = naive_qgen(full, max_q=10)
        return {"count": len(qa), "qa": qa}
    except Exception as e:
        raise HTTPException(500, f"PDF parse error: {e}")

# =====================================================================
# ---------------- Socratic Sidekick (topic-adaptive questions) -------
# =====================================================================

# Minimal add-on: generate 5 tailored questions for any topic.
# Endpoints:
#   POST /socratic/start { "topic": "Bayes' theorem" }
#     -> { "session_id": "...", "question": "<Q1>", "total": 5 }
#   POST /socratic/reply { "session_id": "...", "answer": "..." }
#     -> either { "done": False, "question": "<next Q>" }
#        or     { "done": True }  # (no synthesis here by design)

import os, json, uuid

from pydantic import BaseModel

class StartSidekickIn(BaseModel):
    topic: str

class ReplySidekickIn(BaseModel):
    session_id: str
    answer: str

# Try Gemini for topic-specific questions, else fallback templates
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
try:
    from google.genai import Client
    _genai = Client(api_key=GEMINI_KEY) if GEMINI_KEY else None
except Exception:
    _genai = None

DEFAULT_TEMPLATES = [
    "In one sentence, what about **{topic}** do you want to understand right now?",
    "Who is the audience for your explanation of **{topic}** (future you, a classmate, a beginner)?",
    "State the core idea of **{topic}** in your own words (3–5 sentences).",
    "Give a concrete example or mini case where **{topic}** applies.",
    "What is a common misconception about **{topic}**, and how would you correct it?"
]

def generate_socratic_questions(topic: str) -> List[str]:
    topic = (topic or "").strip()
    # If Gemini is available, ask it for 5 short, topic-tailored questions.
    if _genai and topic:
        try:
            prompt = (
                "Generate 5 short Socratic questions that guide a learner to explain the topic themselves.\n"
                "Cover: (1) scope/intent, (2) audience/context, (3) core idea in own words, "
                "(4) concrete example, (5) common misconception & correction.\n"
                "Be topic-specific, simple, and non-leading. Output JSON only as an array of strings.\n"
                f'Topic: "{topic}"'
            )
            resp = _genai.models.generate_content(
                model="gemini-1.5-flash",
                contents=[{"role":"user","parts":[{"text":prompt}]}]
            )
            raw = (getattr(resp, "text", "") or "").strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.startswith("json"):
                    raw = raw[4:].strip()
            data = json.loads(raw)
            qs = [str(x).strip() for x in data if str(x).strip()]
            if len(qs) >= 5:
                return qs[:5]
        except Exception:
            pass
    # Fallback: templates with {topic}
    return [q.format(topic=topic or "your topic") for q in DEFAULT_TEMPLATES]

# In-memory session store:
# { session_id: { "topic": str, "q_index": int, "questions": [str] } }
SOCRATIC_SESSIONS: Dict[str, Dict] = {}

@app.post("/socratic/start")
def socratic_start(payload: StartSidekickIn):
    sid = str(uuid.uuid4())
    qs = generate_socratic_questions(payload.topic)
    SOCRATIC_SESSIONS[sid] = {"topic": payload.topic.strip(), "q_index": 0, "questions": qs}
    return {"session_id": sid, "question": qs[0], "total": len(qs)}

@app.post("/socratic/reply")
def socratic_reply(payload: ReplySidekickIn):
    sess = SOCRATIC_SESSIONS.get(payload.session_id)
    if not sess:
        raise HTTPException(404, "Session not found")
    qi = sess["q_index"]
    qs = sess["questions"]
    if qi >= len(qs):
        return {"done": True}  # already finished

    # advance to next question
    sess["q_index"] += 1
    if sess["q_index"] < len(qs):
        return {"done": False, "question": qs[sess["q_index"]]}
    else:
        return {"done": True}
