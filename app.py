import os
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

FIGMA_API_BASE = "https://api.figma.com/v1"
FIGMA_TOKEN = os.getenv("FIGMA_TOKEN", "").strip()
SERVICE_KEY = os.getenv("SERVICE_KEY", "").strip()
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))

app = FastAPI(
    title="Figma Proxy API",
    version="1.0.0",
    description="Small authenticated API for Custom GPT actions to query Figma.",
)


def _require_service_key(x_service_key: Optional[str]) -> None:
    if not SERVICE_KEY:
        return
    if not x_service_key or x_service_key != SERVICE_KEY:
        raise HTTPException(status_code=401, detail="Invalid service key")


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


@app.get("/figma/files/{file_key}")
async def get_file(
    file_key: str,
    x_service_key: Optional[str] = Header(default=None, alias="X-Service-Key"),
) -> JSONResponse:
    _require_service_key(x_service_key)
    data = await _figma_get(f"/files/{file_key}")
    return JSONResponse(content=data)


@app.get("/figma/files/{file_key}/nodes")
async def get_file_nodes(
    file_key: str,
    ids: str = Query(..., description="Comma-separated node IDs"),
    depth: Optional[int] = Query(default=None, ge=1, le=10),
    x_service_key: Optional[str] = Header(default=None, alias="X-Service-Key"),
) -> JSONResponse:
    _require_service_key(x_service_key)
    params = {"ids": ids}
    if depth is not None:
        params["depth"] = depth
    data = await _figma_get(f"/files/{file_key}/nodes", params=params)
    return JSONResponse(content=data)


@app.get("/figma/files/{file_key}/images")
async def get_file_images(
    file_key: str,
    ids: str = Query(..., description="Comma-separated node IDs"),
    format: str = Query(default="png", pattern="^(jpg|png|svg|pdf)$"),
    scale: float = Query(default=1.0, ge=0.01, le=4.0),
    x_service_key: Optional[str] = Header(default=None, alias="X-Service-Key"),
) -> JSONResponse:
    _require_service_key(x_service_key)
    params = {"ids": ids, "format": format, "scale": scale}
    data = await _figma_get(f"/images/{file_key}", params=params)
    return JSONResponse(content=data)
