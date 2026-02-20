import os
from typing import Annotated, Optional
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

load_dotenv()

FIGMA_API_BASE = "https://api.figma.com/v1"
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

FIGMA_TOKEN = os.getenv("FIGMA_TOKEN", "").strip()
SERVICE_KEY = os.getenv("SERVICE_KEY", "").strip()
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or ""
).strip()

MS_TENANT_ID = os.getenv("MS_TENANT_ID", "").strip()
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "").strip()
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET", "").strip()
SP_SITE_ID = os.getenv("SP_SITE_ID", "").strip()
SP_DRIVE_ID = os.getenv("SP_DRIVE_ID", "").strip()
SP_FILE_PATH = os.getenv("SP_FILE_PATH", "").strip()
SP_WORKSHEET_NAME = os.getenv("SP_WORKSHEET_NAME", "").strip()

app = FastAPI(
    title="Figma Proxy API",
    version="1.0.0",
    description="Small authenticated API for Custom GPT actions to query Figma.",
)

service_key_header = APIKeyHeader(name="X-Service-Key", auto_error=False)


class HealthResponse(BaseModel):
    ok: bool


class RootResponse(BaseModel):
    name: str
    ok: bool
    openapi: str
    docs: str


class SharePointSheetResponse(BaseModel):
    file_name: str
    file_web_url: Optional[str] = None
    worksheet: str
    headers: list[str]
    total_rows: int
    rows: list[dict[str, str]]


def custom_openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    openapi_schema["openapi"] = "3.1.0"
    if PUBLIC_BASE_URL:
        openapi_schema["servers"] = [{"url": PUBLIC_BASE_URL}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


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


def _require_sharepoint_config() -> None:
    missing = []
    if not MS_TENANT_ID:
        missing.append("MS_TENANT_ID")
    if not MS_CLIENT_ID:
        missing.append("MS_CLIENT_ID")
    if not MS_CLIENT_SECRET:
        missing.append("MS_CLIENT_SECRET")
    if not SP_SITE_ID:
        missing.append("SP_SITE_ID")
    if not SP_DRIVE_ID:
        missing.append("SP_DRIVE_ID")
    if not SP_FILE_PATH:
        missing.append("SP_FILE_PATH")
    if missing:
        raise HTTPException(
            status_code=500,
            detail={"message": "SharePoint configuration is missing", "missing": missing},
        )


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


async def _graph_access_token(client: httpx.AsyncClient) -> str:
    token_url = f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token"
    payload = {
        "client_id": MS_CLIENT_ID,
        "client_secret": MS_CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    response = await client.post(token_url, data=payload)
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={"graph_status": response.status_code, "graph_body": response.text},
        )
    token = response.json().get("access_token")
    if not token:
        raise HTTPException(status_code=502, detail="No Graph access token returned")
    return token


async def _graph_get(
    client: httpx.AsyncClient, token: str, path: str, params: Optional[dict] = None
) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.get(f"{GRAPH_API_BASE}{path}", headers=headers, params=params)
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={"graph_status": response.status_code, "graph_body": response.text},
        )
    return response.json()


def _normalize_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _values_to_rows(values: list[list[object]]) -> tuple[list[str], list[dict[str, str]]]:
    if not values:
        return [], []

    raw_headers = values[0]
    headers: list[str] = []
    header_counts: dict[str, int] = {}
    for index, raw_header in enumerate(raw_headers):
        header = _normalize_cell(raw_header).strip() or f"column_{index + 1}"
        if header in header_counts:
            header_counts[header] += 1
            header = f"{header}_{header_counts[header]}"
        else:
            header_counts[header] = 1
        headers.append(header)

    rows: list[dict[str, str]] = []
    for raw_row in values[1:]:
        row_values = [_normalize_cell(item) for item in raw_row]
        if not any(cell.strip() for cell in row_values):
            continue
        row: dict[str, str] = {}
        for idx, header in enumerate(headers):
            row[header] = row_values[idx] if idx < len(row_values) else ""
        rows.append(row)

    return headers, rows


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return {"ok": True}


@app.head("/health", include_in_schema=False)
def health_head() -> Response:
    return Response(status_code=200)


@app.get("/health/", include_in_schema=False)
def health_slash() -> HealthResponse:
    return {"ok": True}


@app.head("/health/", include_in_schema=False)
def health_slash_head() -> Response:
    return Response(status_code=200)


@app.get("/healthz", include_in_schema=False)
def healthz() -> HealthResponse:
    return {"ok": True}


@app.head("/healthz", include_in_schema=False)
def healthz_head() -> Response:
    return Response(status_code=200)


@app.get("/", response_model=RootResponse)
def root() -> RootResponse:
    return {
        "name": app.title,
        "ok": True,
        "openapi": "/openapi.json",
        "docs": "/docs",
    }


@app.head("/", include_in_schema=False)
def root_head() -> Response:
    return Response(status_code=200)


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


@app.get(
    "/sharepoint/dci-architecture-plan",
    response_model=SharePointSheetResponse,
)
async def get_dci_architecture_plan(
    _: None = Depends(require_service_key),
) -> SharePointSheetResponse:
    _require_sharepoint_config()

    path = quote(SP_FILE_PATH.lstrip("/"), safe="/")
    item_prefix = f"/sites/{SP_SITE_ID}/drives/{SP_DRIVE_ID}/root:/{path}:"

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        token = await _graph_access_token(client)

        metadata = await _graph_get(
            client,
            token,
            item_prefix,
            params={"$select": "name,webUrl"},
        )

        worksheet_id: str
        worksheet_name: str
        if SP_WORKSHEET_NAME:
            worksheet_name_encoded = quote(SP_WORKSHEET_NAME, safe="")
            worksheet_data = await _graph_get(
                client,
                token,
                f"{item_prefix}/workbook/worksheets/{worksheet_name_encoded}",
                params={"$select": "id,name"},
            )
            worksheet_id = worksheet_data["id"]
            worksheet_name = worksheet_data.get("name", SP_WORKSHEET_NAME)
        else:
            worksheet_list = await _graph_get(
                client,
                token,
                f"{item_prefix}/workbook/worksheets",
                params={"$top": 1, "$select": "id,name"},
            )
            worksheets = worksheet_list.get("value", [])
            if not worksheets:
                raise HTTPException(
                    status_code=502,
                    detail="No worksheet found in SharePoint workbook",
                )
            worksheet_id = worksheets[0]["id"]
            worksheet_name = worksheets[0].get("name", "Sheet1")

        used_range = await _graph_get(
            client,
            token,
            f"{item_prefix}/workbook/worksheets/{worksheet_id}/usedRange(valuesOnly=true)",
            params={"$select": "values"},
        )
        values = used_range.get("values", [])
        headers, rows = _values_to_rows(values)

        return {
            "file_name": metadata.get("name", ""),
            "file_web_url": metadata.get("webUrl"),
            "worksheet": worksheet_name,
            "headers": headers,
            "total_rows": len(rows),
            "rows": rows,
        }
