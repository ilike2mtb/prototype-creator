"""LLM prototype generation endpoint — used by the React frontend only.

include_in_schema=False hides this router from the Custom GPT's OpenAPI spec.
"""

import anthropic as _anthropic
from fastapi import APIRouter, HTTPException

from models import ChatRequest, ChatResponse
from services import anthropic_service

router = APIRouter(prefix="/api", include_in_schema=False)


def _anthropic_detail(exc: _anthropic.APIStatusError) -> str:
    """Extract a clean human-readable message from an Anthropic API error."""
    try:
        return exc.body.get("error", {}).get("message") or str(exc)
    except Exception:
        return str(exc)


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    try:
        message, artifacts = await anthropic_service.run_chat(
            messages=request.messages,
            framework=request.framework,
            output_type=request.output_type,
            mode=request.mode,
            drupal_version=request.drupal_version,
            figma_params=request.figma_params,
        )
        return ChatResponse(
            message=message,
            artifacts=[artifacts] if artifacts else [],
        )
    except _anthropic.AuthenticationError as e:
        raise HTTPException(status_code=401, detail=_anthropic_detail(e))
    except _anthropic.PermissionDeniedError as e:
        raise HTTPException(status_code=403, detail=_anthropic_detail(e))
    except _anthropic.RateLimitError as e:
        raise HTTPException(status_code=429, detail=_anthropic_detail(e))
    except _anthropic.APIStatusError as e:
        # Catches BadRequestError (credits, invalid params) and any other 4xx/5xx
        raise HTTPException(status_code=e.status_code or 502, detail=_anthropic_detail(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
