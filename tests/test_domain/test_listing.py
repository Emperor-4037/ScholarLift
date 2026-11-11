from scholarship_finder.domain.listing import registrable, is_listing, award_links

BASE = "https://www.example.edu/funding/"
MD = """
[Master's Scholarship in Data Science](/funding/ds-masters)
[PhD Fellowship](/funding/phd-fellowship)
[Excellence Award](/funding/excellence)
[Global Bursary Programme](/funding/bursary)
[Reddit thread](https://reddit.com/r/scholarships/x)
[External Award](https://randomsite.com/award)
[News item](/news/2026-award)
[About us](/about)
"""
def test_registrable():
    assert registrable("www2.daad.de") == "daad.de"
    assert registrable("ox.ac.uk") == "ox.ac.uk"

def test_is_listing():
    assert is_listing(MD, BASE) is True
    assert is_listing("[A Scholarship](/a) [B Fellowship](/b)", BASE) is False

def test_award_links_filters():
    res = award_links(MD, BASE, {"master", "data", "science"}, allowed_domains=set())
    urls = [u for _, u, _ in res]
    assert all("example.edu" in u for u in urls)
    assert not any("reddit" in u or "randomsite" in u or "/news/" in u for u in urls)
    assert len(res) <= 5
    names = [n for n, _, _ in res]
    assert any("Data Science" in n for n in names)
    # profile-token overlap ranks first
    assert "Data Science" in res[0][0]
