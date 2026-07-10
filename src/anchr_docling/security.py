import hmac
from typing import Annotated
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from anchr_docling.config import Settings
from anchr_docling.errors import SourceDownloadError

bearer_scheme = HTTPBearer(auto_error=False)
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def require_bearer_token(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> None:
    expected = request.app.state.settings.api_token_value()
    supplied = (
        credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
    )
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def validate_download_url(url: str, settings: Settings) -> None:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() != "https":
        raise SourceDownloadError("download URL must use HTTPS")
    if not hostname or hostname not in settings.download_hosts():
        raise SourceDownloadError("download URL host is not allowed")
    if parsed.username or parsed.password:
        raise SourceDownloadError("download URL must not contain user information")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SourceDownloadError("download URL contains an invalid port") from exc
    if port not in (None, 443):
        raise SourceDownloadError("download URL must use the default HTTPS port")


def open_validated_response(
    client: httpx.Client,
    source_url: str,
    settings: Settings,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    current_url = source_url
    for redirect_count in range(settings.max_download_redirects + 1):
        validate_download_url(current_url, settings)
        request = client.build_request("GET", current_url, headers=headers)
        response = client.send(request, stream=True)
        if response.status_code not in _REDIRECT_STATUSES:
            return response

        location = response.headers.get("Location")
        response.close()
        if not location:
            raise SourceDownloadError("download redirect did not include a location")
        if redirect_count >= settings.max_download_redirects:
            raise SourceDownloadError("download URL exceeded the redirect limit")
        current_url = urljoin(current_url, location)

    raise SourceDownloadError("download URL exceeded the redirect limit")


def download_validated_bytes(
    source_url: str,
    settings: Settings,
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> bytes:
    timeout = httpx.Timeout(timeout_seconds)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = open_validated_response(client, source_url, settings)
            try:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise SourceDownloadError("download exceeded the configured size limit")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                response.close()
    except SourceDownloadError:
        raise
    except httpx.HTTPStatusError as exc:
        raise SourceDownloadError(
            f"download URL responded HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise SourceDownloadError("failed to download allowed URL") from exc
