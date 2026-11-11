"""ScholarLift CLI.

``scholarship-run`` (no args)            → serve the web UI via uvicorn
``scholarship-run analyze …``            → headless run through the same
                                           gateway; prints progress + timing,
                                           writes the view-model JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path


def _serve() -> None:
    import uvicorn
    from scholarship_finder.config import get_settings

    cfg = get_settings()
    uvicorn.run(
        "scholarship_finder.api.main:app",
        host=cfg.api_host,
        port=cfg.api_port,
        reload=cfg.api_reload,
    )


def _parse_doc(spec: str) -> tuple[Path, str]:
    """``file[:LABEL]`` — label in RESUME|TRANSCRIPT|OTHER (default OTHER)."""
    if ":" in spec and not Path(spec).exists():
        # Windows drive letters (C:\…) already exist as paths, so the colon
        # split only applies to non-existent path-like specs.
        head, _, tail = spec.rpartition(":")
        if tail.upper() in ("RESUME", "TRANSCRIPT", "OTHER") and Path(head).exists():
            return Path(head), tail.upper()
    path = Path(spec)
    label = "OTHER"
    low = path.name.lower()
    if "resume" in low or "cv" in low:
        label = "RESUME"
    elif "transcript" in low:
        label = "TRANSCRIPT"
    return path, label


async def _analyze(args: argparse.Namespace) -> int:
    from scholarship_finder.api.view_model import build_view_model
    from scholarship_finder.composition import (
        build_gateway,
        start_run_with_intake_async,
    )
    from scholarship_finder.domain.models import CandidateProfile

    profile = None
    if args.profile:
        profile = CandidateProfile.model_validate(
            json.loads(Path(args.profile).read_text(encoding="utf-8")))

    files = []
    for spec in args.doc or []:
        path, label = _parse_doc(spec)
        if not path.is_file():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2
        files.append((path.read_bytes(), path.name, label))

    from scholarship_finder.adapters.llama_server import LlamaServerManager
    from scholarship_finder.config import get_settings

    manager = LlamaServerManager(get_settings())
    print(f"Local LLM: {'healthy' if await manager.start() else 'unavailable'}")
    try:
        return await _run_and_report(args, files, profile, build_gateway,
                                     start_run_with_intake_async, build_view_model)
    finally:
        await manager.stop()


async def _run_and_report(args, files, profile, build_gateway,
                          start_run_with_intake_async, build_view_model) -> int:
    gateway = build_gateway()
    t0 = time.perf_counter()
    t_run = time.monotonic()
    print(f"Starting run ({len(files)} document(s), "
          f"{'with' if profile else 'no'} form profile)…")

    run_id = await start_run_with_intake_async(
        gateway,
        files=files,
        form_profile=profile,
        display_name=args.display_name,
        github_username=args.github,
    )
    print(f"run_id: {run_id}")

    cursor = 0
    while True:
        events = gateway.subscribe_events(run_id, cursor)
        for ev in events:
            cursor = ev.seq
            msg = ev.payload.get("message", ev.kind.value)
            print(f"  [{time.monotonic()-t_run:6.1f}s][{ev.node:>22}] {msg}")
        if gateway.run_finished(run_id):
            for ev in gateway.subscribe_events(run_id, cursor):
                print(f"  [{time.monotonic()-t_run:6.1f}s][{ev.node:>22}] {ev.payload.get('message', ev.kind.value)}")
            break
        await asyncio.sleep(0.4)

    state = gateway.get_snapshot(run_id)
    elapsed = time.perf_counter() - t0
    vm = build_view_model(state, vault=gateway.get_vault(run_id), finished=True)

    out = Path(args.out or "out.json")
    out.write_text(json.dumps(vm, indent=2, ensure_ascii=False, default=str),
                   encoding="utf-8")
    state_path = out.with_suffix(".state.json")
    if state is not None:
        state_path.write_text(
            json.dumps(state.model_dump(mode="json"), indent=2,
                       ensure_ascii=False, default=str), encoding="utf-8")
    outcome = vm["outcome"]
    print(f"\nDone in {elapsed:.0f}s — outcome={outcome}, "
          f"verified={vm['counters']['verified']}, top5={len(vm['top_5'])}")
    print(f"View-model written to {out}")
    return 0 if outcome in ("COMPLETE", "DEGRADED") else 1


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(
        prog="scholarship-run",
        description="ScholarLift — AI scholarship finder")
    sub = parser.add_subparsers(dest="cmd")

    kb_p = sub.add_parser("kb", help="knowledge base admin")
    kb_p.add_argument("--stats", action="store_true")
    kb_p.add_argument("--purge", action="store_true")
    an = sub.add_parser("analyze", help="headless run → view-model JSON")
    an.add_argument("--profile", help="CandidateProfile JSON file")
    an.add_argument("--doc", action="append",
                    help="file[:RESUME|TRANSCRIPT|OTHER] (repeatable)")
    an.add_argument("--display-name", default=None)
    an.add_argument("--github", default=None)
    an.add_argument("--out", default="out.json")

    args = parser.parse_args()
    if args.cmd == "kb":
        from scholarship_finder.adapters.knowledge_base import get_kb
        kb = get_kb()
        if kb is None:
            print("KB disabled"); return 0
        if args.purge:
            kb.purge()
        if args.stats or args.purge:
            import json as _j
            print(_j.dumps(kb.stats()))
        return 0
    if args.cmd == "analyze":
        sys.exit(asyncio.run(_analyze(args)))
    _serve()


if __name__ == "__main__":
    main()
