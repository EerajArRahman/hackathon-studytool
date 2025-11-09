import os
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Session, create_engine, select
from datetime import datetime
from typing import List, Optional, Dict
from pydantic import BaseModel
from pypdf import PdfReader

from models import Deck, Card, BlogPost, next_due_time

# --- DB ---
DB_URL = "sqlite:///db.sqlite3"
engine = create_engine(DB_URL, echo=False)
SQLModel.metadata.create_all(engine)

# --- FastAPI ---
app = FastAPI(title="StudyTool API", version="0.2")
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

class StartSidekickIn(BaseModel):
    topic: str

class ReplySidekickIn(BaseModel):
    session_id: str
    answer: str

class PostIn(BaseModel):
    title: str
    content: str

# ---------- Existing routes (unchanged) ----------
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

@app.post("/cards", response_model=Card)
def create_card(card: CardIn):
    with Session(engine) as sess:
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

@app.get("/review/next", response_model=Optional[Card])
def next_card(deck_id: int, tag: Optional[str] = None):
    with Session(engine) as sess:
        stmt = select(Card).where(Card.deck_id == deck_id)
        if tag:
            stmt = stmt.where(Card.tag == tag)
        stmt = stmt.where(Card.due_at <= datetime.utcnow()).order_by(Card.due_at)
        return sess.exec(stmt).first()

@app.post("/review/submit", response_model=Card)
def submit_result(payload: ReviewResultIn):
    result = payload.result
    if result not in {"again", "good", "easy"}:
        raise HTTPException(400, "Invalid result")
    with Session(engine) as sess:
        card = sess.get(Card, payload.card_id)
        if not card:
            raise HTTPException(404, "Card not found")
        card.last_result = result
        if result == "again":
            card.wrong_count += 1
        else:
            card.right_count += 1
        card.due_at = next_due_time(result)
        sess.add(card); sess.commit(); sess.refresh(card)
        return card

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

# ---------- PDF Q-Gen (unchanged) ----------
def naive_qgen(text: str, max_q: int = 8):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    qa = []
    for ln in lines:
        if ":" in ln and len(qa) < max_q:
            term, definition = ln.split(":", 1)
            qa.append({"q": f"What is {term.strip()}?", "a": definition.strip()})
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
        full = "\n".join((p.extract_text() or "") for p in reader.pages)
        qa = naive_qgen(full, max_q=10)
        return {"count": len(qa), "qa": qa}
    except Exception as e:
        raise HTTPException(500, f"PDF parse error: {e}")

# ---------- Gemini client ----------
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
try:
    from google.genai import Client
    genai_client = Client(api_key=GEMINI_KEY) if GEMINI_KEY else None
except Exception:
    genai_client = None

# In-memory session store: {session_id: {"topic": str, "q_index": int, "qa": list[dict]}}
SOCRATIC_SESSIONS: Dict[str, Dict] = {}

SOCRATIC_QUESTIONS = [
    "What would you like to learn today? Give a one-sentence topic.",
    "Who is the audience for your explanation (your future self, a classmate, a beginner)?",
    "State the core idea in your own words in 3–5 sentences.",
    "Give a concrete example or mini case where this idea applies.",
    "What is a common misconception or pitfall, and how would you correct it?"
]

def synthesize_with_gemini(topic: str, qa_pairs: List[Dict[str, str]]) -> str:
    """Format the 5 answers into a clean Feynman-style note using Gemini (if available)."""
    if not genai_client:
        # Fallback: simple local formatter
        lines = [f"# {topic}\n"]
        for i, (q, a) in enumerate([(SOCRATIC_QUESTIONS[i], x["a"]) for i, x in enumerate(qa_pairs)], 1):
            lines.append(f"## {i}. {q}\n{a}\n")
        lines.append("\n**TL;DR:** Teach the idea in one or two lines.")
        return "\n".join(lines)

    parts = [
        "Turn the following Q&A into a clean, concise Feynman-style note with:\n"
        "- a clear title\n- short intro\n- bullet points for core idea\n- one worked example\n"
        "- a 'Common Pitfall' note\n- a short TL;DR.\nKeep it simple and well-formatted Markdown."
    ]
    for i, qa in enumerate(qa_pairs):
        parts.append(f"Q{i+1}: {SOCRATIC_QUESTIONS[i]}\nA{i+1}: {qa['a']}")
    prompt = "\n\n".join(parts)

    resp = genai_client.models.generate_content(
        model="gemini-1.5-flash",
        contents=[{"role":"user","parts":[{"text":f"Topic: {topic}\n{prompt}"}]}]
    )
    text = getattr(resp, "text", None) or (resp.candidates[0].content.parts[0].text if getattr(resp, "candidates", None) else None)
    return text or "Could not generate content. Please ensure GEMINI_API_KEY is set."

# ---------- Socratic Sidekick routes ----------
import uuid

@app.post("/socratic/start")
def socratic_start(payload: StartSidekickIn):
    sid = str(uuid.uuid4())
    SOCRATIC_SESSIONS[sid] = {"topic": payload.topic.strip(), "q_index": 0, "qa": []}
    return {"session_id": sid, "question": SOCRATIC_QUESTIONS[0]}

@app.post("/socratic/reply")
def socratic_reply(payload: ReplySidekickIn):
    sess = SOCRATIC_SESSIONS.get(payload.session_id)
    if not sess:
        raise HTTPException(404, "Session not found")
    qi = sess["q_index"]
    if qi >= len(SOCRATIC_QUESTIONS):
        raise HTTPException(400, "Session already complete")

    # store answer
    sess["qa"].append({"q": SOCRATIC_QUESTIONS[qi], "a": payload.answer.strip()})
    sess["q_index"] += 1

    # next or synthesize
    if sess["q_index"] < len(SOCRATIC_QUESTIONS):
        return {"done": False, "question": SOCRATIC_QUESTIONS[sess["q_index"]]}
    else:
        content = synthesize_with_gemini(sess["topic"], sess["qa"])
        title = sess["topic"] or "My Learning Note"
        return {"done": True, "title": title, "content": content}

# ---------- Posts (blog) ----------
@app.get("/posts", response_model=List[BlogPost])
def list_posts():
    with Session(engine) as sess:
        return sess.exec(select(BlogPost).order_by(BlogPost.created_at.desc())).all()

@app.post("/posts", response_model=BlogPost)
def create_post(p: PostIn):
    with Session(engine) as sess:
        bp = BlogPost(title=p.title.strip() or "Untitled", content=p.content.strip())
        sess.add(bp); sess.commit(); sess.refresh(bp)
        return bp
