import base64
import json
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from anchr_docling._utils import (
    collect_child_text,
    ref_to_block_id,
    resolve_block_type,
    resolve_page_no,
)
from anchr_docling.config import settings
from anchr_docling.errors import ImageUploadError, add_warning, sanitize_error_message
from anchr_docling.schemas import EncryptedCredentials, OssUploadOptions, ParseWarning, ParseOptions

_log = logging.getLogger(__name__)

@dataclass(frozen=True)
class OssCredentials:
    access_key_id: str
    access_key_secret: str
    security_token: str
    expiration: str | None = None


@dataclass
class ImageUploadContext:
    uploader: "OssImageUploader | None"
    unavailable_code: str | None = None
    unavailable_message: str | None = None




def build_image_upload_context(
    oss_options: OssUploadOptions | None,
    warnings: list[ParseWarning],
    request_id: str | None = None,
) -> ImageUploadContext:
    if oss_options is None:
        return ImageUploadContext(
            uploader=None,
            unavailable_code="image_upload_skipped_no_credentials",
            unavailable_message="OSS credentials were not provided; image upload was skipped.",
        )

    try:
        credentials = decrypt_oss_credentials(oss_options.encrypted_credentials)
        return ImageUploadContext(
            uploader=OssImageUploader(
                endpoint=oss_options.endpoint,
                bucket_name=oss_options.bucket,
                base_path=oss_options.base_path,
                credentials=credentials,
                request_id=request_id,
            )
        )
    except Exception as exc:
        message = sanitize_error_message(exc)
        add_warning(
            warnings,
            code="image_upload_failed",
            message=f"OSS credentials could not be decrypted: {message}",
        )
        return ImageUploadContext(
            uploader=None,
            unavailable_code="image_upload_failed",
            unavailable_message="OSS credentials could not be decrypted.",
        )


def attach_picture_image_metadata(
    document: Any,
    item: Any,
    block: dict[str, Any],
    upload_context: ImageUploadContext | None,
    warnings: list[ParseWarning],
) -> None:
    block_id = block["blockId"]
    block["imageKey"] = None
    block["imageUploadStatus"] = "no_image"
    block["imageUploadError"] = None

    image = get_picture_image(document, item)
    if image is None:
        return

    block["imageMimeType"] = "image/png"
    block["imageWidth"], block["imageHeight"] = image.size

    if upload_context is None or upload_context.uploader is None:
        code = (
            upload_context.unavailable_code
            if upload_context is not None and upload_context.unavailable_code is not None
            else "image_upload_skipped_no_credentials"
        )
        message = (
            upload_context.unavailable_message
            if upload_context is not None and upload_context.unavailable_message is not None
            else "OSS credentials were not provided; image upload was skipped."
        )
        block["imageUploadStatus"] = (
            "skipped_no_credentials"
            if code == "image_upload_skipped_no_credentials"
            else "failed"
        )
        if block["imageUploadStatus"] == "failed":
            block["imageUploadError"] = message
        add_warning(warnings, code=code, message=message, block_id=block_id)
        return

    try:
        image_key = upload_context.uploader.upload_png(block_id, image)
        block["imageKey"] = image_key
        block["imageUploadStatus"] = "uploaded"
    except Exception:
        message = "Failed to upload image to OSS."
        block["imageUploadStatus"] = "failed"
        block["imageUploadError"] = message
        add_warning(
            warnings,
            code="image_upload_failed",
            message=message,
            block_id=block_id,
        )


def get_picture_image(document: Any, item: Any) -> Any | None:
    get_image = getattr(item, "get_image", None)
    if get_image is None:
        return None
    try:
        return get_image(doc=document)
    except Exception:
        return None


def _get_picture_alt_text(document: Any, item: Any) -> str:
    """Get alt text for a picture item.

    Prefers the document caption via ``FloatingItem.caption_text``.
    Falls back to collecting child/caption text (same logic as blocks mode).
    """
    caption_text_fn = getattr(item, "caption_text", None)
    if caption_text_fn is not None:
        try:
            return caption_text_fn(document)
        except Exception:
            pass
    texts = collect_child_text(document, item)
    return texts[0] if texts else ""


