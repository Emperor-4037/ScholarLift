"""Streamlit UI — Autonomous Multi-Agent AI Scholarship Finder

Phase 4 UI features:
- Intake form: file upload + display name + GitHub username + privacy consent + DEMO_ACCESS_CODE gate
- Live telemetry trace: event polling via cursor-based subscription
- Top-5 comparison cards with evidence badges, trust tier, gap analysis
- Streamed SOP viewer with Markdown/PDF export
- "Clear My Data Now" button (vault purge)
- "Force Live Refresh" toggle (bypass cache)
- REPLAY mode badge for "Rahul" sample persona
- Pre-flight canary in sidebar

Architecture: thin UI layer — all intelligence is in the engine.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from typing import Any

import nest_asyncio
nest_asyncio.apply()  # Allow asyncio.run() inside Streamlit's running event loop

from dotenv import load_dotenv
load_dotenv()

import streamlit as st

# Page config (must be first Streamlit call)
st.set_page_config(
    page_title="AI Scholarship Finder",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)

logger = logging.getLogger(__name__)

# ── Lazy composition (only once per session) ──────────────────────────────────

@st.cache_resource(show_spinner="🔧 Initializing engine…")
def _get_gateway():
    """Build and cache the RunGateway singleton."""
    from scholarship_finder.composition import build_gateway
    return build_gateway()


# ── Session state helpers ─────────────────────────────────────────────────────

def _init_session():
    defaults = {
        "run_id": None,
        "event_cursor": 0,
        "vault": None,
        "events": [],
        "phase_a_ready": False,
        "sop_text": "",
        "sop_chunks": [],
        "force_refresh": False,
        "demo_access_granted": False,
        "replay_mode": False,
        "last_poll": 0.0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _clear_data():
    """Purge PIIVault and reset run state."""
    vault = st.session_state.get("vault")
    if vault:
        vault.purge()
    for key in ["run_id", "event_cursor", "vault", "events", "phase_a_ready",
                "sop_text", "sop_chunks"]:
        st.session_state[key] = None if key not in ("events", "sop_chunks") else []
    st.session_state["event_cursor"] = 0
    st.success("🗑️ All your data has been cleared from memory.")


# ── Demo access gate ──────────────────────────────────────────────────────────

def _check_demo_access() -> bool:
    from scholarship_finder.config import get_settings
    required = get_settings().demo_access_code or os.getenv("DEMO_ACCESS_CODE", "")
    if not required:
        return True  # no gate configured
    if st.session_state.demo_access_granted:
        return True
    code = st.sidebar.text_input("🔑 Demo Access Code", type="password", key="access_code_input")
    if st.sidebar.button("Submit Code"):
        if code == required:
            st.session_state.demo_access_granted = True
            st.rerun()
        else:
            st.sidebar.error("Invalid access code.")
    return False


# ── Sidebar ───────────────────────────────────────────────────────────────────

def _render_sidebar():
    with st.sidebar:
        st.title("🎓 Scholarship Finder")
        st.caption("Autonomous Multi-Agent AI")

        st.divider()

        # Force Live Refresh toggle
        st.session_state.force_refresh = st.toggle(
            "⚡ Force Live Refresh",
            value=st.session_state.get("force_refresh", False),
            help="Bypass cache and fetch fresh data. Use sparingly to preserve quota.",
        )

        # Replay mode
        st.session_state.replay_mode = st.toggle(
            "🎬 REPLAY Mode (demo)",
            value=st.session_state.get("replay_mode", False),
            help="Use pre-recorded 'Rahul' sample persona for demo purposes.",
        )

        if st.session_state.replay_mode:
            st.info("🔁 **REPLAY MODE** — using sample persona data")

        st.divider()

        # Clear data button
        if st.button("🗑️ Clear My Data Now", use_container_width=True):
            _clear_data()

        st.divider()

        # Pre-flight canary
        with st.expander("🔬 System Status"):
            _render_preflight()


def _render_preflight():
    """Quick check of configured providers."""
    from scholarship_finder.config import get_settings
    cfg = get_settings()

    checks = []
    checks.append(("GEMINI_API_KEY", bool(cfg.gemini_api_key or os.getenv("GEMINI_API_KEY"))))
    checks.append(("TAVILY_API_KEY", bool(cfg.tavily_api_key or os.getenv("TAVILY_API_KEY"))))
    checks.append(("JINA_API_KEY", bool(cfg.jina_api_key or cfg.jina_reader_key or os.getenv("JINA_API_KEY") or os.getenv("JINA_READER_KEY"))))
    checks.append(("GITHUB_TOKEN", bool(cfg.github_token or os.getenv("GITHUB_TOKEN"))))

    for name, ok in checks:
        icon = "✅" if ok else "⚠️"
        st.caption(f"{icon} {name}")

    if all(ok for _, ok in checks[:2]):
        st.success("Core providers ready")
    else:
        st.warning("Missing API keys — running in stub/REPLAY mode")


# ── Intake form ───────────────────────────────────────────────────────────────

def _render_intake_form():
    st.header("📋 Tell us about yourself")

    col1, col2 = st.columns([2, 1])

    with col1:
        uploaded_file = st.file_uploader(
            "Upload your Resume / CV",
            type=["pdf", "txt", "md"],
            help="Your file is processed in memory only and never stored.",
        )

    with col2:
        display_name = st.text_input(
            "Your Name (optional)",
            placeholder="Used to personalize the SOP",
            help="Stored in a secure in-memory vault with 15-min TTL.",
        )
        github_username = st.text_input(
            "GitHub Username (optional)",
            placeholder="e.g. octocat",
            help="Used to enrich your profile with project signals.",
        )

    st.divider()

    # Privacy consent
    privacy_ok = st.checkbox(
        "✅ I understand my data is processed in memory only, not stored or sent to third parties "
        "beyond the AI providers configured for this deployment.",
        key="privacy_consent",
    )

    start_btn = st.button(
        "🚀 Find My Scholarships" if not st.session_state.replay_mode else "🎬 Run Demo (REPLAY)",
        type="primary",
        disabled=not privacy_ok or (uploaded_file is None and not st.session_state.replay_mode),
        use_container_width=True,
    )

    if start_btn:
        _start_run(uploaded_file, display_name, github_username)
        st.rerun()


def _start_run(uploaded_file, display_name: str, github_username: str):
    """Launch a new run."""
    gateway = _get_gateway()

    if st.session_state.replay_mode:
        # Load bundled sample persona
        file_bytes = _load_replay_resume()
        filename = "rahul_sample_resume.pdf"
        mime_type = "application/pdf"
        display_name = display_name or "Rahul"
    else:
        file_bytes = uploaded_file.read()
        filename = uploaded_file.name
        ext = filename.rsplit(".", 1)[-1].lower()
        mime_type = {
            "pdf": "application/pdf",
            "txt": "text/plain",
            "md": "text/markdown",
        }.get(ext, "application/pdf")

    async def _run():
        from scholarship_finder.composition import start_run_with_intake
        return await start_run_with_intake(
            gateway,
            file_bytes=file_bytes,
            filename=filename,
            mime_type=mime_type,
            display_name=display_name.strip() or None,
            github_username=github_username.strip() or None,
            is_replay=st.session_state.replay_mode,
        )

    try:
        loop = asyncio.get_event_loop()
        run_id = loop.run_until_complete(_run())
        st.session_state.run_id = run_id
        st.session_state.event_cursor = 0
        st.session_state.events = []
        st.session_state.phase_a_ready = False
        st.session_state.sop_text = ""
        st.session_state.sop_chunks = []
    except Exception as exc:
        st.error(f"❌ Failed to start run: {exc}")
        logger.exception("start_run failed")


def _load_replay_resume() -> bytes:
    """Load bundled sample resume for REPLAY mode."""
    sample_path = os.path.join(os.path.dirname(__file__), "sample_persona", "rahul_resume.txt")
    if os.path.exists(sample_path):
        with open(sample_path, "rb") as f:
            return f.read()
    # Inline fallback
    return b"""Rahul Kumar
