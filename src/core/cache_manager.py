"""Shared disk cache for model responses and generated reports."""
from joblib import Memory

from src.core.config import load_config


class CacheManager:
    memory = Memory(load_config().CACHE_PATH, verbose=0)
    CACHE_LIMIT = "200M"

    @staticmethod
    def trim_cache():
        CacheManager.memory.reduce_size(bytes_limit=CacheManager.CACHE_LIMIT)

    @staticmethod
    def clear_cache():
        CacheManager.memory.clear(warn=False)
