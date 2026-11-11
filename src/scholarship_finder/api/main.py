"""FastAPI application — serves the single-page web UI + /api routes."""
from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from scholarship_finder.api.routes import router
from scholarship_finder.composition import build_gateway

logger = logging.getLogger(__name__)

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
    logging.getLogger("uvicorn.access").addFilter(_DropDevtoolsAccessLogs())
    logger.info("FastAPI app starting up")
    from scholarship_finder.adapters.llama_server import LlamaServerManager
    from scholarship_finder.config import get_settings
    app.state.llama_manager = LlamaServerManager(get_settings())
    app.state.llama_healthy = await app.state.llama_manager.start()
    app.state.gateway = build_gateway()
    logger.info("Gateway built successfully")
    yield
    logger.info("FastAPI app shutting down")
    await app.state.llama_manager.stop()


app = FastAPI(
    title="ScholarLift — AI Scholarship Finder",
    description="Autonomous multi-agent scholarship finder",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


@app.get("/json/version")
async def json_version():
    return {"name": app.title, "version": app.version,
            "description": app.description}


_WEB = os.path.join(os.path.dirname(__file__), "..", "web")
static_dir = os.path.join(_WEB, "static")
templates_dir = os.path.join(_WEB, "templates")
os.makedirs(static_dir, exist_ok=True)
os.makedirs(templates_dir, exist_ok=True)

if os.path.exists(static_dir):
    class _FreshStatic(StaticFiles):
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

    app.mount("/static", _FreshStatic(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
async def root():
    index = os.path.join(templates_dir, "index.html")
    if os.path.exists(index):
        def _rd(*parts):
            with open(os.path.join(static_dir, *parts), encoding="utf-8") as f:
                return f.read()
        html = _rd("..", "templates", "index.html")
        html = html.replace('<link rel="stylesheet" href="/static/css/style.css">',
                            "<style>" + _rd("css", "style.css") + "</style>")
        html = html.replace('<script src="/static/js/app.js"></script>',
                            "<script>" + _rd("js", "app.js").replace("</script", "<\\/script") + "</script>")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})
    return {"name": app.title, "hint": "web UI not bundled"}
