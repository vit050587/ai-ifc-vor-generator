import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    ollama_url: str
    DOCUMENTS_PATH: str
    model_ollama: str
    KOEFS_PATH: str
    PRICE_COST_PATH: str


def load_config() -> Config:
    return Config(
        ollama_url=os.getenv("OLLAMA_BASE_URL", "http://ollama:11434"),
        DOCUMENTS_PATH=os.getenv("DOCUMENTS_PATH", "data/perechen_kr.xlsx"),
        KOEFS_PATH=os.getenv("KOEFS_PATH", "data/koefs.xlsx"),
        PRICE_COST_PATH=os.getenv("PRICE_COST_PATH", "data/price_cost.xlsx"),
        model_ollama=os.getenv("NORMS_LLM_MODEL", "yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest"),
    )
