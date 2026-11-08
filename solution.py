"""Challenge-compatible entry point."""

from __future__ import annotations

from dataclasses import asdict

from knowledge_system import KnowledgeEngine


class Submission:
    def __init__(self) -> None:
        self._engine = KnowledgeEngine()

    def ingest(self, snapshot: object) -> None:
        self._engine.ingest(snapshot)

    def answer(self, question: str, as_of: str | None = None) -> object:
        result = self._engine.answer(question, as_of)
        try:
            from contract import Answer as ContractAnswer, Source as ContractSource
        except ImportError:
            return asdict(result)
        return ContractAnswer(
            text=result.text,
            sources=[
                ContractSource(source.url, source.snapshot_id, source.quote)
                for source in result.sources
            ],
            confident=result.confident,
            conflict_note=result.conflict_note,
        )

    def conflicts(self) -> list[object]:
        return self._engine.conflicts()


def build() -> Submission:
    return Submission()
