"""Agent 3: Source Auditor — verify candidates into real opportunities.

Streaming per-candidate pipeline (one coroutine per candidate under a single
semaphore, results kept as each finishes):

  KB check → page text (Tavily focused chunks → full page) → deterministic
  pre-gate → single-page schema-constrained extraction (+ regex hints) →
  build + span-verify → KB write

* Every evidence quote is span-verified against the fetched page text.
* Listing pages yield Leads and, one hop deep, their award links become new
  candidates audited in the same pass (deterministic, no LLM).
* The knowledge base (profile-independent facts only) lets later runs reuse
  verified opportunities and skip known-bad pages without fetch or LLM.
* Stops starting new work at the soft deadline and cuts outstanding work
  when discovery time runs out; finished pages are always kept.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from scholarship_finder.domain.models import (
    Candidate,
    DeadlineInfo,
    EligibilityCriteria,
    EvidenceRef,
    FieldStatus,
    FundingInfo,
    LanguageRequirement,
    Lead,
    LLMTier,
    Opportunity,
    OpportunityField,
    SwarmState,
    TrustTier,
    VerificationResult,
)
from scholarship_finder.domain.eligibility import degree_levels_of
from scholarship_finder.domain.listing import award_links, is_listing
from scholarship_finder.domain.pregate import pregate
from scholarship_finder.domain.source_filters import is_news_or_report
from scholarship_finder.domain.req_hints import extract_hints, hints_block
from scholarship_finder.domain.span_verifier import verify_span
from scholarship_finder.domain.url import extract_domain
from scholarship_finder.engine.context import emit, soft_deadline_passed
from scholarship_finder.engine import context as run_context
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.fetch import FetchPort
from scholarship_finder.ports.llm import LLMPort, TaskSpec

logger = logging.getLogger(__name__)

_FUNDING_RE = re.compile(
    r"scholarship|fellowship|stipend|funding|tuition.?waiver|bursar|"
    r"stipendium|bourse|beca|beurs|assistantship|grant", re.I)
_MIN_CONTENT = 200
_EXCERPT = 3200
_ONE_HOP_CAP = 10
_TRUSTED = (TrustTier.OFFICIAL_INSTITUTION, TrustTier.RECOGNIZED_FUNDER)
_KB_REJECT_OK = frozenset({"junk", "news_or_report", "too_short",
                           "no_funding_terms", "not_verified"})
_ONE_HOP_MIN_TIME = 45.0
_ROUND_S = (70.0, 60.0)    # wall-clock cap per audit round (first pass, expansion pass)
_FOCUS_QUERY = ("eligibility requirements who can apply nationality degree level "
                "GPA language test funding amount covered costs deadline how to apply")

_SYSTEM = """You extract facts from ONE web page about a scholarship/fellowship.
Return ONE JSON object with these keys:
is_specific_opportunity (true only if the page describes one specific funded award),
is_listing_page (true if it mainly lists several different awards),
listed_opportunity_names [str], name, provider, host_countries [str],
degree_levels [str], fields_of_study [str],
funding {coverage, amount_text, amount_value (number|null), currency, period, covers [str]},
deadline {text, iso_date ("YYYY-MM-DD"|null)},
eligibility {nationalities [str], nationality_text, open_to_international (bool|null),
  degree_levels [str], fields_of_study [str], min_gpa_value (number|null),
  min_gpa_scale (number|null), min_gpa_text,
  language_requirements [{test, min_score, text}],
  min_work_experience_months (int|null), max_age (int|null), other [str]},
selection_criteria [str], how_to_apply, application_url,
evidence {name, deadline, funding, eligibility} — each a sentence copied VERBATIM
from the page (15-300 chars), or "" if the page has none.
Only facts stated on the page. Use null/""/[] when absent. Never guess."""

_S = {"type": "string"}
_SA = {"type": "array", "items": {"type": "string"}}
_NUM = {"type": ["number", "null"]}
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_specific_opportunity": {"type": "boolean"},
        "is_listing_page": {"type": "boolean"},
        "listed_opportunity_names": _SA,
        "name": _S, "provider": _S,
        "host_countries": _SA, "degree_levels": _SA, "fields_of_study": _SA,
        "funding": {"type": "object", "properties": {
            "coverage": _S, "amount_text": _S, "amount_value": _NUM,
            "currency": _S, "period": _S, "covers": _SA}},
        "deadline": {"type": "object", "properties": {
            "text": _S, "iso_date": {"type": ["string", "null"]}}},
        "eligibility": {"type": "object", "properties": {
            "nationalities": _SA, "nationality_text": _S,
            "open_to_international": {"type": ["boolean", "null"]},
            "degree_levels": _SA, "fields_of_study": _SA,
            "min_gpa_value": _NUM, "min_gpa_scale": _NUM, "min_gpa_text": _S,
            "language_requirements": {"type": "array", "items": {
                "type": "object", "properties": {
                    "test": _S, "min_score": _NUM, "text": _S}}},
            "min_work_experience_months": _NUM, "max_age": _NUM, "other": _SA}},
        "selection_criteria": _SA, "how_to_apply": _S, "application_url": _S,
        "evidence": {"type": "object", "properties": {
            "name": _S, "deadline": _S, "funding": _S, "eligibility": _S}},
    },
    "required": ["is_specific_opportunity", "is_listing_page", "name", "evidence"],
}


@dataclass
class _Run:
    """Mutable per-invocation funnel state (single event loop, no locking needed)."""
    state: SwarmState
    kb: Any
    target: int
    opps: list[Opportunity]
    leads: list[Lead]
    verified: int = 0
    kb_hits: int = 0
    fetched: int = 0
    pregated: int = 0
    extracted: int = 0
    one_hop: int = 0
    weak: int = 0
    levels: set = field(default_factory=set)
    replay_listing: list = field(default_factory=list)
    gate: dict[str, int] = field(default_factory=dict)
    focused: dict[str, str] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)

    def count(self, reason: str) -> None:
        self.gate[reason] = self.gate.get(reason, 0) + 1

    def enough(self) -> bool:
        return self.verified >= self.target + 4


def _dtl() -> float:
    ctx = run_context.current()
    return ctx.discovery_time_left() if ctx is not None else float("inf")


def _concurrency() -> int:
    try:
        from scholarship_finder.config import get_settings
        return max(3, int(get_settings().local_llm_parallel) + 2)
    except Exception:
        return 3


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()[:16]


def _profile_tokens(profile: Any) -> set[str]:
    if profile is None:
        return set()
    p = profile.preferences
    words = " ".join([p.degree_level or "", *p.target_fields, *p.countries,
                      profile.nationality or "",
                      *(getattr(e, "field", "") or "" for e in profile.education)])
    toks = set(re.findall(r"[a-z]{3,}", words.lower()))
    lv = degree_levels_of(p.degree_level or "")
    if "masters" in lv:
        toks |= {"master", "masters", "msc", "postgraduate", "graduate"}
    if "phd" in lv:
        toks |= {"phd", "doctoral", "doctorate"}
    if "bachelors" in lv:
        toks |= {"bachelor", "bachelors", "undergraduate"}
    return toks


class SourceAuditorAgent:
    def __init__(self, llm_port: LLMPort, fetch_port: FetchPort) -> None:
        self._llm = llm_port
        self._fetch = fetch_port

    async def run(self, state: SwarmState, bus: EventBus) -> dict:
        from scholarship_finder.adapters.knowledge_base import get_kb
        cands = [c for c in state.candidates if not c.audited
                 and not is_junk_url(c.canonical_url or "")]
        if not cands:
            return {}
        r = _Run(state=state, kb=get_kb(), target=_target(),
                 opps=list(state.opportunities), leads=list(state.leads))
        r.verified = sum(1 for o in r.opps if o.is_existence_verified())
        r.seen = {c.canonical_url for c in state.candidates}
        r.levels = _target_levels(state.profile)
        await self._prefetch_focused(r, [c.canonical_url for c in cands])

        sem = asyncio.Semaphore(_concurrency())
        tasks: set[asyncio.Task] = set()

        def spawn(c: Candidate, depth: int) -> None:
            tasks.add(asyncio.create_task(self._guarded(sem, r, c, depth, spawn)))

        for c in cands:
            spawn(c, 0)
        t_end = time.monotonic() + _ROUND_S[min(state.run_meta.expansion_rounds, 1)]
        while tasks:
            if _dtl() <= 0 or time.monotonic() >= t_end:
                break
            done, _ = await asyncio.wait(
                set(tasks), timeout=max(0.1, min(_dtl(), t_end - time.monotonic(), 5.0)),
                return_when=asyncio.FIRST_COMPLETED)
            tasks -= done
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    logger.warning("auditor task failed: %r", t.exception())
        if tasks:
            emit("source_auditor",
                 f"Audit time up — keeping finished pages, dropping {len(tasks)} slow page(s) in flight")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        reasons = ("; " + ", ".join(f"{k} {v}" for k, v in sorted(r.gate.items()))
                   if r.gate else "")
        emit("source_auditor",
             f"Audit funnel: {len(cands)} cands → {r.kb_hits} kb → {r.fetched} fetched → "
             f"{r.pregated} pregated → {r.extracted} extracted → {r.verified} verified "
             f"(+{r.weak} weak-source, {len(r.leads) - len(state.leads)} leads, "
             f"{r.one_hop} one-hop){reasons}")
        return {
            "opportunities": _dedupe(r.opps),
            "leads": _dedupe_leads(r.leads),
            "candidates": state.candidates,
        }

    # ── per-candidate pipeline ──────────────────────────────────────────────
    async def _guarded(self, sem, r: _Run, c: Candidate, depth: int, spawn) -> None:
        async with sem:
            if soft_deadline_passed() or r.enough() or _dtl() <= 0:
                return
            try:
                await self._process(r, c, depth, spawn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("auditor: %s failed: %r", c.canonical_url, exc, exc_info=True)

    async def _process(self, r: _Run, c: Candidate, depth: int, spawn) -> None:
        url = c.canonical_url
        c.audited = True
        if r.kb is not None and self._kb_lookup(r, c):
            if c in r.replay_listing and depth == 0:     # cached listing: re-derive its children
                r.replay_listing.remove(c)
                full = r.kb.get_page(url) or await self._page_text(c)
                await self._one_hop(r, c, full, spawn)
            return
        text = _sanitize_page(r.focused.get(url) or await self._page_text(c))
        if not text:
            r.count("fetch_failed")          # transient — never cached
            return
        r.fetched += 1
        h = _hash(text)
        ok, reason = pregate(c, text, r.state.profile)
        if not ok:
            r.count(reason)
            if r.kb is not None and reason in _KB_REJECT_OK:
                r.kb.put_verdict(url, h, "rejected", reason=reason)
            return
        r.pregated += 1
        hints = extract_hints(text)
        item = await self._extract_one(c, text, hints)
        if item is None:
            r.count("llm_failed")            # transient — never cached
            return
        r.extracted += 1
        listing = bool(item.get("is_listing_page")) and not item.get("is_specific_opportunity")
        full = ""
        if listing or not item.get("is_specific_opportunity"):
            full = await self._page_text(c)
            listing = listing or is_listing(full, url)
        if listing:
            names = [_clean_text(n.get("name", "") if isinstance(n, dict) else n)
                     for n in (item.get("listed_opportunity_names") or [])
                     if isinstance(n, (str, dict))][:10]
            names = [n for n in names if _name_ok(n)]
            r.leads += [Lead(name=n, source_url=url) for n in names]
            if r.kb is not None:
                r.kb.put_verdict(url, h, "listing", lead_names=names)
            if depth == 0:
                await self._one_hop(r, c, full, spawn)
            return
        if c.trust_tier == TrustTier.AGGREGATOR or is_news_or_report(url, c.title or ""):
            r.count("aggregator_or_news")     # leads-only, never an opportunity
            return
        opp = None
        try:
            opp = _build_opp(item, c, text, hints)
        except Exception as exc:
            logger.warning("auditor: build failed for %s: %r", url, exc)
        if opp is not None and _off_level(opp, r.levels):
            r.count("level_mismatch_extracted")   # student-specific: not cached as a verdict
            if r.kb is not None and opp.is_existence_verified():
                r.kb.put_opportunity(url, opp, h, c.trust_tier.value)
            return
        if opp is None or not opp.is_existence_verified():
            if r.kb is not None:
                r.kb.put_verdict(url, h, "rejected", reason="not_verified")
            r.count("not_verified")
            return
        if r.kb is not None:
            r.kb.put_opportunity(url, opp, h, c.trust_tier.value)
        self._accept(r, opp)

    def _kb_lookup(self, r: _Run, c: Candidate) -> bool:
        """True when the KB fully answered this candidate (no fetch/LLM needed)."""
        url = c.canonical_url
        cached = r.kb.get_opportunity(url)
        if cached is not None:
            r.kb_hits += 1
            if _off_level(cached, r.levels):
                r.count("level_mismatch_extracted")
            else:
                self._accept(r, cached, note="(cached)")
            return True
        v = r.kb.get_verdict(url)
        if not v:
            return False
        r.kb_hits += 1
        if v.get("verdict") == "listing":
            r.leads += [Lead(name=n, source_url=url) for n in (v.get("lead_names") or [])[:10]]
            r.replay_listing.append(c)
            return True
        if v.get("verdict") == "rejected":
            r.count("kb_rejected")
            return True
        return False

    def _accept(self, r: _Run, opp: Opportunity, note: str = "") -> None:
        if _is_dup(opp, r.opps):
            return
        r.opps.append(opp)
        if not opp.is_existence_verified():
            return
        if opp.verification.trust_tier in _TRUSTED:
            r.verified += 1
            emit("source_auditor",
                 f"Verified {r.verified}/{r.target}: {opp.name} — "
                 f"{extract_domain(opp.official_url)} {note}".rstrip(),
                 verified=r.verified)
        else:
            r.weak += 1
            emit("source_auditor",
                 f"Weak source (not counted): {opp.name} — {extract_domain(opp.official_url)}")

    async def _one_hop(self, r: _Run, parent: Candidate, full: str, spawn) -> None:
        if (parent.trust_tier not in _TRUSTED or r.one_hop >= _ONE_HOP_CAP
                or _dtl() <= _ONE_HOP_MIN_TIME or not full):
            return
        links = award_links(full, parent.canonical_url, _profile_tokens(r.state.profile))
        children = []
        for name, link, _score in links:
            if r.one_hop >= _ONE_HOP_CAP:
                break
            if link in r.seen:
                continue
            r.seen.add(link)
            r.one_hop += 1
            children.append(Candidate(
                url=link, canonical_url=link, domain=extract_domain(link),
                trust_tier=parent.trust_tier, title=name, origin="listing",
                country_hint=parent.country_hint, kind_hint=parent.kind_hint))
        if not children:
            return
        emit("source_auditor",
             f"Listing {extract_domain(parent.canonical_url)} → {len(children)} award pages")
        r.state.candidates.extend(children)
        await self._prefetch_focused(r, [ch.canonical_url for ch in children])
        for ch in children:
            spawn(ch, 1)

    async def _prefetch_focused(self, r: _Run, urls: list[str]) -> None:
        extractor = getattr(self._fetch, "_tavily", None) or getattr(self._fetch, "tavily", None)
        if extractor is None or not hasattr(extractor, "extract_focused") or not urls:
            return
        chunks = [urls[i:i + 20] for i in range(0, min(len(urls), 60), 20)]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*[extractor.extract_focused(u, _FOCUS_QUERY) for u in chunks],
                               return_exceptions=True),
                timeout=max(5.0, min(40.0, _dtl())))
        except Exception:
            return
        for res in results:
            if isinstance(res, dict):
                r.focused.update(res)

    async def _page_text(self, c: Candidate) -> str:
        try:
            doc = await self._fetch.fetch(c.canonical_url)
            return doc.normalized_text or ""
        except Exception:
            return ""

    async def _extract_one(self, c: Candidate, text: str, hints: dict) -> dict | None:
        prompt = "\n".join(x for x in (
            hints_block(hints), f"PAGE url={c.canonical_url} title={c.title or ''}",
            _excerpt(text)) if x)
        spec = TaskSpec(tier=LLMTier.FAST, system=_SYSTEM, prompt=prompt,
                        json_mode=True, json_schema=_SCHEMA, max_output_tokens=800,
                        temperature=0.1, purpose="audit_extract")
        for attempt in (0, 1):
            try:
                res = await asyncio.wait_for(self._llm.complete_json(spec),
                                             timeout=max(5.0, _dtl()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt == 0 and type(exc).__name__ == "LLMUnavailableError" and _dtl() > 20:
                    await asyncio.sleep(3)
                    continue
                logger.warning("auditor extract failed for %s: %r", c.canonical_url, exc)
                return None
            item = _single_item(res.parsed)
            logger.info("audit page %s → name=%r specific=%s listing=%s", c.canonical_url,
                        (item or {}).get("name"), (item or {}).get("is_specific_opportunity"),
                        (item or {}).get("is_listing_page"))
            return item
        return None


_TLD_COUNTRY = (
    (".ac.uk", "United Kingdom"), (".gov.uk", "United Kingdom"), (".co.uk", "United Kingdom"),
    (".edu.au", "Australia"), (".gov.au", "Australia"), (".edu.sg", "Singapore"),
    (".gov.sg", "Singapore"), (".gc.ca", "Canada"), (".edu", "United States"),
    (".de", "Germany"), (".ca", "Canada"), (".sg", "Singapore"), (".nl", "Netherlands"),
    (".fr", "France"), (".se", "Sweden"), (".ie", "Ireland"), (".nz", "New Zealand"),
    (".jp", "Japan"), (".kr", "South Korea"), (".au", "Australia"), (".uk", "United Kingdom"),
)


def _country_from_url(url: str) -> str:
    """Host country implied by an institution's TLD; '' when ambiguous (.org/.com)."""
    host = extract_domain(url)
    return next((c for t, c in _TLD_COUNTRY if host.endswith(t)), "")


