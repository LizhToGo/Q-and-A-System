from __future__ import annotations

import hashlib
from io import BytesIO

import fitz
from PIL import Image
import pytest

from backend.app.db import SessionLocal
from backend.app.models import Material, SourceDocument


def _sources(tmp_path):
    path = tmp_path / "source.pdf"
    with fitz.open() as pdf:
        pdf.new_page(width=200, height=100)
        page = pdf.new_page(width=200, height=100)
        page.draw_rect(fitz.Rect(0, 0, 100, 100), color=None, fill=(1, 0, 0))
        page.draw_rect(fitz.Rect(100, 0, 200, 100), color=None, fill=(0, 0, 1))
        page.set_rotation(90)
        pdf.save(path)
    with SessionLocal() as db:
        document = SourceDocument(course_id=1, filename="source.pdf", file_hash=hashlib.sha256(path.read_bytes()).hexdigest(), path=str(path), page_count=2, parser="pymupdf", status="published")
        foreign = SourceDocument(course_id=1, filename="foreign.pdf", file_hash="f" * 64, path=str(path), page_count=2, parser="pymupdf", status="published")
        db.add_all([document, foreign])
        db.flush()
        reference = Material(course_id=1, source_document_id=document.id, page_number=1, source_anchor=f"doc:{document.id}:p1", material_type="text", title="引用页", content="这一变换对见表3.1。\n\n" + "完整教材正文。" * 250 + "\n\n\\[f_s>2f_h\\]", verified=True, metadata_json={"content_format": "markdown", "figures": [{"caption": "表3.1 仅引用，不在本页"}]})
        table = Material(course_id=1, source_document_id=document.id, page_number=2, source_anchor=f"doc:{document.id}:p2", material_type="text", title="实际表格页", content="## 表3.1 四种形式\n\n原图位于本页。", verified=True, metadata_json={"tables": [{"caption": "表3.1 四种形式", "page_number": 2, "bbox": [0, 0.5, 1, 1]}]})
        wrong_book = Material(course_id=1, source_document_id=foreign.id, page_number=1, source_anchor=f"doc:{foreign.id}:p1", material_type="text", title="同名外书图表", content="## 表3.1 另一教材", verified=True, metadata_json={"tables": [{"caption": "表3.1 另一教材", "bbox": [0, 0, 1, 1]}]})
        db.add_all([reference, table, wrong_book])
        db.commit()
        return document.id, reference.id, table.id, path


