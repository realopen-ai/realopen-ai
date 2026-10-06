"""SM-2-based scheduling, adapted to four ratings and a 10-minute Again step.

Successful reviews use SM-2's 1/6 day intervals, ease floor and ease update.
Rating mapping: Again=2, Hard=3, Good=4, Easy=5. No randomness or LLM calls.
Reference: https://www.super-memory.org/archive/english/ol/sm2.htm
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import math


@dataclass(frozen=True)
class Schedule:
    due_at: datetime
    interval_days: int
    ease: float
    repetitions: int
    lapses: int


def schedule_review(
    *,
    rating: str,
    now: datetime,
    interval_days: int = 0,
    ease: float = 2.5,
    repetitions: int = 0,
    lapses: int = 0,
) -> Schedule:
    grades = {"again": 2, "hard": 3, "good": 4, "easy": 5}
    if rating not in grades:
        raise ValueError("Invalid review rating")
    grade = grades[rating]
    new_ease = max(1.3, ease + 0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    if rating == "again":
        return Schedule(now + timedelta(minutes=10), 0, new_ease, 0, lapses + 1)
    interval = 1 if repetitions == 0 else 6 if repetitions == 1 else math.ceil(interval_days * ease)
    interval = min(36500, max(1, interval))
    return Schedule(now + timedelta(days=interval), interval, new_ease, repetitions + 1, lapses)
