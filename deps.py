from typing import Annotated, Optional

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

from config import settings

service_key_header = APIKeyHeader(name="X-Service-Key", auto_error=False)


def require_service_key(
    x_service_key: Annotated[Optional[str], Depends(service_key_header)],
) -> None:
    """FastAPI dependency: validate the X-Service-Key header.
    If SERVICE_KEY is not configured, all requests are allowed through.
    """
    if not settings.service_key:
        return
    if not x_service_key or x_service_key != settings.service_key:
        raise HTTPException(status_code=401, detail="Invalid service key")
