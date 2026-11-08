from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

from .contracts import Chunk, PageRecord, RankedChunk, SnapshotRecord

TOKEN_RE = re.compile(
    r"[a-z]+(?:[-/][a-z0-9]+)*|\d[\d,]*(?:\.\d+)?(?:[-/]\d+)*(?:-[a-z]+)?%?",
    re.I,
)
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
TAG_RE = re.compile(r"<[^>]+>")
NUMBER_RE = re.compile(r"(?<![A-Za-z])\$?\d[\d,]*(?:\.\d+)?%?")

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "can", "could", "do", "does",
    "for", "from", "get", "give", "has", "have", "how", "i", "in", "is", "it",
    "me", "my", "of", "on", "or", "our", "please", "right", "tell", "that",
    "the", "their", "there", "this", "to", "us", "was", "what", "when", "where",
    "which", "who", "with", "would", "you", "your", "dupaco",
}

SYNONYMS = {
    "apy": {"yield", "annual", "percentage"},
    "apr": {"rate", "interest"},
    "atm": {"locator", "locations"},
    "branch": {"location", "office"},
    "branches": {"location", "office"},
    "certificate": {"cd"},
    "cd": {"certificate", "certificates"},
    "card": {"visa", "debit"},
    "checking": {"draft"},
    "fee": {"charge", "cost"},
    "hours": {"open", "close"},
    "lobby": {"branch"},
    "mortgage": {"home", "loan"},
    "rate": {"apr", "apy", "interest", "dividend"},
    "recognition": {"recognized", "recognize", "award"},
    "recognized": {"recognition", "award"},
    "routing": {"aba"},
    "savings": {"share", "deposit"},
}


def tokenize(text: str) -> tuple[str, ...]:
    values = []
    for raw in TOKEN_RE.findall(text.lower().replace("–", "-").replace("—", "-")):
        token = raw.replace(",", "")
        if token not in STOP_WORDS and len(token) > 1:
            values.append(token)
            if "-" in token and not token.endswith("%"):
                values.extend(part for part in token.split("-") if len(part) > 1)
            if token.endswith(("ches", "shes", "xes", "zes")) and len(token) > 4:
                values.append(token[:-2])
            elif token.endswith("ies") and len(token) > 4:
                values.append(token[:-3] + "y")
            elif token.endswith("s") and len(token) > 3:
                values.append(token[:-1])
    return tuple(values)


def _clean_markdown(text: str) -> str:
    text = IMAGE_RE.sub(r"\1", text)
    text = LINK_RE.sub(r"\1 (\2)", text)
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"[*_`>#~]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _table_rows(lines: list[str], page: PageRecord, heading: str) -> list[Chunk]:
    rows = [line.strip() for line in lines if line.strip() and not re.fullmatch(r"\|?[\s:|-]+\|?", line.strip())]
    if not rows:
        return []
    header = rows[0]
    output = []
    for row in rows[1:]:
        text = " | ".join(part for part in (heading, header, row) if part)
        clean = _clean_markdown(text)
        if len(tokenize(clean)) >= 2:
            output.append(Chunk(page, clean, heading, tokenize(clean)))
    if len(rows) == 1:
        clean = _clean_markdown(" | ".join(part for part in (heading, rows[0]) if part))
        if tokenize(clean):
            output.append(Chunk(page, clean, heading, tokenize(clean)))
    return output


def _schedule_chunks(page: PageRecord) -> list[Chunk]:
    output = []
    section = None
    entries: list[str] = []
    day_line = re.compile(
        r"^\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
        r"(?:\s*[-–]\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))?\s*:",
        re.I,
    )

    def flush() -> None:
        if section and len(entries) > 1:
            text = _clean_markdown(
                f"{page.title} {section} hours: {' '.join(entries)}"
            )
            tokens = tokenize(text)
            if tokens:
                output.append(Chunk(page, text, f"{section} hours", tokens))
        entries.clear()

    for line in page.markdown.splitlines():
        section_match = re.match(
            r"^\s*\*\*(Lobby|Drive[- ]?up)\*\*\s*(.*)$", line, re.I
        )
        if section_match:
            flush()
            section = "Drive-up" if section_match.group(1).lower().startswith("drive") else "Lobby"
            tail = section_match.group(2).strip()
            if day_line.match(tail):
                entries.append(tail)
            continue
        if section and not line.strip():
            continue
        if section and day_line.match(line):
            entries.append(line.strip())
            continue
        if section:
            flush()
            section = None
    flush()
    return output


