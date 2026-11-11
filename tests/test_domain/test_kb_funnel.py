"""KB + funnel-v2 pure-module tests."""
import time
from scholarship_finder.domain.pregate import pregate
from scholarship_finder.domain.req_hints import extract_hints
from scholarship_finder.domain.models import Candidate, CandidateProfile, Preferences


def _c(url, title=""):
    return Candidate(url=url, canonical_url=url, domain="x.edu", title=title)

def _prof(level="masters"):
    return CandidateProfile(preferences=Preferences(
        degree_level=level, target_fields=["CS"], countries=["Canada"]))

TXT = ("Fully funded masters scholarship covering tuition and living costs. "
       * 20)

def test_pregate_junk():
    ok, r = pregate(_c("https://reddit.com/r/x"), TXT, _prof())
    assert not ok and r == "junk"

def test_pregate_short():
    ok, r = pregate(_c("https://daad.de/x"), "short", _prof())
    assert not ok and r == "too_short"

def test_pregate_no_funding():
    ok, r = pregate(_c("https://daad.de/x"), "generic university info " * 40, _prof())
    assert not ok and r == "no_funding_terms"

def test_pregate_level_mismatch():
    phd = ("Doctoral PhD fellowship for postdocs. " * 20)
    ok, r = pregate(_c("https://daad.de/x"), phd, _prof("masters"))
    assert not ok and r == "level_mismatch"

def test_pregate_phd_never_dropped():
    ok, _ = pregate(_c("https://daad.de/x"), TXT + " doctoral PhD", _prof("phd"))
    assert ok

def test_pregate_news():
    ok, r = pregate(_c("https://edu.edu/news/2026"), TXT, _prof())
    assert not ok and r == "news_or_report"

def test_pregate_pass():
    ok, _ = pregate(_c("https://daad.de/x"), TXT, _prof())
    assert ok


def test_hints():
    txt = ("Applicants need IELTS 6.5 or TOEFL 90. GPA minimum 3.0/4.0, "
           "75% marks preferred. Deadline: 15 March 2027. "
           "Stipend €1,200 per month. For masters and doctoral study.")
    h = extract_hints(txt)
    assert "ielts" in h and "6.5" in h["ielts"]["value"]
    assert "gpa" in h and "3.0" in h["gpa"]["value"]
    assert "deadline" in h and "March" in h["deadline"]["value"]
    assert "amount" in h
    assert "degree_levels" in h


def test_kb_roundtrip(tmp_path):
    from scholarship_finder.adapters.knowledge_base import KnowledgeBase
    kb = KnowledgeBase(tmp_path / "k.db")
    kb.put_search("k1", [{"url": "u"}])
    assert kb.get_search("k1") == [{"url": "u"}]
    kb.put_verdict("u1", "", "rejected", reason="junk")
    assert kb.get_verdict("u1")["verdict"] == "rejected"
    kb.put_page("u1", "text")
    assert kb.get_page("u1") == "text"
    kb.purge()
    assert kb.stats()["page_cache"] == 0

def test_kb_ttl(tmp_path):
    import sqlite3
    from scholarship_finder.adapters.knowledge_base import KnowledgeBase
    kb = KnowledgeBase(tmp_path / "k.db", search_ttl_hours=1)
    kb.put_search("k", [{"a": 1}])
    conn = sqlite3.connect(str(tmp_path / "k.db"))
    conn.execute("UPDATE search_cache SET fetched_at=?", (time.time() - 7200,))
    conn.commit()
    assert kb.get_search("k") is None
