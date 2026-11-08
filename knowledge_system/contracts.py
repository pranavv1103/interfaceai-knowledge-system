from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Source:
    url: str
    snapshot_id: Optional[str] = None
    quote: Optional[str] = None


@dataclass
class Answer:
    text: str
    sources: list[Source] = field(default_factory=list)
    confident: bool = True
    conflict_note: Optional[str] = None


@dataclass
class Conflict:
    topic: str
    values: list[str]
    sources: list[Source]
    resolution: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class PageRecord:
    url: str
    title: str
    markdown: str
    fetched_at: str
    snapshot_id: str
    taken_on: str


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: str
    taken_on: str
    pages: tuple[PageRecord, ...]


@dataclass(frozen=True)
class Chunk:
    page: PageRecord
    text: str
    heading: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class RankedChunk:
    chunk: Chunk
    score: float
    matched_terms: int
