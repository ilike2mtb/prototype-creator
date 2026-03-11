import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from config import settings
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
)


def custom_openapi() -> dict:
    """Custom OpenAPI schema: pin to 3.1.0 and inject the canonical server URL."""
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(figma_router)
app.include_router(sharepoint_router)
app.include_router(chat_router)   # hidden from OpenAPI schema (include_in_schema=False)


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"ok": True}


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "name": app.title,
        "ok": True,
        "openapi": "/openapi.json",
        "docs": "/docs",
    }


@app.middleware("http")
async def log_every_request(request: Request, call_next):
    if settings.debug:
        print("INCOMING:", request.method, request.url.path)
    response = await call_next(request)
    if settings.debug:
        print("STATUS:", response.status_code)
    return response
