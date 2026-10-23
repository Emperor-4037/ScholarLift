# Screen Recording Guide for TetraNoodle Hackathon Demo

## Overview
This guide will help you create a compelling 5-minute screen recording showing your AI agents in action.

## Prerequisites
- Screen recording software (OBS Studio, Loom, or Windows Game Bar)
- Your Streamlit app running locally
- API keys configured (or use REPLAY mode for zero-key demo)

## Recording Setup

### Option 1: Use REPLAY Mode (Recommended for Reliable Demo)
**Advantages:** No API keys needed, fast, reliable, perfect for demos

### Option 2: Use Live Mode
**Advantages:** Shows real AI agents working, more impressive
**Requirements:** Valid GEMINI_API_KEY and TAVILY_API_KEY

## Demo Script (5 Minutes)

### 0:00 - 0:30: Introduction
**Action:** Show the README and explain the project
- "This is the Autonomous Multi-Agent AI Scholarship Finder"
- "It uses 5 AI agents working together to find scholarships"
- "Built for the TetraNoodle AI Hackathon 2026"

### 0:30 - 1:00: System Overview
**Action:** Show the ARCHITECTURE.md file
- Open ARCHITECTURE.md in a browser or markdown viewer
- "The system has 5 agents: Profile Enricher, Opportunity Scout, Source Auditor, Ranking Strategist, and Application Strategist"
- "It uses real APIs like Gemini, Tavily, and GitHub"
- "All with resilience layers for error handling"

### 1:00 - 1:30: Start the Application
**Action:** Launch Streamlit app
```bash
cd C:\ScholarShip_Finder
streamlit run streamlit_app.py
```
- "Now let's start the application"
- "This is the Streamlit UI"
- "You can see the sidebar with system status"

### 1:30 - 2:00: Show REPLAY Mode Setup
**Action:** Enable REPLAY mode in sidebar
- "For this demo, I'll use REPLAY mode which uses a sample persona"
- "Toggle the REPLAY Mode switch"
- "This uses a pre-recorded student profile named Rahul"

### 2:00 - 2:30: Show System Status
**Action:** Expand "System Status" in sidebar
- "Let's check the system status"
- "You can see which API providers are configured"
- "In REPLAY mode, it works even without API keys"

### 2:30 - 3:00: Start the Run
**Action:** Click "Run Demo (REPLAY)" button
- "Now I'll click to start the demo"
- "The system processes the resume"
- "You can see the live telemetry appearing"

### 3:00 - 3:45: Show Live Telemetry
**Action:** Expand "Live Telemetry" section
- "Watch the agents working in real-time"
- "You can see each agent's progress"
- "Profile Enricher extracts skills and projects"
- "Opportunity Scout searches for scholarships"
- "Source Auditor verifies official sources"
- "Ranking Strategist scores and ranks opportunities"

### 3:45 - 4:15: Show Top-5 Results
**Action:** Scroll through the top 5 opportunity cards
- "Here are the top 5 opportunities found"
- "Each card shows the match score, funding level, and trust tier"
- "You can see evidence badges for verified requirements"
- "Gap analysis shows what's missing"
- "Official links to apply"

### 4:15 - 4:40: Show SOP Generation
**Action:** Scroll to the Statement of Purpose section
- "The system also drafts a Statement of Purpose"
- "It includes citations from the student's profile"
- "You can export it as Markdown or PDF"

### 4:40 - 5:00: Show Privacy Features
**Action:** Click "Clear My Data Now" button
- "Privacy is important - all data is stored in memory only"
- "Click Clear My Data to purge everything"
- "The PII Vault auto-purges after 15 minutes"
- "No data is stored permanently"

## Alternative: Live Mode Demo (If You Have API Keys)

If you want to show live agents working:

1. **Setup (0:30):** Disable REPLAY mode, show API keys are configured
2. **Upload Resume (0:45):** Upload a real resume (PDF or text)
3. **Enter Profile (0:30):** Enter name and GitHub username
4. **Run Live (2:00):** Click "Find My Scholarships" and watch real-time
5. **Show Results (1:15):** Same as REPLAY mode but with real data

## Recording Tips

### Before Recording
- Close unnecessary applications
- Set screen resolution to 1920x1080
- Test the app works smoothly
- Prepare any sample resumes if using live mode

### During Recording
- Speak clearly and at a moderate pace
- Point to UI elements with your mouse
- Wait for UI to load before moving on
- Keep movements smooth and deliberate

### After Recording
- Trim any mistakes or long pauses
- Add captions if possible
- Export in MP4 format at 1080p
- Keep file size under 100MB for easy sharing

## Recording Software Options

### Windows Game Bar (Built-in)
- Press `Win + G` to open
- Click record button
- Saves to Videos/Captures folder

### OBS Studio (Free, Professional)
- Download from obsproject.com
- Better quality and editing options
- Can add overlays and text

### Loom (Free, Easy)
- Download from loom.com
- Web-based, easy to share
- Good for quick recordings

## What to Highlight for Judges

1. **Autonomous Agents:** Emphasize that agents work together without human intervention
2. **Real APIs:** Mention using real search and LLM APIs
3. **Trust Verification:** Show evidence badges and trust tiers
4. **Privacy:** Highlight PII scrubbing and vault
5. **Resilience:** Mention the 6-layer resilience stack
6. **Clean Architecture:** Reference the hexagonal architecture

## Common Issues and Solutions

### Issue: App is slow
**Solution:** Use REPLAY mode for faster demo

### Issue: API rate limits
**Solution:** Use REPLAY mode or wait for quota to reset

### Issue: Screen recording is blurry
**Solution:** Increase resolution to 1080p, use better recording software

### Issue: Audio is unclear
**Solution:** Use a good microphone, record in quiet environment

## Final Checklist

- [ ] App runs smoothly without errors
- [ ] REPLAY mode works (or live mode with API keys)
- [ ] All 5 agents complete successfully
- [ ] Top-5 results display correctly
- [ ] SOP generates and displays
- [ ] Clear My Data button works
- [ ] Recording is clear and audible
- [ ] Demo is under 5 minutes
- [ ] Key features are highlighted

## Upload Instructions

Once recorded:
1. Save as `demo_recording.mp4`
2. Upload to Google Drive, Dropbox, or YouTube (unlisted)
3. Add link to your GitHub repository README
4. Include in hackathon submission

## Example README Addition

```markdown
## Demo Video

Watch the 5-minute demo showing the AI agents in action:

[![Demo Video](https://img.youtube.com/vi/VIDEO_ID/0.jpg)](https://www.youtube.com/watch?v=VIDEO_ID)

Or download: [demo_recording.mp4](https://drive.google.com/file/d/FILE_ID/view)
```
