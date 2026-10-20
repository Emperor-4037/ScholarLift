"""Pure deterministic GPA normalization strategies.

All results are estimates — labelled as such in the UI.
An institution-stated conversion captured by the Auditor overrides any generic formula.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GPAResult:
    """Result of a GPA conversion."""
    value: float
    target_scale: str
    method: str
    confidence: float   # 0.0 – 1.0
    note: str


# ── Individual strategy functions ────────────────────────────────────────────

def _bavarian(cgpa: float, scale: float, g_min: float | None = None) -> GPAResult:
    """Modified Bavarian formula for German university targets.

    N = 1 + 3 * (G_max - G_student) / (G_max - G_min)
    Result is on the German 1.0–4.0 scale (1.0 = best).
    """
    g_max = scale
    if g_min is None:
        # Typical passing grade: 40% of scale
        g_min = scale * 0.4
    if g_max <= g_min:
        raise ValueError(f"g_max ({g_max}) must be greater than g_min ({g_min})")
    raw = 1.0 + 3.0 * (g_max - cgpa) / (g_max - g_min)
    clamped = max(1.0, min(4.0, raw))
    return GPAResult(
        value=round(clamped, 2),
        target_scale="german_1to4",
        method="bavarian_formula",
        confidence=0.70,
        note="Estimate via modified Bavarian formula. Verify with target institution.",
    )


def _indian_to_us(cgpa: float, scale: float, **_: object) -> GPAResult:
    """Map Indian 10-point CGPA to US 4.0 GPA using standard breakpoints."""
    if scale != 10.0:
        # Normalise to 10-point first
        cgpa = cgpa / scale * 10.0

    breakpoints = [
        (9.0, 4.00),
        (8.5, 3.90),
        (8.0, 3.70),
        (7.5, 3.50),
        (7.0, 3.30),
        (6.5, 3.10),
        (6.0, 3.00),
        (5.5, 2.70),
        (5.0, 2.50),
        (4.5, 2.30),
        (0.0, 2.00),
    ]
    gpa_4 = 2.00
    for threshold, gpa in breakpoints:
        if cgpa >= threshold:
            gpa_4 = gpa
            break

    return GPAResult(
        value=round(gpa_4, 2),
        target_scale="us_4point",
        method="indian_to_us",
        confidence=0.60,
        note="Estimate using Indian 10-point to US 4.0 breakpoint table.",
    )


def _percentage_to_us(cgpa: float, scale: float, **_: object) -> GPAResult:
    """Convert percentage marks to US 4.0 GPA."""
    # Normalise to percentage
    pct = cgpa if scale == 100.0 else (cgpa / scale * 100.0)

    breakpoints = [
        (90.0, 4.00),
        (85.0, 3.90),
        (80.0, 3.70),
        (75.0, 3.50),
        (70.0, 3.30),
        (65.0, 3.10),
        (60.0, 3.00),
        (55.0, 2.70),
        (50.0, 2.50),
        (45.0, 2.30),
        (0.0, 2.00),
    ]
    gpa_4 = 2.00
    for threshold, gpa in breakpoints:
        if pct >= threshold:
            gpa_4 = gpa
            break

    return GPAResult(
        value=round(gpa_4, 2),
        target_scale="us_4point",
        method="percentage_to_us",
        confidence=0.50,
        note="Estimate via percentage-to-US-4.0 breakpoint table.",
    )


def _passthrough(cgpa: float, scale: float, **_: object) -> GPAResult:
    """Identity conversion: input is already on the target scale."""
    return GPAResult(
        value=round(cgpa, 2),
        target_scale=f"native_{scale}",
        method="passthrough",
        confidence=1.00,
        note="No conversion applied; value already on the target scale.",
    )


# ── Public registry & API ─────────────────────────────────────────────────────

GPA_STRATEGY_REGISTRY: dict[str, object] = {
    "bavarian": _bavarian,
    "indian_to_us": _indian_to_us,
    "percentage_to_us": _percentage_to_us,
    "passthrough": _passthrough,
}


def normalize_gpa(
    cgpa: float,
    scale: float,
    target: str = "us_4point",
    g_min: float | None = None,
) -> GPAResult:
    """Convert a CGPA to the requested target scale.

    Args:
        cgpa:   Raw CGPA (must be 0 ≤ cgpa ≤ scale).
        scale:  Maximum possible value on the source scale (e.g. 10.0, 4.0, 100.0).
        target: Target scale key. Valid values: "us_4point", "german_1to4", "passthrough".
        g_min:  Optional passing-grade floor for the Bavarian formula.

    Returns:
        GPAResult with value, method, confidence, and note.

    Raises:
        ValueError: On invalid inputs.
    """
    if scale <= 0:
        raise ValueError(f"scale must be positive, got {scale}")
    if not (0.0 <= cgpa <= scale):
        raise ValueError(f"cgpa {cgpa} is outside [0, {scale}]")

    strategy_map: dict[str, str] = {
        "us_4point": "indian_to_us" if scale == 10.0 else
                     "percentage_to_us" if scale == 100.0 else
                     "passthrough",
        "german_1to4": "bavarian",
        "passthrough": "passthrough",
    }

    strategy_name = strategy_map.get(target, "passthrough")
    strategy_fn = GPA_STRATEGY_REGISTRY[strategy_name]

    return strategy_fn(cgpa, scale, g_min=g_min)  # type: ignore[call-arg,operator]
