from datetime import datetime, timedelta
from sqlmodel import SQLModel, Field, Relationship
from typing import Optional, List

class Deck(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: Optional[str] = None
    cards: List["Card"] = Relationship(back_populates="deck")

class Card(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    deck_id: int = Field(foreign_key="deck.id")
    tag: str = "general"
    question: str
    answer: str

    # spaced repetition fields
    ease: float = 2.5        # for future SM-2-lite
    interval_min: int = 0    # minutes
    due_at: datetime = Field(default_factory=lambda: datetime.utcnow())

    last_result: Optional[str] = None   # "again" | "good" | "easy"
    wrong_count: int = 0
    right_count: int = 0

    deck: Optional[Deck] = Relationship(back_populates="cards")

def next_due_time(result: str) -> datetime:
    now = datetime.utcnow()
    if result == "again":
        return now + timedelta(minutes=1)
    if result == "good":
        return now + timedelta(minutes=10)
    if result == "easy":
        return now + timedelta(days=1)
    return now + timedelta(minutes=5)