def _target_levels(profile: Any) -> set[str]:
    if profile is None:
        return set()
    return degree_levels_of(getattr(profile.preferences, "degree_level", "") or "")


def _off_level(opp: Opportunity, target: set[str]) -> bool:
    """True when the opportunity states degree levels and none match the target."""
    if not target:
        return False
    stated: set[str] = set()
    for lv in [*opp.degree_levels, *opp.eligibility.degree_levels]:
        stated |= degree_levels_of(lv)
    return bool(stated) and not (stated & target)


def _single_item(parsed: Any) -> dict | None:
    """Accept a bare object, the legacy {"pages": [...]} wrapper, or a list."""
    if isinstance(parsed, dict) and isinstance(parsed.get("pages"), list):
        parsed = parsed["pages"]
    if isinstance(parsed, list):
        parsed = next((x for x in parsed if isinstance(x, dict)), None)
    return parsed if isinstance(parsed, dict) else None


_KEY_LINE = re.compile(
    r"deadline|eligib|require|gpa|ielts|toefl|pte|duolingo|nationality|citizen|resident|"
    r"degree|who can apply|how to apply|criteria|master|bachelor|phd|doctoral|"
    r"benefit|award|amount|stipend|covers?|tuition|allowance|per (month|year|annum)|"
    r"open to|applicants? must|candidates? must|minimum|closing", re.I)


