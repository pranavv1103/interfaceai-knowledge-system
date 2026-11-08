from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date, datetime
from typing import Optional

from .conflicts import find_conflicts
from .contracts import Answer, Conflict, PageRecord, RankedChunk, SnapshotRecord, Source
from .ingestion import select_snapshot, to_snapshot
from .retrieval import (
    SearchIndex,
    answer_text,
    contains_qualifiers,
    has_rate_evidence,
    has_numeric_value,
)
from .temporal import date_from_question, is_historical_question

LOGGER = logging.getLogger(__name__)
ABSTENTION = "I don't know from the supplied website snapshots; please contact Dupaco to confirm."
ENTITY_STOP_WORDS = {
    "avenue", "bank", "branch", "can", "community", "credit", "do", "dupaco",
    "how", "i", "is", "street", "the", "what", "where", "which", "who",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "business", "personal",
    "apr", "aprs", "apy", "apys", "atm", "atms", "cd", "cds", "url",
}


def _is_numeric_question(question: str) -> bool:
    if re.search(r"\b(?:url|link|website address)\b", question, re.I):
        return False
    return bool(re.search(
        r"\b(rates?|aprs?|apys?|interest|dividend|fee|cost|charge|amount|how much|"
        r"how many|number|hours|time|address|routing number|phone number)\b",
        question.lower(),
    ))


def _is_rate_question(question: str) -> bool:
    if re.search(r"\b(?:url|link|website address)\b", question, re.I):
        return False
    return bool(re.search(r"\b(rates?|aprs?|apys?|interest|dividend)\b", question, re.I))


def _is_list_question(question: str) -> bool:
    text = question.lower()
    if "certificate" in text and not re.search(r"\b\d+\s*[- ]?\s*month", text):
        return True
    if "credit card" in text and re.search(r"\b(?:rates?|aprs?)\b", text):
        return True
    return any(phrase in text for phrase in (
        "what are", "list", "all ", "provide current", "what credit cards",
        "what rates", "which rates", "do any", "how many",
    ))


def _required_entities(question: str) -> set[str]:
    words = re.findall(r"\b[A-Z][A-Za-z]*(?:-[A-Z][A-Za-z]*)?\b", question)
    return {
        word.lower() for index, word in enumerate(words)
        if word.lower() not in ENTITY_STOP_WORDS and index > 0
    }


def _is_membership_question(question: str) -> bool:
    return bool(
        re.search(r"\b(?:eligible|membership|join)\b", question, re.I)
        or re.search(r"\bmember\b", question, re.I)
    )


def _publication_date(item: RankedChunk) -> Optional[date]:
    header = "\n".join(item.chunk.page.markdown.splitlines()[:20])
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},\s+20\d{2}\b",
        header,
        re.I,
    )
    if not match:
        return None
    try:
        return datetime.strptime(match.group(), "%B %d, %Y").date()
    except ValueError:
        return None


def _schedule_opening(item: RankedChunk) -> Optional[str]:
    match = re.search(
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
        r"(?:\s*[-–]\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))?"
        r"\s*:\s*(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|noon))",
        item.chunk.text,
        re.I,
    )
    return re.sub(r"\s+", " ", match.group(1).lower()) if match else None


def _saturday_hours(item: RankedChunk) -> Optional[str]:
    match = re.search(
        r"\bSaturday:\s*(.*?)(?=\s+Sunday:|$)",
        item.chunk.text,
        re.I,
    )
    if not match:
        return None
    hours = re.sub(r"\s+", " ", match.group(1).strip().lower())
    return None if not hours or "closed" in hours or "atm only" in hours else hours


