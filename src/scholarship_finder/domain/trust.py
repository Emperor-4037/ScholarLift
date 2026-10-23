"""Deterministic domain-trust classification.

Maps a domain string to a TrustTier based on TLD patterns and a curated allow-list.
All logic is pure and requires no network access.
"""
from __future__ import annotations

import re

from scholarship_finder.domain.models import TrustTier

# ── Pattern tables ─────────────────────────────────────────────────────────────

# Official academic / government TLD/domain patterns
_OFFICIAL_PATTERNS: list[re.Pattern[str]] = [p for p in map(re.compile, [
    r"\.edu$",
    r"\.edu\.[a-z]{2}$",          # .edu.au, .edu.sg, .edu.pk, .edu.in
    r"\.ac\.[a-z]{2}$",           # .ac.uk, .ac.jp, .ac.in, .ac.nz
    r"(?:^|\.)(uni-[a-z]+)\.de$", # uni-muenchen.de, uni-bonn.de
    r"(?:^|\.)(tu-[a-z]+)\.de$",  # tu-berlin.de, tu-munich.de
    r"(?:^|\.)(th-[a-z]+)\.de$",  # th-rosenheim.de
    r"(?:^|\.)(hs-[a-z]+)\.de$",  # hs-mannheim.de
    r"(?:^|\.)(fh-[a-z]+)\.de$",  # fh-aachen.de
    r"\.gov$",
    r"\.gov\.[a-z]{2}$",          # .gov.au, .gov.sg, .gov.in
    r"\.gc\.ca$",                 # Government of Canada
    r"\.gouv\.fr$",
    r"\.bund\.de$",
    r"\.admin\.ch$",
    r"^ntu\.edu\.sg$",
    r"^nus\.edu\.sg$",
    r"^mit\.edu$",
    r"^stanford\.edu$",
    r"^cam\.ac\.uk$",
    r"^ox\.ac\.uk$",
    r"^ethz\.ch$",
    r"^epfl\.ch$",
    r"^tum\.de$",
    r"^kit\.edu$",
    r"^rwth-aachen\.de$",
])]

# Recognised funders / scholarship bodies
_FUNDER_DOMAINS: frozenset[str] = frozenset({
    "daad.de",
    "fulbright.org",
    "fulbrightscholars.org",
    "chevening.org",
    "commonwealthscholarships.ac.uk",
    "eramus.eu",
    "erasmusplus.eu",
    "eacea.ec.europa.eu",
    "studyineurope.eu",
    "scholars4dev.com",
    "research.gov",           # NSF
    "nserc-crsng.gc.ca",      # NSERC Canada
    "sshrc-crsh.gc.ca",       # SSHRC Canada
    "mos.gov.cn",             # China Scholarship Council
    "csc.edu.cn",
    "a-star.edu.sg",          # A*STAR Singapore
    "moe.gov.sg",
    "studienstiftung.de",
    "avh.de",                 # Alexander von Humboldt
    "humboldt-foundation.de",
    "dkjs.de",
    "aauw.org",
    "schmidtfutures.com",
    "gates.com",
    "gatesfoundation.org",
    "soros.org",
    "opensocietyfoundations.org",
    "oppenheimer.co.za",
    "mastercardfdn.org",
    "africainsuranceorganisation.org",
})

# Known aggregator domains (leads only; not Top-5 eligible by themselves)
_AGGREGATOR_DOMAINS: frozenset[str] = frozenset({
    "scholarships.com",
    "scholarship-positions.com",
    "mastersportal.eu",
    "phdportal.eu",
    "bachelorsportal.eu",
    "findamasters.com",
    "findaphd.com",
    "scholars.co",
    "opportunitiesforafricans.com",
    "opportunitydesk.org",
    "afterschoolafrica.com",
    "youthop.com",
    "abroadly.com",
    "hotcoursesabroad.com",
    "idp.com",
    "studylink.com",
    "studyabroad.com",
    "collegexpress.com",
    "niche.com",
    "cappex.com",
    "fastweb.com",
    "collegeboard.org",
    "petersons.com",
    # Additional aggregators to filter
    "onlinemastersdegrees.com",
    "studyportals.com",
    "study.eu",
    "mastersdegrees.com",
    "phdstudies.com",
    "bachelorsdegrees.com",
    "scholarships.net",
    "scholarshipowl.com",
    "scholarships360.org",
    "scholarshipamerica.org",
    "unigo.com",
    "chegg.com",
    "cappex.com",
    "zing.collegeboard.org",
    "bigfuture.collegeboard.org",
    "youthopportunities.org",
    "youthop.com",
    "opportunitiesforyouths.com",
    "youthforchange.org",
    "scholarshiptab.com",
    "scholarshipsinindia.com",
    "budgains.com",
    "scholarroo.com",
    "scholarshipsinindia.in",
    "scholarshipsite.com",
    "scholarship-portal.com",
    "scholarships2.com",
    "internationalscholarships.com",
    "scholarship-links.com",
    "scholarshiphunter.com",
    "scholarshippositions.com",
    "scholarshipscanada.com",
    "usascholarships.com",
    "ukscholarships.com",
    "australiascholarships.com",
})


# ── Public API ─────────────────────────────────────────────────────────────────

def classify_domain(domain: str) -> TrustTier:
    """Return the TrustTier for *domain* (lower-cased, www-stripped).

    Args:
        domain: e.g. ``"mit.edu"``, ``"daad.de"``, ``"scholarship-positions.com"``

    Returns:
        TrustTier enum value.
    """
    d = domain.lower().strip()
    if d.startswith("www."):
        d = d[4:]

    # Aggregator check first (highest false-positive risk)
    if d in _AGGREGATOR_DOMAINS:
        return TrustTier.AGGREGATOR

    # Recognised funder
    if d in _FUNDER_DOMAINS:
        return TrustTier.RECOGNIZED_FUNDER

    # Official institution patterns
    for pattern in _OFFICIAL_PATTERNS:
        if pattern.search(d):
            return TrustTier.OFFICIAL_INSTITUTION

    return TrustTier.UNKNOWN


def is_top_5_eligible(trust_tier: TrustTier) -> bool:
    """An AGGREGATOR domain alone cannot appear in the Top 5.

    It must resolve to an official page (the Auditor follows one hop within
    the same registered domain to find the official URL).
    """
    return trust_tier in (TrustTier.OFFICIAL_INSTITUTION, TrustTier.RECOGNIZED_FUNDER)