def _excerpt(text: str) -> str:
    """Title line + requirement/funding-dense lines in page order, ≤_EXCERPT chars."""
    lines = [ln.strip() for ln in re.split(r"\n+", text or "") if ln.strip()]
    if not lines:
        return (text or "")[:_EXCERPT]
    keep = lines[:1] + [ln for ln in lines[1:]
                        if _FUNDING_RE.search(ln) or _KEY_LINE.search(ln)]
    out = "\n".join(keep)
    if len(out) < 600:                      # sparse page: fall back to the head
        out = "\n".join(lines)
    return out[:_EXCERPT]


_GENERIC_NAMES = re.compile(
    r"^(page|home|index|scholarships?|award|program|apply|admissions?|"
    r"untitled|n/?a|none|unknown|title|search|login|main|welcome)$", re.I)


def _sanitize_page(t: str) -> str:
    """Fix mojibake but keep line structure (the excerpt and link parsing need it)."""
    if not t:
        return t or ""
    t = re.sub(r"(?<=[A-Za-z])\ufffd(?=[a-z]|\s|s)", "\u2019", t).replace("\ufffd", " ")
    t = re.sub(r"[ \t\f\v\r]+", " ", t)
    return re.sub(r"\n\s*\n\s*", "\n\n", t).strip()


