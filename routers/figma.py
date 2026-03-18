"""Figma API HTTP endpoints — served to the Custom GPT via OpenAPI."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from deps import require_service_key
import services.integrations as figma_service
from config import settings
from models import (
    ErrorResponse,
    FigmaComponentsResponse,
    FigmaExportedImagesResponse,
    FigmaFileResponse,
    FigmaFramesResponse,
    FigmaImagesResponse,
    FigmaNodesResponse,
    FigmaStylesResponse,
    FigmaVariablesResponse,
)

router = APIRouter(tags=["Figma"])


FIGMA_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Invalid request parameters."},
    401: {"model": ErrorResponse, "description": "Invalid or missing service key."},
    403: {"model": ErrorResponse, "description": "Figma upstream request was forbidden."},
    404: {"model": ErrorResponse, "description": "Requested Figma resource was not found."},
    429: {"model": ErrorResponse, "description": "Figma upstream rate limit exceeded."},
    502: {"model": ErrorResponse, "description": "Figma upstream request failed."},
    503: {"model": ErrorResponse, "description": "Figma integration is not configured."},
}


def _raise_for_figma_error(data: dict) -> None:
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail={"message": "Invalid upstream response"})

    if data.get("error"):
        raise HTTPException(status_code=503, detail=str(data["error"]))

    figma_status = data.get("figma_status")
    if figma_status is not None:
        upstream_status = int(figma_status)
        status_code = upstream_status if upstream_status in {400, 401, 403, 404, 429} else 502
        raise HTTPException(
            status_code=status_code,
            detail={
                "message": "Figma upstream request failed",
                "figma_status": upstream_status,
                "figma_body": data.get("figma_body"),
            },
        )


@router.get(
    "/figma/file",
    include_in_schema=False,
    response_model=FigmaFileResponse,
    responses=FIGMA_ERROR_RESPONSES,
    summary="Get Figma file",
    description="Return basic metadata and a slimmed document tree for a Figma file.",
)
async def get_file(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(
        default=None,
        description="Optional Figma file key override.",
        examples=["AbCdEfGhIjKlMn"],
    ),
) -> FigmaFileResponse:
    if settings.debug:
        print("Received get_file request:", file_key)
    data = await figma_service.get_file(file_key)
    _raise_for_figma_error(data)
    return FigmaFileResponse.model_validate(data)


@router.get(
    "/figma/file/nodes",
    response_model=FigmaNodesResponse,
    responses=FIGMA_ERROR_RESPONSES,
    summary="Get file nodes",
    description="Return slimmed node data for one or more Figma node IDs.",
)
async def get_file_nodes(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(
        default=None,
        description="Optional Figma file key override.",
        examples=["AbCdEfGhIjKlMn"],
    ),
    ids: Optional[str] = Query(
        default=None,
        description="Comma-separated node IDs.",
        examples=["12:34,56:78"],
    ),
    depth: Optional[int] = Query(default=None, ge=1, le=10, description="Optional traversal depth.", examples=[2]),
) -> FigmaNodesResponse:
    if settings.debug:
        print("Received get_file_nodes request: ids=%s depth=%s" % (ids, depth))
    data = await figma_service.get_nodes(file_key=file_key, ids=ids, depth=depth)
    _raise_for_figma_error(data)
    return FigmaNodesResponse.model_validate(data)


@router.get(
    "/figma/file/images",
    response_model=FigmaImagesResponse,
    responses=FIGMA_ERROR_RESPONSES,
    summary="Get file images",
    description="Return rendered image URLs for specific Figma node IDs.",
)
async def get_file_images(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(
        default=None,
        description="Optional Figma file key override.",
        examples=["AbCdEfGhIjKlMn"],
    ),
    ids: Optional[str] = Query(
        default=None,
        description="Comma-separated node IDs.",
        examples=["12:34,56:78"],
    ),
    format: Optional[str] = Query(
        default=None,
        pattern="^(jpg|png|svg|pdf)$",
        description="Optional image export format.",
        examples=["png"],
    ),
    scale: Optional[float] = Query(
        default=None,
        ge=0.01,
        le=4.0,
        description="Optional export scale multiplier.",
        examples=[2],
    ),
) -> FigmaImagesResponse:
    data = await figma_service.get_file_images(
        file_key=file_key, ids=ids, format=format, scale=scale
    )
    _raise_for_figma_error(data)
    return FigmaImagesResponse.model_validate(data)


@router.post("/figma/file/images", include_in_schema=False)
async def get_file_images_post_alias(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(default=None, description="Optional Figma file key override."),
    ids: Optional[str] = Query(default=None, description="Comma-separated node IDs."),
    format: Optional[str] = Query(default=None, pattern="^(jpg|png|svg|pdf)$"),
    scale: Optional[float] = Query(default=None, ge=0.01, le=4.0),
) -> FigmaImagesResponse:
    data = await figma_service.get_file_images(
        file_key=file_key, ids=ids, format=format, scale=scale
    )
    _raise_for_figma_error(data)
    return FigmaImagesResponse.model_validate(data)


@router.get(
    "/figma/file/frames",
    response_model=FigmaFramesResponse,
    responses=FIGMA_ERROR_RESPONSES,
    summary="Get file frames",
    description="Return frame metadata discovered within the requested Figma node scope.",
)
async def get_file_frames(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(
        default=None,
        description="Optional Figma file key override.",
        examples=["AbCdEfGhIjKlMn"],
    ),
    ids: Optional[str] = Query(
        default=None,
        description="Comma-separated root node IDs used to scope frame discovery.",
        examples=["12:34,56:78"],
    ),
    depth: Optional[int] = Query(default=None, ge=1, le=10, description="Optional traversal depth.", examples=[2]),
) -> FigmaFramesResponse:
    data = await figma_service.get_frames(file_key=file_key, ids=ids, depth=depth)
    _raise_for_figma_error(data)
    return FigmaFramesResponse.model_validate(data)


@router.get(
    "/figma/file/variables",
    response_model=FigmaVariablesResponse,
    responses=FIGMA_ERROR_RESPONSES,
    summary="Get file variables",
    description="Return local variables such as colors, spacing tokens, and typography scales.",
)
async def get_file_variables(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(
        default=None,
        description="Optional Figma file key override.",
        examples=["AbCdEfGhIjKlMn"],
    ),
) -> FigmaVariablesResponse:
    """Return local variables (design tokens — colors, spacing, type scales) from the Figma file."""
    data = await figma_service.get_variables(file_key=file_key)
    _raise_for_figma_error(data)
    return FigmaVariablesResponse.model_validate(data)


@router.get(
    "/figma/file/components",
    response_model=FigmaComponentsResponse,
    responses=FIGMA_ERROR_RESPONSES,
    summary="Get file components",
    description="Return reusable component metadata from the Figma file.",
)
async def get_file_components(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(
        default=None,
        description="Optional Figma file key override.",
        examples=["AbCdEfGhIjKlMn"],
    ),
) -> FigmaComponentsResponse:
    """Return the component library (names, descriptions, node IDs) from the Figma file."""
    data = await figma_service.get_components(file_key=file_key)
    _raise_for_figma_error(data)
    return FigmaComponentsResponse.model_validate(data)


@router.get(
    "/figma/file/styles",
    response_model=FigmaStylesResponse,
    responses=FIGMA_ERROR_RESPONSES,
    summary="Get file styles",
    description="Return published Figma styles such as fills, text styles, effects, and grids.",
)
async def get_file_styles(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(
        default=None,
        description="Optional Figma file key override.",
        examples=["AbCdEfGhIjKlMn"],
    ),
) -> FigmaStylesResponse:
    """Return published styles (color fills, text styles, effects, grids) from the Figma file."""
    data = await figma_service.get_styles(file_key=file_key)
    _raise_for_figma_error(data)
    return FigmaStylesResponse.model_validate(data)


@router.get(
    "/figma/file/frames/images",
    response_model=FigmaExportedImagesResponse,
    responses=FIGMA_ERROR_RESPONSES,
    summary="Export frame images",
    description="Export rendered image URLs for selected frames, or for all discovered frames when no IDs are provided.",
)
async def export_frame_images(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(
        default=None,
        description="Optional Figma file key override.",
        examples=["AbCdEfGhIjKlMn"],
    ),
    ids: Optional[str] = Query(
        default=None,
        description="Optional comma-separated frame IDs. If omitted, all frames are exported.",
        examples=["12:34,56:78"],
    ),
    format: Optional[str] = Query(
        default=None,
        pattern="^(jpg|png|svg|pdf)$",
        description="Optional image export format.",
        examples=["png"],
    ),
    scale: Optional[float] = Query(
        default=None,
        ge=0.01,
        le=4.0,
        description="Optional export scale multiplier.",
        examples=[2],
    ),
) -> FigmaExportedImagesResponse:
    data = await figma_service.export_images(
        file_key=file_key, ids=ids, format=format, scale=scale
    )
    _raise_for_figma_error(data)
    return FigmaExportedImagesResponse.model_validate(data)


@router.post("/figma/file/frames/images", include_in_schema=False)
async def export_frame_images_post_alias(
    _: None = Depends(require_service_key),
    file_key: Optional[str] = Query(default=None, description="Optional Figma file key override."),
    ids: Optional[str] = Query(
        default=None,
        description="Optional comma-separated frame IDs. If omitted, all frames are exported.",
    ),
    format: Optional[str] = Query(default=None, pattern="^(jpg|png|svg|pdf)$"),
    scale: Optional[float] = Query(default=None, ge=0.01, le=4.0),
) -> FigmaExportedImagesResponse:
    data = await figma_service.export_images(
        file_key=file_key, ids=ids, format=format, scale=scale
    )
    _raise_for_figma_error(data)
    return FigmaExportedImagesResponse.model_validate(data)
