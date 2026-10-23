// Application state
let currentRunId = null;
let eventSource = null;
let runCounter = 0;
let resultsFetchedForRun = null;

// DOM elements
const uploadForm = document.getElementById('upload-form');
const progressSection = document.getElementById('progress-section');
const resultsSection = document.getElementById('results-section');
const telemetryLog = document.getElementById('telemetry-log');
const progressFill = document.getElementById('progress-fill');
const opportunitiesGrid = document.getElementById('opportunities-grid');
const sopText = document.getElementById('sop-text');
const clearDataBtn = document.getElementById('clear-data');
const profileSummary = document.getElementById('profile-summary');
const enrichmentTips = document.getElementById('enrichment-tips');

// Upload handler
uploadForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    
    const resumeFile = document.getElementById('resume').files[0];
    
    if (!resumeFile) {
        alert('Please select a resume file');
        return;
    }
    
    const formData = new FormData();
    formData.append('file', resumeFile);
    formData.append('display_name', document.getElementById('display-name').value);
    formData.append('github_username', document.getElementById('github').value);
    formData.append('force_refresh', document.getElementById('force-refresh').checked);
    
    try {
        const response = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });
        
        const data = await response.json();
        
        if (response.ok) {
            currentRunId = data.run_id;
            resultsFetchedForRun = null;
            runCounter++;
            startEventStream(currentRunId);
            showProgress();
        } else {
            alert('Upload failed: ' + data.detail);
        }
    } catch (error) {
        alert('Upload failed: ' + error.message);
    }
});

// SSE event streaming
function startEventStream(runId) {
    eventSource = new EventSource(`/api/events/${runId}`);
    
    eventSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleEvent(data);
        } catch (e) {
            console.error('Failed to parse event:', event.data);
        }
    };
    
    eventSource.onerror = () => {
        console.error('SSE connection error');
        eventSource.close();
    };
}

// Event handler
function handleEvent(event) {
    const timestamp = new Date().toLocaleTimeString();
    const logEntry = `[Run #${runCounter}] [${timestamp}] ${event.node || 'system'}: ${event.kind || event.type}<br>`;
    telemetryLog.innerHTML += logEntry;
    telemetryLog.scrollTop = telemetryLog.scrollHeight;

    // Show critical errors prominently
    if (event.kind === 'error' || event.severity === 'ERROR') {
        showError(event);
    }

    // Update progress based on phase
    if (event.kind === 'node_started') {
        updateProgress(event.node);
    }

    // Show results when phase A is ready (top-5 ranked)
    if (event.kind === 'phase_a_ready') {
        showResults();
    }

    if (event.kind === 'run_degraded') {
        showError(event);
        showResults();
    }

    // Handle SOP chunks
    if (event.kind === 'sop_chunk') {
        sopText.textContent += event.payload.chunk;
    }

    // Handle completion
    if (event.type === 'complete' || event.kind === 'run_completed') {
        eventSource.close();
        showResults();
    }
}

// Show error prominently
function showError(event) {
    const errorMessage = event.payload?.message || event.payload?.reason || 'Unknown error';
    const errorDetail = event.payload?.detail || '';
    const errorDiv = document.createElement('div');
    errorDiv.className = 'error-message';
    errorDiv.style.cssText = 'background: #fee; border: 1px solid #f00; padding: 10px; margin: 10px 0; border-radius: 4px; color: #c00;';
    errorDiv.innerHTML = `
        <strong>Error:</strong> ${errorMessage}<br>
        ${errorDetail ? `<small>${errorDetail}</small>` : ''}
    `;
    telemetryLog.parentNode.insertBefore(errorDiv, telemetryLog.nextSibling);
}

// Progress update
function updateProgress(node) {
    const phases = ['profile_enricher', 'opportunity_scout', 'source_auditor', 'ranking_strategist', 'application_strategist'];
    const index = phases.indexOf(node);
    if (index >= 0) {
        const progress = ((index + 1) / phases.length) * 100;
        progressFill.style.width = `${progress}%`;
    }
}

