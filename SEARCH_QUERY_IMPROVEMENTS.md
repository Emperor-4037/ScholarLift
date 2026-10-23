# Search Query Optimization Improvements

## Overview
Implemented Phase 1 and Phase 2 of the search query optimization plan for the Opportunity Scout agent to improve discovery of scholarship and funding opportunities.

## Changes Made

### File: `src/scholarship_finder/agents/opportunity_scout.py`

#### 1. Enhanced LLM Prompt (Phase 1)
- Updated `_QUERY_PLAN_PROMPT` with query expansion techniques
- Added Chain-of-Thought reasoning field ("reasoning")
- Explicit instruction to use synonyms and OR operators
- Added guidance on using quotes for exact phrases
- Emphasized funding-specific keywords with expanded list
- Added query length constraint (max 400 characters for Tavily API)
- Included query expansion strategy section

#### 2. Query Quality Metrics (Phase 1)
- Added query length validation with truncation for Tavily API
- Implemented funding keyword counting and logging
- Added reasoning field extraction and logging
- Logs query metadata: intent, country, keyword count, length, reasoning

#### 3. Domain Filtering Strategy (Phase 2)
- Added trusted domain lists for each intent:
  - `_TRUSTED_SCHOLARSHIP_DOMAINS`: .gov, .edu, major scholarship sites
  - `_TRUSTED_PROGRAM_DOMAINS`: .edu domains
  - `_TRUSTED_FUNDER_DOMAINS`: .gov, .edu, major funding organizations
- Implemented `_get_trusted_domains()` helper function
- Applied domain filtering to alternate queries (every other query)
- Alternates between domain-constrained and unconstrained searches

#### 4. Enhanced Fallback Queries (Phase 1)
- Updated `_fallback_queries()` with query expansion
- Added 5 distinct query variants with different funding keywords:
  - Variant 1: Exact phrase matching with "fully funded"
  - Variant 2: Synonym expansion with OR operators
  - Variant 3: Program funding with institutional language
  - Variant 4: International-specific opportunities
  - Variant 5: Research-focused fellowships and grants
- Uses quotes for exact phrases where appropriate
- Includes multiple funding term variants per query

#### 5. Query Variants Feature (Phase 2)
- Implemented `_generate_query_variants()` function
- Creates 2-3 variants per base query:
  - Variant 1: Exact phrase match (adds quotes to key phrases)
  - Variant 2: Domain-constrained (if not already applied)
- Added feature toggle `_ENABLE_QUERY_VARIANTS` (disabled by default)
- Modified `_execute_searches()` to generate and execute variants when enabled
- **Note**: Disabled by default to control API costs; can be enabled for testing

#### 6. Additional Improvements
- Fixed result type handling in `_execute_searches()` (moved `elif isinstance(result, list)` before else)
- Improved error handling consistency
- Added comprehensive docstrings

## Research-Based Techniques Applied

1. **Query Expansion**: LLM-based expansion with Chain-of-Thought prompting
2. **Template-Based Design**: Structured prompt for deterministic output
3. **Semantic Query Expansion**: Synonyms and OR operators
4. **Domain-Constrained Search**: Trusted source targeting
5. **Multi-Query Aggregation**: Parallel execution with variants (optional)
6. **Funding-Specific Keywords**: Expanded vocabulary for better relevance

## Expected Benefits

1. **Improved Recall**: Query expansion and synonyms capture more opportunities
2. **Better Precision**: Domain filtering reduces noise from low-quality sources
3. **Higher Funding Relevance**: Enhanced funding keyword targeting
4. **Reduced Retry Rate**: Better initial queries reduce need for retry rounds
5. **Measurable Quality**: Query metrics enable monitoring and tuning

## Configuration

### Feature Toggles
- `_ENABLE_QUERY_VARIANTS`: Set to `True` to enable query variant generation
  - Increases API usage (2-3x queries per profile)
  - Improves recall through diverse search strategies
  - Recommended for testing before production use

### Trusted Domain Lists
Can be customized by modifying the domain lists at the top of the file:
- `_TRUSTED_SCHOLARSHIP_DOMAINS`
- `_TRUSTED_PROGRAM_DOMAINS`
- `_TRUSTED_FUNDER_DOMAINS`

## Testing

- All domain tests pass (191/192 - 1 pre-existing failure unrelated to changes)
- Phase 3 smoke tests pass (15/15)
- Import test successful
- No breaking changes to existing functionality

## Next Steps (Phase 3 - Optional)

1. **Query Decomposition**: Detect complex profiles and decompose into sub-queries
2. **Iterative Refinement**: Analyze results and refine queries based on patterns
3. **A/B Testing**: Compare query quality metrics before/after with real profiles
4. **Performance Monitoring**: Track candidates discovered per profile
5. **Enable Query Variants**: Test with `_ENABLE_QUERY_VARIANTS = True` after baseline measurement

## References

- Query Expansion by Prompting LLMs: arxiv.org/abs/2305.03653
- Query Rewriting Strategies: Elasticsearch Labs
- Tavily Search Best Practices: docs.tavily.com
- Semantic Query Expansion: PMC (NIH)
- Scholarship Search Best Practices: University of Washington Libraries