def _d(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _ls(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, (str, int, float))][:20]
    return []


def _clean_text(s: Any) -> str:
    """Normalize mojibake: U+FFFD between letters → apostrophe, else space."""
    if not isinstance(s, str):
        s = str(s) if s else ""
    if not s:
        return s
    s = re.sub(r"(?<=[A-Za-z])�(?=[a-z])", "’", s)
    s = re.sub(r"(?<=[A-Za-z])�(?=\s|s)", "’", s)
    s = s.replace("�", " ")
    return re.sub(r"\s+", " ", s).strip()


def _name_ok(name: str) -> bool:
    return bool(name) and len(name) >= 4 and not _GENERIC_NAMES.match(name)


def _build_opp(item: dict, cand: Candidate, page_text: str,
               hints: dict | None = None) -> Opportunity | None:
    name = _clean_text(item.get("name") or "")
    if not _name_ok(name):
        # Fall back to the page title (first line / <title>-like heading).
        name = _clean_text((cand.title or "").split("–")[0].split("|")[0])
    if not _name_ok(name):
        return None
    ev = _d(item.get("evidence"))
    fields = []
    for fn, key in (("name", "name"), ("deadline", "deadline"),
                    ("funding", "funding"), ("eligibility", "eligibility")):
        quote = ev.get(key) if isinstance(ev.get(key), str) else ""
        status = FieldStatus.UNVERIFIED
        ref = None
        if quote:
            vr = verify_span(quote, page_text)
            if vr.matched and 15 <= len(quote) <= 400:
                try:
                    ref = EvidenceRef(source="web", location=cand.canonical_url,
                                      span=quote[:400], offset=vr.offset)
                    status = FieldStatus.EVIDENCED
                except Exception:
                    ref = None
        fields.append(OpportunityField(field_name=fn, value=quote,
                                       status=status, evidence=ref))
    fund = _d(item.get("funding"))
    raw_dl = item.get("deadline")
    if isinstance(raw_dl, list):
        raw_dl = raw_dl[0] if raw_dl else {}
    dl = _d(raw_dl)
    el = _d(item.get("eligibility"))
    iso = (str(dl.get("iso_date") or "")[:10] or None)
    is_past = None
    if iso:
        try:
            is_past = date.fromisoformat(iso[:10]) < date.today()
        except ValueError:
            iso, is_past = None, None
    if not _ls(item.get("host_countries")):
        guess = _country_from_url(cand.canonical_url)
        if guess:
            item = {**item, "host_countries": [guess]}
    specific = bool(item.get("is_specific_opportunity", True))
    reachable = True
    verified = reachable and specific and _name_in_page(name, page_text)
    evd = sum(1 for f in fields if f.status == FieldStatus.EVIDENCED)
    opp = Opportunity(
        kind=_safe_kind(item.get("kind")),
        name=name,
        institution_or_funder=_clean_text(item.get("provider") or "") or cand.domain,
        country=", ".join(_ls(item.get("host_countries"))),
        host_countries=_ls(item.get("host_countries")),
        degree_level=", ".join(_ls(item.get("degree_levels"))),
        degree_levels=_ls(item.get("degree_levels")),
        fields_of_study=_ls(item.get("fields_of_study")),
        official_url=(str(item.get("application_url")) if isinstance(
            item.get("application_url"), str) and item.get("application_url")
            else cand.canonical_url),
        application_url=str(item.get("application_url") or ""),
        how_to_apply=str(item.get("how_to_apply") or ""),
        selection_criteria=[str(x.get("criterion", x)) if isinstance(x, dict) else str(x)
                            for x in (item.get("selection_criteria")
                                      if isinstance(item.get("selection_criteria"), list)
                                      else [])][:10],
        fields=fields,
        funding=FundingInfo(
            details=str(fund.get("coverage") or ""),
            amount=str(fund.get("amount_text")) if fund.get("amount_text") else None,
            amount_value=(fund.get("amount_value") if isinstance(
                fund.get("amount_value"), (int, float)) else None),
            currency=str(fund.get("currency")) if fund.get("currency") else None,
            period=str(fund.get("period")) if fund.get("period") else None,
            covers=_ls(fund.get("covers")),
        ),
        deadlines=[DeadlineInfo(
            date=str(dl.get("text")) if dl.get("text") else None,
            iso_date=iso, is_past=is_past,
            description=str(dl.get("text") or ""),
        )] if (dl.get("text") or iso) else [],
        eligibility=EligibilityCriteria(
            nationalities=_ls(el.get("nationalities")),
            nationality_text=str(el.get("nationality_text") or ""),
            open_to_international=(el.get("open_to_international")
                                   if isinstance(el.get("open_to_international"), bool)
                                   else None),
            degree_levels=_ls(el.get("degree_levels")),
            fields_of_study=_ls(el.get("fields_of_study")),
            min_gpa_value=(el.get("min_gpa_value") if isinstance(
                el.get("min_gpa_value"), (int, float)) else None),
            min_gpa_scale=(el.get("min_gpa_scale") if isinstance(
                el.get("min_gpa_scale"), (int, float)) else None),
            min_gpa_text=str(el.get("min_gpa_text") or ""),
            language_requirements=[_lang_req(r) for r in
                                   (el.get("language_requirements") or [])
                                   if isinstance(r, dict) and r.get("test")],
            min_work_experience_months=(el.get("min_work_experience_months")
                if isinstance(el.get("min_work_experience_months"), (int, float))
                else None),
            max_age=(el.get("max_age") if isinstance(el.get("max_age"), (int, float))
                     else None),
            other=[str(x) for x in (el.get("other") or [])],
        ),
        verification=VerificationResult(
            reachable=reachable, trust_tier=cand.trust_tier,
            existence_verified=verified,
            completeness_score=round(evd / 4.0, 2), evidenced_fields=evd,
            failure_notes=[] if verified else ["name or specifics not evidenced"],
        ),
        origin=cand.origin,
    )
    if hints:
        try:
            _apply_hints(opp, hints, page_text, cand.canonical_url)
        except Exception as exc:
            logger.debug("hint fill skipped for %s: %r", cand.canonical_url, exc)
    return opp


