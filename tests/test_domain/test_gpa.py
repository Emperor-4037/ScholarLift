"""Tests for GPA normalization strategies."""
from __future__ import annotations

import pytest

from scholarship_finder.domain.gpa import normalize_gpa, GPAResult


class TestBavarianFormula:
    def test_perfect_gpa(self):
        result = normalize_gpa(10.0, 10.0, target="german_1to4")
        assert result.method == "bavarian_formula"
        assert result.value == pytest.approx(1.0, abs=0.01)

    def test_average_gpa_8_2(self):
        # 8.2/10.0 with g_min=4.0 → N = 1 + 3*(10-8.2)/(10-4) = 1 + 3*1.8/6 = 1.9
        result = normalize_gpa(8.2, 10.0, target="german_1to4")
        expected = 1.0 + 3.0 * (10.0 - 8.2) / (10.0 - 4.0)
        assert result.value == pytest.approx(expected, abs=0.01)
        assert 1.0 <= result.value <= 4.0

    def test_low_gpa_clamped(self):
        # GPA at the passing threshold should give ~4.0
        result = normalize_gpa(4.0, 10.0, target="german_1to4")
        assert result.value == pytest.approx(4.0, abs=0.01)

    def test_confidence_is_0_7(self):
        result = normalize_gpa(8.0, 10.0, target="german_1to4")
        assert result.confidence == pytest.approx(0.70)

    def test_note_contains_estimate(self):
        result = normalize_gpa(8.0, 10.0, target="german_1to4")
        assert "estimate" in result.note.lower() or "bavarian" in result.note.lower()


class TestIndianToUS:
    def test_cgpa_9_5_maps_to_4_0(self):
        result = normalize_gpa(9.5, 10.0, target="us_4point")
        assert result.value == pytest.approx(4.0)
        assert result.method == "indian_to_us"

    def test_cgpa_8_2_maps_to_3_7(self):
        result = normalize_gpa(8.2, 10.0, target="us_4point")
        assert result.value == pytest.approx(3.70, abs=0.01)

    def test_cgpa_7_0_maps_to_3_3(self):
        result = normalize_gpa(7.0, 10.0, target="us_4point")
        assert result.value == pytest.approx(3.30, abs=0.01)

    def test_confidence_is_0_6(self):
        result = normalize_gpa(8.0, 10.0, target="us_4point")
        assert result.confidence == pytest.approx(0.60)


class TestPercentageToUS:
    def test_95_percent_maps_to_4_0(self):
        result = normalize_gpa(95.0, 100.0, target="us_4point")
        assert result.method == "percentage_to_us"
        assert result.value == pytest.approx(4.0)

    def test_72_percent_maps_to_3_3(self):
        result = normalize_gpa(72.0, 100.0, target="us_4point")
        assert result.value == pytest.approx(3.30, abs=0.01)


class TestPassthrough:
    def test_already_on_4_point(self):
        result = normalize_gpa(3.7, 4.0, target="passthrough")
        assert result.method == "passthrough"
        assert result.value == pytest.approx(3.7)
        assert result.confidence == pytest.approx(1.0)


class TestValidation:
    def test_cgpa_above_scale_raises(self):
        with pytest.raises(ValueError, match="outside"):
            normalize_gpa(10.5, 10.0)

    def test_negative_cgpa_raises(self):
        with pytest.raises(ValueError, match="outside"):
            normalize_gpa(-1.0, 10.0)

    def test_zero_scale_raises(self):
        with pytest.raises(ValueError, match="positive"):
            normalize_gpa(5.0, 0.0)

    def test_unknown_target_falls_back_to_passthrough(self):
        result = normalize_gpa(3.5, 4.0, target="nonexistent_target")
        assert result.method == "passthrough"
