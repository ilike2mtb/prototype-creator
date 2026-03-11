from pydantic import BaseModel
from typing import Optional


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    framework: str = "drupal11"
    output_type: str = "both"
    mode: str = "arch"
    drupal_version: Optional[str] = None
    figma_params: Optional[dict] = None


class ChatResponse(BaseModel):
    message: str
    artifacts: list[dict] = []
