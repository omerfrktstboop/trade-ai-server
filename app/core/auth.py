"""Bearer token authentication dependency.

Every protected endpoint injects this dependency. The dependency reads the
``Authorization: Bearer ***`` header and validates it against the configured
``API_TOKEN`` from settings.

Returns 401 in both cases:
  - Token is missing from the request
  - Token is present but does not match the configured value
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

# auto_error=False so we control the exact response ourselves (always 401)
bearer_scheme = HTTPBearer(
    scheme_name="Bearer",
    description="Enter your API token (configured via API_TOKEN env var)",
    auto_error=False,
)


async def verify_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> str:
    """Validate the Bearer token against the configured API token.

    Returns the token string on success so endpoints can pass it downstream
    if needed.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if credentials.credentials != settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials
