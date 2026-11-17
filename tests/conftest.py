"""Shared fixtures: real PDF/DOCX/TXT/MD files built on the fly (no binary fixtures in git)."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def make_pdf(pages: list[str]) -> bytes:
    """Build a small but valid PDF with one text page per entry (empty string = page without text)."""
    objects: list[bytes] = []
    font_id = 3 + 2 * len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    for i, text in enumerate(pages):
        lines = [l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for l in text.splitlines()]
        ops = ["BT", "/F1 11 Tf", "50 780 Td", "14 TL"] + [f"({l}) '" for l in lines] + ["ET"] if lines else []
        stream = "\n".join(ops).encode("latin-1")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font_id} 0 R >> >> "
                       f"/Contents {4 + 2 * i} 0 R >>".encode())
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def encrypt_pdf(data: bytes, password: str = "secret") -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(data)).pages:
        writer.add_page(page)
    writer.encrypt(password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_docx() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("Contract Terms", level=1)
    document.add_paragraph("The supplier shall deliver the software within ninety days of signature.")
    document.add_paragraph("Late delivery triggers a penalty of two percent per week, capped at twenty percent.")
    document.add_heading("Confidentiality", level=1)
    document.add_paragraph("Both parties keep proprietary information confidential for five years.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Milestone", "Deadline"
    table.cell(1, 0).text, table.cell(1, 1).text = "Acceptance testing", "Day 75"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


CIVIL_PDF_PAGES = [
    "Brazilian Civil Code - excerpt\nArticle 186. Whoever, by voluntary action or omission, negligence or\n"
    "imprudence, violates a right and causes damage to another commits an unlawful act.",
    "Article 927. Whoever, by an unlawful act, causes damage to another\nis obliged to repair it.",
    "Article 944. Compensation is measured by the extent of the damage.",
]


@pytest.fixture
def civil_pdf() -> bytes:
    return make_pdf(CIVIL_PDF_PAGES)


@pytest.fixture
def contract_docx() -> bytes:
    return make_docx()


@pytest.fixture
def library(tmp_path):
    from companion.library import DocumentLibrary

    return DocumentLibrary(tmp_path)
