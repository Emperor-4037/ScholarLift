"""Shared junk/news deny-lists for every funnel path (scout, auditor, expansion)."""
from __future__ import annotations
import re

JUNK_RE = re.compile(
    r"reddit\.com|instagram\.com|facebook\.com|x\.com|twitter\.com|youtube\.com|"
    r"quora\.com|linkedin\.com|tiktok\.com|medium\.com|pmc\.ncbi|arxiv\.org|"
    r"doi\.org|tavily\.com|pinterest\.com|snapchat\.com|discord|telegram|whatsapp", re.I)

NEWS_RE = re.compile(
    r"/news/|/press/|press[-_]?release|annual[-_]?report|privacy[-_]?impact|"
    r"/blog/|newsroom|/media/|/events?/|conference[-_]?|webinar|podcast|"
    r"bbc\.|cnn\.|reuters\.|guardian\.|timesofindia|indianexpress|cicnews|"
    r"//news\.|announces", re.I)


def is_junk_url(url: str) -> bool:
    return bool(url) and bool(JUNK_RE.search(url))


def is_news_or_report(url: str, title: str = "") -> bool:
    return bool(NEWS_RE.search(url)) or bool(NEWS_RE.search(title or ""))
