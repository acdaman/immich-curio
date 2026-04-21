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

    queue_target_size: int = 10
    dry_run: bool = False

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
