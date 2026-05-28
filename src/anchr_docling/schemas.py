from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


OutputFormat = Literal["markdown", "html", "text", "json"]
FormattedContent = str | dict[str, Any]


class ParseOptions(BaseModel):
    output_format: OutputFormat = Field(default="markdown", alias="outputFormat")
    ocr: bool = False
    ocr_fallback: bool = Field(default=False, alias="ocrFallback")
    table_structure: bool = True
    validate_text_quality: bool = Field(default=True, alias="validateTextQuality")

    @field_validator("output_format", mode="before")
    @classmethod
    def normalize_output_format(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class ParseRequest(BaseModel):
    request_id: str | None = Field(default=None, alias="requestId")
    source_url: HttpUrl = Field(alias="sourceUrl")
    file_name: str | None = Field(default=None, alias="fileName")
    mime_type: str | None = Field(default=None, alias="mimeType")
    options: ParseOptions = Field(default_factory=ParseOptions)


class ParsedPage(BaseModel):
    page_no: int | None = Field(default=None, alias="pageNo")
    text: FormattedContent


class ParseResponse(BaseModel):
    request_id: str | None = Field(default=None, alias="requestId")
    parser: str
    format: str
    text: FormattedContent
    pages: list[ParsedPage] = Field(default_factory=list)
