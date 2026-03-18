import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel

from config import settings


class HealthResponse(BaseModel):
    ok: bool


class RootResponse(BaseModel):
    name: str
    ok: bool
    openapi: str
    docs: str
from routers.figma import router as figma_router
from routers.sharepoint import router as sharepoint_router
from routers.chat import router as chat_router

logging.basicConfig(level=logging.INFO)

# PUBLIC_BASE_URL drives the "servers" block in the OpenAPI schema that the
# Custom GPT reads.  Fall back to the Render-injected URL if not set explicitly.
PUBLIC_BASE_URL = (
    settings.public_base_url or os.getenv("RENDER_EXTERNAL_URL", "")
).strip()

app = FastAPI(
    title="Figma Proxy API",
    version="1.0.0",
    description="Small authenticated API for Custom GPT actions to query Figma.",
    openapi_version="3.1.0",
)


def custom_openapi() -> dict:
    """Custom OpenAPI schema: emit 3.1.0 and inject the canonical server URL."""
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        openapi_version=app.openapi_version,
    )
    openapi_schema = _normalize_openapi_for_gpt_actions(openapi_schema)
    openapi_schema["openapi"] = "3.1.0"
    if PUBLIC_BASE_URL:
        openapi_schema["servers"] = [{"url": PUBLIC_BASE_URL}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema


def _normalize_openapi_for_gpt_actions(value: Any) -> Any:
    """Normalize nullable schema fragments into a GPT-friendlier OpenAPI shape."""
    if isinstance(value, list):
        return [_normalize_openapi_for_gpt_actions(item) for item in value]

    if not isinstance(value, dict):
        return value

    normalized = {
        key: _normalize_openapi_for_gpt_actions(item)
        for key, item in value.items()
    }

    any_of = normalized.get("anyOf")
    if isinstance(any_of, list):
        non_null_options = []
        has_null_option = False
        for option in any_of:
            if isinstance(option, dict) and option.get("type") == "null":
                has_null_option = True
            else:
                non_null_options.append(option)
        if has_null_option and len(non_null_options) == 1 and isinstance(non_null_options[0], dict):
            merged = dict(non_null_options[0])
            for key, item in normalized.items():
                if key != "anyOf" and key not in merged:
                    merged[key] = item
            merged["nullable"] = True
            return merged

    return normalized


app.openapi = custom_openapi

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(figma_router)
app.include_router(sharepoint_router)
app.include_router(chat_router)   # hidden from OpenAPI schema (include_in_schema=False)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(ok=True)


@app.get("/", response_model=RootResponse)
def root() -> RootResponse:
    return RootResponse(
        name=app.title,
        ok=True,
        openapi="/openapi.json",
        docs="/docs",
    )


@app.middleware("http")
async def log_every_request(request: Request, call_next):
    if settings.debug:
        print("INCOMING:", request.method, request.url.path)
    response = await call_next(request)
    if settings.debug:
        print("STATUS:", response.status_code)
    return response
