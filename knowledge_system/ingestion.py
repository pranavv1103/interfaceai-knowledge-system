from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from .contracts import PageRecord, SnapshotRecord

LOGGER = logging.getLogger(__name__)


def _read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def to_snapshot(snapshot: Any) -> SnapshotRecord:
    snapshot_id = _read(snapshot, "snapshot_id")
    taken_on = _read(snapshot, "taken_on")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("snapshot_id must be a non-empty string")
    if not isinstance(taken_on, str):
        raise ValueError("taken_on must be an ISO-8601 date")
    try:
        date.fromisoformat(taken_on)
    except ValueError as exc:
        raise ValueError(f"invalid snapshot date {taken_on!r}") from exc

    raw_pages = _read(snapshot, "pages")
    if not isinstance(raw_pages, (list, tuple)):
        raise ValueError("snapshot.pages must be a list or tuple")

    pages = []
    for index, raw_page in enumerate(raw_pages):
        url = _read(raw_page, "url")
        markdown = _read(raw_page, "markdown")
        title = _read(raw_page, "title", "")
        fetched_at = _read(raw_page, "fetched_at", taken_on)
        if not isinstance(url, str) or not url.strip():
            LOGGER.warning("Skipping page %d in %s: missing URL", index, snapshot_id)
            continue
        if not isinstance(markdown, str) or not markdown.strip():
            LOGGER.warning("Skipping page %s in %s: missing text", url, snapshot_id)
            continue
        if not isinstance(title, str):
            title = str(title)
        if not isinstance(fetched_at, str):
            fetched_at = taken_on
        pages.append(
            PageRecord(
                url=url.strip(),
                title=title.strip(),
                markdown=markdown,
                fetched_at=fetched_at,
                snapshot_id=snapshot_id.strip(),
                taken_on=taken_on,
            )
        )

    LOGGER.info("Ingested snapshot %s dated %s with %d usable pages",
                snapshot_id, taken_on, len(pages))
    return SnapshotRecord(snapshot_id.strip(), taken_on, tuple(pages))


def select_snapshot(
    snapshots: list[SnapshotRecord], as_of: Optional[str]
) -> Optional[SnapshotRecord]:
    if not snapshots:
        return None
    ordered = sorted(snapshots, key=lambda item: (item.taken_on, item.snapshot_id))
    if as_of is None:
        return ordered[-1]
    try:
        requested = date.fromisoformat(as_of)
    except (TypeError, ValueError):
        return None
    eligible = [item for item in ordered if date.fromisoformat(item.taken_on) <= requested]
    return eligible[-1] if eligible else None
