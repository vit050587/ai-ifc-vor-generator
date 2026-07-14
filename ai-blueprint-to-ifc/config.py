from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
import logging
from typing import Literal


class BlueprintSettings(BaseModel):
    zoom: int = Field(default=6, gt=0)
    tile_size: int = Field(default=720, gt=0)
    tile_overlap: float = Field(default=37, ge=0, lt=100)


class WallDetectionSettings(BaseModel):
    confidence: float = Field(default=0.4, ge=0, le=1)
    iou: float = Field(default=0.9, ge=0, le=1)
    image_size: int = Field(default=736, gt=0)


class WallMergeSettings(BaseModel):
    overlap_threshold: float = Field(default=0.5, ge=0, le=1)
    angle_threshold_degrees: float = Field(default=5, ge=0, le=90)
    edge_similarity_threshold: float = Field(default=0, ge=0, le=1)


class WallTrimSettings(BaseModel):
    overlap_threshold: float = Field(default=0.01, ge=0, le=1)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        env_nested_delimiter="__",
        extra="ignore",
    )

    APP_NAME: str = ""
    APP_VERSION: str = "1.0.0"

    OLLAMA_MODEL_NAME: str = "hf.co/unsloth/Qwen3-VL-8B-Instruct-GGUF:Q5_K_M"
    OLLAMA_BASE_URL: str = "http://ollama:11434"

    TEMPERATURE: float = 0.0
    TOP_K: int = 1
    TOP_P: float = 1.0
    SEED: int = 12
    MIROSTAT: float = 0.0
    MIROSTAT_TAU: float = 30.0
    FREQUENCY_PENALTY: float = 0.0
    PRESENCE_PENALTY: float = 0.0

    # Пути
    LOG_DIR: Path = Path("/app/logs")
    RECORDS_DIR: Path = Path("/app/logs/characteristics_records")
    LLM_REQUESTS_RECORDS_DIR: Path = Path("/app/logs/llm_requests_records")
    RECORDS_MAX_RECORDS: int = 500
    LLM_REQUESTS_RECORDS_MAX_RECORDS: int = 3000
    LOG_RETENTION_DAYS: int = 30

    MAX_RETRIES: int = 3
    RETRY_DELAY: float = 1.0
    LLM_HARD_TIMEOUT: int = 300

    PROMPTS_DIR: str = "/prompts"
    PAGES_DIR: Path = Path("pages")
    LEGENDS_DIR: Path = Path("legends")

    DEBUG_DIR: Path = Path("debug")
    DEBUG_IMAGES_DIR: Path = Path("blueprint_walls")
    DEBUG_WALLS_HIGHLIGHTED_DIR: Path = Path("walls_highlited")
    DEBUG_LAYOUTS_DIR: Path = DEBUG_DIR / Path("layouts")
    DEBUG_LEGEND_LAYOUTS_DIR: Path = DEBUG_DIR / Path("legend_layouts")
    DEBUG_LEGEND_LAYOUTS_FILTERED_DIR: Path = DEBUG_DIR / Path("legend_layouts") / Path("filtered")

    TRAIN_TG_FILE: str = "/app/logs/train_tg.jsonl"
    TRAIN_QA_FILE: str = "/app/logs/train_qa.jsonl"

    MODELS_DIR: Path = Path("models")
    TG_MODEL_DIR: str = "GreenMap/qwen3-vl-4b-ru-blueprint-extractor"
    YOLO_WALLS_MODEL: Path = MODELS_DIR / "yolo_walls_obb.pt"
    YOLO_LAYOUT_MODEL: Path = MODELS_DIR / "yolo_layout.pt"
    YOLO_LEGEND_LAYOUT_MODEL: Path = MODELS_DIR / "yolo_legend_layout.pt"
    DINO_HATCHING_MODEL: Path = MODELS_DIR / "dino_hatching.pt"

    NEW_LEGEND_CREATION_SCORE_THRESHOLD: float = 0.0
    HATCHING_SCORE_THRESHOLD: float = 0.7
    MAX_WALL_AREA_FOR_DELETE: float = 6
    LEGEND_LAYOUT_MIN_INSIDE_RATIO: float = 0.7

    LAYOUT_ZOOM: int = 2
    LEGEND_ZOOM: int = 3
    HATCHING_ZOOM: int = 6

    LOGGING_LEVEL: str = "DEBUG"

    MAX_IMAGE_PIXELS:int=1500*1500
    DINO_IMAGE_SIZE: int = 518
    DINO_MAX_BATCH_SIZE: int = 16
    TRANSFORMERS_LOCALS_FILES_ONLY:bool = False
    TRANSFORMERS_CUDA_NUM:int = 0

    BLUEPRINT: BlueprintSettings = Field(default_factory=BlueprintSettings)
    WALL_DETECTION: WallDetectionSettings = Field(default_factory=WallDetectionSettings)
    WALL_MERGE: WallMergeSettings = Field(default_factory=WallMergeSettings)
    WALL_TRIM: WallTrimSettings = Field(default_factory=WallTrimSettings)


settings = Settings()


# Создаем необходимые директории
settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
settings.RECORDS_DIR.mkdir(parents=True, exist_ok=True)
settings.LLM_REQUESTS_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
