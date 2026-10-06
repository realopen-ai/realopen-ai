import re
import uuid
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def similar_question(a: str, b: str) -> bool:
    a, b = normalized(a), normalized(b)
    # Ignore Markdown decoration/punctuation, but preserve numbers and operators.
    a, b = re.sub(r"[*_`?!.,]", "", a), re.sub(r"[*_`?!.,]", "", b)
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    return a == b or (min(len(a), len(b)) >= 25 and SequenceMatcher(None, a, b).ratio() >= 0.94)


class CardInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    front: str = Field(min_length=1, max_length=2000)
    back: str = Field(min_length=1, max_length=4000)
    source_reference: str | None = Field(default=None, max_length=500)
    source_page: int | None = Field(default=None, ge=1)
    source_chunk_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def distinct_sides(self):
        if normalized(self.front) == normalized(self.back):
            raise ValueError("Front and back must be different")
        return self


class DeckMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class DeckInput(DeckMetadata):
    cards: list[CardInput] = Field(default_factory=list, max_length=100)
    source_conversation_id: uuid.UUID | None = None
    source_document_id: uuid.UUID | None = None

    @field_validator("cards")
    @classmethod
    def unique_cards(cls, cards):
        for index, card in enumerate(cards):
            if any(similar_question(card.front, other.front) for other in cards[:index]):
                raise ValueError("Duplicate or near-duplicate card questions are not allowed")
        return cards


class CardRewrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card: CardInput
    action: Literal["shorter", "harder", "recall", "split", "correct"]


class CardReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cards: list[CardInput] = Field(min_length=1, max_length=2)


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: Literal["again", "hard", "good", "easy"]
    review_id: uuid.UUID  # Idempotency key: retries never schedule a second review.
    expected_reviews: int = Field(ge=0)
