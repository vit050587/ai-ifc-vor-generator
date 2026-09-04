import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    ollama_url: str
    DOCUMENTS_PATH: str          # перечень работ для режима КР
    AR_DOCUMENTS_PATH: str       # перечень работ для режима АР
    MSSK_EXCEL_PATH: str
    model_ollama: str
    KOEFS_PATH: str
    PRICE_COST_PATH: str
    # API справочника работ ТСН (для режима КР)
    WORKS_API_URL: str
    # Эндпоинт стоимости работ ТСН (curAll — цена за единицу измерения)
    WORKS_RESOURCES_API_URL: str
    WORKS_API_TOKEN: str  # fallback-токен, если Keycloak-клиент не настроен
    # Keycloak для автоматического обновления токена (client_credentials)
    KEYCLOAK_TOKEN_URL: str
    KEYCLOAK_CLIENT_ID: str
    KEYCLOAK_CLIENT_SECRET: str


def load_config() -> Config:
    return Config(
        ollama_url=os.getenv("OLLAMA_BASE_URL", "http://ollama:11434"),
        DOCUMENTS_PATH=os.getenv("DOCUMENTS_PATH", "data/perechen_kr.xlsx"),
        AR_DOCUMENTS_PATH=os.getenv("AR_DOCUMENTS_PATH", "data/perechen_ar.xlsx"),
        MSSK_EXCEL_PATH=os.getenv("MSSK_EXCEL_PATH", "data/elements_mssk.xlsx"),
        KOEFS_PATH=os.getenv("KOEFS_PATH", "data/koefs.xlsx"),
        PRICE_COST_PATH=os.getenv("PRICE_COST_PATH", "data/price_cost.xlsx"),
        model_ollama=os.getenv("NORMS_LLM_MODEL", "yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest"),
        WORKS_API_URL=os.getenv(
            "WORKS_API_URL",
            "https://normativ.mgexp.org/digital-collection/api/v1/digital-collection/building-elements/positions",
        ),
        WORKS_RESOURCES_API_URL=os.getenv(
            "WORKS_RESOURCES_API_URL",
            "https://normativ.mgexp.org/digital-collection/api/v1/digital-collection/works/resources",
        ),
        WORKS_API_TOKEN=os.getenv("WORKS_API_TOKEN", ""),
        KEYCLOAK_TOKEN_URL=os.getenv(
            "KEYCLOAK_TOKEN_URL",
            "https://normativ-idm.mgexp.org/realms/normativ/protocol/openid-connect/token",
        ),
        KEYCLOAK_CLIENT_ID=os.getenv("KEYCLOAK_CLIENT_ID", ""),
        KEYCLOAK_CLIENT_SECRET=os.getenv("KEYCLOAK_CLIENT_SECRET", ""),
    )
