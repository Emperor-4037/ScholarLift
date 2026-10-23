# Source Auditor JSON Extraction Fixes

## Problem
The Source Auditor was failing with "Unterminated string" JSON errors when extracting opportunity data from web pages. This caused the system to return 0 opportunities and mark the run as DEGRADED.

## Root Causes

1. **Malformed JSON from LLM**: The LLM was generating JSON with unescaped quotes, backslashes, and special characters in the `evidence_spans` field, causing JSON parsing to fail.

2. **Insufficient Prompt Guidance**: The extraction prompt didn't provide clear instructions on how to handle special characters in evidence spans.

3. **No JSON Repair Logic**: The system had no fallback mechanism to repair common JSON issues before failing completely.

## Solutions Implemented

### 1. Enhanced Extraction Prompt
**File**: `src/scholarship_finder/agents/source_auditor.py`

Added explicit rules for JSON generation:
- Instructions to escape special characters properly (\", \\, \n)
- Alternative: use shorter, cleaner spans that avoid special characters
- Added "ENSURE VALID JSON" constraint
- Guidance to select clean, continuous text segments (15-400 chars)
- Prefer spans from single sentences or phrases
- Added critical instructions for field extraction (country, funding, institution names)

### 2. JSON Cleaning Function
**File**: `src/scholarship_finder/agents/source_auditor.py`

Added `_clean_json_string()` method:
- Removes control characters except \n, \r, \t
- Provides a foundation for future JSON repair logic
- Can be extended with more sophisticated repair strategies

### 3. JSON Repair Logic
**File**: `src/scholarship_finder/agents/source_auditor.py`

Enhanced `_extract_and_verify_batch()` with:
- Try-catch for initial JSON parse
- Attempt to extract just the array content on failure
- Detailed error logging with problematic content sample
- Graceful fallback returning empty list on irreparable errors

### 4. Additional Improvements

#### Relevant Section Extraction
Added `_extract_relevant_sections()` method:
- Extracts only paragraphs containing scholarship/funding keywords
- Limits to 4000 characters to reduce token usage
- Falls back to first 10 paragraphs if no relevant sections found
- Extracts headers/titles for context

#### Relevant Link Extraction
Added `_extract_relevant_links()` method:
- Extracts URLs that look like actual program pages
- Filters out listing/search pages
- Limits to 5 links per page
- Supports recursive link following (depth = 1)

#### Funding Filtering
Enhanced `_build_opportunity()`:
- Filters out pages explicitly marked as "NONE" funding
- Allows pages with UNKNOWN or PARTIAL funding
- Checks funding details for funding keywords before filtering
- Reduces noise from general program pages

#### Field Extraction
Added fields to extraction:
- `funding_details`: specific funding amount or description
- `eligibility`: list of key eligibility requirements
- `country` and `degree_level` evidence spans
- Combined requirements and eligibility in final output

## Configuration Changes

### Increased Wave/Batch Sizes
- `_WAVE_SIZE`: 6 → 10 (more parallel fetches)
- `_BATCH_SIZE`: 4 → 5 (more pages per extraction)
- `_EARLY_STOP`: 10 → 20 (stop after more verified opportunities)
- `_MAX_WAVES`: 5 → 4 (better coverage with balanced efficiency)

### Added Recursive Link Following
- `_RECURSIVE_DEPTH`: 1 (follow links from listing pages)
- `_MAX_CONCURRENT_FETCHES`: 6 (conservative for Send API)

## Testing

- Import test: ✅ Passed
- Span verifier tests: ✅ 13/13 passed
- Phase 3 smoke tests: ✅ 15/15 passed

## Expected Benefits

1. **Higher Success Rate**: JSON repair logic handles malformed LLM output gracefully
2. **Better Opportunity Discovery**: Relevant section extraction and link following find more opportunities
3. **Reduced Noise**: Funding filtering removes general program pages without funding
4. **Improved Completeness**: Additional fields (eligibility, funding_details) provide richer data
5. **Better Coverage**: Increased wave/batch sizes and recursive links find more opportunities

## Monitoring

Watch for these log messages:
- `"Initial JSON parse failed, attempting repair"` - JSON issues being repaired
- `"JSON repair failed"` - Irreparable JSON (should be rare)
- `"Skipping X - explicitly marked as no funding"` - Filtering working
- `"SourceAuditor: fetching N linked pages at depth Y"` - Recursive link following

## Future Enhancements

1. **Advanced JSON Repair**: Implement more sophisticated quote escaping fixes
2. **Span Quality Scoring**: Rate evidence spans and prefer high-quality ones
3. **Link Relevance Scoring**: Score extracted links and prioritize best ones
4. **Recursive Depth Tuning**: Make `_RECURSIVE_DEPTH` configurable per profile
5. **Adaptive Wave Size**: Adjust `_WAVE_SIZE` based on candidate count
