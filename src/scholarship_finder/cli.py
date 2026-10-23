"""CLI entry point for running the application."""
import uvicorn
from scholarship_finder.config import get_settings

def main():
    """Run the FastAPI application."""
    cfg = get_settings()
    uvicorn.run(
        "scholarship_finder.api.main:app",
        host=cfg.api_host,
        port=cfg.api_port,
        reload=cfg.api_reload,
    )

if __name__ == "__main__":
    main()
