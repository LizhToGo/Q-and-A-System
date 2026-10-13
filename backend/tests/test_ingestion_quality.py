from __future__ import annotations

from io import BytesIO

import fitz
from PIL import Image

from backend.app.services.ingestion.service import _extract_pdf


def test_full_page_image_native_text_is_flagged_for_manual_review(tmp_path):
    path = tmp_path / "scanned.pdf"
    image_buffer = BytesIO()
    Image.new("RGB", (32, 32), "white").save(image_buffer, format="PNG")

    document = fitz.open()
    page = document.new_page(width=200, height=300)
    page.insert_image(page.rect, stream=image_buffer.getvalue())
    page.insert_text((20, 40), "native text layer that is long enough to pass the old length-only gate")
    document.save(path)
    document.close()

    page_count, items, warnings, parser = _extract_pdf(path)

    assert page_count == 1
    assert parser == "pymupdf"
    assert items[0]["confidence"] <= 0.35
    assert items[0]["needs_review"] is True
    assert "full_page_image_native_text" in items[0]["payload"]["quality_flags"]
    assert any(warning["code"] == "low_quality_text" for warning in warnings)
