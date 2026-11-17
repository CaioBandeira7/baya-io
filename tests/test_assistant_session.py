"""Assistant grounding and verification, session context invalidation, turn assembly."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from companion import config
from companion.assistant import Assistant, focus_of, verify
from companion.pipeline import TurnAssembler, VadSegmenter, is_meaningful
from companion.session import EventBus, Session


class FakeGenerator:
    model = "fake"

    def __init__(self, reply, delay=0.0, gate=None):
        self.reply, self.delay, self.gate, self.prompts = reply, delay, gate, []

    def __call__(self, prompt, on_text):
        self.prompts.append(prompt)
        if self.gate:
            self.gate.wait(5)
        time.sleep(self.delay)
        on_text(self.reply[:10])
        return self.reply


class FakePipeline:
    def __init__(self, *args):
        pass

    def start(self):
        pass

    def stop(self):
        pass


# --- focus and verification -----------------------------------------------------------

def test_focus_skips_introductions():
    turn = ("Thanks, that was helpful. Our clients often ask about liability. "
            "So, what must be proven for the defendant to be held responsible?")
    assert focus_of(turn) == "So, what must be proven for the defendant to be held responsible?"
    assert focus_of("Let's look at the contract. Tell me the penalty for late delivery.") == \
        "Tell me the penalty for late delivery."


def test_verify_removes_fake_citations_and_flags_numbers():
    sources = [{"label": "S1", "excerpt": "Late delivery triggers a penalty of two percent per week."}]
    summary, flags, cited = verify("The penalty is two percent per week [S1]. It is capped at 30% [S4].", sources)
    assert "[S4]" not in summary and "[S1]" in summary and cited == {"S1"}
    assert any("S4" in f for f in flags) and any("30%" in f for f in flags)


def test_verify_flags_uncited_summary():
    _, flags, cited = verify("Something without a citation.", [{"label": "S1", "excerpt": "text"}])
    assert not cited and any("did not cite" in f for f in flags)


# --- assistant -------------------------------------------------------------------------------

def test_assistant_returns_excerpts_and_grounded_summary(library, civil_pdf, contract_docx):
    library.add("civil.pdf", civil_pdf)
    library.add("contract.docx", contract_docx)
    gen = FakeGenerator("Whoever causes damage by an unlawful act must repair it [S1].")
    item = Assistant(library, gen).respond("Who is obliged to repair the damage caused by an unlawful act?")
    assert item["summary"].endswith("[S1].") and item["flags"] == []
    assert item["citations"][0]["doc_name"] == "civil.pdf" and item["citations"][0]["cited"] is True
    assert "Article 927" in item["citations"][0]["excerpt"]          # original passage is returned
    assert "[S1] civil.pdf (p. 2)" in gen.prompts[0]


def test_assistant_stays_silent_without_relevant_documents(library, civil_pdf):
    library.add("civil.pdf", civil_pdf)
    gen = FakeGenerator("anything")
    item = Assistant(library, gen).respond("Nice weather today, isn't it?")
    assert item["no_match"] is True and item["citations"] == []   # a question gets a notice, not silence
    assert Assistant(library, gen).respond("Nice weather today.") is None
    assert gen.prompts == []                                      # no model call without evidence


def test_short_question_matches_a_single_word(library):
    library.add("about_me.md", b"# About me\nMy name is Caio Bandeira and I live in Brasilia.")
    item = Assistant(library, FakeGenerator("My name is Caio Bandeira [S1].")).respond("What's your name?")
    assert item["no_match"] is False and item["citations"][0]["doc_name"] == "about_me.md"


def test_only_the_cited_source_is_shown(library, civil_pdf, contract_docx):
    library.add("civil.pdf", civil_pdf)
    library.add("contract.docx", contract_docx)
    item = Assistant(library, FakeGenerator("Damage must be repaired [S1].")).respond(
        "Who must repair damage caused by an unlawful act, and what is the compensation for the damage?")
    assert len(item["citations"]) == 1 and item["citations"][0]["label"] == "S1"


def test_assistant_no_answer_keeps_excerpts_only(library, civil_pdf):
    library.add("civil.pdf", civil_pdf)
    item = Assistant(library, FakeGenerator("NO_ANSWER")).respond("What does article 944 say about compensation damage?")
    assert item["summary"] == "" and item["citations"]


def test_assistant_survives_model_outage(library, civil_pdf):
    import requests

    def broken(prompt, on_text):
        raise requests.ConnectionError("ollama down")

    library.add("civil.pdf", civil_pdf)
    item = Assistant(library, broken).respond("What does article 944 say about compensation damage?")
    assert item["citations"] and "unavailable" in item["flags"][0]


# --- session invalidation ---------------------------------------------------------------------

def make_session(library, generator):
    session = Session(library, EventBus(), lambda *a: FakePipeline(), Assistant(library, generator))
    session.start("system")
    return session


def wait_for(predicate, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_deleting_document_removes_its_context(library, civil_pdf, contract_docx):
    civil, _ = library.add("civil.pdf", civil_pdf)
    contract, _ = library.add("contract.docx", contract_docx)
    session = make_session(library, FakeGenerator("Summary [S1]."))
    session.add_turn("system", "Who must repair damage caused by an unlawful act?", time.perf_counter())
    assert wait_for(lambda: len(session.context) == 1)
    session.add_turn("system", "What is the penalty for late delivery of the software?", time.perf_counter())
    assert wait_for(lambda: len(session.context) == 2)

    library.delete(civil.id)

    assert [c["citations"][0]["doc_id"] for c in session.context] == [contract.id]
    assert library.search("unlawful act repair damage") == []


def test_deactivating_document_removes_its_context(library, civil_pdf):
    civil, _ = library.add("civil.pdf", civil_pdf)
    session = make_session(library, FakeGenerator("Summary [S1]."))
    session.add_turn("system", "Who must repair damage caused by an unlawful act?", time.perf_counter())
    assert wait_for(lambda: len(session.context) == 1)
    library.set_active(civil.id, False)
    assert session.context == []


def test_document_deleted_while_answer_is_being_generated(library, civil_pdf):
    civil, _ = library.add("civil.pdf", civil_pdf)
    gate = threading.Event()
    session = make_session(library, FakeGenerator("Summary [S1].", gate=gate))
    session.add_turn("system", "Who must repair damage caused by an unlawful act?", time.perf_counter())
    time.sleep(0.2)                       # generation is now waiting on the gate
    library.delete(civil.id)
    gate.set()
    time.sleep(0.4)
    assert session.context == []          # the stale answer is discarded, never shown


def test_stop_clears_everything(library, civil_pdf):
    library.add("civil.pdf", civil_pdf)
    session = make_session(library, FakeGenerator("Summary [S1]."))
    session.add_turn("system", "Who must repair damage caused by an unlawful act?", time.perf_counter())
    assert wait_for(lambda: len(session.context) == 1)
    session.stop()
    assert session.state == "idle" and session.transcript == [] and session.context == []


# --- audio logic without hardware ----------------------------------------------------------

def test_turn_assembler_merges_segments_until_pause():
    turns = TurnAssembler(gap=0.5)
    turns.segment_queued("system")
    turns.segment_queued("system")
    turns.transcribed("system", "Our company works with contracts.", 10.0)
    assert turns.ready(10.6) == []                         # second segment still being transcribed
    turns.transcribed("system", "What is the penalty for late delivery?", 11.0)
    assert turns.ready(11.2) == []                         # pause too short
    turns.voice("system", True)
    assert turns.ready(12.0) == []                         # speaker talking again
    turns.voice("system", False)
    assert turns.ready(12.0) == [("system", "Our company works with contracts. What is the penalty for late delivery?", 11.0)]


def test_vad_segmenter_cuts_after_silence():
    loud = np.full(config.FRAME_SAMPLES, 0.2, dtype=np.float32)
    quiet = np.zeros(config.FRAME_SAMPLES, dtype=np.float32)
    segmenter = VadSegmenter(lambda pcm, rate: max(pcm) != 0)
    outputs = [segmenter.feed(loud) for _ in range(20)]       # 600 ms of speech
    outputs += [segmenter.feed(quiet) for _ in range(30)]     # 900 ms of silence
    segments = [o for o in outputs if o is not None]
    assert len(segments) == 1 and len(segments[0]) >= 20 * config.FRAME_SAMPLES


def test_vad_ignores_short_noise():
    loud = np.full(config.FRAME_SAMPLES, 0.2, dtype=np.float32)
    quiet = np.zeros(config.FRAME_SAMPLES, dtype=np.float32)
    segmenter = VadSegmenter(lambda pcm, rate: max(pcm) != 0)
    outputs = [segmenter.feed(loud) for _ in range(3)] + [segmenter.feed(quiet) for _ in range(30)]
    assert all(o is None for o in outputs)                    # 90 ms click is not speech


@pytest.mark.parametrize("text,keep", [("Okay.", False), ("Um, uh.", False), ("", False),
                                       ("What is the penalty?", True), ("Thank you all.", True)])
def test_filler_filter(text, keep):
    assert is_meaningful(text) is keep


def test_removed_citation_leaves_clean_punctuation():
    summary, _, _ = verify("Damage must be repaired [S9].", [{"label": "S1", "excerpt": "x"}])
    assert summary == "Damage must be repaired."


def test_plain_statement_needs_stronger_evidence(library, civil_pdf):
    library.add("civil_code_excerpt.pdf", civil_pdf)
    assistant = Assistant(library, None)
    assert assistant.respond("Good question, let me check the civil code excerpt.") is None
    assert assistant.respond("What does the civil code excerpt say?") is not None


def test_repeated_evidence_does_not_create_duplicate_cards(library, civil_pdf):
    library.add("civil.pdf", civil_pdf)
    session = make_session(library, FakeGenerator("Summary [S1]."))
    session.add_turn("system", "Who must repair damage caused by an unlawful act?", time.perf_counter())
    assert wait_for(lambda: len(session.context) == 1)
    session.add_turn("system", "So who has to repair damage from an unlawful act?", time.perf_counter())
    time.sleep(0.4)
    assert len(session.context) == 1
