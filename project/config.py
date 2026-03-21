from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://combflow:change_me@db/combflow"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # Security
    api_key: str = ""
    jwt_secret: str = ""  # HMAC key for JWT signing; falls back to api_key if unset

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
    api_base_url: str = ""  # e.g. "https://api.example.com" — used in footer link

    # Logging
    sql_echo: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",  # ignore POSTGRES_USER etc. injected by docker-compose
    )


settings = Settings()
