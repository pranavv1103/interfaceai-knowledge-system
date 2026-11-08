from __future__ import annotations

import unittest

from knowledge_system.contracts import Chunk, PageRecord, RankedChunk
from knowledge_system.engine import ABSTENTION, KnowledgeEngine, _topic_candidates
from knowledge_system.ingestion import to_snapshot
from knowledge_system.retrieval import (
    chunks_for_page,
    contains_qualifiers,
    has_rate_evidence,
    tokenize,
)


def make_snapshot(
    snapshot_id: str,
    taken_on: str,
    pages: list[dict[str, str]],
) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "taken_on": taken_on,
        "pages": [
            {
                "url": page["url"],
                "title": page.get("title", ""),
                "markdown": page["markdown"],
                "fetched_at": taken_on,
            }
            for page in pages
        ],
    }


class KnowledgeSystemTests(unittest.TestCase):
    def test_selects_current_and_historical_values_with_citations(self) -> None:
        engine = KnowledgeEngine()
        url = "https://example.test/fees"
        engine.ingest(
            make_snapshot(
                "baseline",
                "2026-01-05",
                [{"url": url, "markdown": "# Fees\nDebit card replacement fee: $2.50."}],
            )
        )
        engine.ingest(
            make_snapshot(
                "update",
                "2026-03-01",
                [{"url": url, "markdown": "# Fees\nDebit card replacement fee: $5.00."}],
            )
        )

        old_answer = engine.answer(
            "What is the debit card replacement fee?", as_of="2026-01-15"
        )
        current_answer = engine.answer("What is the debit card replacement fee?")

        self.assertIn("$2.50", old_answer.text)
        self.assertEqual("baseline", old_answer.sources[0].snapshot_id)
        self.assertIn("$5.00", current_answer.text)
        self.assertEqual(url, current_answer.sources[0].url)
        self.assertIn("$5.00", current_answer.sources[0].quote or "")

    def test_carries_forward_pages_omitted_from_later_snapshots(self) -> None:
        engine = KnowledgeEngine()
        url = "https://example.test/fees"
        engine.ingest(
            make_snapshot(
                "baseline",
                "2026-01-05",
                [{"url": url, "markdown": "# Fees\nLost card fee: $2.50."}],
            )
        )
        engine.ingest(make_snapshot("recrawl", "2026-03-01", []))

        answer = engine.answer("What is the lost card fee?")

        self.assertIn("$2.50", answer.text)
        self.assertEqual("baseline", answer.sources[0].snapshot_id)

    def test_detects_a_changed_rate_and_selects_the_latest_value(self) -> None:
        engine = KnowledgeEngine()
        url = "https://example.test/certificates"
        for snapshot_id, taken_on, rate in (
            ("baseline", "2026-01-05", "3.50%"),
            ("update", "2026-03-01", "4.25%"),
        ):
            engine.ingest(
                make_snapshot(
                    snapshot_id,
                    taken_on,
                    [{
                        "url": url,
                        "markdown": f"# Certificate Rates\n17-month certificate APY is {rate} APY.",
                    }],
                )
            )

        conflicts = engine.conflicts()

        self.assertEqual(1, len(conflicts))
        self.assertIn("3.50%", conflicts[0].values)
        self.assertIn("4.25%", conflicts[0].values)
        self.assertEqual("4.25%", conflicts[0].resolution)

    def test_avoids_mistaking_a_loan_to_value_percentage_for_an_apr(self) -> None:
        self.assertFalse(
            has_rate_evidence("HELOC APR is variable based on 75% loan-to-value.")
        )
        self.assertTrue(has_rate_evidence("The quoted rate is 6.25% APR."))
        self.assertTrue(
            has_rate_evidence("| Loan | Rate | APR |\n| HELOC | 6.25% | 6.30% |")
        )

    def test_applies_model_year_and_term_qualifiers(self) -> None:
        text = "2024-27 Model Year | 60 Months | 5.39% APR"
        page = PageRecord(
            "https://example.test/auto",
            "Auto rates",
            text,
            "2026-09-28",
            "latest",
            "2026-09-28",
        )
        chunk = Chunk(page, text, "", tokenize(text))
        evidence = [RankedChunk(chunk, 5.0, 3)]

        self.assertTrue(
            contains_qualifiers(
                "What's the APR for a 60-month auto loan on a 2025 model-year vehicle?",
                evidence,
            )
        )
        self.assertFalse(
            contains_qualifiers(
                "What's the APR for a 60-month auto loan on a 2020 model-year vehicle?",
                evidence,
            )
        )

    def test_groups_branch_schedule_lines_without_mixing_service_types(self) -> None:
        page = PageRecord(
            "https://example.test/branch",
            "Branch hours",
            (
                "**Lobby** Monday-Thursday: 8:30 a.m.-5 p.m.\n\n"
                "Friday: 8:30 a.m.-6 p.m.\n\n"
                "Saturday: 8:30 a.m.-12:30 p.m.\n\n"
                "Sunday: Closed\n\n"
                "**Drive-up** Monday-Friday: 8 a.m.-6 p.m.\n\n"
                "Saturday: 9 a.m.-noon\n\n"
                "Sunday: ATM only"
            ),
            "2026-09-28",
            "latest",
            "2026-09-28",
        )

        chunks = chunks_for_page(page)
        lobby = next(chunk for chunk in chunks if chunk.heading == "Lobby hours")
        drive_up = next(chunk for chunk in chunks if chunk.heading == "Drive-up hours")

        self.assertIn("Saturday: 8:30 a.m.-12:30 p.m.", lobby.text)
        self.assertNotIn("Drive-up", lobby.text)
        self.assertIn("Saturday: 9 a.m.-noon", drive_up.text)

    def test_selects_distinct_lobby_schedules_for_comparison_questions(self) -> None:
        schedules = []
        for url, text, score in (
            (
                "https://example.test/branch-a",
                "Branch A Lobby hours Monday-Friday: 9 a.m.-5 p.m. "
                "Saturday: 9 a.m.-noon Sunday: Closed",
                10.0,
            ),
            (
                "https://example.test/branch-b",
                "Branch B Lobby hours Monday-Friday: 8:30 a.m.-5 p.m. "
                "Saturday: 8:30 a.m.-12:30 p.m. Sunday: Closed",
                9.0,
            ),
        ):
            page = PageRecord(url, "Branch hours", text, "", "latest", "2026-09-28")
            chunk = Chunk(page, text, "Lobby hours", tokenize(text))
            schedules.append(RankedChunk(chunk, score, 4))

        selected = _topic_candidates(
            "Do all Dupaco branches keep the same lobby hours?", schedules
        )

        self.assertEqual(2, len(selected))
        self.assertNotEqual(
            selected[0].chunk.page.url,
            selected[1].chunk.page.url,
        )
        self.assertNotEqual(
            selected[0].chunk.text.split("Saturday:", 1)[1].split("Sunday:", 1)[0],
            selected[1].chunk.text.split("Saturday:", 1)[1].split("Sunday:", 1)[0],
        )

    def test_abstains_when_a_construction_rate_is_not_published(self) -> None:
        engine = KnowledgeEngine()
        engine.ingest(
            make_snapshot(
                "latest",
                "2026-09-28",
                [{
                    "url": "https://www.dupaco.com/personal/loans/home-loans",
                    "markdown": (
                        "# Construction Loans\n"
                        "Construction loans are available. Contact a mortgage lender."
                    ),
                }],
            )
        )

        answer = engine.answer("What interest rate do you charge on a construction loan?")

        self.assertEqual(ABSTENTION, answer.text)
        self.assertFalse(answer.confident)
        self.assertEqual([], answer.sources)

    def test_broad_card_rate_questions_include_personal_and_business_cards(self) -> None:
        engine = KnowledgeEngine()
        engine.ingest(
            make_snapshot(
                "latest",
                "2026-09-28",
                [
                    {
                        "url": "https://www.dupaco.com/personal/credit-cards",
                        "markdown": (
                            "# Personal Credit Cards\n"
                            "Platinum Visa has an APR of 12.50%. "
                            "Rewards Visa has an APR of 15.50%."
                        ),
                    },
                    {
                        "url": "https://www.dupaco.com/business/credit-cards",
                        "markdown": (
                            "# Business Credit Cards\n"
                            "Business Platinum Visa has an APR of 15.50%."
                        ),
                    },
                ],
            )
        )

        answer = engine.answer("I would like to know the rates for the credit cards")

        self.assertIn("12.50%", answer.text)
        self.assertIn("15.50%", answer.text)
        self.assertEqual(2, len(answer.sources))

    def test_branch_member_question_retrieves_the_closure_notice(self) -> None:
        engine = KnowledgeEngine()
        engine.ingest(
            make_snapshot(
                "latest",
                "2026-09-28",
                [{
                    "url": "https://www.dupaco.com/newsroom/hy-vee-branch-closure",
                    "markdown": (
                        "# Hy-Vee branch closure\n"
                        "Hy-Vee branch members can use full-service branches at "
                        "Key West and Sycamore Street."
                    ),
                }],
            )
        )

        answer = engine.answer(
            "Where can Hy-Vee branch members go for full-service banking?"
        )

        self.assertIn("Key West", answer.text)
        self.assertIn("Sycamore Street", answer.text)
        self.assertTrue(answer.sources)

    def test_rejects_malformed_snapshot_dates_and_skips_invalid_pages(self) -> None:
        with self.assertRaises(ValueError):
            to_snapshot({"snapshot_id": "bad", "taken_on": "2026-02-31", "pages": []})

        snapshot = to_snapshot(
            {
                "snapshot_id": "partial",
                "taken_on": "2026-09-28",
                "pages": [
                    {"url": "", "markdown": "missing URL"},
                    {"url": "https://example.test/page", "markdown": "# Valid\nText."},
                    {"url": "https://example.test/empty", "markdown": ""},
                ],
            }
        )
        self.assertEqual(1, len(snapshot.pages))
        self.assertEqual("https://example.test/page", snapshot.pages[0].url)


if __name__ == "__main__":
    unittest.main()
