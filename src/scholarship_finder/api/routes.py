"""API routes — multipart run intake, SSE events, view-model, SOP, admin.

Never returns raw fetched page content or vault contents. SOP text is
rehydrated with the candidate name *for display only*.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import PlainTextResponse, StreamingResponse

from scholarship_finder.api.view_model import build_view_model
from scholarship_finder.config import get_settings
from scholarship_finder.domain.models import CandidateProfile

logger = logging.getLogger(__name__)

router = APIRouter()

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLES_DIR = _REPO_ROOT / "samples"
_SAMPLE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.(md|txt|json)$")

_LABELS = ("RESUME", "TRANSCRIPT", "OTHER")


def _gw(request: Request):
    gw = getattr(request.app.state, "gateway", None)
    if gw is None:
        raise HTTPException(status_code=503, detail="Run gateway not initialised")
    return gw


# ── Runs ─────────────────────────────────────────────────────────────────────

@router.post("/runs")
async def create_run(
    request: Request,
    files: list[UploadFile] = File(default=[]),
    labels: str = Form(default=""),
    profile: str = Form(default=""),
    display_name: str = Form(default=""),
    github_username: str = Form(default=""),
    access_code: str = Form(default=""),
):
    """Start a run: multipart files + form profile JSON."""
    cfg = get_settings()
    if cfg.demo_access_code and access_code != cfg.demo_access_code:
        raise HTTPException(status_code=403, detail="Invalid access code")

    gw = _gw(request)

    llm = getattr(gw, "llm_port", None)
    if llm is not None and hasattr(llm, "available") and not llm.available():
        raise HTTPException(status_code=503,
                            detail="No LLM provider configured — check API keys")

    form_profile = None
    if profile.strip():
        try:
            form_profile = CandidateProfile.model_validate(json.loads(profile))
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"Invalid profile JSON: {exc}") from exc

    label_list: list[str] = []
    if labels.strip():
        try:
            label_list = [str(x).upper() for x in json.loads(labels)]
        except Exception:
            label_list = [x.strip().upper() for x in labels.split(",")]

    intake_files: list[tuple[bytes, str, str]] = []
    for i, uf in enumerate(files):
        if not uf.filename:
            continue
        label = label_list[i] if i < len(label_list) and label_list[i] in _LABELS else "OTHER"
        content = await uf.read()
        intake_files.append((content, uf.filename, label))

    from scholarship_finder.composition import start_run_with_intake_async
    try:
        run_id = await start_run_with_intake_async(
            gw,
            files=intake_files,
            form_profile=form_profile,
            display_name=display_name.strip() or None,
            github_username=github_username.strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {"run_id": run_id}


@router.get("/runs/{run_id}/events")
async def run_events(request: Request, run_id: str, cursor: int = 0):
    """SSE stream; clients reconnect with ?cursor=<last seq>."""
    gw = _gw(request)
    if gw.get_snapshot(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return StreamingResponse(
        gw.subscribe_events_async(run_id, cursor=cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}")
async def run_view(request: Request, run_id: str):
    """Progressive UI view-model (safe: no raw content, no vault data)."""
    gw = _gw(request)
    state = gw.get_snapshot(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")
    vault = gw.get_vault(run_id)
    return build_view_model(state, vault=vault, finished=gw.run_finished(run_id))


@router.post("/runs/{run_id}/sop", status_code=202)
async def request_sop(request: Request, run_id: str, body: dict | None = None):
    """Request an on-demand SOP draft for one opportunity → 202."""
    gw = _gw(request)
    if gw.get_snapshot(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    opp_id = (body or {}).get("opportunity_id") or (body or {}).get("opp_id")
    if not opp_id:
        raise HTTPException(status_code=400, detail="opportunity_id required")
    ok = gw.request_sop(run_id, opp_id)
    if not ok:
        raise HTTPException(status_code=503, detail="SOP drafting unavailable")
    return {"status": "generating", "opportunity_id": opp_id}


@router.get("/runs/{run_id}/sop/{opp_id}")
async def sop_status(request: Request, run_id: str, opp_id: str):
    """SOP status + rehydrated text + claim ledger + removed claims."""
    gw = _gw(request)
    state = gw.get_snapshot(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")
    pkg = state.applications.get(opp_id)
    if pkg is None:
        # Generation may be in flight on another thread.
        opp = state.opportunity(opp_id)
        if opp is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        return {"status": "generating", "opportunity_id": opp_id}
    vault = gw.get_vault(run_id)
    vm = build_view_model(state, vault=vault)["applications"][opp_id]
    vm["opportunity_id"] = opp_id
    return vm


@router.get("/runs/{run_id}/sop/{opp_id}/download")
async def sop_download(request: Request, run_id: str, opp_id: str):
    """Download the SOP as a Markdown attachment (name rehydrated)."""
    gw = _gw(request)
    state = gw.get_snapshot(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")
    pkg = state.applications.get(opp_id)
    if pkg is None or not pkg.sop_text:
        raise HTTPException(status_code=404, detail="No SOP ready for this opportunity")
    vault = gw.get_vault(run_id)
    text = vault.rehydrate(pkg.sop_text) if vault else pkg.sop_text
    opp = state.opportunity(opp_id)
    title = f"Statement of Purpose — {(opp.name if opp else opp_id)[:80]}"
    body = f"# {title}\n\n{text}\n"
    fname = re.sub(r"[^A-Za-z0-9_-]+", "_", (opp.name if opp else "sop"))[:60] or "sop"
    return PlainTextResponse(
        body,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fname}.md"'},
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str):
    gw = _gw(request)
    if gw.get_snapshot(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    gw.cancel(run_id)
    return {"cancelled": True, "run_id": run_id}


@router.delete("/runs/{run_id}")
async def purge_run(request: Request, run_id: str):
    """Delete run state + PII vault entirely."""
    gw = _gw(request)
    ok = gw.purge(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"purged": True, "run_id": run_id}


# ── Meta ─────────────────────────────────────────────────────────────────────

@router.get("/health")
async def health(request: Request):
    """Provider/chain config status — never exposes key values."""
    cfg = get_settings()
    gw = getattr(request.app.state, "gateway", None)
    router_obj = getattr(gw, "llm_router", None) if gw else None
    providers = sorted(getattr(router_obj, "_providers", {}).keys()) if router_obj else []
    return {
        "status": "ok",
        "llm_available": bool(
            getattr(getattr(gw, "llm_port", None), "available", lambda: providers)()
        ) if gw else bool(providers),
        "providers": providers,
        "chains": {
            "fast": len(cfg.fast_models()),
            "reasoning": len(cfg.reasoning_models()),
            "vision": len(cfg.vision_models()),
        },
        "search": bool(cfg.tavily_api_key or cfg.jina_api_key or cfg.serper_api_key),
        "search_providers": [
            n for n, k in (("tavily", cfg.tavily_api_key),
                           ("jina", cfg.jina_api_key),
                           ("serper", cfg.serper_api_key)) if k],
        "access_code_required": bool(cfg.demo_access_code),
    }


@router.get("/samples/{name}")
async def get_sample(name: str):
    """Serve a bundled fictional sample file (samples/*.md|json)."""
    if not _SAMPLE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid sample name")
    path = (_SAMPLES_DIR / name).resolve()
    if _SAMPLES_DIR not in path.parents and path != _SAMPLES_DIR:
        raise HTTPException(status_code=400, detail="Invalid sample name")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Sample not found")
    media = "application/json" if name.endswith(".json") else "text/plain"
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type=media)
