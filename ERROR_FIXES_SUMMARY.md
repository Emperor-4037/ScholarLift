# Error Fixes Summary

## Root Cause Analysis

The recurring errors (AttributeError: 'NoneType' object has no attribute 'upper'/'lower') occur because of a Python `.get()` behavior:

```python
# BUGGY pattern:
data.get("key", "").upper()  # If key exists with None value, returns None.upper() → AttributeError

# CORRECT pattern:
(data.get("key") or "").upper()  # If key exists with None, returns "" after "or None" → no error
```

The `.get(key, default)` method returns the **default only when the key doesn't exist**. If the key exists with a `None` value, it returns `None` (not the default).

## All Bugs Fixed

### 1. source_auditor.py - Line 674, 676
**Error**: `AttributeError: 'NoneType' object has no attribute 'upper'`
**Fix**: Changed from `data.get("funding_level", "").upper()` to `(data.get("funding_level") or "").upper()`
**Also fixed**: `data.get("kind", "").upper()` → `(data.get("kind") or "").upper()`

### 2. source_auditor.py - Line 280-281
**Error**: `AttributeError: 'NoneType' object has no attribute 'lower'`
**Fix**: Changed from `data.get("institution_or_funder", "").lower()` to `(data.get("institution_or_funder") or "").lower()`
**Status**: Already fixed in previous session

### 3. Missing asyncio imports (4 files)
**Error**: `NameError: name 'asyncio' is not defined`
**Files**: llm_openai.py, opportunity_scout.py, search_tavily.py, vision_ocr.py
**Fix**: Added `import asyncio` at module level
**Status**: Already fixed in previous session

### 4. Index errors (5 files)
**Error**: Potential `IndexError` on empty lists
**Files**: application_strategist.py, enrichment_tips.py, composition.py, url.py, app.js
**Fix**: Added length checks before list indexing
**Status**: Already fixed in previous session

### 5. Profile 404 error
**Error**: GET /api/profile/{run_id} returns 404 (profile is None)
**Root Cause**: `handle.set_state()` only called on graph success, not on failure
**Fix**: Added state preservation on TimeoutError, CancelledError, and Exception in gateway.py
**Status**: Fixed in current session

### 6. Prompt formatting KeyError
**Error**: `KeyError: ' and end with '` in opportunity_scout.py
**Root Cause**: Literal braces `{}` in prompt templates not escaped for `.format()`
**Fix**: Escaped all JSON schema braces with `{{` and `}}` in all agent prompts
**Status**: Already fixed in previous session

### 7. Empty LLM responses
**Error**: LLM returns empty JSON → 0 opportunities
**Fix**: Increased max_output_tokens to 8192, added temperature parameter, added retry mechanism
**Status**: Already fixed in previous session

## Comprehensive Scan Results

### No Remaining Issues Found

1. **`.get() + method()` patterns**: No buggy patterns found (all fixed)
2. **Syntax errors**: None
3. **Missing imports**: False positives (comments/type hints)
4. **Bare except clauses**: None
5. **Chained .get()**: None
6. **Direct dict access**: 12 cases (all safe - internal reducers)
7. **List indexing**: 13 cases (all safe - have conditional guards)

## Files Modified

1. `src/scholarship_finder/agents/source_auditor.py` - Fixed None.upper() errors
2. `src/scholarship_finder/agents/profile_enricher.py` - Added debug logging
3. `src/scholarship_finder/agents/opportunity_scout.py` - Fixed asyncio import, prompt formatting
4. `src/scholarship_finder/agents/application_strategist.py` - Fixed index errors
5. `src/scholarship_finder/agents/ranking_strategist.py` - No changes needed
6. `src/scholarship_finder/domain/enrichment_tips.py` - Fixed index error
7. `src/scholarship_finder/domain/url.py` - Fixed index error
8. `src/scholarship_finder/composition.py` - Fixed index error
9. `src/scholarship_finder/engine/gateway.py` - Fixed state preservation, added logging
10. `src/scholarship_finder/engine/graph.py` - Added state logging
11. `src/scholarship_finder/adapters/llm_openai.py` - Fixed asyncio import
12. `src/scholarship_finder/adapters/search_tavily.py` - Fixed asyncio import
13. `src/scholarship_finder/adapters/vision_ocr.py` - Fixed asyncio import
14. `src/scholarship_finder/web/static/js/app.js` - Fixed null checks

## Why These Errors Occur

1. **LLM returns None values**: When LLM extraction fails or returns partial data, fields may have `None` values instead of being absent
2. **Python .get() behavior**: `.get(key, default)` returns default only when key is missing, not when key exists with None
3. **Asyncio import missing**: Some files used asyncio functions without importing the module
4. **State not preserved on failure**: Background thread crashes didn't update state, leaving profile as None
5. **Prompt formatting**: Python's `.format()` interprets `{}` as placeholders, needing `{{` for literal braces

## Prevention

**Defensive Coding Patterns to Use**:

```python
# For dict access with potential None values:
value = (data.get("key") or "").upper()  # Handles None gracefully

# For list indexing:
if items and len(items) > 0:
    first = items[0]

# For state updates:
try:
    # operation
except Exception as exc:
    # preserve partial state before failing
    handle.set_state(partial_state)
    raise

# For LLM responses:
if not result.content or len(result.content) < 10:
    # Use fallback or retry
```

## Verification

All code has been scanned for:
- ✓ None.method() errors
- ✓ IndexError on empty lists
- ✓ Missing imports
- ✓ Syntax errors
- ✓ Bare except clauses
- ✓ Prompt formatting issues
- ✓ State preservation on failures

The codebase is now more robust against these common runtime errors.