def test_full_evidence_resolves_actual_cross_page_table_in_same_book(client, tmp_path):
    document_id, reference_id, table_id, path = _sources(tmp_path)
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    response = client.get(f"/api/materials/{reference_id}/evidence")
    assert response.status_code == 200
    result = response.json()
    assert result["full_content"].endswith("\\[f_s>2f_h\\]")
    assert result["content_format"] == "markdown"
    assert result["page_image_url"] == f"/api/source-documents/{document_id}/pages/1/image"
    assert [(entry["page_number"], entry["kind"]) for entry in result["media"]] == [(2, "table")]
    crop_response = client.get(result["media"][0]["url"])
    assert crop_response.status_code == 200
    with Image.open(BytesIO(crop_response.content)) as image:
        assert abs(image.width - image.height) <= 2
        assert image.getpixel((image.width // 2, image.height // 2))[:3] == (0, 0, 255)
    full_response = client.get(f"/api/source-documents/{document_id}/pages/2/image")
    with Image.open(BytesIO(full_response.content)) as image:
        assert image.height > image.width
        assert image.getpixel((image.width // 2, image.height // 4))[:3] == (255, 0, 0)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash


@pytest.mark.parametrize("change", [
    {"bbox": [-0.1, 0, 1, 1]}, {"bbox": [0.8, 0, 0.2, 1]},
    {"bbox": [0, 0, float("nan"), 1]}, {"page_number": 1},
])
def test_invalid_crop_cannot_render_another_page_or_region(client, tmp_path, change):
    _, _, table_id, _ = _sources(tmp_path)
    with SessionLocal() as db:
        table = db.get(Material, table_id)
        table.metadata_json = {"tables": [{"caption": "表3.1 四种形式", "page_number": 2, "bbox": [0, 0.5, 1, 1], **change}]}
        db.commit()
    assert client.get(f"/api/materials/{table_id}/media/tables/0").status_code == 404


def test_original_page_rejects_unknown_pages_and_unreviewed_crop(client, tmp_path):
    document_id, _, table_id, _ = _sources(tmp_path)
    for page in (0, 3):
        assert client.get(f"/api/source-documents/{document_id}/pages/{page}/image").status_code == 404
    with SessionLocal() as db:
        table = db.get(Material, table_id)
        table.verified = False
        db.commit()
    assert client.get(f"/api/materials/{table_id}/media/tables/0").status_code == 404


@pytest.mark.parametrize("label", ["图1-31", "图F1.4"])
def test_hyphen_and_exercise_figure_references_find_the_original_same_book_page(client, tmp_path, label):
    _, reference_id, figure_id, _ = _sources(tmp_path)
    with SessionLocal() as db:
        reference = db.get(Material, reference_id)
        reference.content = f"这一关系见{label}。"
        figure = db.get(Material, figure_id)
        figure.content = f"## {label} 原图"
        figure.metadata_json = {"figures": [{"caption": f"{label} 原图", "page_number": 2, "bbox": [0, 0.5, 1, 1]}]}
        db.commit()
    evidence = client.get(f"/api/materials/{reference_id}/evidence").json()
    assert [(media["kind"], media["page_number"]) for media in evidence["media"]] == [("figure", 2)]
    image = client.get(evidence["media"][0]["url"])
    with Image.open(BytesIO(image.content)) as crop:
        assert crop.getpixel((crop.width // 2, crop.height // 2))[:3] == (0, 0, 255)


def test_reviewed_unnumbered_table_embeds_only_on_its_real_page(client, tmp_path):
    _, reference_id, table_id, _ = _sources(tmp_path)
    with SessionLocal() as db:
        reference = db.get(Material, reference_id)
        reference.content = "其他页，没有此表的编号引用。"
        table = db.get(Material, table_id)
        table.content = "本页原始未编号的进制对照表。"
        table.metadata_json = {"tables": [{"caption": None, "page_number": 2, "bbox": [0, 0.5, 1, 1]}]}
        db.commit()
    assert client.get(f"/api/materials/{reference_id}/evidence").json()["media"] == []
    own = client.get(f"/api/materials/{table_id}/evidence").json()
    assert [(media["kind"], media["page_number"]) for media in own["media"]] == [("table", 2)]
    image = client.get(own["media"][0]["url"])
    with Image.open(BytesIO(image.content)) as crop:
        assert crop.getpixel((crop.width // 2, crop.height // 2))[:3] == (0, 0, 255)


def test_reviewed_crop_orientation_does_not_rotate_the_original_pdf(client, tmp_path):
    _, _, table_id, path = _sources(tmp_path)
    original = path.read_bytes()
    with SessionLocal() as db:
        table = db.get(Material, table_id)
        table.metadata_json = {"tables": [{
            "caption": "表1 方向校订", "page_number": 2,
            "bbox": [0, 0, 1, 1], "display_rotation": 180,
        }]}
        db.commit()
    evidence = client.get(f"/api/materials/{table_id}/evidence").json()
    crop_response = client.get(evidence["media"][0]["url"])
    with Image.open(BytesIO(crop_response.content)) as image:
        assert image.getpixel((image.width // 2, image.height // 4))[:3] == (0, 0, 255)
        assert image.getpixel((image.width // 2, 3 * image.height // 4))[:3] == (255, 0, 0)
    assert path.read_bytes() == original


def test_multipart_table_preserves_both_original_pages(client, tmp_path):
    path = tmp_path / "two-part-table.pdf"
    with fitz.open() as pdf:
        for color in ((1, 0, 0), (0, 0, 1), (1, 1, 1)):
            page = pdf.new_page(width=200, height=100)
            page.draw_rect(page.rect, color=None, fill=color)
        pdf.save(path)
    with SessionLocal() as db:
        document = SourceDocument(course_id=1, filename=path.name, path=str(path), page_count=3,
                                  file_hash=hashlib.sha256(path.read_bytes()).hexdigest(), parser="pymupdf")
        db.add(document)
        db.flush()
        for page in (1, 2):
            db.add(Material(course_id=1, source_document_id=document.id, page_number=page,
                            source_anchor=f"doc:{document.id}:p{page}", title="表格分部", material_type="text",
                            content="表3-7" + ("（续）" if page == 2 else ""), verified=True,
                            metadata_json={"tables": [{"label": "表3-7", "caption": "变换对" + ("（续）" if page == 2 else ""),
                                                       "bbox": [0, 0, 1, 1], "page_number": page}]}))
        reference = Material(course_id=1, source_document_id=document.id, page_number=3,
                             source_anchor=f"doc:{document.id}:p3", title="引用", material_type="text",
                             content="完整变换对参见表3-7。", verified=True, metadata_json={})
        db.add(reference)
        db.commit()
        reference_id = reference.id
    media = client.get(f"/api/materials/{reference_id}/evidence").json()["media"]
    assert [item["page_number"] for item in media] == [1, 2]
    for item, color in zip(media, ((255, 0, 0), (0, 0, 255))):
        with Image.open(BytesIO(client.get(item["url"]).content)) as image:
            assert image.getpixel((image.width // 2, image.height // 2))[:3] == color


def test_spaced_ocr_figure_numbers_do_not_match_a_shorter_reference(client, tmp_path):
    _, reference_id, figure_id, _ = _sources(tmp_path)
    with SessionLocal() as db:
        reference = db.get(Material, reference_id)
        reference.content = "本段仅引用图5.3。"
        figure = db.get(Material, figure_id)
        figure.content = "图\n5 . 3 1 \n\n图\n5 . 32"
        figure.metadata_json = {"figures": [
            {"caption": "图5.31 零极点图", "page_number": 2},
            {"caption": "图5.32 滤波器结构", "page_number": 2},
        ]}
        db.commit()
    assert client.get(f"/api/materials/{reference_id}/evidence").json()["media"] == []
    own_media = client.get(f"/api/materials/{figure_id}/evidence").json()["media"]
    assert [entry["label"] for entry in own_media] == [
        "图5.31 零极点图 · 原页（未裁剪）",
        "图5.32 滤波器结构 · 原页（未裁剪）",
    ]
    with SessionLocal() as db:
        db.get(Material, reference_id).content = "本段引用图5.31。"
        db.commit()
    media = client.get(f"/api/materials/{reference_id}/evidence").json()["media"]
    assert [entry["label"] for entry in media] == ["图5.31 零极点图 · 原页（未裁剪）"]
