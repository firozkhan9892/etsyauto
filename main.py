"""End-to-end pipeline: research trends, generate listing data with
Groq/NVIDIA, compile digital assets, and upload a draft to Etsy.

Run from project root:
    python main.py "habit tracker"                 # live upload (needs creds)
    python main.py --seed "budget tracker" --dry-run
    python main.py --output-dir output_test --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from src.config import get_settings
from src.trend_analyzer import fetch_rising_trends, fetch_etsy_autocomplete
from src.content_generator import generate_etsy_listing_data
from src.asset_builder import AssetBuilder
from src.etsy_client import EtsyClient, EtsyAPIError, MissingCredentialsError
from src.database import is_keyword_processed, log_listing

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = "output"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Research, generate, and draft an Etsy digital-product listing automatically.",
    )
    parser.add_argument(
        "topic",
        nargs="?",
        default=None,
        help="Seed topic (positional; alternative to --seed).",
    )
    parser.add_argument(
        "--seed",
        default="digital planner",
        help="Seed topic to research. Default: 'digital planner'.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Generate all assets locally but do NOT call the Etsy API. "
            "Automatically implied when Etsy credentials are missing."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for generated assets. Default: '{DEFAULT_OUTPUT_DIR}'.",
    )
    parser.add_argument(
        "--geo",
        default="US",
        help="Google Trends geo code. Default: US.",
    )
    parser.add_argument(
        "--timeframe",
        default="now 7-d",
        help="Google Trends timeframe. Default: 'now 7-d'.",
    )
    parser.add_argument(
        "--taxonomy-id",
        type=int,
        default=2047,
        help="Etsy taxonomy ID for the listing. Default: 2047 (verify!).",
    )
    parser.add_argument(
        "--max-topics",
        type=int,
        default=5,
        help="How many trending keywords to consider. Default: 5.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_keywords(seed: str) -> list[str]:
    """Build a small set of seed keywords from a single user topic."""
    return [
        seed,
        f"{seed} template",
        f"{seed} printable",
        f"{seed} pdf",
    ]


def validate_etsy_credentials() -> bool:
    """Return True if Etsy write credentials (keystring, token, shop) exist."""
    settings = get_settings()
    return settings.has_etsy_credentials


def _pick_topic(trends: list[dict], seed: str, max_topics: int) -> str:
    """Select the best opportunity from trending keywords and validate via Etsy.

    Skips any keyword already targeted within the duplication window, so the
    agent picks the next best (non-duplicate) candidate instead.
    """
    candidates = [t["keyword"] for t in trends[:max_topics]] or [seed]

    enriched: dict[str, int] = {}
    for kw in candidates:
        if is_keyword_processed(kw):
            print(f"      Skipping already-processed keyword: {kw}")
            continue

        suggestions = fetch_etsy_autocomplete(kw)
        enriched[kw] = len(suggestions)

    if not enriched:
        if is_keyword_processed(seed):
            print("      WARNING: seed topic already processed in last 60 days.")
        return candidates[0]

    best = max(enriched, key=lambda k: (enriched[k], -candidates.index(k)))
    return best


def _build_outline(listing: dict) -> dict:
    """Normalise the product outline, carrying the title forward for the PDF."""
    outline = dict(listing.get("content_outline") or {})
    outline.setdefault("title", listing.get("title", "Digital Product"))
    return outline


def _emit_result(result: dict) -> None:
    """Print a machine-readable result line for the scheduler to capture."""
    print(f"ETS_RESULT_JSON={json.dumps(result)}")
    sys.stdout.flush()


def _review_url(shop_id: str, listing_id: int) -> str:
    return f"https://www.etsy.com/your/shops/{shop_id}/draft/{listing_id}/edit"


def _print_summary(summary_data: dict) -> None:
    """Render a clean, padded ASCII table from *summary_data*."""
    width = 72
    sep = "+" + "-" * width + "+"

    def row(label: str, value: str) -> str:
        inner = f" {label:<20} {value}"
        return "|" + inner.ljust(width) + "|"

    mode = "DRY-RUN (local only)" if summary_data.get("dry_run") else "LIVE UPLOAD"

    print("\n" + sep)
    print("|" + " ETSY DIGITAL PRODUCT PIPELINE — SUMMARY ".center(width) + "|")
    print(sep)
    print(row("Mode", mode))
    print(row("Topic", summary_data.get("topic", "?")))
    print(row("Title", summary_data.get("title", "?")))
    print(row("Tags count", str(summary_data.get("tags_count", "?"))))
    print(row("Mockup", summary_data.get("image_path", "-")))
    print(row("Product PDF", summary_data.get("pdf_path", "-")))

    if summary_data.get("listing_id"):
        print(sep)
        print(row("Listing ID", str(summary_data["listing_id"])))
        print(row("Etsy Review URL", summary_data.get("review_url", "-")))
    print(sep)
    footer = (
        " Assets saved locally. Run without --dry-run to upload. "
        if summary_data.get("dry_run")
        else " The listing is a DRAFT. Review it before publishing. "
    )
    print("|" + footer.center(width) + "|")
    print(sep + "\n")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run(
    seed: str = "digital planner",
    dry_run: bool = False,
    output_dir: str = "output",
    argv: list[str] | None = None,
) -> dict:
    """Run the full pipeline and return a summary dict.

    - dry_run=True (or missing credentials) -> assets only, no Etsy calls.
    - dry_run=False with valid credentials -> full flow up to Etsy draft.

    When *argv* is provided (CLI invocation) the parsed flags override the
    Python-side defaults; when called programmatically (e.g. tests) the
    ``seed`` / ``dry_run`` / ``output_dir`` arguments are authoritative.
    """
    # Parse CLI only when explicit argv is supplied (e.g. from __main__).
    parsed_args = parse_args(argv) if argv is not None else None
    if parsed_args is not None:
        seed = (parsed_args.topic or parsed_args.seed) or seed
        output_dir = parsed_args.output_dir or output_dir

    logging.basicConfig(
        level=logging.DEBUG if (parsed_args and parsed_args.verbose) else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = get_settings()
    builder = AssetBuilder()

    # 1. Research: expand the seed into keywords
    seed_keywords = _seed_keywords(seed)
    print(f"[1/5] Researching Google Trends for: {seed}")
    trends = fetch_rising_trends(
        seed_keywords,
        geo=parsed_args.geo if parsed_args else "US",
        timeframe=parsed_args.timeframe if parsed_args else "now 7-d",
    )
    print(f"      Found {len(trends)} rising related queries.")

    # 2. Select the single best opportunity (Trends + Etsy buyer-intent)
    max_topics = parsed_args.max_topics if parsed_args else 5
    print("[2/5] Selecting top opportunity and validating on Etsy...")
    selected_topic = _pick_topic(trends, seed, max_topics)
    print(f"      Selected: {selected_topic}")

    # 3. Generate SEO listing data + product outline
    print("[3/5] Generating SEO listing data (Groq, fallback NVIDIA NIM)...")
    listing = generate_etsy_listing_data(selected_topic)
    print(f"      Title: {listing['title']}")

    # 4. Compile product asset + marketing mockup (always local)
    print("[4/5] Compiling product PDF and marketing mockup...")
    safe = seed.replace(" ", "_")
    pdf_path = builder.compile_product_pdf(
        _build_outline(listing), f"{safe}_product.pdf", output_dir=output_dir
    )
    image_path = builder.create_listing_image(
        listing["title"],
        f"{safe}_listing.png",
        output_dir=output_dir,
    )

    base = {
        "topic": selected_topic,
        "title": listing["title"],
        "suggested_price": listing["suggested_price"],
        "tags": listing["tags"],
        "image_path": str(image_path),
        "pdf_path": str(pdf_path),
        "listing_id": None,
        "review_url": None,
    }

    # 5. Decide whether to hit the Etsy API.
    is_dry = dry_run or (
        parsed_args.dry_run if parsed_args is not None else False
    )
    should_upload = (not is_dry) and validate_etsy_credentials()

    if not should_upload:
        print("\n[DRY RUN] Etsy API skipped; assets generated locally.")
        _print_summary({**base, "tags_count": len(listing["tags"]), "dry_run": True})
        _emit_result(base)
        return base

    # 6. Upload as a DRAFT (mockup + product file), never publish.
    print("[5/5] Creating Etsy draft listing and uploading assets...")
    client = EtsyClient()
    client.require_credentials()

    taxonomy_id = parsed_args.taxonomy_id if parsed_args else 2047
    draft = client.create_draft_listing(
        title=listing["title"],
        description=listing["description"],
        tags=listing["tags"],
        price=listing["suggested_price"],
        taxonomy_id=taxonomy_id,
    )
    listing_id = draft["listing_id"]
    print(f"      Draft listing created: {listing_id}")

    client.upload_listing_image(listing_id, str(image_path))
    print("      Mockup image uploaded.")

    client.upload_digital_file(listing_id, str(pdf_path))
    print("      Product PDF uploaded.")

    log_listing(
        keyword=selected_topic,
        listing_id=str(listing_id),
        status="draft",
        pdf_path=str(pdf_path),
        tags=listing["tags"],
    )
    print("      Processing logged to database.")

    result = {
        **base,
        "listing_id": listing_id,
        "review_url": _review_url(settings.etsy_shop_id, listing_id),
    }

    _print_summary({**result, "tags_count": len(listing["tags"]), "dry_run": False})
    _emit_result(result)
    return result


if __name__ == "__main__":
    try:
        run(argv=sys.argv[1:])
    except (EtsyAPIError, MissingCredentialsError, RuntimeError, ValueError) as exc:
        logger.error("Pipeline failed: %s", exc)
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
