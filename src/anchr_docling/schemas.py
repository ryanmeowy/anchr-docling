from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


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

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("output_format", mode="before")
    @classmethod
    def normalize_output_format(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class EncryptedCredentials(BaseModel):
    iv: str
    ciphertext: str
    tag: str | None = None


class OssUploadOptions(BaseModel):
    endpoint: str
    bucket: str
    base_path: str = Field(default="", alias="basePath")
    encrypted_credentials: EncryptedCredentials = Field(alias="encryptedCredentials")

    model_config = ConfigDict(populate_by_name=True)


class ParseRequest(BaseModel):
    request_id: str | None = Field(default=None, alias="requestId")
    source_url: HttpUrl = Field(alias="sourceUrl")
    file_name: str | None = Field(default=None, alias="fileName")
    options: ParseOptions = Field(default_factory=ParseOptions)
    oss: OssUploadOptions | None = None

    model_config = ConfigDict(populate_by_name=True)


class ParsedPage(BaseModel):
    page_no: int | None = Field(default=None, alias="pageNo")
    text: str

    model_config = ConfigDict(populate_by_name=True)


class ParseWarning(BaseModel):
    code: str
    message: str
    block_id: str | None = Field(default=None, alias="blockId")

    model_config = ConfigDict(populate_by_name=True)


class ParseResponse(BaseModel):
    request_id: str | None = Field(default=None, alias="requestId")
    parser: str
    format: str
    file_type: str = Field(alias="fileType")
    text: str
    pages: list[ParsedPage] = Field(default_factory=list)
    document: dict[str, Any] | None = None
    blocks: list[dict[str, Any]] | None = None
    chunks: list[dict[str, Any]] | None = None
    images: list[dict[str, Any]] | None = None
    warnings: list[ParseWarning] | None = None

    model_config = ConfigDict(populate_by_name=True)
