"""Figma API HTTP endpoints — served to the Custom GPT via OpenAPI."""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from deps import require_service_key
import services.integrations as figma_service
from config import settings

router = APIRouter()


@router.get("/figma/file", include_in_schema=False)
async def get_file(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(default=None, description="Optional Figma file key override."),
) -> JSONResponse:
    if settings.debug:
        print("Received get_file request:", file_key)
    data = await figma_service.get_file(file_key)
    return JSONResponse(content=data)


@router.get("/figma/file/nodes")
async def get_file_nodes(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(default=None, description="Optional Figma file key override."),
    ids: Optional[str] = Query(default=None, description="Comma-separated node IDs."),
    depth: Optional[int] = Query(default=None, ge=1, le=10),
) -> JSONResponse:
    if settings.debug:
        print("Received get_file_nodes request: ids=%s depth=%s" % (ids, depth))
    data = await figma_service.get_nodes(file_key=file_key, ids=ids, depth=depth)
    return JSONResponse(content=data)


@router.post("/figma/file/images")
async def get_file_images(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(default=None, description="Optional Figma file key override."),
    ids: Optional[str] = Query(default=None, description="Comma-separated node IDs."),
    format: Optional[str] = Query(default=None, pattern="^(jpg|png|svg|pdf)$"),
    scale: Optional[float] = Query(default=None, ge=0.01, le=4.0),
) -> JSONResponse:
    data = await figma_service.get_file_images(
        file_key=file_key, ids=ids, format=format, scale=scale
    )
    return JSONResponse(content=data)


@router.get("/figma/file/frames")
async def get_file_frames(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(default=None, description="Optional Figma file key override."),
    ids: Optional[str] = Query(
        default=None,
        description="Comma-separated root node IDs used to scope frame discovery.",
    ),
    depth: Optional[int] = Query(default=None, ge=1, le=10),
) -> JSONResponse:
    data = await figma_service.get_frames(file_key=file_key, ids=ids, depth=depth)
    return JSONResponse(content=data)


@router.get("/figma/file/variables")
async def get_file_variables(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(default=None, description="Optional Figma file key override."),
) -> JSONResponse:
    """Return local variables (design tokens — colors, spacing, type scales) from the Figma file."""
    data = await figma_service.get_variables(file_key=file_key)
    return JSONResponse(content=data)


@router.get("/figma/file/components")
async def get_file_components(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(default=None, description="Optional Figma file key override."),
) -> JSONResponse:
    """Return the component library (names, descriptions, node IDs) from the Figma file."""
    data = await figma_service.get_components(file_key=file_key)
    return JSONResponse(content=data)


@router.get("/figma/file/styles")
async def get_file_styles(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(default=None, description="Optional Figma file key override."),
) -> JSONResponse:
    """Return published styles (color fills, text styles, effects, grids) from the Figma file."""
    data = await figma_service.get_styles(file_key=file_key)
    return JSONResponse(content=data)


@router.api_route("/figma/file/frames/images", methods=["GET", "POST"])
async def export_frame_images(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(default=None, description="Optional Figma file key override."),
    ids: Optional[str] = Query(
        default=None,
        description="Optional comma-separated frame IDs. If omitted, all frames are exported.",
    ),
    format: Optional[str] = Query(default=None, pattern="^(jpg|png|svg|pdf)$"),
    scale: Optional[float] = Query(default=None, ge=0.01, le=4.0),
) -> JSONResponse:
    data = await figma_service.export_images(
        file_key=file_key, ids=ids, format=format, scale=scale
    )
    return JSONResponse(content=data)
