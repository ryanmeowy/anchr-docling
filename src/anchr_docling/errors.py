from anchr_docling.schemas import ParseWarning


class SourceDownloadError(RuntimeError):
    pass


class DoclingParseError(RuntimeError):
    pass


class ImageUploadError(RuntimeError):
    pass


def add_warning(
    warnings: list[ParseWarning],
    *,
    code: str,
    message: str,
    block_id: str | None = None,
) -> None:
    warning = ParseWarning(code=code, message=message, block_id=block_id)
    if warning not in warnings:
        warnings.append(warning)


def sanitize_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    return message[:200]
