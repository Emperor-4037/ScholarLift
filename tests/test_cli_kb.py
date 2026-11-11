import json, sys
from scholarship_finder.cli import main
from scholarship_finder.adapters.knowledge_base import get_kb

def test_kb_stats(capsys):
    sys.argv = ["scholarship-run", "kb", "--stats"]
    main()
    out = json.loads(capsys.readouterr().out)
    for k in ("opportunities", "page_verdicts", "search_cache", "page_cache"):
        assert k in out

def test_kb_purge(capsys):
    kb = get_kb()
    kb.put_verdict("u", "", "rejected", reason="x")
    sys.argv = ["scholarship-run", "kb", "--purge"]
    main()
    out = json.loads(capsys.readouterr().out)
    assert all(v == 0 for v in out.values())
