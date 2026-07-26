from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

OutputFormat = Literal["markdown", "html", "text", "json", "blocks", "chunks"]


class ParseOptions(BaseModel):
    output_format: OutputFormat = Field(default="markdown", alias="outputFormat")
    ocr: bool = False
    ocr_fallback: bool = Field(default=False, alias="ocrFallback")
    table_structure: bool = True
    formula_enrichment: bool = Field(default=False, alias="formulaEnrichment")
    validate_text_quality: bool = Field(default=True, alias="validateTextQuality")
    chunk_min_tokens: int = Field(default=400, alias="chunkMinTokens")
    chunk_max_tokens: int = Field(default=800, alias="chunkMaxTokens")
    use_native_chunker: bool = Field(default=False, alias="useNativeChunker")
    include_embedded_images: bool = Field(default=False, alias="includeEmbeddedImages")

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("output_format", mode="before")
    @classmethod
    def normalize_output_format(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class EncryptedCredentials(BaseModel):
    version: Literal[1] | None = None
    key_id: str | None = Field(default=None, alias="keyId", min_length=1)
    nonce: str | None = None
    ciphertext: str
    tag: str | None = None
    expiration: str | None = None
    iv: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class OssUploadOptions(BaseModel):
    endpoint: str
    bucket: str
    base_path: str = Field(default="", alias="basePath")
    encrypted_credentials: EncryptedCredentials = Field(alias="encryptedCredentials")

    model_config = ConfigDict(populate_by_name=True)


class ParseRequest(BaseModel):
    request_id: str = Field(alias="requestId", min_length=1, max_length=200)
    contract_version: Literal[1, 2, 3] | None = Field(default=None, alias="contractVersion")
    source_revision: str | None = Field(
        default=None,
        alias="sourceRevision",
        min_length=1,
        max_length=80,
    )
    source_url: HttpUrl = Field(alias="sourceUrl")
    file_name: str | None = Field(default=None, alias="fileName")
    options: ParseOptions = Field(default_factory=ParseOptions)
    oss: OssUploadOptions | None = None

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def validate_versioned_contract(self) -> "ParseRequest":
        if self.contract_version not in {2, 3}:
            return self
        if not self.source_revision:
            raise ValueError("sourceRevision is required for versioned contracts")
        if self.file_name is None or not self.file_name.strip():
            raise ValueError("fileName is required for versioned contracts")
        self.file_name = self.file_name.strip()
        if self.contract_version == 3 and self.oss is not None:
            encrypted = self.oss.encrypted_credentials
            if (
                encrypted.version != 1
                or not encrypted.key_id
                or not encrypted.nonce
                or not encrypted.tag
                or not encrypted.expiration
            ):
                raise ValueError("contractVersion 3 requires AEAD encrypted credentials")
        return self


class ParsedPage(BaseModel):
    page_no: int | None = Field(default=None, alias="pageNo")
    text: str

    model_config = ConfigDict(populate_by_name=True)


class ParseWarning(BaseModel):
    code: str
    message: str
    block_id: str | None = Field(default=None, alias="blockId")

    model_config = ConfigDict(populate_by_name=True)


class EmbeddedImageCoordinates(BaseModel):
    l: float  # noqa: E741 - wire-contract field name
    t: float
    r: float
    b: float
    coord_origin: str = Field(
        alias="coordOrigin",
        validation_alias=AliasChoices("coordOrigin", "coord_origin"),
    )

    model_config = ConfigDict(populate_by_name=True)


class EmbeddedImageBbox(BaseModel):
    page_no: int = Field(alias="pageNo")
    bbox: EmbeddedImageCoordinates

    model_config = ConfigDict(populate_by_name=True)


class EmbeddedImageArtifact(BaseModel):
    artifact_version: Literal[1] = Field(alias="artifactVersion")
    block_id: str = Field(alias="blockId", min_length=1)
    image_object_key: str | None = Field(default=None, alias="imageObjectKey")
    upload_status: Literal["UPLOADED", "SKIPPED", "FAILED"] = Field(alias="uploadStatus")
    page_no: int | None = Field(default=None, alias="pageNo")
    bboxes: list[EmbeddedImageBbox] | None = None
    image_width: int | None = Field(default=None, alias="imageWidth")
    image_height: int | None = Field(default=None, alias="imageHeight")
    mime_type: str | None = Field(default=None, alias="mimeType")
    content_hash: str | None = Field(default=None, alias="contentHash")
    alt: str | None = None
    caption: str | None = None
    context_text: str | None = Field(default=None, alias="contextText")
    ocr_text: str | None = Field(default=None, alias="ocrText")
    url: str | None = None

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def validate_uploaded_identity(self) -> "EmbeddedImageArtifact":
        if self.upload_status == "UPLOADED" and not self.image_object_key:
            raise ValueError("uploaded embedded image requires imageObjectKey")
        return self


class ParseResponse(BaseModel):
    request_id: str = Field(alias="requestId")
    parser: str
    format: str
    file_type: str = Field(alias="fileType")
    text: str
    pages: list[ParsedPage] = Field(default_factory=list)
    document: dict[str, Any] | None = None
    blocks: list[dict[str, Any]] | None = None
    chunks: list[dict[str, Any]] | None = None
    images: list[EmbeddedImageArtifact] | None = None
    warnings: list[ParseWarning] | None = None

    model_config = ConfigDict(populate_by_name=True)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobError(BaseModel):
    code: str
    message: str


class JobResponse(BaseModel):
    job_id: str = Field(alias="jobId")
    request_id: str = Field(alias="requestId")
    status: JobStatus
    created_at: datetime = Field(alias="createdAt")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    finished_at: datetime | None = Field(default=None, alias="finishedAt")
    result: ParseResponse | None = None
    error: JobError | None = None

    model_config = ConfigDict(populate_by_name=True)
