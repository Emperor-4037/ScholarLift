# Scholarship Aggregator Filtering Implementation

## Problem
The system was returning scholarship listing/aggregator websites (like "Scholarships & Resources" and program directories) instead of direct scholarship opportunities. This violated the hackathon requirement to find actual scholarships, not websites that list scholarships.

## Solution Implemented

### Phase 1: Critical Fixes (Completed)

#### 1. LLM-Based Listing Page Detection
**File**: `src/scholarship_finder/agents/source_auditor.py`

**Added `is_listing_page` field to extraction prompt**:
- LLM now classifies each page as a listing page or direct scholarship
- Clear instructions on what constitutes a listing page vs direct opportunity
- Examples provided for both types

**Added `_is_listing_page()` detection method**:
- Multi-criteria detection:
  - LLM classification (`is_listing_page` field)
  - Name keyword matching ("scholarships & resources", "find scholarships", "database", "directory")
  - Institution genericness detection ("Online Master's Degrees", "Scholarship Finder")
  - Generic name detection ("Scholarships", "Funding" without specific program name)
- Comprehensive logging for transparency

**Added filtering in `_build_opportunity()`**:
- Early filter before building opportunity object
- Logs filtered pages for monitoring
- Prevents listing pages from being processed further

#### 2. Expanded Aggregator Domain List
**File**: `src/scholarship_finder/domain/trust.py`

**Expanded `_AGGREGATOR_DOMAINS` list**:
- Added 36+ new aggregator domains
- Includes: onlinemastersdegrees.com, studyportals.com, scholarshipowl.com, etc.
- Covers major scholarship databases and program portals
- Ensures trust classification catches aggregators

#### 3. Domain Exclusion in Search Queries
**File**: `src/scholarship_finder/agents/opportunity_scout.py`

**Added `_EXCLUDE_DOMAINS` list**:
- 24+ aggregator domains to exclude from search results
- Applied to all search queries (both LLM-generated and fallback)
- Prevents aggregators from appearing in candidate list

**Removed aggregators from trusted domain lists**:
- Removed scholarships.com, fastweb.com, bigfuture.collegeboard.org from `_TRUSTED_SCHOLARSHIP_DOMAINS`
- Now only includes official domains (.gov, .edu)
- Reduces chance of aggregators being included via domain filtering

**Applied exclude_domains to all queries**:
- Added `exclude_domains=_EXCLUDE_DOMAINS` to SearchQuery creation
- Applied to both LLM-generated queries and fallback queries
- Applied to query variants when enabled

## Detection Criteria

### Listing Page Characteristics
The system filters pages that match any of these criteria:

1. **LLM Classification**: LLM marks page as listing page
2. **Name Keywords**: Contains "scholarships & resources", "find scholarships", "database", "directory", "list of"
3. **Generic Institution**: "Online Master's Degrees", "Scholarship Finder", "Study Portal"
4. **Generic Name**: "Scholarships", "Funding", "Graduate Funding" without specific program name
5. **Domain Match**: Matches known aggregator domain (via trust classification)

### Direct Scholarship Page Characteristics
Pages that pass the filter:
- Specific program name: "DeepMind Scholarship 2026", "DAAD Scholarship Program"
- Official institution: "University of XYZ", "National Science Foundation"
- Official domain: .edu, .gov, or recognized funder domain
- Detailed sections: Eligibility, funding, application process
- Direct application link or instructions

## Configuration

### Aggregator Domain Lists

**Exclude Domains** (opportunity_scout.py):
```python
_EXCLUDE_DOMAINS = [
    "scholarships.com",
    "fastweb.com",
    "onlinemastersdegrees.com",
    "studyportals.com",
    "mastersportal.eu",
    "phdportal.eu",
    "findamasters.com",
    "findaphd.com",
    "opportunitydesk.org",
    "youthop.com",
    # ... 24 total
]
```

**Aggregator Domains** (trust.py):
```python
_AGGREGATOR_DOMAINS = frozenset({
    "scholarships.com",
    "mastersportal.eu",
    "onlinemastersdegrees.com",
    "studyportals.com",
    # ... 58 total
})
```

**Trusted Domains** (opportunity_scout.py):
```python
_TRUSTED_SCHOLARSHIP_DOMAINS = [
    ".gov",  # Government agencies
    ".edu",  # Universities and colleges
    # Removed: scholarships.com, fastweb.com, etc.
]
```

## Monitoring

Watch for these log messages:

**Listing Page Filtering**:
```
INFO: LLM classified as listing page: scholarships & resources from online master's degrees
INFO: Name contains listing keyword 'scholarships & resources': scholarships & resources for data science
INFO: Institution is generic 'online master's degrees': online master's degrees
INFO: Filtering out listing page: Scholarships & Resources for Data Science Master's Students
```

**Domain Exclusion**:
- Search queries will automatically exclude aggregator domains
- Trust classification will mark aggregators as AGGREGATOR tier
- `is_top_5_eligible()` prevents AGGREGATOR tier from appearing in Top 5

## Testing

### Unit Tests
✅ Trust classification tests: 22/22 passed
✅ Phase 3 smoke tests: 15/15 passed
✅ Import tests: Successful

### Manual Verification
1. Run with sample profile that previously returned aggregators
2. Check logs for "Filtering out listing page" messages
3. Verify Top 5 contains only direct scholarship opportunities
4. Verify no aggregator domains in results

## Expected Results

1. **No Aggregators in Results**: Scholarship listing sites filtered out completely
2. **Only Direct Opportunities**: Top 5 contains only actual scholarship/program pages
3. **Better Trust Scores**: Results only from official institutions and recognized funders
4. **Improved User Experience**: Users get direct links to apply, not links to more searching
5. **Compliance with Requirements**: System finds actual scholarships as per problem statement

## Tradeoffs

### Quality vs Quantity
- **Tradeoff**: Filtering reduces total candidates but increases quality
- **Decision**: Prioritize quality for hackathon - better to find 5 real scholarships than 20 listing sites

### Domain-Based vs Content-Based
- **Tradeoff**: Domain filtering is faster but less accurate
- **Decision**: Use both - domain-based for initial filter, content-based for final verification

### False Positives Risk
- **Risk**: Might filter out legitimate multi-scholarship pages from official sources
- **Mitigation**: LLM classification is context-aware and can distinguish official university scholarship pages from commercial aggregators

## Future Enhancements (Phase 3)

1. **Page Content Analysis**: Analyze page structure for multiple opportunity sections
2. **Recall Enhancement**: Add whitelist for official university scholarship directories
3. **Adaptive Filtering**: Adjust filtering strictness based on result count
4. **User Feedback**: Allow users to mark false positives/negatives
5. **ML Classification**: Train classifier on labeled listing vs direct pages

## Related Files

- `src/scholarship_finder/agents/source_auditor.py` - Listing page detection and filtering
- `src/scholarship_finder/domain/trust.py` - Aggregator domain classification
- `src/scholarship_finder/agents/opportunity_scout.py` - Domain exclusion in search queries
