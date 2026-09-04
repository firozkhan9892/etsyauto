"""AI content generation for Etsy listings.

Uses Groq as the primary LLM provider and NVIDIA NIM (build.nvidia.com)
as an automatic fallback. Both expose OpenAI-compatible chat-completions
interfaces, so we parse/validate their JSON with Pydantic.

Primary   : groq "llama-3.3-70b-versatile"
Fallback  : openai.OpenAI(base_url=https://integrate.api.nvidia.com/v1)
            model "meta/llama-3.3-70b-instruct"
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from groq import Groq
from openai import OpenAI
from pydantic import BaseModel, Field, field_validator

from .config import get_settings

logger = logging.getLogger(__name__)

# Groq (primary): default is the model confirmed working on this account.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
# NVIDIA NIM (fallback): override NVIDIA_MODEL if a different tier/model is
# available on the linked NVIDIA Cloud account.
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0


# ---------------------------------------------------------------------------
# Pydantic schema – enforces the strict JSON contract
# ---------------------------------------------------------------------------

class EtsyListingData(BaseModel):
    title: str = Field(
        ...,
        max_length=140,
        description="SEO-optimized Etsy title, max 140 characters, keywords separated by commas.",
    )
    tags: list[str] = Field(
        ...,
        min_length=13,
        max_length=13,
        description="Exactly 13 unique tags, each under 20 characters.",
    )
    description: str = Field(
        ...,
        description="Markdown description with benefits, download guide, and 'AI-assisted design' disclaimer.",
    )
    suggested_price: float = Field(..., gt=0, description="Recommended USD price.")
    content_outline: dict[str, Any] = Field(
        ...,
        description="Structured dict with titles, headers, bullet items, and body text to build the product.",
    )

    @field_validator("title")
    @classmethod
    def _title_bound(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 140:
            raise ValueError(f"Title is {len(v)} chars, max 140 allowed.")
        return v

    @field_validator("tags")
    @classmethod
    def _tags_valid(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in v:
            t = tag.strip().lower()
            if len(t) > 20:
                raise ValueError(f"Tag '{t}' is {len(t)} chars, max 20 allowed.")
            if t not in cleaned:
                cleaned.append(t)
        if len(cleaned) != 13:
            raise ValueError(f"Tags must be exactly 13 unique, got {len(cleaned)}.")
        return cleaned

    @field_validator("description")
    @classmethod
    def _description_has_disclaimer(cls, v: str) -> str:
        low = v.lower()
        if "ai-assisted" not in low and "ai assisted" not in low:
            raise ValueError("Description must include an 'AI-assisted design' disclaimer.")
        return v.strip()


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert Etsy copywriter and digital-product strategist.
Given a single topic keyword, produce a complete Etsy listing draft as JSON.

Rules:
- title: under 140 characters, leading with the highest-intent keyword,
  secondary keywords separated by commas.
- tags: EXACTLY 13 tags, each under 20 characters, lowercase, unique.
  Use natural phrases with spaces between words (e.g. "digital planner"),
  no punctuation or symbols.
- description: Markdown. Sections: ## What You Get, ## How to Download,
  ## Disclaimer. The ## Disclaimer section MUST contain the exact phrase
  "AI-assisted design" (e.g. "This product was created with AI-assisted
  design."). Keep 300-600 words.
- suggested_price: a realistic USD float (e.g. 4.99).
- content_outline: a structured JSON object (titles, headers, bullet items,
  and text) describing the actual digital product so it can be built.
- CRITICAL: output is a product listing, not a chat message. NEVER include
  greetings, sign-offs, or assistant commentary such as "I'm happy to help",
  "Here is your listing", or any conversational filler in any field.
- Return ONLY the JSON object. No markdown fences, no labels, no commentary.
"""


def _build_user_prompt(topic_keyword: str) -> str:
    return (
        f"Create a full Etsy listing draft for this digital product topic: "
        f"'{topic_keyword}'.\n\n"
        "Think step-by-step about buyer intent, pricing, and SEO before "
        "returning the final JSON.\n\n"
        "REMINDERS:\n"
        "- The description MUST contain a '## Disclaimer' section that "
        "includes the exact phrase 'AI-assisted design'.\n"
        "- Output ONLY valid JSON: keys are title, tags, description, "
        "suggested_price, content_outline.\n"
        "- No greetings or sign-offs anywhere."
    )


# ---------------------------------------------------------------------------
# Provider clients
# ---------------------------------------------------------------------------

def _new_groq_client():
    return Groq(api_key=get_settings().groq_api_key)


def _new_nvidia_client():
    return OpenAI(
        base_url=NVIDIA_BASE_URL,
        api_key=get_settings().nvidia_api_key,
    )


def _call_groq(client, system: str, user: str):
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.7,
        max_tokens=4000,
    )
    return resp.choices[0].message.content


def _call_nvidia(client, system: str, user: str):
    resp = client.chat.completions.create(
        model=NVIDIA_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.7,
        max_tokens=4000,
    )
    return resp.choices[0].message.content


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_etsy_listing_data(topic_keyword: str) -> dict[str, Any]:
    """Generate a validated Etsy listing for *topic_keyword*.

    Tries Groq first; on any failure automatically falls back to NVIDIA NIM.
    Returns a plain dict matching :class:`EtsyListingData`.
    """
    if not topic_keyword or not topic_keyword.strip():
        raise ValueError("topic_keyword must be a non-empty string.")

    settings = get_settings()
    system = _SYSTEM_PROMPT
    user = _build_user_prompt(topic_keyword)

    last_exc: Exception | None = None

    # ---- Primary: Groq ----
    if settings.groq_api_key:
        try:
            client = _new_groq_client()
            return _generate_with_retries(
                topic_keyword,
                lambda: _call_groq(client, system, user),
                "Groq",
            )
        except Exception as exc:
            last_exc = exc
            logger.warning("Groq failed (%s); falling back to NVIDIA.", exc)
    else:
        logger.warning("GROQ_API_KEY not set; skipping Groq.")

    # ---- Fallback: NVIDIA NIM ----
    if settings.nvidia_api_key:
        try:
            client = _new_nvidia_client()
            return _generate_with_retries(
                topic_keyword,
                lambda: _call_nvidia(client, system, user),
                "NVIDIA",
            )
        except Exception as exc:
            last_exc = exc
            logger.error("NVIDIA NIM fallback also failed: %s", exc)
    else:
        logger.error("NVIDIA_API_KEY not set; cannot fall back.")

    raise RuntimeError(
        f"No AI provider available for '{topic_keyword}' (Groq and NVIDIA both failed)."
    ) from last_exc


def _generate_with_retries(
    topic: str,
    call_fn: Any,
    provider: str,
) -> dict[str, Any]:
    """Call the provider afresh on each attempt and validate each response.

    Unlike re-parsing the same text, a fresh call lets the model correct a
    schema violation (e.g. a missing disclaimer) on the next attempt.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            raw = call_fn()
            return _validate(raw, topic, provider)
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "%s attempt %d/%d failed for '%s': %s",
                provider, attempt, _MAX_RETRIES, topic, exc,
            )
            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_BASE ** attempt)

    raise ValueError(
        f"Provider '{provider}' failed after {_MAX_RETRIES} attempts."
    ) from last_exc


def _validate(raw: str, topic: str, provider: str) -> dict[str, Any]:
    """Strip code fences, parse JSON, and validate against the schema."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    data = json.loads(text)
    listing = EtsyListingData.model_validate(data)
    logger.info("Validated %s output for '%s'.", provider, topic)
    return listing.model_dump()
