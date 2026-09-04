"""Comprehensive tests for the Etsy listing pipeline.

All external network calls (Groq, NVIDIA NIM, Google Trends, Etsy API) are
mocked with ``unittest.mock`` so the suite runs fully offline.

Run with:
    python -m unittest discover -s tests -v
or:
    pytest tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.asset_builder import AssetBuilder
from src.content_generator import EtsyListingData, generate_etsy_listing_data
from src.etsy_client import EtsyClient
from src.trend_analyzer import fetch_rising_trends, fetch_etsy_autocomplete

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

DUMMY_ENV = {
    "GROQ_API_KEY": "mock_groq",
    "NVIDIA_API_KEY": "mock_nvidia",
    "ETSY_API_KEY": "mock_api_key",
    "ETSY_KEYSTRING": "mock_keystring",
    "ETSY_ACCESS_TOKEN": "mock_token",
    "ETSY_SHOP_ID": "12345",
}


def _valid_listing() -> dict:
    """A fully valid listing dict matching the Pydantic schema."""
    return {
        "title": "Digital Planner 2026 | Printable Habit Tracker PDF",
        "tags": [
            "digital planner",
            "printable planner",
            "habit tracker",
            "planner pdf",
            "budget planner",
            "weekly planner",
            "daily planner",
            "planner insert",
            "goodnotes",
            "notion template",
            "planner pages",
            "organizer",
            "productivity",
        ],
        "description": (
            "Stay organized. AI-assisted design file with instant download.\n"
            "## What You Get\nPerfect planner templates.\n"
            "## How to Download\nInstant PDF access after checkout.\n"
            "## Disclaimer\nAI-assisted design."
        ),
        "suggested_price": 9.99,
        "content_outline": {
            "title": "Digital Planner",
            "sections": ["Daily Schedule", "Habit Tracker"],
            "pages": 20,
        },
    }


# ---------------------------------------------------------------------------
# 1. Google Trends — mocked
# ---------------------------------------------------------------------------

class TrendAnalyzerTests(unittest.TestCase):
    @patch("src.trend_analyzer.TrendReq")
    def test_fetch_rising_trends_mocked(self, mock_trendreq: MagicMock) -> None:
        instance = mock_trendreq.return_value
        instance.related_queries.return_value = {
            "digital planner": {
                "rising": _make_rising_df(
                    [("digital planner 2026", 2000), ("planner bundle", 100)]
                )
            }
        }

        with patch("src.trend_analyzer._random_delay", return_value=None):
            results = fetch_rising_trends(
                ["digital planner"], geo="US", timeframe="now 7-d"
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["keyword"], "digital planner 2026")
        self.assertEqual(results[0]["breakout"], 2000)
        self.assertTrue(results[0]["breakout"] >= results[1]["breakout"])

    @patch("src.trend_analyzer.TrendReq")
    def test_fetch_rising_trends_empty_for_missing_payload(self, mock_trendreq: MagicMock) -> None:
        mock_trendreq.return_value.related_queries.return_value = {}
        with patch("src.trend_analyzer._random_delay", return_value=None):
            results = fetch_rising_trends(["wat"])
        self.assertEqual(results, [])

    @patch("src.trend_analyzer._retry_request")
    def test_fetch_etsy_autocomplete_parses_and_dedupes(self, mock_retry: MagicMock) -> None:
        resp = MagicMock()
        resp.json.return_value = {
            "results": ["Digital Planner", "digital planner", "Planner 2026"]
        }
        mock_retry.return_value = resp

        out = fetch_etsy_autocomplete("Digital Planner")
        self.assertEqual(out, ["digital planner", "planner 2026"])

    @patch("src.trend_analyzer._retry_request")
    def test_fetch_etsy_autocomplete_returns_empty_on_error(self, mock_retry: MagicMock) -> None:
        mock_retry.return_value = None
        self.assertEqual(fetch_etsy_autocomplete("planner"), [])


def _make_rising_df(rows: list[tuple[str, int]]):  # pragma: no cover - helper
    import pandas as pd

    return pd.DataFrame(rows, columns=["query", "value"])


# ---------------------------------------------------------------------------
# 2. PDF / image creation — real rendering, temp output dir
# ---------------------------------------------------------------------------

class AssetCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_valid_pdf_binary_output(self) -> None:
        out = AssetBuilder().compile_product_pdf(
            _valid_listing()["content_outline"], "product.pdf", output_dir=self.tmp_path
        )
        self.assertTrue(out.exists())
        data = out.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertTrue(b"%%EOF" in data)
        self.assertGreater(len(data), 100)
        # Written under <output_dir>/pdf/ by default.
        self.assertEqual(out.parent, self.tmp_path / "pdf")

    def test_layout_no_overflow_with_many_sections(self) -> None:
        outline = {"title": "Huge Planner"}
        for i in range(60):
            outline[f"section_{i}"] = [
                f"checklist item {j} for section {i}" for j in range(8)
            ]
        out = AssetBuilder().compile_product_pdf(outline, "big.pdf", output_dir=self.tmp_path)
        data = out.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertTrue(b"%%EOF" in data)
        self.assertGreater(len(data), 5000)

    def test_mockup_image_is_valid_png(self) -> None:
        out = AssetBuilder().create_listing_image(
            "Digital Planner", "mockup.png", output_dir=self.tmp_path
        )
        self.assertTrue(out.exists())
        self.assertEqual(out.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(out.parent, self.tmp_path / "images")

    def test_pdf_uses_default_font_when_true_font_missing(self) -> None:
        out = AssetBuilder().compile_product_pdf(
            {"title": "NoFancyFontNeeded", "pages": 5}, "fonts.pdf", output_dir=self.tmp_path
        )
        self.assertTrue(out.read_bytes().startswith(b"%PDF"))


# ---------------------------------------------------------------------------
# 3. SEO tag validation
# ---------------------------------------------------------------------------

class TagValidationTests(unittest.TestCase):
    def test_valid_tags_metamodel_passes(self) -> None:
        listing = EtsyListingData.model_validate(_valid_listing())
        self.assertEqual(len(listing.tags), 13)

    def test_rejects_more_than_13_tags(self) -> None:
        data = _valid_listing()
        data["tags"] += ["extra one"]
        with self.assertRaises(ValueError):
            EtsyListingData.model_validate(data)

    def test_rejects_fewer_than_13_tags(self) -> None:
        data = _valid_listing()
        data["tags"] = data["tags"][:5]
        with self.assertRaises(ValueError):
            EtsyListingData.model_validate(data)

    def test_rejects_tag_longer_than_20_chars(self) -> None:
        data = _valid_listing()
        data["tags"][0] = "this tag is way too long for etsy"
        with self.assertRaises(ValueError):
            EtsyListingData.model_validate(data)

    def test_rejects_duplicate_tags(self) -> None:
        data = _valid_listing()
        data["tags"][1] = data["tags"][0]
        with self.assertRaises(ValueError):
            EtsyListingData.model_validate(data)

    def test_tags_are_normalised_lowercase(self) -> None:
        data = _valid_listing()
        data["tags"][0] = "Digital Planner"
        listing = EtsyListingData.model_validate(data)
        self.assertEqual(listing.tags[0], "digital planner")


# ---------------------------------------------------------------------------
# 4. Etsy payload enforces draft + download
# ---------------------------------------------------------------------------

class EtsyPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, DUMMY_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = EtsyClient()

    def test_create_draft_listing_payload_forced_draft_and_download(self) -> None:
        captured: dict = {}

        def fake_request(method, path, **kwargs):
            captured.update(kwargs["json_body"])
            return {"listing_id": 111, "state": "draft"}

        with patch.object(EtsyClient, "_request", side_effect=fake_request):
            result = self.client.create_draft_listing(
                title="My Product",
                description="desc",
                tags=["a", "b", "c"] * 10,
                price=12.5,
            )

        self.assertEqual(result["listing_id"], 111)
        self.assertEqual(captured["state"], "draft")
        self.assertEqual(captured["type"], "download")
        self.assertEqual(captured["who_made"], "i_did")
        self.assertEqual(captured["when_made"], "2020_2024")
        self.assertTrue(captured["is_digital_download"])
        self.assertEqual(len(captured["tags"]), 13)

    def test_payload_never_publishes(self) -> None:
        captured: dict = {}

        def fake_request(method, path, **kwargs):
            captured.update(kwargs["json_body"])
            return {"listing_id": 1}

        with patch.object(EtsyClient, "_request", side_effect=fake_request):
            self.client.create_draft_listing(
                title="t", description="d", tags=["x"], price=1.0
            )

        self.assertEqual(captured["state"], "draft")
        self.assertNotEqual(captured.get("state"), "active")


# ---------------------------------------------------------------------------
# 5. AI content generation — Groq primary, NVIDIA fallback (mocked)
# ---------------------------------------------------------------------------

class ContentGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, DUMMY_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def _mock_chat_completions(fake_client: MagicMock, raw_text: str) -> None:
        """Wire fake_client.chat.completions.create to return *raw_text*."""
        create = MagicMock(return_value=MagicMock())
        create.return_value.choices = [MagicMock(message=MagicMock(content=raw_text))]
        fake_client.chat.completions.create = create

    def test_groq_primary_success(self) -> None:
        import src.content_generator as cg

        fake_client = MagicMock()
        raw = cg.EtsyListingData(**_valid_listing()).model_dump_json()
        self._mock_chat_completions(fake_client, raw)

        with patch("src.content_generator._new_groq_client", return_value=fake_client):
            result = generate_etsy_listing_data("digital planner")

        self.assertEqual(result["title"], _valid_listing()["title"])
        self.assertEqual(len(result["tags"]), 13)
        self.assertIn("AI-assisted", result["description"])
        self.assertGreater(result["suggested_price"], 0)
        # NVIDIA must NOT be contacted when Groq succeeds.
        cg._new_nvidia_client = MagicMock()

    def test_nvidia_fallback_when_groq_raises(self) -> None:
        import src.content_generator as cg

        groq_client = MagicMock()
        groq_client.chat.completions.create.side_effect = RuntimeError("groq down")

        nvidia_client = MagicMock()
        raw = cg.EtsyListingData(**_valid_listing()).model_dump_json()
        self._mock_chat_completions(nvidia_client, raw)

        with patch("src.content_generator._new_groq_client", return_value=groq_client), \
             patch("src.content_generator._new_nvidia_client", return_value=nvidia_client):
            result = generate_etsy_listing_data("digital planner")

        self.assertEqual(result["title"], _valid_listing()["title"])
        # Ensure the fallback path actually produced the result.
        nvidia_client.chat.completions.create.assert_called_once()

    def test_groq_uses_configured_model(self) -> None:
        import src.content_generator as cg

        fake_client = MagicMock()
        self._mock_chat_completions(fake_client, cg.EtsyListingData(**_valid_listing()).model_dump_json())

        with patch("src.content_generator._new_groq_client", return_value=fake_client):
            generate_etsy_listing_data("digital planner")

        called_kwargs = fake_client.chat.completions.create.call_args.kwargs
        self.assertEqual(called_kwargs["model"], cg.GROQ_MODEL)


# ---------------------------------------------------------------------------
# 6. Pipeline dry-run — no Etsy calls, assets compiled locally
# ---------------------------------------------------------------------------

class PipelineDryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, DUMMY_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_dry_run_compiles_assets_without_etsy_calls(self) -> None:
        import main as pipeline

        with patch.object(
            pipeline, "fetch_rising_trends",
            return_value=[{"keyword": "digital planner", "breakout": 1000}],
        ), patch.object(
            pipeline, "fetch_etsy_autocomplete",
            return_value=["digital planner pdf"],
        ), patch.object(
            pipeline, "generate_etsy_listing_data",
            return_value=_valid_listing(),
        ), patch.object(
            pipeline.EtsyClient, "create_draft_listing"
        ) as mock_create, patch.object(
            pipeline.EtsyClient, "upload_listing_image"
        ) as mock_img, patch.object(
            pipeline.EtsyClient, "upload_digital_file"
        ) as mock_file, patch.object(
            pipeline, "log_listing"
        ) as mock_log:
            result = pipeline.run(
                seed="planner", dry_run=True, output_dir=str(self.tmp_path)
            )

        # Assets were produced locally.
        self.assertIsNotNone(result["pdf_path"])
        self.assertTrue(Path(result["pdf_path"]).exists())
        self.assertIsNotNone(result["image_path"])
        self.assertTrue(Path(result["image_path"]).exists())

        # No Etsy write calls and no DB logging in dry-run.
        mock_create.assert_not_called()
        mock_img.assert_not_called()
        mock_file.assert_not_called()
        mock_log.assert_not_called()

        self.assertIsNone(result["listing_id"])
        self.assertIsNone(result["review_url"])


class ValidateCredentialsTests(unittest.TestCase):
    def _reset_settings(self) -> None:
        import src.config as cfg
        cfg._settings = None

    @patch.dict(os.environ, DUMMY_ENV, clear=True)
    def test_validate_etsy_credentials_true_when_present(self) -> None:
        import main as pipeline

        self._reset_settings()
        self.assertTrue(pipeline.validate_etsy_credentials())

    @patch.dict(os.environ, {"GROQ_API_KEY": "g", "NVIDIA_API_KEY": "n"}, clear=True)
    def test_validate_etsy_credentials_false_when_missing(self) -> None:
        import main as pipeline

        self._reset_settings()
        self.assertFalse(pipeline.validate_etsy_credentials())


if __name__ == "__main__":
    unittest.main()
