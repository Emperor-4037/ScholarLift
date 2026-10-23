"""FastAPI application for scholarship finder.

Non-breaking addition - new FastAPI application alongside existing Streamlit UI.
Users can choose which UI to use.
"""
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import os

from scholarship_finder.composition import build_gateway
from scholarship_finder.api.routes import router, set_app

logger = logging.getLogger(__name__)

# Cursor's browser (and Chrome DevTools) probe local servers for a CDP
# endpoint at GET /json/version. That is not an application route; without
# handling it, uvicorn logs a 404 on every editor refresh.
_DEVTOOLS_PROBE_MARKERS = (" /json/version ", " /json/list ", " /json HTTP")


class _DropDevtoolsAccessLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return not any(marker in message for marker in _DEVTOOLS_PROBE_MARKERS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle.

    Non-breaking addition - builds gateway on startup.
    """
    logging.getLogger("uvicorn.access").addFilter(_DropDevtoolsAccessLogs())
    logger.info("FastAPI app starting up")
    # Build gateway on startup
    app.state.gateway = build_gateway()
    logger.info("Gateway built successfully")
    yield
    logger.info("FastAPI app shutting down")


app = FastAPI(
    title="AI Scholarship Finder",
    description="Autonomous Multi-Agent AI Scholarship Finder",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware (non-breaking addition)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes (non-breaking addition)
set_app(app)
app.include_router(router, prefix="/api")


@app.get("/json/version")
async def json_version():
    """Answer DevTools version probes and expose the app version.

    Returning 200 with ordinary app metadata (no webSocketDebuggerUrl)
    stops Cursor/Chrome from treating this process as a debug target
    while also removing the 404 from the access log.
    """
    return {
        "name": app.title,
        "version": app.version,
        "description": app.description,
    }

# Mount static files (non-breaking addition)
static_dir = os.path.join(os.path.dirname(__file__), "..", "web", "static")
templates_dir = os.path.join(os.path.dirname(__file__), "..", "web", "templates")

# Create directories if they don't exist
os.makedirs(static_dir, exist_ok=True)
os.makedirs(templates_dir, exist_ok=True)

if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

if os.path.exists(templates_dir):
    templates = Jinja2Templates(directory=templates_dir)
    app.mount("/templates", StaticFiles(directory=templates_dir), name="templates")

    @app.get("/")
    async def root(request: Request):
        """Serve the main application page."""
        return templates.TemplateResponse("index.html", {"request": request})
else:
    logger.warning("Templates directory not found, web UI unavailable")