// UI helpers
function showProgress() {
    progressSection.classList.remove('hidden');
    resultsSection.classList.add('hidden');
}

function renderProfile(profileData) {
    const profile = profileData.profile;
    const flags = profileData.completeness_flags;
    const tips = profileData.enrichment_tips;
    const score = profileData.completeness_score;

    // Render profile summary
    let summaryHtml = `<h3>📊 Profile Completeness: ${(score * 100).toFixed(0)}%</h3>`;

    if (profile.education && profile.education.length > 0) {
        summaryHtml += `<p><strong>Education:</strong> ${profile.education.map(e => e.degree + ' in ' + e.field).join(', ')}</p>`;
    }
    if (profile.skills && profile.skills.length > 0) {
        summaryHtml += `<p><strong>Skills:</strong> ${profile.skills.slice(0, 5).join(', ')}${profile.skills.length > 5 ? '...' : ''}</p>`;
    }
    if (profile.preferences) {
        const countries = profile.preferences.countries || [];
        const fields = profile.preferences.target_fields || [];
        if (countries.length > 0) {
            summaryHtml += `<p><strong>Target Countries:</strong> ${countries.join(', ')}</p>`;
        }
        if (fields.length > 0) {
            summaryHtml += `<p><strong>Target Fields:</strong> ${fields.join(', ')}</p>`;
        }
    }

    profileSummary.innerHTML = summaryHtml;

    // Render enrichment tips
    let tipsHtml = '<h3>💡 Profile Enrichment Tips</h3><ul>';
    if (tips.length === 0) {
        tipsHtml += '<li>✅ Your profile is complete! No enrichment needed.</li>';
    } else {
        tips.forEach(tip => {
            tipsHtml += `<li>${tip}</li>`;
        });
    }
    tipsHtml += '</ul>';
    enrichmentTips.innerHTML = tipsHtml;
}

function showResults() {
    if (!currentRunId || resultsFetchedForRun === currentRunId) {
        return;
    }
    resultsFetchedForRun = currentRunId;
    progressSection.classList.add('hidden');
    resultsSection.classList.remove('hidden');

    // Fetch profile, enriched opportunities, and rehydrated SOP in parallel
    Promise.all([
        fetch(`/api/profile/${currentRunId}`).then(res => res.ok ? res.json() : {profile: {}, completeness_flags: {}, enrichment_tips: [], completeness_score: 0}),
        fetch(`/api/opportunities/${currentRunId}`).then(res => res.ok ? res.json() : {opportunities: []}),
        fetch(`/api/sop/${currentRunId}`).then(res => res.ok ? res.json() : {sop_text: ''})
    ])
    .then(([profileData, oppData, sopData]) => {
        // Render profile with enrichment tips
        renderProfile(profileData);

        // Use enriched opportunity data with rationale and gaps
        const enrichedOpps = oppData.opportunities || [];
        renderOpportunities(enrichedOpps);

        // Use rehydrated SOP (with [CANDIDATE_NAME] replaced)
        const rehydratedSOP = sopData.sop_text || '';

        // Replace FACT_XXX placeholders with actual values from ledger
        // Note: The ledger is not returned by the sop endpoint, so we skip this replacement
        // The SOP should already have placeholders stripped by the backend
        let finalSOP = rehydratedSOP;

        // Clean up any remaining placeholders with brackets (shouldn't happen but just in case)
        finalSOP = finalSOP.replace(/\[FACT_\d+\]/g, '');
        finalSOP = finalSOP.replace(/\[FACT_\d+(?:,\s*FACT_\d+)*\]/g, '');
        finalSOP = finalSOP.replace(/\s+/g, ' ').trim(); // Clean up extra spaces

        console.log('SOP after cleanup:', finalSOP);
        renderSOP(finalSOP);
    })
    .catch(error => {
        console.error('Failed to fetch results:', error);
    });
}

