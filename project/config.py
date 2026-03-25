from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://combflow:change_me@db/combflow"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # CORS
    cors_origins: list[str] = []  # e.g. ["https://combflow.app"]

    # HAFSQL (public Hive PostgreSQL)
    hafsql_host: str = "hafsql-sql.mahdiyari.info"
    hafsql_port: int = 5432
    hafsql_db: str = "haf_block_log"
    hafsql_user: str = "hafsql_public"
    hafsql_password: str = "hafsql_public"
    hafsql_connect_timeout: int = 10

    # Worker
    api_url: str = "http://combflow-app:8000"

    # Public URLs
    caddy_ui: str = ""  # e.g. "hivecomb.com" — UI domain
    caddy_api: str = ""  # e.g. "api.example.com" — API domain

    @staticmethod
    def _host_to_url(host: str) -> str:
        """Derive full URL from a hostname. Bare hostname = HTTPS."""
        if not host:
            return ""
        if host.endswith(":80") or host.endswith(":443"):
            bare, port = host.rsplit(":", 1)
            return f"http://{bare}" if port == "80" else f"https://{bare}"
        return f"https://{host}"

    @property
    def site_url(self) -> str:
        return self._host_to_url(self.caddy_ui)

    @property
    def api_base_url(self) -> str:
        return self._host_to_url(self.caddy_api)

    # Logging
    sql_echo: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",  # ignore POSTGRES_USER etc. injected by docker-compose
    )


settings = Settings()
