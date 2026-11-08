from __future__ import annotations

import re
from calendar import monthrange
from datetime import date
from typing import Optional

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def date_from_question(question: str) -> Optional[str]:
    lower = question.lower()
    match = re.search(
        r"\b(?:as of|in|during|by)\s+(" + "|".join(MONTHS) + r")\s+(20\d{2})\b",
        lower,
    )
    if not match:
        return None
    month = MONTHS[match.group(1)]
    year = int(match.group(2))
    return date(year, month, monthrange(year, month)[1]).isoformat()


def is_historical_question(question: str) -> bool:
    return bool(re.search(
        r"\b(as of|what was|when was|last year|in 20\d{2}|during 20\d{2})\b",
        question.lower(),
    ))