function renderOpportunities(opportunities) {
    if (!opportunities || opportunities.length === 0) {
        opportunitiesGrid.innerHTML = '<p>No opportunities found</p>';
        document.getElementById('opportunities-heading').textContent = 'No Opportunities Found';
        return;
    }

    // Update heading dynamically based on actual count
    const count = opportunities.length;
    const heading = document.getElementById('opportunities-heading');
    if (count === 1) {
        heading.textContent = 'Top Opportunity';
    } else {
        heading.textContent = `Top ${count} Opportunities`;
    }

    opportunitiesGrid.innerHTML = opportunities.map(item => {
        const opp = item.opportunity || {};
        const rationale = item.rationale || '';
        const compositeScore = item.composite_score || 0;
        const gaps = item.gaps || [];
        const rank = item.rank || '?';

        return `
        <div class="opportunity-card">
            <div class="opp-header">
                <h3>${opp.name || 'Unnamed Opportunity'}</h3>
                <span class="match-score">Match: ${Math.round(compositeScore)}/100 (Rank #${rank})</span>
            </div>
            <p><strong>Institution:</strong> ${opp.institution_or_funder || 'Unknown'}</p>
            <p><strong>Country:</strong> ${opp.country || 'Unknown'}</p>
            <p><strong>Funding:</strong> ${opp.funding?.level || opp.funding?.details || 'Unknown'}</p>
            <p><strong>Degree Level:</strong> ${opp.degree_level || 'Unknown'}</p>

            <!-- Rationale Section -->
            ${rationale ? `
                <div class="rationale-section">
                    <h4>Why This Fits You</h4>
                    <p>${rationale}</p>
                </div>
            ` : ''}

            <!-- Deadline -->
            ${opp.deadlines && opp.deadlines.length > 0 ? `
                <p><strong>Deadline:</strong> ${opp.deadlines[0].date || 'Check official page'}</p>
            ` : ''}

            ${opp.official_url ? `<p><a href="${opp.official_url}" target="_blank" class="btn-link">View Official Page & Apply</a></p>` : ''}
        </div>
        `;
    }).join('');
}

function renderSOP(sopText) {
    const sopElement = document.getElementById('sop-text');
    const MAX_WORDS = 200; // Reduced from full SOP

    if (!sopText) {
        sopElement.textContent = 'No SOP generated';
        return;
    }

    const words = sopText.split(/\s+/);
    if (words.length <= MAX_WORDS) {
        sopElement.textContent = sopText;
    } else {
        const truncated = words.slice(0, MAX_WORDS).join(' ');
        const remaining = words.slice(MAX_WORDS).join(' ');

        sopElement.innerHTML = `
            <div id="sop-truncated">${truncated}...</div>
            <div id="sop-full" style="display:none;">${sopText}</div>
            <button id="expand-sop" class="btn btn-secondary" style="margin-top:10px;">Read More</button>
        `;

        document.getElementById('expand-sop').addEventListener('click', () => {
            const truncated = document.getElementById('sop-truncated');
            const full = document.getElementById('sop-full');
            const btn = document.getElementById('expand-sop');

            if (full.style.display === 'none') {
                truncated.style.display = 'none';
                full.style.display = 'block';
                btn.textContent = 'Show Less';
            } else {
                truncated.style.display = 'block';
                full.style.display = 'none';
                btn.textContent = 'Read More';
            }
        });
    }
}

// Clear data
clearDataBtn.addEventListener('click', () => {
    if (currentRunId) {
        fetch(`/api/cancel/${currentRunId}`, { method: 'POST' })
            .catch(error => console.error('Failed to cancel:', error));
    }
    currentRunId = null;
    resultsFetchedForRun = null;
    if (eventSource) {
        eventSource.close();
    }
    telemetryLog.textContent = '';
    progressFill.style.width = '0%';
    progressSection.classList.add('hidden');
    resultsSection.classList.add('hidden');
    profileSummary.textContent = '';
    enrichmentTips.textContent = '';
    uploadForm.reset();
    // Note: runCounter is NOT reset to maintain numbering across runs
});
