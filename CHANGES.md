# CHANGES

ScholarLift originally imported and passed its unit tests but failed on any real run: ranking crashed on every run, the SOP was discarded by its own verifier, robots.txt handling blocked most sites, search ignored the student, and eligibility never read the requirements. This is what changed and why.

## Pipeline bugs fixed
- Ranking crashed on an undefined variable and an unescaped prompt brace; it now judges field alignment from the LLM and computes every number deterministically.
- The SOP prompt forbade citations while the verifier required them. SOPs are now written against a numbered fact pack (`[F#]`), checked, repaired once and stripped of unsupported claims (removed claims are listed).
- `robots.txt` handling used a naive line match; it now uses `urllib.robotparser`. HTTP clients are per event loop (the shared client caused "Event loop is closed").
- LangGraph join deadlock fixed (the strategist always runs, and no-ops without a top five).
- Stale state fields, dead quota code, a missing `logger`, an invalid entry point and non-existent model references removed.

## Student model and intake
- The profile now holds nationality, research, publications, work, leadership, awards, financial need, language tests and target preferences.
- Up to five documents (PDF, TXT, MD, DOCX; magic-byte checked) can be uploaded. A student's own target countries are authoritative; the LLM can no longer add destinations they never listed.

## Search and verification funnel
- **Propose, then verify:** the LLM proposes named programmes, a resolver finds each official page, and code checks that the page exists, names the programme, and that every evidence quote appears verbatim in the fetched text.
- **Channels:** profile/country/nationality queries, an official-source registry per destination (`domain/data/official_sources.json`), proposer/resolver, and expansion over leads found on listing pages.
- **Deterministic pre-gates before any LLM call** (`domain/pregate.py`): junk/news/annual-report URLs, too-short pages, no funding vocabulary, and wrong degree level in either direction.
- **Listing pages are followed one hop** (`domain/listing.py`), only from official or recognised-funder parents. Aggregator and news pages produce leads only.
- **Regex requirement hints** (`domain/req_hints.py`) are given to the extractor and fill IELTS/GPA/deadline only when the source sentence is verifiably on the page.
- **One page per extraction call**, schema-constrained for the local model, streaming (results kept as each page finishes, so a deadline never loses finished work).
- Only trusted-source hits (official institution or recognised funder) count toward the verified target; weak sources are kept but flagged `unverified_source`. Extracted host countries fall back to the site TLD when the page does not state one.
- Aggregators such as scholars4dev were misclassified as recognised funders; fixed.

## Eligibility and ranking
- `domain/eligibility.py` evaluates nationality, degree level, field, GPA (4/5/10/100 scales), language tests (cross-test conversions marked as estimates), work experience, destination and deadline against the student's actual values. Missing data is `UNKNOWN`, never assumed to pass; `requirements_unverified` is used instead of faking confidence.
- Scores are deterministic and explained component by component. Top five caps providers and puts weak or off-target results last; near-misses produce concrete gap advice.

## LLM layer
- Agents request a tier (`FAST`, `REASONING`, `VISION`); the router (`adapters/llm_router.py`) owns models, per-model rate limits, circuit breakers, run budgets and failover.
- **Local Qwen2.5-7B (llama.cpp) is part of every text chain.** The router sends each request to whichever model is expected to finish first (queue wait plus prefill plus generation), so free-tier 429s fall through to the local model and vice versa. `adapters/llama_server.py` can start and stop `llama-server` itself. See `.env.example` (`LOCAL_LLM_*`).
- Groq model IDs are no longer mangled; 429s no longer trip circuit breakers; empty reasoning responses are retried.

## Time budget
- 360 s hard, 240 s soft, 90 s always reserved for ranking, plan and SOP. Cancellation really cancels. Partial results survive timeouts.

## Knowledge base (`runs/knowledge.db`, SQLite)
- Caches only profile-independent public facts: search results (24 h), page text and extracted opportunities (7 days), and page verdicts. No student data is stored. Student-specific rejections (wrong level) are never cached as verdicts.
- `scholarship-run kb --stats` and `scholarship-run kb --purge`. The web purge endpoint does not touch it.

## API and UI
- SSE progress, snapshot view model, cancel, purge, on-demand SOP per card, Markdown download. Access code enforced when configured. The frontend builds the DOM with `textContent` (no `innerHTML` from web or LLM text).
- `/` serves the page with `style.css` and `app.js` inlined into a single response (`Cache-Control: no-store`), so embedded previews that drop `/static` requests still render correctly. `/static` is still mounted for direct use.

## Known limits
- Free-tier cloud LLMs and the Tavily plan quota are the real throughput limits. When Tavily's plan limit is hit, searches fail with `QuotaExhaustedError`, and only cached results and the Jina secondary search remain.
- The local 7B is slower than the cloud models per call and weaker at extraction; every fact it extracts still has to be quoted from the page.
- A top five can have fewer than five entries when too few pages state checkable requirements. The system reports that rather than inventing results.

## Round 2: reliability on the real search stack, and the new UI
- **Search keeps working without Tavily.** Tavily now answers 401 for the configured key. The Tavily adapter fails fast (30 min) after an auth/plan-limit error; the scout falls back to Jina for every query, including the proposer-to-official-page resolver, which previously only used the primary and resolved 0 of 12 programmes.
- **Registry channel was never filtered.** `_execute` dropped `include_domains`, so the per-country official-directory queries ran unfiltered. Fixed. Jina gets repeated `site` params; a 422 "no search results" is an empty result, not an error; 25 s timeout plus one retry.
- **`.env` comment bug.** `KEY=   # comment` is read by python-dotenv as the value. That silently disabled the Jina Reader tier (non-ASCII header) and turned on the access-code gate with a junk code. `Settings` now drops comment values; `.env.example` has no inline comments.
- **Profile enrichment can no longer sink a run.** One bad LLM value (`funding_required="unknown"`) raised in the merge, so ranking was skipped and the run degraded with zero results. The merge now keeps every field/item that validates and drops the rest.
- **Hung models.** A cloud model that hung held every audit slot for 45 s per call and was retried. FAST calls now time out at 14-40 s, a timeout is not retried on the same model, and the model is deprioritised for 90 s.
- **Audit rounds are time-capped** (70 s first pass, 60 s expansion) so stragglers cannot starve the expansion round; finished pages are kept.
- **Wrong-direction schemes.** The India registry listed ICCR (a scheme for foreigners studying in India). Replaced with outbound funders; proposals whose host country is outside the student's targets are dropped.
- **Improvement plan.** Informational/unjudged rules (funding fit, field text, destination) no longer produce junk "gap" clusters; passed deadlines no longer produce `-590 days` hints. The view model now carries `gaps` (type, description, remedy) and rule `blocking/closable`.
- **New frontend** (no external assets): hero + guided form with chip inputs, drop zone and profile-strength meter; live pipeline board with progress and activity timeline; tabbed results (top picks with score rings, evidence quotes, eligibility checks; almost-eligible with fixes; all opportunities with filters; improvement plan; reading-view SOP with copy/download/print); light/dark theme; mobile layout; `?demo=` example results. Browser-checked with Playwright against real Chrome.
- Tests: 30+ new tests (config, Jina adapter, Tavily fail-fast, scout fallback/host filter, enricher salvage, router hang, audit cap, gaps).