def enrich_markdown_text(
    document: Any,
    markdown_text: str,
    upload_context: ImageUploadContext | None,
    warnings: list[ParseWarning],
    image_cache: dict[str, str | None],
    page_no: int | None = None,
    images: list[dict[str, Any]] | None = None,
) -> str:
    """Replace ``<!-- image -->`` placeholders with OSS-hosted markdown images.

    Iterates pictures in document order, uploads them via the uploader,
    and substitutes each placeholder sequentially.  A per-self-ref cache
    avoids uploading the same picture more than once (shared between
    document-level and per-page texts).

    When *images* is provided, each picture is appended as a structured
    dict (``url``, ``pageNo``, ``blockId``, ``alt``) for the top-level
    ``images`` response field.
    """
    placeholder = "<!-- image -->"
    if placeholder not in markdown_text:
        return markdown_text

    # Collect picture items in document traversal order,
    # optionally filtered by page.
    picture_items: list[tuple[Any, str]] = []
    for item, _ in document.iterate_items(
        with_groups=True, traverse_pictures=True
    ):
        ref = getattr(item, "self_ref", None)
        if not ref or resolve_block_type(item) != "picture":
            continue
        if page_no is not None:
            item_page_no = resolve_page_no(document, item)
            if item_page_no != page_no:
                continue
        picture_items.append((item, ref_to_block_id(ref)))

    if not picture_items:
        return markdown_text

    # Build ordered lists of alt texts and image URLs.
    alt_texts: list[str] = []
    image_urls: list[str | None] = []

    for item, block_id in picture_items:
        self_ref: str = getattr(item, "self_ref", "")

        # Cache hit: reuse previously resolved URL (or None).
        if self_ref in image_cache:
            image_urls.append(image_cache[self_ref])
            alt_texts.append(
                image_cache.get(f"{self_ref}_alt", "") or ""
            )
            continue

        alt_text = _get_picture_alt_text(document, item)
        alt_texts.append(alt_text)
        image_cache[f"{self_ref}_alt"] = alt_text

        url: str | None = None
        if upload_context is not None and upload_context.uploader is not None:
            image = get_picture_image(document, item)
            if image is not None:
                try:
                    image_key = upload_context.uploader.upload_png(
                        block_id, image
                    )
                    url = upload_context.uploader.build_image_url(image_key)
                except Exception:
                    add_warning(
                        warnings,
                        code="image_upload_failed",
                        message="Failed to upload image to OSS.",
                        block_id=block_id,
                    )

        image_urls.append(url)
        image_cache[self_ref] = url

        if images is not None:
            item_page_no = resolve_page_no(document, item)
            images.append({
                "url": url,
                "pageNo": item_page_no,
                "blockId": block_id,
                "alt": alt_text,
            })

    # Replace placeholders one at a time, in order.
    result = markdown_text
    for url, alt in zip(image_urls, alt_texts):
        if url is not None:
            result = result.replace(
                placeholder, f"![{alt}]({url})", 1
            )

    return result


class OssImageUploader:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket_name: str,
        base_path: str,
        credentials: OssCredentials,
        request_id: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.bucket_name = bucket_name
        self.base_path = base_path.strip("/")
        self.credentials = credentials
        self.request_id = request_id

    def upload_png(self, block_id: str, image: Any) -> str:
        try:
            import oss2
        except ImportError as exc:
            raise ImageUploadError("oss2 is not installed") from exc

        image_key = self.build_image_key(block_id)
        content = encode_png(image)
        auth = oss2.StsAuth(
            self.credentials.access_key_id,
            self.credentials.access_key_secret,
            self.credentials.security_token,
        )
        bucket = oss2.Bucket(auth, self.endpoint, self.bucket_name)
        bucket.put_object(image_key, content, headers={"Content-Type": "image/png"})
        return image_key

    def build_image_key(self, block_id: str) -> str:
        suffix = self.request_id or str(int(time.time() * 1000))
        safe_suffix = suffix.replace("/", "_").replace(":", "_")
        stem = block_id.replace("/", "_")
        filename = f"{stem}_{safe_suffix}.png"
        if not self.base_path:
            return filename
        return f"{self.base_path}/{filename}"

    def build_image_url(self, image_key: str) -> str:
        """Build the full OSS URL for an image key.

        OSS URL format (virtual-hosted style): https://<bucket>.<endpoint>/<key>
        """
        return f"https://{self.bucket_name}.{self.endpoint}/{image_key}"



def encode_png(image: Any) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def decrypt_oss_credentials(encrypted: EncryptedCredentials) -> OssCredentials:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
    except ImportError as exc:
        raise ImageUploadError("cryptography is not installed") from exc

    key = load_oss_encrypt_key()
    iv = decode_base64_field(encrypted.iv, "iv")
    ciphertext = decode_base64_field(encrypted.ciphertext, "ciphertext")
    if len(iv) != 16:
        raise ImageUploadError("encrypted credentials iv must be 16 bytes")

    decryptor = Cipher(
        algorithms.AES256(key), modes.CBC(iv)
    ).decryptor()

    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()

    payload = json.loads(plaintext.decode("utf-8"))
    return OssCredentials(
        access_key_id=require_string(payload, "accessKeyId"),
        access_key_secret=require_string(payload, "accessKeySecret"),
        security_token=require_string(payload, "securityToken"),
        expiration=payload.get("expiration"),
    )


def load_oss_encrypt_key() -> bytes:
    configured = settings.oss_encrypt_key.strip()
    if not configured:
        raise ImageUploadError("OSS encryption key is not configured")

    try:
        decoded = base64.b64decode(configured, validate=True)
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass

    raw = configured.encode("utf-8")
    if len(raw) != 32:
        raise ImageUploadError(
            "OSS encryption key must be 32 bytes or base64-encoded 32 bytes"
        )
    return raw


def decode_base64_field(value: str, field_name: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ImageUploadError(
            f"encrypted credentials {field_name} is not valid base64"
        ) from exc


def require_string(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ImageUploadError(f"encrypted credentials missing {field_name}")
    return value


def _validate_image_size(source_path: Path) -> None:
    """Raise ``SourceDownloadError`` if the image exceeds the configured pixel limit.

    Also lifts PIL's decompression-bomb limit so Docling's internal pipeline
    stages (e.g. OCR) don't reject large page images.
    """
    try:
        from PIL import Image
    except ImportError:
        return

    Image.MAX_IMAGE_PIXELS = None

    image_suffixes = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp"}
    if source_path.suffix.lower() not in image_suffixes:
        return

    limit = settings.max_image_megapixels * 1_000_000
    with Image.open(source_path) as img:
        w, h = img.size
        pixels = w * h
        if pixels > limit:
            raise SourceDownloadError(
                f"Image size ({w}x{h} = {pixels:,} pixels) exceeds "
                f"limit of {settings.max_image_megapixels} megapixels "
                f"({limit:,} pixels)"
            )

