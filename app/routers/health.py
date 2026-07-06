"""Health check endpoint."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check() -> JSONResponse:
    """Return health status of the server."""
    return JSONResponse(content={"status": "ok"})