def _topic_candidates(question: str, ranked: list[RankedChunk]) -> list[RankedChunk]:
    query = question.lower()
    selected = ranked

    route = None
    if any(term in query for term in ("mobile wallet", "digital wallet", "mobile payment")):
        route = lambda url: "mobile-payments" in url
    elif re.search(r"\b(?:url|link|website address)\b", query) and any(
        term in query for term in ("branch", "atm", "locator")
    ):
        route = lambda url: "/contact" in url
    elif "galena" in query and any(term in query for term in ("branch", "address", "location")):
        route = lambda url: "/contact" in url
    elif "contact information" in query or "update my contact" in query:
        route = lambda url: "/contact" in url
    elif "credit card" in query or "credit cards" in query:
        if "business" in query:
            route = lambda url: "business/credit-cards" in url
        elif "personal" in query:
            route = lambda url: "personal/credit-cards" in url
        else:
            route = lambda url: "credit-cards" in url
    elif "construction" in query and "loan" in query:
        route = lambda url: "home-loans" in url
    elif any(term in query for term in ("mortgage", "home loan", "30-year", "adjustable-rate")):
        route = lambda url: "home-loans" in url
    elif any(term in query for term in ("auto loan", "vehicle", "model-year", "motorcycle")):
        route = lambda url: "auto-motorcycle-loans" in url
    elif "certificate" in query or re.search(r"\bcd\b", query):
        route = lambda url: "personal/savings-account" in url or url.rstrip("/").endswith("/rates")
    elif "checking" in query and _is_rate_question(question):
        route = lambda url: "checking" in url
    elif _is_membership_question(question):
        route = lambda url: "/about/join" in url
    elif "home$tart" in query or "hometart" in query:
        route = lambda url: "hometart" in url or "first-time-homebuyers" in url
    if route:
        routed = [item for item in selected if route(item.chunk.page.url.lower())]
        if routed:
            selected = routed

    if any(term in query for term in ("mobile wallet", "digital wallet", "mobile payment")):
        wallet = [
            item for item in selected
            if any(
                service in item.chunk.text.lower()
                for service in ("apple pay", "google pay", "samsung pay")
            )
        ]
        if wallet:
            selected = wallet

    if "construction" in query and "loan" in query:
        selected = [
            item for item in selected
            if "construction" in item.chunk.text.lower()
        ]

    if "home$tart" in query or "hometart" in query:
        relevant = [
            item for item in selected
            if "home$tart" in item.chunk.text.lower()
            or "hometart" in item.chunk.text.lower()
        ]
        grant_details = [
            item for item in relevant
            if "$" in item.chunk.text
            and "down payment" in item.chunk.text.lower()
            and "closing costs" in item.chunk.text.lower()
        ]
        selected = grant_details or relevant

    if _is_membership_question(question):
        states = [
            item for item in selected
            if all(state in item.chunk.text.lower() for state in ("iowa", "illinois", "wisconsin"))
        ]
        eligibility = [
            item for item in selected
            if "eligible to join" in item.chunk.text.lower()
            or "charter area" in item.chunk.text.lower()
        ]
        if states:
            selected = states + [item for item in eligibility if item not in states]

    if any(term in query for term in ("mortgage", "home loan", "30-year", "adjustable-rate")):
        if "construction" not in query and "rate" in query:
            purchase_rows = [
                item for item in selected
                if "purchase:" in item.chunk.text.lower()
                and "30-year fixed conventional" in item.chunk.text.lower()
            ]
            if "refinance" in query:
                purchase_rows = [
                    item for item in selected
                    if "refinance:" in item.chunk.text.lower()
                    and "30-year fixed conventional" in item.chunk.text.lower()
                ]
            if purchase_rows and not re.search(r"\$\s*[\d,]+|\b\d+\s*k\b", query):
                thresholds = []
                for item in purchase_rows:
                    match = re.search(r"\$([\d,]+)\s+or less", item.chunk.text, re.I)
                    if match:
                        thresholds.append((int(match.group(1).replace(",", "")), item))
                if thresholds:
                    minimum = min(amount for amount, _ in thresholds)
                    maximum = max(amount for amount, _ in thresholds)
                    selected = [
                        item for amount, item in thresholds if amount == minimum
                    ]
                    if maximum != minimum:
                        selected.extend(
                            item for amount, item in thresholds if amount == maximum
                        )

    if any(term in query for term in ("lobby hours", "branch hours", "drive-up hours", "lobby", "drive-up")):
        schedule_word = "drive-up" if "drive-up" in query else "lobby"
        schedules = [
            item for item in selected
            if schedule_word in item.chunk.text.lower()
            and re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|noon)\b", item.chunk.text, re.I)
        ]
        if schedules:
            schedule_pages = {item.chunk.page.url for item in schedules}
            selected = [
                item for item in selected
                if item.chunk.page.url in schedule_pages
                and re.search(
                    r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|noon)\b",
                    item.chunk.text,
                    re.I,
                )
                and not (
                    schedule_word == "lobby"
                    and "drive-up" in item.chunk.text.lower()
                )
            ]
            combined = [
                item for item in selected
                if schedule_word in item.chunk.heading.lower()
            ]
            if combined:
                selected = combined
            if not _required_entities(question):
                if schedule_word == "lobby" and "all " in query and "same" in query:
                    examples = []
                    saturday_hours = set()
                    for item in selected:
                        hours = _saturday_hours(item)
                        if hours and hours not in saturday_hours:
                            examples.append(item)
                            saturday_hours.add(hours)
                        if len(examples) == 2:
                            break
                    selected = examples or selected[:2]
                else:
                    openings = [
                        opening for item in selected
                        if (opening := _schedule_opening(item)) is not None
                    ]
                    if openings:
                        common_opening = Counter(openings).most_common(1)[0][0]
                        selected = [
                            item for item in selected
                            if _schedule_opening(item) == common_opening
                        ][:2] or selected[:2]
                    else:
                        selected = selected[:2]

    if "how many" in query and "branch" in query:
        count_evidence = [
            item for item in selected
            if "branch office locations" in item.chunk.text.lower()
            and has_numeric_value(item.chunk.text)
        ]
        if count_evidence:
            dated = [
                (published, item)
                for item in count_evidence
                if (published := _publication_date(item)) is not None
            ]
            if dated:
                latest = max(published for published, _ in dated)
                selected = [item for published, item in dated if published == latest]
            else:
                selected = count_evidence

    if "certificate" in query or re.search(r"\bcd\b", query):
        if re.search(r"\b(?:url|link|website address)\b", query):
            links = [
                item for item in selected
                if "certificate" in item.chunk.text.lower()
                and "http" in item.chunk.text.lower()
            ]
            if links:
                selected = links
        elif _is_rate_question(question) and not re.search(
            r"\b\d+\s*[- ]?\s*month", query
        ):
            certificate_rows = [
                item for item in selected
                if "certificate" in item.chunk.text.lower()
                and has_rate_evidence(item.chunk.text)
            ]
            if certificate_rows:
                selected = certificate_rows


    if "bright savings" in query:
        preferred = [item for item in selected if "bright savings" in item.chunk.text.lower()]
        if preferred:
            selected = preferred
    elif "savings" in query and not any(term in query for term in ("health savings", "holiday", "bright")):
        if "tier" in query or _is_rate_question(question):
            preferred = [
                item for item in selected
                if "share savings" in item.chunk.text.lower()
                and "health savings" not in item.chunk.text.lower()
            ]
            if preferred:
                selected = preferred
    return selected


def _dedupe_sources(chunks: list[RankedChunk]) -> list[Source]:
    sources = []
    seen = set()
    for item in chunks:
        page = item.chunk.page
        if page.url in seen:
            continue
        seen.add(page.url)
        sources.append(Source(page.url, page.snapshot_id, answer_text(item.chunk)[:240]))
    return sources


class KnowledgeEngine:
    def __init__(self) -> None:
        self._snapshots: dict[str, SnapshotRecord] = {}
        self._indexes: dict[str, SearchIndex] = {}
        self._effective_pages: dict[str, dict[str, PageRecord]] = {}

    def ingest(self, snapshot: object) -> None:
        record = to_snapshot(snapshot)
        existing = self._snapshots.get(record.snapshot_id)
        if existing and existing != record:
            raise ValueError(f"snapshot_id {record.snapshot_id!r} was already ingested with different content")
        if existing:
            return
        self._snapshots[record.snapshot_id] = record
        ordered = sorted(
            self._snapshots.values(),
            key=lambda item: (item.taken_on, item.snapshot_id),
        )
        start = next(index for index, item in enumerate(ordered) if item.snapshot_id == record.snapshot_id)
        if start:
            previous = ordered[start - 1]
            pages = dict(self._effective_pages[previous.snapshot_id])
        else:
            pages = {}
        for item in ordered[start:]:
            pages = dict(pages)
            for page in item.pages:
                pages[page.url] = page
            self._effective_pages[item.snapshot_id] = pages
            effective = SnapshotRecord(
                item.snapshot_id,
                item.taken_on,
                tuple(pages[url] for url in sorted(pages)),
            )
            self._indexes[item.snapshot_id] = SearchIndex(effective)

    def _selected_snapshot(self, question: str, as_of: Optional[str]) -> Optional[SnapshotRecord]:
        snapshots = list(self._snapshots.values())
        query_date = as_of or date_from_question(question)
        if query_date:
            try:
                date.fromisoformat(query_date)
            except ValueError:
                LOGGER.warning("Invalid as_of date %r; abstaining", query_date)
                return None
        return select_snapshot(snapshots, query_date)

    def answer(self, question: str, as_of: Optional[str] = None) -> Answer:
        if not isinstance(question, str) or not question.strip():
            return Answer(ABSTENTION, confident=False)
        snapshot = self._selected_snapshot(question, as_of)
        if snapshot is None:
            return Answer(ABSTENTION, confident=False)

        search_queries = [question]
        query = question.lower()
        if "contact information" in query or "update my contact" in query:
            search_queries.append(
                f"{question} Shine Online Mobile Banking account contact phone"
            )
        if any(term in query for term in ("mobile wallet", "digital wallet", "mobile payment")):
            search_queries.append(
                f"{question} Apple Pay Google Pay Samsung Pay mobile payments"
            )
        by_chunk: dict[tuple[str, str], RankedChunk] = {}
        for search_query in search_queries:
            for item in self._indexes[snapshot.snapshot_id].rank(search_query, limit=250):
                key = (item.chunk.page.url, item.chunk.text)
                if key not in by_chunk or by_chunk[key].score < item.score:
                    by_chunk[key] = item
        ranked = sorted(
            by_chunk.values(),
            key=lambda item: (-item.score, item.chunk.page.url, item.chunk.text),
        )
        if not ranked:
            return Answer(ABSTENTION, confident=False)

        ranked = _topic_candidates(question, ranked)
        if not ranked:
            return Answer(ABSTENTION, confident=False)

        is_historical = is_historical_question(question) or as_of is not None
        is_list = _is_list_question(question)
        is_rate = _is_rate_question(question)
        candidates = [
            item for item in ranked
            if has_numeric_value(item.chunk.text) or not _is_numeric_question(question)
        ]
        if is_rate:
            candidates = [
                item for item in candidates if has_rate_evidence(item.chunk.text)
            ]
        entities = _required_entities(question)
        if entities:
            entity_candidates = [
                item for item in candidates
                if all(entity in item.chunk.text.lower() for entity in entities)
            ]
            if entity_candidates:
                candidates = entity_candidates
            else:
                return Answer(ABSTENTION, confident=False)
        if not candidates:
            return Answer(ABSTENTION, confident=False)

        broad_card_list = (
            is_list
            and "credit card" in question.lower()
            and not any(term in question.lower() for term in ("business", "personal"))
        )
        qualified_candidates = [
            item for item in candidates if contains_qualifiers(question, [item])
        ]
        if qualified_candidates:
            candidates = qualified_candidates
        selected = candidates[:120 if broad_card_list else (40 if is_list else 10)]
        if not contains_qualifiers(question, selected):
            return Answer(ABSTENTION, confident=False)
        evidence_words = " ".join(
            item.chunk.text.lower() for item in selected[:12]
        )
        if any(entity not in evidence_words for entity in entities):
            return Answer(ABSTENTION, confident=False)

        best_score = selected[0].score
        if broad_card_list:
            evidence = [
                item for item in selected
                if has_rate_evidence(item.chunk.text)
            ]
        else:
            score_floor = max(
                best_score * (0.15 if is_list else 0.48),
                best_score - (12.0 if is_list else 3.2),
            )
            evidence = [item for item in selected if item.score >= score_floor]
        if not evidence or best_score < 1.0:
            return Answer(ABSTENTION, confident=False)

        if _is_numeric_question(question):
            if is_rate:
                evidence = [
                    item for item in evidence
                    if has_rate_evidence(item.chunk.text)
                ]
            else:
                evidence = [item for item in evidence if has_numeric_value(item.chunk.text)]
            if not evidence:
                return Answer(ABSTENTION, confident=False)

        broad_recognition_query = not is_list and any(
            term in query for term in ("recognition", "award", "honor", "honour")
        )
        if not is_list:
            evidence = evidence[:5 if broad_recognition_query else 3]
        answer_parts = []
        used_text = set()
        ordered_evidence = []
        seen_urls = set()
        if is_list:
            for item in evidence:
                url = item.chunk.page.url
                if url not in seen_urls:
                    seen_urls.add(url)
                    ordered_evidence.append(item)
            ordered_evidence.extend(item for item in evidence if item.chunk.page.url in seen_urls)
        else:
            ordered_evidence = evidence
        for item in ordered_evidence:
            if (
                "certificate" in question.lower()
                and _is_rate_question(question)
                and not re.search(r"\b\d+\s*[- ]?\s*month", question, re.I)
                and "|" in item.chunk.text
            ):
                cells = [part.strip() for part in item.chunk.text.split("|") if part.strip()]
                text = " | ".join(cells[-4:]) if len(cells) >= 8 else answer_text(item.chunk)
            else:
                text = answer_text(item.chunk)
            if text and text not in used_text:
                used_text.add(text)
                answer_parts.append(text)
            if len(answer_parts) >= (
                40 if "certificate" in question.lower() and is_list
                else (8 if is_list else (5 if broad_recognition_query else 3))
            ):
                break

        if not answer_parts:
            return Answer(ABSTENTION, confident=False)
        if (
            "certificate" in question.lower()
            and _is_rate_question(question)
            and not re.search(r"\b\d+\s*[- ]?\s*month", question, re.I)
        ):
            answer = "Certificate rates (dividend rate | APY | minimum): " + "; ".join(answer_parts)
        else:
            answer = " ".join(answer_parts)
        answer = answer[:2800]
        sources = _dedupe_sources(evidence)
        if any(term in question.lower() for term in ("lobby hours", "branch hours", "drive-up hours")) and not _required_entities(question):
            if "same" in question.lower():
                answer = "No, hours vary by branch. " + answer
            else:
                answer = "Hours vary by branch. " + answer
            contact_page = next(
                (
                    page for page in self._effective_pages[snapshot.snapshot_id].values()
                    if "/contact" in page.url.lower()
                    and "branch or ATM" in page.markdown
                ),
                None,
            )
            if contact_page and not any("/contact" in source.url for source in sources):
                sources.append(
                    Source(contact_page.url, contact_page.snapshot_id, "branch or ATM")
                )
            if contact_page:
                locator = "https://www.dupaco.com/contact#dupaco-locator-widget_1769206785258"
                answer += f" For exact hours, use the [branch and ATM locator]({locator})."
        if not sources:
            LOGGER.warning("Retrieved evidence without a citable source for question %r", question)
            return Answer(ABSTENTION, confident=False)

        conflict_note = None
        if is_historical:
            conflict_note = f"Evidence is from snapshot {snapshot.snapshot_id} ({snapshot.taken_on})."
        return Answer(answer, sources=sources[:5], confident=True, conflict_note=conflict_note)

    def conflicts(self) -> list[Conflict]:
        ordered = sorted(
            self._snapshots.values(),
            key=lambda item: (item.taken_on, item.snapshot_id),
        )
        return find_conflicts(ordered)
