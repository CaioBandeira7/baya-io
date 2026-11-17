"""Document library: ingestion of real files, BM25 retrieval and invalidation."""

from __future__ import annotations

from pathlib import Path

import pytest

from companion.library import DocumentError, DocumentLibrary, split_into_chunks, safe_name, tokenize
from conftest import REPO, encrypt_pdf, make_pdf


# --- Ingestion of real documents ------------------------------------------------------

def test_pdf_pages_are_indexed_with_page_locations(library, civil_pdf):
    doc, created = library.add("civil_code.pdf", civil_pdf)
    assert created and doc.ext == ".pdf" and doc.chunk_count == 3
    results = library.search("obliged to repair damage")
    assert results[0].location == "p. 2"
    assert "Article 927" in results[0].text


def test_docx_headings_and_tables(library, contract_docx):
    library.add("contract.docx", contract_docx)
    penalty = library.search("late delivery penalty")[0]
    assert penalty.location == "§ Contract Terms"
    assert "two percent" in penalty.text
    milestone = library.search("acceptance testing deadline")[0]
    assert milestone.location == "table 1"


def test_real_repository_markdown(library):
    readme = (REPO / "README.md").read_bytes()
    architecture = (REPO / "docs" / "architecture.md").read_bytes()
    library.add("README.md", readme)
    library.add("architecture.md", architecture)
    top = library.search("WebRTC voice activity detection aggressiveness")[0]
    assert top.doc_name in {"README.md", "architecture.md"}
    assert top.location.startswith("§ ")


def test_txt_in_windows_encoding_with_accents(library):
    text = "Responsabilidade civil\n\nO dano moral exige prova do nexo causal.\n\nPrazo prescricional de três anos."
    library.add("notas.txt", text.encode("cp1252"))
    hit = library.search("prazo prescricional tres anos")[0]  # query without the accent
    assert "três anos" in hit.text
    assert hit.location.startswith("lines ")


# --- Problem cases ------------------------------------------------------------------------

@pytest.mark.parametrize("name,data,message", [
    ("empty.txt", b"", "empty"),
    ("payload.exe", b"MZ\x90\x00", "Unsupported"),
    ("fake.pdf", b"this is not a pdf", "not a valid PDF"),
    ("broken.pdf", b"%PDF-1.4\n garbage without objects", "could not be read"),
    ("fake.docx", b"plain text pretending to be docx", "not a valid DOCX"),
    ("binary.txt", b"\x00\x01\x02\x03binary", "binary"),
    ("blank.md", b"   \n\n  \n", "No readable text"),
])
def test_rejected_files(library, name, data, message):
    with pytest.raises(DocumentError, match=message):
        library.add(name, data)
    assert library.list_documents() == []


def test_scanned_pdf_without_text_layer(library):
    with pytest.raises(DocumentError, match="scanned PDF"):
        library.add("scan.pdf", make_pdf(["", ""]))


def test_password_protected_pdf(library, civil_pdf):
    with pytest.raises(DocumentError, match="password-protected"):
        library.add("locked.pdf", encrypt_pdf(civil_pdf))


def test_oversized_file(tmp_path):
    small = DocumentLibrary(tmp_path, max_bytes=100)
    with pytest.raises(DocumentError, match="larger than"):
        small.add("big.txt", b"word " * 100)


def test_path_traversal_name_is_neutralised(library):
    doc, _ = library.add("../../Windows/System32/evil.txt", b"Harmless text with enough words in it.")
    assert doc.name == "evil.txt"
    stored = list(library.files_dir.iterdir())
    assert len(stored) == 1 and stored[0].parent == library.files_dir
    assert safe_name('..\\..\\a<b>:c?.md') == "abc.md"


def test_duplicate_upload_is_not_indexed_twice(library, civil_pdf):
    first, created_first = library.add("a.pdf", civil_pdf)
    second, created_second = library.add("copy.pdf", civil_pdf)
    assert created_first and not created_second and first.id == second.id
    assert len(library.list_documents()) == 1


# --- Activation, deletion and invalidation ---------------------------------------------------

def test_deactivate_hides_results_and_reactivate_restores(library, civil_pdf):
    doc, _ = library.add("civil.pdf", civil_pdf)
    assert library.search("unlawful act damage")
    library.set_active(doc.id, False)
    assert library.search("unlawful act damage") == []
    assert not any(library.is_chunk_live(c) for c in [f"{doc.id}-0", f"{doc.id}-1"])
    library.set_active(doc.id, True)
    assert library.search("unlawful act damage")


def test_delete_invalidates_chunks_cache_file_and_notifies(library, civil_pdf, contract_docx):
    events = []
    library.add_listener(lambda event, doc_id: events.append((event, doc_id)))
    civil, _ = library.add("civil.pdf", civil_pdf)
    contract, _ = library.add("contract.docx", contract_docx)
    cached = library.search("damage repair")          # populates the cache
    assert cached and all(r.doc_id == civil.id for r in cached)
    chunk_ids = [r.chunk_id for r in cached]
    generation = library.generation

    library.delete(civil.id)

    assert library.search("damage repair") == []      # cached result not served again
    assert library.generation > generation
    assert all(library.get_chunk(c) is None for c in chunk_ids)
    assert not (library.files_dir / f"{civil.id}.pdf").exists()
    assert ("deleted", civil.id) in events
    assert [d.id for d in library.list_documents()] == [contract.id]
    with pytest.raises(KeyError):
        library.delete(civil.id)


def test_index_persists_and_ignores_missing_files(tmp_path, civil_pdf, contract_docx):
    first = DocumentLibrary(tmp_path)
    civil, _ = first.add("civil.pdf", civil_pdf)
    contract, _ = first.add("contract.docx", contract_docx)
    first.set_active(contract.id, False)
    (first.files_dir / f"{civil.id}.pdf").unlink()    # file removed outside the app

    reloaded = DocumentLibrary(tmp_path)
    docs = reloaded.list_documents()
    assert [d.id for d in docs] == [contract.id] and docs[0].active is False
    assert reloaded.search("penalty") == []           # inactive document stays hidden


# --- Retrieval details ------------------------------------------------------------------------

def test_bm25_prefers_specific_terms(library, civil_pdf, contract_docx):
    library.add("civil.pdf", civil_pdf)
    library.add("contract.docx", contract_docx)
    top = library.search("What is the penalty for late delivery of the software?")[0]
    assert top.doc_name == "contract.docx"
    assert set(top.matched_terms) >= {"penalty", "late", "delivery"}


def test_query_of_only_stopwords_returns_nothing(library, civil_pdf):
    library.add("civil.pdf", civil_pdf)
    assert library.search("what is the of and") == []


def test_long_text_is_chunked_with_overlap():
    text = " ".join(f"Sentence number {i} talks about topic {i % 7}." for i in range(200))
    chunks = split_into_chunks(text, 300, 60)
    assert len(chunks) > 5 and all(len(c) <= 360 for c in chunks)
    words_0, words_1 = set(chunks[0].split()), set(chunks[1].split())
    assert words_0 & words_1                              # overlap carries context forward


def test_tokenizer_folds_accents_and_drops_stopwords():
    assert tokenize("A Responsabilidade é Civil!") == ["responsabilidade", "civil"]
