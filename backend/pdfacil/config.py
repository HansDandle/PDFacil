from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./storage/dev.sqlite3"
    storage_backend: str = "local"
    storage_dir: Path = Path("storage/objects")
    google_fonts_enabled: bool = True

    max_pdf_bytes: int = 50 * 1024 * 1024
    max_pdf_pages: int = 200
    source_url: str = "https://github.com/HansDandle/PDFacil"


def get_settings() -> Settings:
    return Settings()
