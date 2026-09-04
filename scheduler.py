#!/usr/bin/env python
"""Automated daily scheduler for the Etsy listing generator.

Runs ``main.py`` once per day with a rotating set of seed niches so each
day targets a different type of product.  Sends the owner a notification
(Telegram Bot API or Discord webhook) when a new draft is created.

Run 24/7 on a VPS / background terminal:

    python scheduler.py                    # default schedule
    python scheduler.py --interval hours --every 6
    python scheduler.py --once --niche "spreadsheet template"

Signals (SIGINT / SIGTERM / Ctrl+C) trigger a graceful shutdown.
Logs are written to ./logs/scheduler.log and rotated daily.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import requests
import schedule
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "scheduler.log"

load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger("scheduler")

# ---------------------------------------------------------------------------
# Did-not-finish / shutdown control
# ---------------------------------------------------------------------------

_shutdown = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _shutdown
    logger.info("Received signal %d; shutting down gracefully...", signum)
    _shutdown = True


def setup_logging(level: int = logging.INFO) -> None:
    """Configure rotating file + console logging tuned for 24/7 operation."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE,
        when="midnight",
        interval=1,
        backupCount=14,  # keep two weeks of logs
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(level)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console)


# ---------------------------------------------------------------------------
# Notification (Telegram or Discord)
# ---------------------------------------------------------------------------

class Notifier:
    """Send owner notifications via Telegram Bot API or a Discord webhook."""

    def __init__(self) -> None:
        self._telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self._telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self._discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(
            (self._telegram_token and self._telegram_chat)
            or self._discord_webhook
        )

    def notify_draft(self, result: dict, *, shop_id: str) -> bool:
        """Send a New-Draft message summarising a created listing."""
        topic = result.get("topic", "?")
        title = result.get("title", "?")
        listing_id = result.get("listing_id")
        review_url = result.get("review_url")

        if not listing_id or not review_url:
            logger.info("No listing created; skipping notification.")
            return False

        shop_part = shop_id or ""
        fallback_review = (
            f"https://www.etsy.com/your/shops/{shop_part}/draft/{listing_id}/edit"
        )
        link = review_url or fallback_review

        message = (
            "New Etsy draft created\\n"
            "Topic: {topic}\\n"
            "Title: {title}\\n"
            "Draft ID: {listing_id}\\n"
            "Review: {link}"
        ).format(topic=topic, title=title, listing_id=listing_id, link=link)

        if self._discord_webhook:
            return self._send_discord(message)
        if self._telegram_token and self._telegram_chat:
            return self._send_telegram(message)
        return False

    def notify_error(self, exc: Exception) -> bool:
        message = f"Etsy scheduler run failed: {exc}"
        if self._discord_webhook:
            return self._send_discord(message)
        if self._telegram_token and self._telegram_chat:
            return self._send_telegram(message)
        return False

    def _send_telegram(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self._telegram_token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": self._telegram_chat,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                },
                timeout=30,
            )
            resp.raise_for_status()
            logger.info("Telegram notification sent.")
            return True
        except requests.RequestException as exc:
            logger.error("Telegram notification failed: %s", exc)
            return False

    def _send_discord(self, text: str) -> bool:
        try:
            resp = requests.post(
                self._discord_webhook,
                json={"content": text},
                timeout=30,
            )
            resp.raise_for_status()
            logger.info("Discord notification sent.")
            return True
        except requests.RequestException as exc:
            logger.error("Discord notification failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Niche rotation
# ---------------------------------------------------------------------------

def default_niches() -> dict[int, str]:
    """Seed niches keyed by weekday (0=Monday ... 6=Sunday)."""
    return {
        0: "digital planner",
        1: "spreadsheet template",
        2: "checklist",
        3: "budget tracker",
        4: "habit tracker",
        5: "printable wall art",
        6: "journal prompt",
    }


def niche_for_today(niches: dict[int, str] | None = None) -> str:
    """Return today's seed niche based on the day of the week."""
    table = niches or default_niches()
    weekday = time.localtime().tm_wday
    return table.get(weekday, "digital planner")


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

def run_once(niche: str, notifier: Notifier, additional_args: list[str]) -> int:
    """Run main.py once for *niche* and return its process exit code."""
    shop_id = os.environ.get("ETSY_SHOP_ID", "").strip()
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "main.py"),
        niche,
        *additional_args,
    ]
    logger.info("Running: %s", " ".join(cmd))

    start = time.time()
    try:
        proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
    except Exception as exc:
        logger.exception("Failed to launch main.py")
        notifier.notify_error(exc)
        return 1

    elapsed = time.time() - start
    logger.info("main.py finished in %.1fs with exit code %d", elapsed, proc.returncode)

    if proc.stdout:
        logger.debug("main.py stdout:\n%s", proc.stdout)
    if proc.stderr:
        logger.warning("main.py stderr:\n%s", proc.stderr)

    # Parse the JSON result line that main.py emits on success.
    result = _parse_result(proc.stdout)
    if result:
        notifier.notify_draft(result, shop_id=shop_id)

    return proc.returncode


def _parse_result(stdout: str) -> dict | None:
    """Extract the ``ETS_RESULT_JSON`` line printed by main.py, if present."""
    prefix = "ETS_RESULT_JSON="
    for line in stdout.splitlines():
        if line.startswith(prefix):
            try:
                return json.loads(line[len(prefix):])
            except json.JSONDecodeError:
                logger.warning("Failed to parse result JSON line.")
                return None
    return None


# ---------------------------------------------------------------------------
# Scheduling loop
# ---------------------------------------------------------------------------

def schedule_jobs(
    niches: dict[int, str] | None,
    notifier: Notifier,
    interval: str,
    every: int,
    additional_args: list[str],
) -> None:
    """Register the daily job (or run a single niche once)."""
    if interval == "days":
        schedule.every(every).days.at("09:00").do(
            run_once, niche_for_today(niches or {}), notifier, additional_args
        )
        logger.info(
            "Scheduled: every %d day(s) at 09:00 with weekly niche rotation.",
            every,
        )
    else:  # hours
        schedule.every(every).hours.do(
            run_once, niche_for_today(niches or {}), notifier, additional_args
        )
        logger.info("Scheduled: every %d hour(s).", every)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single job immediately and exit.",
    )
    parser.add_argument(
        "--niche", default=None,
        help="Seed niche for --once (default: today's rotated niche).",
    )
    parser.add_argument(
        "--interval", choices=["days", "hours"], default="days",
        help="Scheduling interval unit. Default: days.",
    )
    parser.add_argument(
        "--every", type=int, default=1,
        help="Run every N interval units. Default: 1.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Only log warnings and above to console.",
    )
    args = parser.parse_args(argv)

    setup_logging(logging.WARNING if args.quiet else logging.INFO)
    logger.info("Etsy scheduler starting (PID %d).", os.getpid())

    notifier = Notifier()

    if args.once:
        niche = args.niche or niche_for_today()
        return run_once(niche, notifier, [])

    # Long-running: register signal handlers for graceful shutdown.
    global _shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    schedule_jobs(
        default_niches(),
        notifier,
        args.interval,
        args.every,
        [],
    )

    while not _shutdown:
        schedule.run_pending()
        time.sleep(1)

    logger.info("Scheduler shutting down cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