_TEST_NAMES = {"ielts": "IELTS", "toefl": "TOEFL", "pte": "PTE", "duolingo": "Duolingo"}


def _apply_hints(opp: Opportunity, hints: dict, page_text: str, url: str) -> None:
    """Fill requirements the LLM omitted from regex hints whose source sentence
    is verifiably on the page; that sentence becomes the evidence."""
    def ok(h: dict | None) -> bool:
        return isinstance(h, dict) and bool(h.get("sentence")) and verify_span(
            h["sentence"], page_text).matched

    el = opp.eligibility
    used: list[str] = []
    if not el.language_requirements:
        for key, test in _TEST_NAMES.items():
            h = hints.get(key)
            if ok(h):
                el.language_requirements.append(LanguageRequirement(
                    test=test, min_score=float(h["groups"][0]), text=h["sentence"]))
                used.append(h["sentence"])
    if el.min_gpa_value is None:
        h, pct = hints.get("gpa"), hints.get("gpa_pct")
        if ok(h):
            el.min_gpa_value, el.min_gpa_scale = float(h["groups"][0]), float(h["groups"][1])
            el.min_gpa_text = h["sentence"]
            used.append(h["sentence"])
        elif ok(pct):
            el.min_gpa_value, el.min_gpa_scale = float(pct["groups"][0]), 100.0
            el.min_gpa_text = pct["sentence"]
            used.append(pct["sentence"])
    if not opp.deadlines and ok(hints.get("deadline")):
        h = hints["deadline"]
        try:
            from dateutil import parser as dparser
            d = dparser.parse(h["groups"][0], fuzzy=True).date()
            opp.deadlines.append(DeadlineInfo(date=h["groups"][0], iso_date=d.isoformat(),
                                              is_past=d < date.today(), description=h["sentence"]))
            used.append(h["sentence"])
        except (ValueError, OverflowError):
            pass
    for f in opp.fields:
        if not used:
            break
        if f.status != FieldStatus.EVIDENCED and f.field_name in ("eligibility", "deadline"):
            quote = used[0][:400]
            vr = verify_span(quote, page_text)
            if vr.matched:
                f.value, f.status = quote, FieldStatus.EVIDENCED
                f.evidence = EvidenceRef(source="web", location=url, span=quote, offset=vr.offset)
    opp.verification.evidenced_fields = sum(
        1 for f in opp.fields if f.status == FieldStatus.EVIDENCED)
    opp.verification.completeness_score = round(opp.verification.evidenced_fields / 4.0, 2)


