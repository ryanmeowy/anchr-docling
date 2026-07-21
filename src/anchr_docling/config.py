from pydantic import Field, SecretStr
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
    api_token: SecretStr = Field(default=SecretStr(""), alias="ANCHR_DOCLING_API_TOKEN")
    workers: int = Field(default=1, ge=1, alias="ANCHR_DOCLING_WORKERS")
    queue_capacity: int = Field(default=8, ge=1, alias="ANCHR_DOCLING_QUEUE_CAPACITY")
    queue_wait_timeout_seconds: float = Field(
        default=1800,
        gt=0,
        alias="ANCHR_DOCLING_QUEUE_WAIT_TIMEOUT_SECONDS",
    )
    result_ttl_seconds: float = Field(
        default=600,
        gt=0,
        alias="ANCHR_DOCLING_RESULT_TTL_SECONDS",
    )
    max_retained_results: int = Field(
        default=16,
        ge=1,
        alias="ANCHR_DOCLING_MAX_RETAINED_RESULTS",
    )
    allowed_download_hosts: str = Field(
        default="",
        alias="ANCHR_DOCLING_ALLOWED_DOWNLOAD_HOSTS",
    )
    max_download_redirects: int = Field(
        default=3,
        ge=0,
        le=10,
        alias="ANCHR_DOCLING_MAX_DOWNLOAD_REDIRECTS",
    )
    enable_docs: bool = Field(default=False, alias="ANCHR_DOCLING_ENABLE_DOCS")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def api_token_value(self) -> str:
        return self.api_token.get_secret_value()

    def download_hosts(self) -> frozenset[str]:
        return frozenset(
            host.strip().rstrip(".").lower()
            for host in self.allowed_download_hosts.split(",")
            if host.strip()
        )

    def validate_runtime(self) -> None:
        if len(self.api_token_value()) < 32:
            raise RuntimeError("ANCHR_DOCLING_API_TOKEN must contain at least 32 characters")
        if not self.download_hosts():
            raise RuntimeError("ANCHR_DOCLING_ALLOWED_DOWNLOAD_HOSTS must not be empty")


settings = Settings()
