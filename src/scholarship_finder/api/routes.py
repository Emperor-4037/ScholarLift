"""FastAPI routes for scholarship finder API.

Non-breaking addition - separate module for API endpoints.
"""
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import Optional
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


# We'll attach the app/gateway to the router at mount time
_app = None


def set_app(app):
    """Set the FastAPI app instance (called during app setup)."""
    global _app
    _app = app


@router.post("/upload")
async def upload_resume(
    file: UploadFile = File(...),
    display_name: Optional[str] = Form(None),
    github_username: Optional[str] = Form(None),
    force_refresh: bool = Form(False),
):
    """Upload resume and start analysis.

    Non-breaking addition - new API endpoint.
    """
    if _app is None:
        raise HTTPException(status_code=500, detail="App not initialized")

    # Validate file
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    if not file.filename.lower().endswith(('.pdf', '.txt', '.md')):
        raise HTTPException(status_code=400, detail="Invalid file type. Must be PDF, TXT, or MD")

    # Read file bytes
    file_bytes = await file.read()

    # Determine mime type
    if file.filename.lower().endswith('.pdf'):
        mime_type = "application/pdf"
    else:
        mime_type = "text/plain"

    # Start analysis using existing composition logic
    try:
        from scholarship_finder.composition import start_run_with_intake_async

        run_id = await start_run_with_intake_async(
            gateway=_app.state.gateway,
            file_bytes=file_bytes,
            filename=file.filename,
            mime_type=mime_type,
            display_name=display_name,
            github_username=github_username,
            is_replay=False,
        )
        logger.info(f"Upload successful: run_id={run_id}")
        return {"run_id": run_id, "status": "started"}
    except Exception as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/events/{run_id}")
async def stream_events(run_id: str):
    """Stream real-time events via SSE.

    Non-breaking addition - new SSE endpoint.
    """
    if _app is None:
        raise HTTPException(status_code=500, detail="App not initialized")

    from scholarship_finder.api.sse import SSEStreamingResponse

    async def event_generator():
        async for message in _app.state.gateway.subscribe_events_async(run_id):
            yield message

    return SSEStreamingResponse(event_generator())


@router.get("/snapshot/{run_id}")
async def get_snapshot(run_id: str):
    """Get current state snapshot.

    Non-breaking addition - new API endpoint.
    """
    if _app is None:
        raise HTTPException(status_code=500, detail="App not initialized")

    snapshot = _app.state.gateway.get_snapshot(run_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Run not found")

    return snapshot.model_dump()


@router.post("/cancel/{run_id}")
async def cancel_run(run_id: str):
    """Cancel a running analysis.

    Non-breaking addition - new API endpoint.
    """
    if _app is None:
        raise HTTPException(status_code=500, detail="App not initialized")

    success = _app.state.gateway.cancel(run_id)
    if not success:
        raise HTTPException(status_code=404, detail="Run not found or already finished")

    return {"status": "cancelled", "run_id": run_id}


@router.get("/health")
async def health_check():
    """Health check endpoint.

    Non-breaking addition - new API endpoint.
    """
    return {"status": "healthy"}


@router.get("/runs")
async def list_runs():
    """List all runs.

    Non-breaking addition - new API endpoint.
    """
    if _app is None:
        raise HTTPException(status_code=500, detail="App not initialized")

    return _app.state.gateway.list_runs()


@router.get("/profile/{run_id}")
async def get_profile(run_id: str):
    """Get profile information with enrichment tips.

    Non-breaking addition - new API endpoint.
    """
    if _app is None:
        raise HTTPException(status_code=500, detail="App not initialized")

    snapshot = _app.state.gateway.get_snapshot(run_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Run not found")

    profile = snapshot.profile
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    # Generate data-driven enrichment tips
    from scholarship_finder.domain.enrichment_tips import (
        generate_enrichment_tips,
        format_tips_for_display,
    )

    tips = generate_enrichment_tips(profile)
    formatted_tips = format_tips_for_display(tips)

    # Also include completeness flags for backward compatibility
    flags = profile.completeness_flags or {}

    return {
        "profile": profile.model_dump(),
        "completeness_flags": flags,
        "enrichment_tips": formatted_tips,
        "enrichment_tips_detailed": [t.__dict__ for t in tips],  # Full tip objects for advanced UI
        "completeness_score": sum(1 for v in flags.values() if v) / len(flags) if flags else 0,
    }


@router.get("/sop/{run_id}")
async def get_rehydrated_sop(run_id: str):
    """Get SOP with [CANDIDATE_NAME] placeholders replaced.

    Non-breaking addition - new API endpoint.
    """
    if _app is None:
        raise HTTPException(status_code=500, detail="App not initialized")

    snapshot = _app.state.gateway.get_snapshot(run_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Run not found")

    if not snapshot.application_package:
        raise HTTPException(status_code=404, detail="SOP not generated")

    sop_text = snapshot.application_package.sop_text
    vault_id = snapshot.vault_reference

    # Rehydrate with vault
    if vault_id:
        vault = _app.state.gateway.vault_registry.get(vault_id)
        if vault:
            sop_text = vault.rehydrate(sop_text)
            logger.info(f"Rehydrated SOP for run {run_id} using vault {vault_id}")
        else:
            logger.warning(f"Vault {vault_id} not found or expired for run {run_id}")
    else:
        logger.warning(f"No vault_reference in state for run {run_id}")

    return {"sop_text": sop_text}


@router.get("/opportunities/{run_id}")
async def get_opportunities_with_details(run_id: str):
    """Get opportunities with detailed rationale and eligibility info.

    Non-breaking addition - new API endpoint for enriched opportunity display.
    """
    if _app is None:
        raise HTTPException(status_code=500, detail="App not initialized")

    snapshot = _app.state.gateway.get_snapshot(run_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Run not found")

    # Build opportunity map for easy lookup
    opp_map = {opp.id: opp for opp in snapshot.opportunities}
    enriched = []

    for opp_id in snapshot.top_5:
        opp = opp_map.get(opp_id)
        if not opp:
            continue

        # Find corresponding assessment
        assessment = next((a for a in snapshot.assessments if a.opportunity_id == opp_id), None)

        # Build enriched opportunity object
        enriched_opp = {
            "opportunity": opp.model_dump(),
            "rationale": assessment.rationale if assessment else "",
            "composite_score": assessment.composite_score if assessment else 0,
            "gaps": [g.__dict__ for g in assessment.gaps] if assessment else [],
            "component_scores": [c.__dict__ for c in assessment.component_scores] if assessment else [],
            "rank": assessment.rank if assessment else None,
        }

        enriched.append(enriched_opp)

    return {"opportunities": enriched}