def _lang_req(r: dict) -> LanguageRequirement:
    try:
        score = r.get("min_score")
        return LanguageRequirement(test=str(r["test"]),
                                   min_score=float(score) if score is not None else 0.0,
                                   text=str(r.get("text") or ""))
    except Exception:
        return LanguageRequirement(test=str(r.get("test", "?")), min_score=0.0)


def _safe_kind(raw: Any):
    from scholarship_finder.domain.models import OpportunityKind
    try:
        return OpportunityKind(str(raw or "").lower())
    except (ValueError, TypeError):
        return OpportunityKind.SCHOLARSHIP


_NAME_STOP = frozenset({"the", "a", "an", "of", "for", "in", "and", "to",
                        "scholarship", "scholarships", "award", "program",
                        "programme", "fund", "fellowship", "grant", "international",
                        "students", "student", "fully", "funded"})


def _name_in_page(name: str, page_text: str) -> bool:
    """≥60% of distinctive name tokens appear on the page (stop-words/years out)."""
    toks = [t for t in re.findall(r"[A-Za-z]{3,}", name.lower())
            if t not in _NAME_STOP and not t.isdigit()]
    if not toks:
        return _norm(name)[:20] in _norm(page_text)
    page = _norm(page_text)
    hit = sum(1 for t in toks if t in page)
    return hit / len(toks) >= 0.6


