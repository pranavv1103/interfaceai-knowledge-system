from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from .contracts import Conflict, PageRecord, SnapshotRecord, Source
from .retrieval import chunks_for_page, tokenize

PERCENT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*%")
METRIC_RE = re.compile(r"\b(APY|APR|dividend rate|annual percentage yield)\b", re.I)
IGNORE_TOKENS = {
    "as", "are", "and", "at", "be", "by", "effective", "for", "from", "is",
    "of", "on", "or", "per", "rate", "rates", "the", "this", "to", "with",
}


def _canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _fact_key(text: str) -> tuple[str, str]:
    metric_matches = list(METRIC_RE.finditer(text))
    metric = metric_matches[-1].group(1).lower() if metric_matches else ""
    masked = PERCENT_RE.sub("<percent>", text.lower())
    tokens = [
        token for token in tokenize(masked)
        if token not in IGNORE_TOKENS and token != "percent"
    ]
    return metric, " ".join(tokens)


def _authority(page: PageRecord) -> int:
    url = page.url.lower()
    if any(token in url for token in ("/learn/blog/", "/newsroom/", "calculator", "switch-and-earn")):
        return 0
    if any(token in url for token in ("/rates", "/personal/savings", "/personal/loans", "/credit-cards")):
        return 2
    return 1


def find_conflicts(snapshots: Iterable[SnapshotRecord]) -> list[Conflict]:
    observations: dict[
        tuple[str, str, str],
        list[tuple[str, PageRecord, str, str]],
    ] = defaultdict(list)

    for snapshot in snapshots:
        for page in snapshot.pages:
            for chunk in chunks_for_page(page):
                percentages = PERCENT_RE.findall(chunk.text)
                if len(percentages) != 1:
                    continue
                metric, fact = _fact_key(chunk.text)
                if not metric or not fact:
                    continue
                key = (_canonical_url(page.url), metric, fact)
                observations[key].append(
                    (snapshot.snapshot_id, page, chunk.text, percentages[0].replace(" ", ""))
                )

    conflicts = []
    for (_, metric, fact), entries in observations.items():
        by_value: dict[str, list[tuple[str, PageRecord, str, str]]] = defaultdict(list)
        for entry in entries:
            by_value[entry[3]].append(entry)
        if len(by_value) < 2:
            continue
        if len({entry[1].taken_on for entry in entries}) < 2:
            continue

        latest_by_value = {
            value: max(
                matches,
                key=lambda entry: (entry[1].taken_on, _authority(entry[1]), entry[1].url),
            )
            for value, matches in by_value.items()
        }
        values = sorted(
            by_value,
            key=lambda value: (
                latest_by_value[value][1].taken_on,
                value,
            ),
        )
        resolution = max(
            values,
            key=lambda value: (
                latest_by_value[value][1].taken_on,
                _authority(latest_by_value[value][1]),
            ),
        )
        sources = []
        for value in values:
            snapshot_id, page, text, _ = latest_by_value[value]
            sources.append(Source(page.url, snapshot_id, text[:240]))

        topic_tokens = [token for token in fact.split() if token not in IGNORE_TOKENS]
        topic = " ".join(topic_tokens[:9]).strip().capitalize() or metric.upper()
        conflicts.append(
            Conflict(
                topic=topic,
                values=values,
                sources=sources,
                resolution=resolution,
                reason=(
                    "This same rate statement changed across snapshots; the latest dated "
                    "version is selected as current."
                ),
            )
        )

    conflicts.sort(key=lambda item: (item.topic.lower(), item.values))
    return conflicts
