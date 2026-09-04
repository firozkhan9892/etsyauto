from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT_DIR / "assets"
TEMPLATES_DIR = ASSETS_DIR / "templates"
OUTPUTS_DIR = ROOT_DIR / "output"
OUTPUT_IMAGES_DIR = OUTPUTS_DIR / "images"
OUTPUT_PDF_DIR = OUTPUTS_DIR / "pdf"
DB_PATH = ROOT_DIR / "etsyauto.db"


@dataclass(frozen=True)
class Settings:
    # AI generation providers
    groq_api_key: str = field(default_factory=lambda: os.environ.get("GROQ_API_KEY", ""))
    nvidia_api_key: str = field(default_factory=lambda: os.environ.get("NVIDIA_API_KEY", ""))

    # Etsy Open API v3
    etsy_api_key: str = field(default_factory=lambda: os.environ.get("ETSY_API_KEY", ""))
    etsy_keystring: str = field(default_factory=lambda: os.environ.get("ETSY_KEYSTRING", ""))
    etsy_shared_secret: str = field(default_factory=lambda: os.environ.get("ETSY_SHARED_SECRET", ""))
    etsy_shop_id: str = field(default_factory=lambda: os.environ.get("ETSY_SHOP_ID", ""))
    etsy_access_token: str = field(default_factory=lambda: os.environ.get("ETSY_ACCESS_TOKEN", ""))

    etsy_base_url: str = "https://openapi.etsy.com/v3/application"

    @property
    def has_etsy_credentials(self) -> bool:
        """True only if tokens needed for write calls to Etsy are present."""
        return bool(self.etsy_keystring and self.etsy_access_token and self.etsy_shop_id)

    def ensure_dirs(self) -> None:
        for d in (ASSETS_DIR, TEMPLATES_DIR, OUTPUTS_DIR, OUTPUT_IMAGES_DIR, OUTPUT_PDF_DIR):
            d.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings
