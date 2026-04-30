from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    immich_url: str = "http://immich:2283"
    immich_api_key: str
    immich_user_id: str

    db_host: str = "postgres_db"
    db_port: int = 5432
    db_name: str = "immich"
    db_user: str = "immich_reader"
    db_password: str

    gemini_api_key: str
    gemini_model: str = "gemini-2.5-flash"

    telegram_bot_token: str
    telegram_chat_id: str

    immich_public_url: str = ""  # external URL for links, e.g. https://photos.yourdomain.com
    print_album_id: str  # ID of your "Print" album in Immich (GET /api/albums to find it)
    scoring_prompt_path: str = "/app/prompt.md"
    group_scoring_prompt_path: str = "/app/prompt_group.md"

    queue_target_size: int = 10
    burst_window_seconds: int = 60
    burst_group_max_size: int = 15
    export_dir: str = "/exports"
    export_enabled: bool = False

    dry_run: bool = False
    log_level: str = "INFO"

    @property
    def immich_base_url(self) -> str:
        return self.immich_url.rstrip("/")

    @property
    def db_dsn(self) -> dict:
        return {
            "host": self.db_host,
            "port": self.db_port,
            "dbname": self.db_name,
            "user": self.db_user,
            "password": self.db_password,
        }


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
