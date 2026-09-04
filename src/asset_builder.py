from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from .config import OUTPUT_IMAGES_DIR, OUTPUT_PDF_DIR


def _resolve_dir(base: str | Path | None, default: Path) -> Path:
    """Return the *base* dir as a Path, defaulting to *default* when None."""
    if base is None:
        return default
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


class AssetBuilder:
    def compile_product_pdf(
        self,
        content_outline: dict,
        filename: str,
        output_dir: str | Path | None = None,
    ) -> Path:
        """Build a letter-size PDF from a structured ``content_outline`` dict.

        ``content_outline`` is the value returned by ``content_generator``.
        Each top-level key is rendered as a section heading, and list values
        are rendered as bullet points.

        When ``output_dir`` is provided the file is written to
        ``<output_dir>/pdf/<filename>``; otherwise the default
        ``OUTPUT_PDF_DIR`` is used.

        Example outline::

            {
                "sections": ["Daily Schedule", "Habit Tracker", "Gratitude Log"],
                "pages": 20
            }
        """
        pdf_dir = _resolve_dir(
            Path(output_dir) / "pdf" if output_dir is not None else None,
            OUTPUT_PDF_DIR,
        )
        pdf_dir.mkdir(parents=True, exist_ok=True)
        out = pdf_dir / filename
        c = canvas.Canvas(str(out), pagesize=letter)
        w, h = letter

        title = str(content_outline.get("title", "Digital Product"))
        c.setFont("Helvetica-Bold", 20)
        c.drawString(72, h - 72, title)

        y = h - 120

        for key, value in content_outline.items():
            if key == "title":
                continue

            c.setFont("Helvetica-Bold", 14)
            c.drawString(72, y, f"{key.replace('_', ' ').title()}:")
            y -= 24

            c.setFont("Helvetica", 11)
            if isinstance(value, (list, tuple)):
                for item in value:
                    if y < 72:
                        c.showPage()
                        y = h - 72
                    c.drawString(90, y, f"- {item}")
                    y -= 18
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if y < 72:
                        c.showPage()
                        y = h - 72
                    c.drawString(90, y, f"{sub_key}: {sub_value}")
                    y -= 18
            else:
                c.drawString(90, y, str(value))
                y -= 18

            y -= 12

        c.showPage()
        c.save()
        return out

    def create_listing_image(
        self,
        text: str,
        filename: str,
        size: tuple[int, int] = (2000, 2000),
        bg_color: str = "#FFFFFF",
        text_color: str = "#000000",
        output_dir: str | Path | None = None,
    ) -> Path:
        img = Image.new("RGB", size, bg_color)
        draw = ImageDraw.Draw(img)

        try:
            font = ImageFont.truetype("arial.ttf", size=80)
        except OSError:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (size[0] - tw) // 2
        y = (size[1] - th) // 2
        draw.text((x, y), text, fill=text_color, font=font)

        images_dir = _resolve_dir(
            Path(output_dir) / "images" if output_dir is not None else None,
            OUTPUT_IMAGES_DIR,
        )
        images_dir.mkdir(parents=True, exist_ok=True)
        out = images_dir / filename
        img.save(out, "PNG")
        return out

    def create_pdf(self, pages: list[str], filename: str) -> Path:
        OUTPUT_PDF_DIR.mkdir(parents=True, exist_ok=True)
        out = OUTPUT_PDF_DIR / filename
        c = canvas.Canvas(str(out), pagesize=letter)
        w, h = letter
        for page_text in pages:
            c.setFont("Helvetica", 14)
            c.drawString(72, h - 72, page_text)
            c.showPage()
        c.save()
        return out