def chunks_for_page(page: PageRecord) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []
    paragraph: list[str] = []
    table: list[str] = []
    current_heading = ""

    def flush_paragraph() -> None:
        if not paragraph:
            return
        text = "\n".join(paragraph).strip()
        paragraph.clear()
        if not text:
            return
        clean = _clean_markdown(" ".join(part for part in (current_heading, text) if part))
        if len(clean) > 1800:
            sentences = re.split(r"(?<=[.!?])\s+", clean)
            current = ""
            for sentence in sentences:
                if current and len(current) + len(sentence) > 1200:
                    tokens = tokenize(current)
                    if tokens:
                        chunks.append(Chunk(page, current, current_heading, tokens))
                    current = sentence
                else:
                    current = f"{current} {sentence}".strip()
            if current:
                tokens = tokenize(current)
                if tokens:
                    chunks.append(Chunk(page, current, current_heading, tokens))
            return
        tokens = tokenize(clean)
        if tokens:
            chunks.append(Chunk(page, clean, current_heading, tokens))

    def flush_table() -> None:
        if table:
            chunks.extend(_table_rows(table, page, current_heading))
            table.clear()

    for line in page.markdown.splitlines():
        heading_match = re.match(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading_match:
            flush_paragraph()
            flush_table()
            level = len(heading_match.group(1))
            title = _clean_markdown(heading_match.group(2))
            heading_stack = [(depth, value) for depth, value in heading_stack if depth < level]
            heading_stack.append((level, title))
            current_heading = " > ".join(value for _, value in heading_stack)
            continue
        if line.strip().startswith("|"):
            flush_paragraph()
            table.append(line)
            continue
        flush_table()
        if line.strip():
            paragraph.append(line.strip())
        else:
            flush_paragraph()
    flush_paragraph()
    flush_table()
    chunks.extend(_schedule_chunks(page))
    return chunks


def _expanded_query(question: str) -> Counter[str]:
    query = Counter(tokenize(question))
    for token in tuple(query):
        for synonym in SYNONYMS.get(token, ()):
            query[synonym] += 0.35 * query[token]
    return query


def _source_prior(page: PageRecord, question: str) -> float:
    url = page.url.lower()
    title = page.title.lower()
    query = question.lower()
    prior = 0.0

    rate_question = any(term in query for term in ("rate", "apy", "apr", "interest", "dividend"))
    if rate_question:
        primary_rate_page = any(path in url for path in (
            "/rates", "/personal/savings", "/personal/loans/", "/personal/checking",
            "/personal/credit-cards", "/business-rates", "/business/credit-cards",
            "/checking",
        ))
        if primary_rate_page:
            prior += 3.0
        elif any(path in url for path in ("/learn/blog/", "/newsroom/", "/press/")):
            prior -= 4.5
        elif any(word in url for word in ("calculator", "switch-and-earn")):
            prior -= 5.0
        if "rate" in title or "rate" in url or "rates" in url:
            prior += 0.7
    if any(term in query for term in ("branch", "lobby", "drive-up", "location")):
        if "/contact" in url or url.rstrip("/").endswith("dupaco.com"):
            prior += 1.6
    if any(term in query for term in ("credit card", "visa", "card apr")):
        if "credit-cards" in url:
            prior += 1.2
    if any(term in query for term in ("mortgage", "home loan", "30-year", "adjustable")):
        if "home-loans" in url:
            prior += 1.4
    if any(term in query for term in ("auto loan", "vehicle", "model-year", "motorcycle")):
        if "auto-motorcycle-loans" in url:
            prior += 1.4
    return prior


def rank_chunks(
    snapshot: SnapshotRecord, question: str, limit: int = 30
) -> list[RankedChunk]:
    return SearchIndex(snapshot).rank(question, limit)


class SearchIndex:
    def __init__(self, snapshot: SnapshotRecord) -> None:
        self.chunks = [
            chunk for page in snapshot.pages for chunk in chunks_for_page(page)
        ]
        self.document_frequencies = Counter()
        self.frequencies = []
        total_length = 0
        for chunk in self.chunks:
            frequency = Counter(chunk.tokens)
            self.frequencies.append(frequency)
            total_length += len(chunk.tokens)
            self.document_frequencies.update(frequency.keys())
        self.average_length = total_length / len(self.chunks) if self.chunks else 1.0

    def rank(self, question: str, limit: int = 30) -> list[RankedChunk]:
        if not self.chunks:
            return []
        query = _expanded_query(question)
        if not query:
            return []
        query_terms = set(query)
        rate_question = (
            any(term in question.lower() for term in ("rate", "apy", "apr", "interest", "dividend"))
            and not re.search(r"\b(?:url|link|website address)\b", question, re.I)
        )
        primary_rate_candidates = (
            [
                (chunk, frequency)
                for chunk, frequency in zip(self.chunks, self.frequencies)
                if any(path in chunk.page.url.lower() for path in (
                    "/rates", "/personal/savings", "/personal/loans/",
                    "/personal/checking", "/personal/credit-cards",
                    "/business-rates", "/business/credit-cards", "/checking",
                ))
                and has_rate_evidence(chunk.text)
            ]
            if rate_question else list(zip(self.chunks, self.frequencies))
        )
        chunks = [item[0] for item in primary_rate_candidates]
        frequencies = [item[1] for item in primary_rate_candidates]
        ranked: list[RankedChunk] = []
        for chunk, frequency in zip(chunks, frequencies):
            length = len(chunk.tokens)
            score = 0.0
            matched = 0
            for term, query_weight in query.items():
                tf = frequency.get(term, 0)
                if not tf:
                    continue
                matched += 1
                df = self.document_frequencies[term]
                idf = math.log(1 + (len(self.chunks) - df + 0.5) / (df + 0.5))
                denominator = tf + 1.2 * (
                    1 - 0.75 + 0.75 * length / max(self.average_length, 1)
                )
                score += query_weight * idf * tf * 2.2 / denominator
            if score <= 0:
                continue

            title_tokens = set(tokenize(f"{chunk.page.title} {chunk.page.url}"))
            score += 0.28 * len(query_terms.intersection(title_tokens))
            score += _source_prior(chunk.page, question)
            if len(query_terms) > 1:
                coverage = len(query_terms.intersection(frequency)) / len(query_terms)
                score += coverage * 0.9
            if "how many" in question.lower() and "branch" in question.lower():
                if "branch office locations" in chunk.text.lower():
                    score += 15.0
                elif "0 branches" in chunk.text.lower():
                    score -= 15.0
            ranked.append(RankedChunk(chunk, score, matched))

        ranked.sort(
            key=lambda item: (
                -item.score,
                item.chunk.page.url,
                item.chunk.heading,
                item.chunk.text,
            )
        )
        return ranked[:limit]


def answer_text(chunk: Chunk) -> str:
    text = _clean_markdown(chunk.text)
    text = re.sub(r"\s*\|\s*", " | ", text)
    return text[:700].strip()


def has_numeric_value(text: str) -> bool:
    return bool(NUMBER_RE.search(text))


def has_rate_evidence(text: str) -> bool:
    if not re.search(r"\d[\d,]*(?:\.\d+)?\s*%", text):
        return False
    metric = r"(?:APR|APY|dividend rate|annual percentage yield)"
    if "|" in text and re.search(metric, text, re.I):
        return True
    return bool(
        re.search(rf"\d[\d,]*(?:\.\d+)?\s*%\s*{metric}\b", text, re.I)
        or re.search(
            rf"{metric}\b\s*(?:is|of|at|:|=|as low as|up to|from)\s*"
            r"\d[\d,]*(?:\.\d+)?\s*%",
            text,
            re.I,
        )
    )


def required_qualifiers(question: str) -> tuple[str, ...]:
    tokens = tokenize(question)
    return tuple(
        token for token in tokens
        if re.fullmatch(r"\d+(?:-\d+)*(?:-\w+|/\d+)?", token)
        and (("-" in token or "/" in token) or re.fullmatch(r"20\d{2}", token))
    )


def contains_qualifiers(question: str, evidence: Iterable[RankedChunk]) -> bool:
    required = required_qualifiers(question)
    if not required:
        return True
    text = " ".join(item.chunk.text.lower().replace("–", "-").replace("—", "-") for item in evidence)
    for token in required:
        if re.fullmatch(r"20\d{2}", token):
            if not re.search(r"\b(?:model year|vehicle model|model-year)\b", question, re.I):
                continue
            year = int(token)
            range_match = re.search(r"\b(20\d{2})\s*[-–]\s*(\d{2}|20\d{2})\b", text)
            if range_match:
                start = int(range_match.group(1))
                end_text = range_match.group(2)
                end = int(end_text) if len(end_text) == 4 else (start // 100) * 100 + int(end_text)
                if start <= year <= end:
                    continue
        if re.fullmatch(r"\d+-month", token):
            months = token.split("-", 1)[0]
            if re.search(rf"\b{months}[\s-]+months?\b", text):
                continue
        if re.fullmatch(r"\d+-year", token):
            years = token.split("-", 1)[0]
            if re.search(rf"\b{years}[\s-]+years?\b", text):
                continue
        if token not in " ".join(tokenize(text)):
            return False
    return True
