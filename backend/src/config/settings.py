"""集中配置：全部来自环境变量，默认值与 .env.example 保持一致。"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "freshsla"
    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "freshsla"
    db_user: str = "freshsla"
    db_password: str = "freshsla_pw_2026"

    jwt_secret: str = "freshsla-jwt-secret-change-me-2026"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 480

    redis_url: str = "redis://localhost:6379/0"
    seed_demo: bool = True
    rate_limit_per_minute: int = 600
    rate_limit_write_per_minute: int = 120
    app_timezone_offset_minutes: int = 480
    calibration_days: int = 30

    # ---- 巡检批次：断点续跑 / 发布闸门 ----
    # 单表采集超时秒数；running 批次超过 RUN_STALE_SECONDS 无心跳视为 worker 崩溃可接管
    probe_table_timeout_seconds: int = 30
    run_stale_seconds: int = 300
    # 排障演练：命中的表 code 采集失败（逗号分隔），默认空串=不启用
    probe_fail_tables: str = ""

    @property
    def database_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
