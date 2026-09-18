"""Настройки последовательной обработки смет."""

from pathlib import Path

from pydantic import Field
from .normalization_functions import unit_text_normalize, code_normalize
from collections.abc import Callable
from pydantic_settings import BaseSettings, SettingsConfigDict
import logging

logging.getLogger("httpx").setLevel(logging.WARNING)

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )
    OLLAMA_CONTEXT_LENGTH: int = 16384
    OLLAMA_PROMPTS_PATH: Path = Path("prompts")

    OLLAMA_MODEL: str = "qwen3:14b"
    OLLAMA_NUM_CTX: int = 4096
    OLLAMA_KEEP_ALIVE: str = "30m"
    OLLAMA_CONNECT_TIMEOUT: float = 10.0
    OLLAMA_POOL_TIMEOUT: float = 10.0
    OLLAMA_NUM_PREDICT: int = 512
    TG_NUM_PREDICT: int = 2048

    TEMPERATURE: float = 0.0
    TOP_K: int = 1
    TOP_P: float = 1.0
    SEED: int = 12

    DEBUG_DIR: Path = Path("debug") / "ifc_comparison"
    VALIDATION_RESULT_PATH: Path = DEBUG_DIR / Path("comparison_results/result.json")

    IFC_COMPARISON_GROUP_KEYS: list[dict[str, str | Callable]] = [{"name": "code", "norm_func": code_normalize}, {"name": "unit", "norm_func": unit_text_normalize}]
    STATUSES_TO_VALIDATE: list[str] = ["only_ifc", "quantity_mismatch"]

    LOGGING_LEVEL: int = logging.DEBUG
    DIFFERENCE_TOLERANCE: float = 0.001
    DIFFERENCE_TOLERANCE_PERCENTAGE: float = 0.05


settings = Settings()
