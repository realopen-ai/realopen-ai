import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


class CardInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    front: str = Field(min_length=1, max_length=2000)
    back: str = Field(min_length=1, max_length=4000)
    source_reference: str | None = Field(default=None, max_length=500)

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
        fronts = [normalized(card.front) for card in cards]
        if len(set(fronts)) != len(fronts):
            raise ValueError("Duplicate card questions are not allowed")
        return cards


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: Literal["again", "hard", "good", "easy"]
    review_id: uuid.UUID  # Idempotency key: retries never schedule a second review.
    expected_reviews: int = Field(ge=0)
