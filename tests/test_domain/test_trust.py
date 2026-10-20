"""Tests for domain trust classification."""
from __future__ import annotations

import pytest

from scholarship_finder.domain.models import TrustTier
from scholarship_finder.domain.trust import classify_domain, is_top_5_eligible


class TestClassifyDomain:
    # Official institutions
    def test_edu_tld(self):
        assert classify_domain("mit.edu") == TrustTier.OFFICIAL_INSTITUTION

    def test_ac_uk(self):
        assert classify_domain("cam.ac.uk") == TrustTier.OFFICIAL_INSTITUTION

    def test_ac_in(self):
        assert classify_domain("iitb.ac.in") == TrustTier.OFFICIAL_INSTITUTION

    def test_edu_sg(self):
        assert classify_domain("nus.edu.sg") == TrustTier.OFFICIAL_INSTITUTION

    def test_edu_au(self):
        assert classify_domain("uq.edu.au") == TrustTier.OFFICIAL_INSTITUTION

    def test_german_uni(self):
        assert classify_domain("uni-muenchen.de") == TrustTier.OFFICIAL_INSTITUTION

    def test_tu_domain(self):
        assert classify_domain("tu-berlin.de") == TrustTier.OFFICIAL_INSTITUTION

    def test_gov_tld(self):
        assert classify_domain("nsf.gov") == TrustTier.OFFICIAL_INSTITUTION

    def test_gc_ca(self):
        assert classify_domain("nserc-crsng.gc.ca") == TrustTier.RECOGNIZED_FUNDER

    # Recognised funders
    def test_daad(self):
        assert classify_domain("daad.de") == TrustTier.RECOGNIZED_FUNDER

    def test_fulbright(self):
        assert classify_domain("fulbright.org") == TrustTier.RECOGNIZED_FUNDER

    def test_chevening(self):
        assert classify_domain("chevening.org") == TrustTier.RECOGNIZED_FUNDER

    # Aggregators
    def test_mastersportal(self):
        assert classify_domain("mastersportal.eu") == TrustTier.AGGREGATOR

    def test_findamasters(self):
        assert classify_domain("findamasters.com") == TrustTier.AGGREGATOR

    def test_scholarship_positions(self):
        assert classify_domain("scholarship-positions.com") == TrustTier.AGGREGATOR

    # Unknown
    def test_random_blog(self):
        assert classify_domain("myrandomscholarshipblog.com") == TrustTier.UNKNOWN

    # www prefix stripping
    def test_www_stripped(self):
        assert classify_domain("www.mit.edu") == TrustTier.OFFICIAL_INSTITUTION

    # Case insensitivity
    def test_uppercase_input(self):
        assert classify_domain("MIT.EDU") == TrustTier.OFFICIAL_INSTITUTION


class TestTop5Eligibility:
    def test_official_eligible(self):
        assert is_top_5_eligible(TrustTier.OFFICIAL_INSTITUTION) is True

    def test_funder_eligible(self):
        assert is_top_5_eligible(TrustTier.RECOGNIZED_FUNDER) is True

    def test_aggregator_not_eligible(self):
        assert is_top_5_eligible(TrustTier.AGGREGATOR) is False

    def test_unknown_not_eligible(self):
        assert is_top_5_eligible(TrustTier.UNKNOWN) is False
