from types import SimpleNamespace
from scholarship_finder.domain.pregate import pregate

def _c(url, title=""):
    return SimpleNamespace(canonical_url=url, title=title)
def _p(level):
    return SimpleNamespace(preferences=SimpleNamespace(degree_level=level))
FUND = "Fully funded scholarship covering tuition and stipend. " * 12
PHD = FUND + "doctoral PhD postdoc " * 15

def test_masters_variants():
    for lvl in ("Master's", "MSc", "masters"):
        assert pregate(_c("https://daad.de/x"), PHD, _p(lvl)) == (False, "level_mismatch")

def test_phd_never_dropped():
    assert pregate(_c("https://daad.de/x"), PHD, _p("PhD"))[0] is True

def test_reason_codes():
    assert pregate(_c("https://reddit.com/x"), FUND, _p("masters"))[1] == "junk"
    assert pregate(_c("https://x.edu/news/2026"), FUND, _p("masters"))[1] == "news_or_report"
    assert pregate(_c("https://x.edu/a"), "tiny", _p("masters"))[1] == "too_short"
    assert pregate(_c("https://x.edu/a"), "generic university text " * 30,
                   _p("masters"))[1] == "no_funding_terms"
