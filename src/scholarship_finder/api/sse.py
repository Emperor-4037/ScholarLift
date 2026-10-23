"""SSE (Server-Sent Events) utilities for real-time updates.

Non-breaking addition - separate module for SSE functionality.
"""
from fastapi.responses import StreamingResponse


async def event_stream(generator):
    """Convert async generator to SSE response."""
    async for message in generator:
        yield message


class SSEStreamingResponse(StreamingResponse):
    """Custom SSE response with proper headers.

    Non-breaking addition - used by FastAPI routes.
    """

    def __init__(self, generator, **kwargs):
        super().__init__(
            event_stream(generator),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
            **kwargs
        )
