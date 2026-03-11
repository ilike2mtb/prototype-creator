"""LLM prototype generation endpoint — used by the React frontend only.

include_in_schema=False hides this router from the Custom GPT's OpenAPI spec.
"""

from fastapi import APIRouter

from models import ChatRequest, ChatResponse
from services import anthropic_service

router = APIRouter(prefix="/api", include_in_schema=False)


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    return await anthropic_service.run_chat(
        messages=request.messages,
        framework=request.framework,
        output_type=request.output_type,
        mode=request.mode,
        drupal_version=request.drupal_version,
        figma_params=request.figma_params,
    )
