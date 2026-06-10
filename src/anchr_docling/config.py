from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    host: str = Field(default="127.0.0.1", alias="ANCHR_DOCLING_HOST")
    port: int = Field(default=8091, alias="ANCHR_DOCLING_PORT")
    max_download_mb: int = Field(default=100, alias="ANCHR_DOCLING_MAX_DOWNLOAD_MB")
    connect_timeout_seconds: float = Field(
        default=10.0,
        alias="ANCHR_DOCLING_CONNECT_TIMEOUT_SECONDS",
    )
    read_timeout_seconds: float = Field(
        default=120.0,
        alias="ANCHR_DOCLING_READ_TIMEOUT_SECONDS",
    )
    device: str = Field(default="cpu", alias="ANCHR_DOCLING_DEVICE")
    ocr_engines: str = Field(
        default="ocrmac,rapidocr",
        alias="ANCHR_DOCLING_OCR_ENGINES",
    )
    ocr_lang: str = Field(default="chinese", alias="ANCHR_DOCLING_OCR_LANG")
    force_full_page_ocr: bool = Field(
        default=True,
        alias="ANCHR_DOCLING_FORCE_FULL_PAGE_OCR",
    )
    preload_models: bool = Field(default=True, alias="ANCHR_DOCLING_PRELOAD_MODELS")
    preload_ocr_models: bool = Field(default=False, alias="ANCHR_DOCLING_PRELOAD_OCR_MODELS")
    oss_encrypt_key: str = Field(default="", alias="ANCHR_DOCLING_OSS_ENCRYPT_KEY")
    max_image_megapixels: int = Field(
        default=80,
        alias="ANCHR_DOCLING_MAX_IMAGE_MEGAPIXELS",
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