from scholarship_finder.domain.source_filters import is_junk_url


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _norm_name(s: str) -> str:
    s = _norm(s)
    s = re.sub(r"(19|20)\d{2}.*$", "", s)           # strip year onwards
    s = re.sub(r"(scholarships?|awards?|programs?|fellowships?|scheme|"
               r"application|apply|for international students|fully funded)",
               "", s)
    s = re.sub(r"(1st|2nd|3rd|first|second|third)", "", s)
    return re.sub(r"\s+", " ", s).strip()[:60]


def _is_dup(o: Opportunity, existing: list[Opportunity]) -> bool:
    nk = (_norm_name(o.name), _norm(o.institution_or_funder))
    for e in existing:
        if (_norm_name(e.name), _norm(e.institution_or_funder)) == nk or (
                _norm_name(e.name) and _norm_name(e.name) == _norm_name(o.name)):
            return True
        if e.official_url and o.official_url and e.official_url == o.official_url:
            return True
    return False


def _dedupe(opps: list[Opportunity]) -> list[Opportunity]:
    out: list[Opportunity] = []
    for o in opps:
        if not _is_dup(o, out):
            out.append(o)
        else:
            for i, e in enumerate(out):
                if ((_norm(e.name), _norm(e.institution_or_funder)) ==
                    (_norm(o.name), _norm(o.institution_or_funder)) or
                        (e.official_url and e.official_url == o.official_url)):
                    if o.trust_tier.value > e.trust_tier.value:
                        out[i] = o
                    break
    return out


def _dedupe_leads(leads: list[Lead]) -> list[Lead]:
    seen, out = set(), []
    for l in leads:
        k = _norm(l.name)
        if k and k not in seen:
            seen.add(k); out.append(l)
    return out


def _target() -> int:
    try:
        from scholarship_finder.config import get_settings
        return get_settings().target_verified_opportunities
    except Exception:
        return 10
