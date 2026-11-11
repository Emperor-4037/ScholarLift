# AGENTS.md

## Verify
- Tests: `python -m pytest -q -p no:cacheprovider` (about 3 minutes; run in the background on Windows).
- Lint: `ruff check --select E9,F63,F7,F82,F401,F811,F841 src tests`
- Import check: `python -c "import scholarship_finder.api.main"`
- Boot: `scholarship-run` serves the UI; `GET /` and `GET /api/health` must return 200. `/` inlines the CSS/JS into one response; `/static` is also mounted.
- Set `PYTHONIOENCODING=utf-8` on Windows consoles.

## Live runs
- `python -m scholarship_finder.cli analyze --profile samples/rahul_profile.json --doc samples/rahul_resume.md --doc samples/rahul_transcript.md --out runs/<name>.json` (also writes `<name>.state.json`; redirect the log to `runs/<name>.log`). `runs/report.py <name>.json` prints a summary.
- Local-only mode: run with `GROQ_API_KEY=`, `GEMINI_API_KEY=` and `GOOGLE_API_KEY=` blank for that process only. Env vars beat `.env`.
- Local model: set `LOCAL_LLM_AUTOSTART=true` with `LOCAL_LLM_SERVER_PATH` (llama-server.exe) and `LOCAL_LLM_MODEL_PATH` (GGUF), or start llama-server yourself on `LOCAL_LLM_BASE_URL` (default port 8088). `LOCAL_LLM_PARALLEL` is the number of server slots (3 on the CUDA build).
- Cold-start a measurement: `scholarship-run kb --purge`; inspect the cache with `scholarship-run kb --stats`.
- Free-tier quotas (Groq, Gemini, Tavily plan) are real; check `runs/<name>.log` for `QuotaExhaustedError` before judging results.

## Conventions
- Hexagonal: `domain/` (pure rules, no I/O), `ports/`, `adapters/`, `agents/`, `engine/`. Agents request an LLM tier, never a model ID.
- Evidence quotes must be verified against fetched page text (`domain/span_verifier.py`). Never show an unverified fact as verified.
- Student-specific decisions (level, destination) must never be persisted in the knowledge base.
- Tests get an isolated temp knowledge base automatically (`tests/conftest.py`).
