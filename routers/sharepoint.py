"""SharePoint / MS Graph HTTP endpoints — served to the Custom GPT via OpenAPI."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from deps import require_service_key
import services.integrations as integrations

router = APIRouter()


class SharePointSheetResponse(BaseModel):
    file_name: str
    file_web_url: Optional[str] = None
    worksheet: str
    headers: list[str]
    total_rows: int
    rows: list[dict[str, str]]


@router.get(
    "/sharepoint/dci-architecture-plan",
    response_model=SharePointSheetResponse,
)
async def get_dci_architecture_plan(
    _: None = Depends(require_service_key),
) -> SharePointSheetResponse:
    try:
        return await integrations.get_architecture_plan()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get(
    "/sharepoint/architecture-plan-template",
    response_model=SharePointSheetResponse,
)
async def get_architecture_plan_template(
    _: None = Depends(require_service_key),
) -> SharePointSheetResponse:
    try:
        return await integrations.get_architecture_template()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