Computer Science, IIT Delhi, CGPA 8.7/10
Skills: Python, Machine Learning, PyTorch, Computer Vision
Projects: Neural Architecture Search, Medical Image Segmentation
Seeking: Masters / PhD in Computer Science or AI in Germany or Netherlands
English: IELTS 7.5
"""


# ── Live telemetry trace ──────────────────────────────────────────────────────

def _poll_events():
    """Poll for new events and update session state."""
    gateway = _get_gateway()
    run_id = st.session_state.run_id
    if not run_id:
        return

    new_events = gateway.subscribe_events(run_id, cursor=st.session_state.event_cursor)
    if new_events:
        st.session_state.events.extend(new_events)
        st.session_state.event_cursor = new_events[-1].seq

        # Check for SOP chunks
        for evt in new_events:
            if evt.kind.value == "sop_chunk":
                st.session_state.sop_chunks.append(evt.payload.get("chunk", ""))
            if evt.kind.value == "phase_a_ready":
                st.session_state.phase_a_ready = True

        # Rebuild full SOP
        if st.session_state.sop_chunks:
            st.session_state.sop_text = "".join(st.session_state.sop_chunks)

    st.session_state.last_poll = time.time()


def _render_telemetry():
    """Show a collapsible live event trace."""
    events = st.session_state.get("events", [])
    if not events:
        return

    with st.expander(f"📡 Live Telemetry ({len(events)} events)", expanded=False):
        for evt in events[-20:]:  # show last 20
            icon = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "❌"}.get(
                getattr(evt.severity, "value", "INFO"), "ℹ️"
            )
            st.caption(f"{icon} `{evt.node}` · **{evt.kind.value}** · {evt.payload}")


# ── Top-5 results cards ───────────────────────────────────────────────────────

def _render_results():
    """Render Top-5 opportunity comparison cards."""
    gateway = _get_gateway()
    run_id = st.session_state.run_id
    if not run_id:
        return

    snap = gateway.get_snapshot(run_id)
    if snap is None or not snap.top_5:
        return

    st.header("🏆 Top Opportunities Found")

    if st.session_state.replay_mode:
        st.warning("🔁 **REPLAY MODE** — results from pre-recorded demo data")

    top_ids = snap.top_5
    opps = {o.id: o for o in snap.opportunities}
    assessments = {a.opportunity_id: a for a in (snap.assessments or [])}

    for rank, opp_id in enumerate(top_ids, 1):
        opp = opps.get(opp_id)
        assessment = assessments.get(opp_id)
        if not opp:
            continue

        _render_opp_card(rank, opp, assessment)


def _render_opp_card(rank: int, opp: Any, assessment: Any):
    """Single opportunity card with evidence badges and gap analysis."""
    from scholarship_finder.domain.models import TrustTier, FundingLevel

    trust_badge = {
        "OFFICIAL_INSTITUTION": "🏛️ Official",
        "RECOGNIZED_FUNDER": "🤝 Funder",
        "AGGREGATOR": "📋 Aggregator",
        "UNKNOWN": "❓ Unknown",
    }.get(getattr(opp.verification, "trust_tier", TrustTier.UNKNOWN).value if opp.verification else "UNKNOWN", "❓")

    funding_icon = {
        "FULL_TUITION_STIPEND": "💰💰",
        "FULL_TUITION": "💰",
        "PARTIAL": "💸",
        "NONE": "🚫",
        "UNKNOWN": "❓",
    }.get(opp.funding.level.value if opp.funding else "UNKNOWN", "❓")

    score = assessment.composite_score if assessment else 0
    score_color = "green" if score >= 70 else "orange" if score >= 45 else "red"

    with st.container(border=True):
        col1, col2, col3 = st.columns([3, 1, 1])

        with col1:
            st.subheader(f"#{rank} — {opp.name}")
            st.caption(f"🏫 {opp.institution_or_funder} · 🌍 {opp.country} · 📚 {opp.degree_level}")

        with col2:
            st.metric("Match Score", f"{score:.0f}/100")

        with col3:
            st.caption(f"{trust_badge}")
            st.caption(f"Funding: {funding_icon}")

        # Evidence badges
        evidenced = sum(1 for f in (opp.fields or []) if getattr(f, "status", None) and f.status.value == "EVIDENCED")
        total_fields = len(opp.fields or [])
        if total_fields > 0:
            st.progress(evidenced / total_fields, text=f"Evidence: {evidenced}/{total_fields} fields verified")

        # Deadlines
        if opp.deadlines:
            for dl in opp.deadlines[:1]:
                status = "✅" if dl.status.value == "EVIDENCED" else "⚠️"
                st.caption(f"📅 Deadline: {dl.date or 'not stated'} {status}")

        # Gaps
        if assessment and assessment.gaps:
            blocking = [g for g in assessment.gaps if g.blocking]
            non_blocking = [g for g in assessment.gaps if not g.blocking]
            if blocking:
                st.error(f"🚨 **{len(blocking)} blocking gap(s):** " +
                         "; ".join(g.gap_type for g in blocking[:3]))
            if non_blocking:
                st.warning(f"⚠️ **{len(non_blocking)} gap(s) to address:** " +
                           "; ".join(g.gap_type for g in non_blocking[:3]))

        # Official link
        st.link_button("🔗 View Official Page", opp.official_url)


# ── SOP viewer ────────────────────────────────────────────────────────────────

def _render_sop():
    """Render streamed SOP with export buttons."""
    sop_text = st.session_state.get("sop_text", "")
    if not sop_text:
        return

    st.header("📝 Statement of Purpose")

    if st.session_state.replay_mode:
        st.info("🔁 This SOP was generated from pre-recorded demo data")

    st.markdown(sop_text)

    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "📥 Export as Markdown",
            data=sop_text,
            file_name="statement_of_purpose.md",
            mime="text/markdown",
            use_container_width=True,
        )
    with col2:
        # Simple PDF export via fpdf2 if available, else text fallback
        try:
            from fpdf import FPDF
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("Arial", size=12)
            for line in sop_text.split("\n"):
                pdf.multi_cell(0, 8, txt=line)
            pdf_bytes = pdf.output(dest="S").encode("latin-1")
            st.download_button(
                "📥 Export as PDF",
                data=pdf_bytes,
                file_name="statement_of_purpose.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except ImportError:
            st.download_button(
                "📥 Export as Text",
                data=sop_text,
                file_name="statement_of_purpose.txt",
                mime="text/plain",
                use_container_width=True,
            )


# ── Checklist ─────────────────────────────────────────────────────────────────

def _render_checklist():
    """Render the execution checklist."""
    gateway = _get_gateway()
    run_id = st.session_state.run_id
    if not run_id:
        return

    snap = gateway.get_snapshot(run_id)
    if not snap or not snap.application_package:
        return

    pkg = snap.application_package
    if not pkg.execution_checklist:
        return

    st.header("✅ Your Action Plan")
    for item in pkg.execution_checklist:
        st.markdown(f"- {item}")


# ── Progress indicator ────────────────────────────────────────────────────────

def _render_progress():
    """Show a spinner while the run is in progress."""
    gateway = _get_gateway()
    run_id = st.session_state.run_id
    if not run_id:
        return

    handle = gateway._runs.get(run_id)
    if not handle:
        return

    if not handle.finished:
        events = st.session_state.get("events", [])
        last_node = next(
            (e.node for e in reversed(events) if e.kind.value in ("node_started", "progress")),
            "initializing"
        )
        st.info(f"⏳ Running… current step: **{last_node.replace('_', ' ').title()}**")
        # Auto-refresh
        time.sleep(0.5)
        st.rerun()
    else:
        phase = handle.state.run_meta.phase.value
        outcome = handle.state.run_meta.outcome.value
        icon = {"COMPLETE": "✅", "DEGRADED": "⚠️", "PARTIAL": "🔶", "FAILED_SAFE": "❌"}.get(outcome, "✅")
        st.success(f"{icon} Run complete — outcome: **{outcome}** · phase: {phase}")


# ── Main app ──────────────────────────────────────────────────────────────────

def main():
    _init_session()
    _render_sidebar()

    # Gate
    if not _check_demo_access():
        st.title("🎓 AI Scholarship Finder")
        st.info("Please enter the access code in the sidebar to continue.")
        return

    # Poll events if a run is active
    if st.session_state.run_id:
        _poll_events()

    # Main content
    if st.session_state.run_id is None:
        st.title("🎓 Autonomous AI Scholarship Finder")
        st.markdown(
            "Upload your resume and let the AI find the best scholarship and program opportunities "
            "for you — with evidence-grounded results and a personalized Statement of Purpose."
        )
        _render_intake_form()
    else:
        st.title("🎓 Your Scholarship Results")
        _render_progress()
        _render_telemetry()

        if st.session_state.phase_a_ready:
            _render_results()

        _render_sop()
        _render_checklist()

        if st.button("🔄 Start New Search"):
            _clear_data()
            st.rerun()


if __name__ == "__main__":
    main()
