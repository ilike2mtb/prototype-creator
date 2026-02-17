import os
from typing import Annotated, Optional

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

load_dotenv()

FIGMA_API_BASE = "https://api.figma.com/v1"
FIGMA_TOKEN = os.getenv("FIGMA_TOKEN", "").strip()
SERVICE_KEY = os.getenv("SERVICE_KEY", "").strip()
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))

app = FastAPI(
    title="Figma Proxy API",
    version="1.0.0",
    description="Small authenticated API for Custom GPT actions to query Figma.",
    openapi_version="3.0.3",
)

service_key_header = APIKeyHeader(name="X-Service-Key", auto_error=False)

def _require_service_key(x_service_key: Optional[str]) -> None:
    if not SERVICE_KEY:
        return
    if not x_service_key or x_service_key != SERVICE_KEY:
        raise HTTPException(status_code=401, detail="Invalid service key")


def require_service_key(
    x_service_key: Annotated[Optional[str], Depends(service_key_header)],
) -> None:
    _require_service_key(x_service_key)


def _require_figma_token() -> None:
    if not FIGMA_TOKEN:
        raise HTTPException(status_code=500, detail="FIGMA_TOKEN is not configured")


async def _figma_get(path: str, params: Optional[dict] = None) -> dict:
    _require_figma_token()
    headers = {"X-Figma-Token": FIGMA_TOKEN}
    url = f"{FIGMA_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.get(url, headers=headers, params=params)
        if response.status_code >= 400:
            detail = {
                "figma_status": response.status_code,
                "figma_body": response.text,
            }
            raise HTTPException(status_code=502, detail=detail)
        return response.json()


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/")
def root() -> dict:
    return {
        "name": app.title,
        "ok": True,
        "openapi": "/openapi.json",
        "docs": "/docs",
    }


@app.get("/figma/files/{file_key}")
async def get_file(
    file_key: str,
    _: None = Depends(require_service_key),
) -> JSONResponse:
    data = await _figma_get(f"/files/{file_key}")
    return JSONResponse(content=data)


@app.get("/figma/files/{file_key}/nodes")
async def get_file_nodes(
    file_key: str,
    _: None = Depends(require_service_key),
    ids: str = Query(..., description="Comma-separated node IDs"),
    depth: Optional[int] = Query(default=None, ge=1, le=10),
) -> JSONResponse:
    params = {"ids": ids}
    if depth is not None:
        params["depth"] = depth
    data = await _figma_get(f"/files/{file_key}/nodes", params=params)
    return JSONResponse(content=data)


@app.get("/figma/files/{file_key}/images")
async def get_file_images(
    file_key: str,
    _: None = Depends(require_service_key),
    ids: str = Query(..., description="Comma-separated node IDs"),
    format: str = Query(default="png", pattern="^(jpg|png|svg|pdf)$"),
    scale: float = Query(default=1.0, ge=0.01, le=4.0),
) -> JSONResponse:
    params = {"ids": ids, "format": format, "scale": scale}
    data = await _figma_get(f"/images/{file_key}", params=params)
    return JSONResponse(content=data)
