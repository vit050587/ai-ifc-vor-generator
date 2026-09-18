from src.core.prompt_manager import PromptManager
from typing import Any, Optional
from functools import lru_cache
from langchain_ollama import OllamaLLM
import json
from .utils import _get_json_from_response, execute_llm_chain
from joblib import Memory

from .config import settings
from src.core.config import load_config

_cfg = load_config()

OLLAMA_URL = _cfg.ollama_url
memory = Memory("cache", verbose=0)

class OllamaService:
    def __init__(self, prompts_path):
        self.prompt_manager = PromptManager(prompts_path)
        self.prompt_manager.load_all()

        self.llm = OllamaLLM(
            model=settings.OLLAMA_MODEL,
            base_url=OLLAMA_URL,
            temperature=settings.TEMPERATURE,
            top_k=settings.TOP_K,
            top_p=settings.TOP_P,
            seed=settings.SEED,
            num_ctx=settings.OLLAMA_NUM_CTX,
            num_predict=settings.TG_NUM_PREDICT,
            keep_alive=settings.OLLAMA_KEEP_ALIVE
        )


    def get_tg_model_answer(
            self,
            prompt_name: str,
            payload: dict[str, str],
            generation_kwargs: Optional[dict[str, Any]] = None
            ):
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        generation_kwargs_json = json.dumps(
            generation_kwargs,
            ensure_ascii=False,
            sort_keys=True,
        )
        return self._get_tg_model_answer_cached(
            prompt_name,
            payload_json,
            generation_kwargs_json,
        )

    @lru_cache(maxsize=1024)
    def _get_tg_model_answer_cached(
            self,
            prompt_name: str,
            payload_json: str,
            generation_kwargs_json: str,
            ):
        """generation_kwargs заменяет все аргументы на переданные"""
        payload = json.loads(payload_json)
        generation_kwargs = json.loads(generation_kwargs_json)
        llm = self.llm
        # Если есть аргументы то заменяем не трогая оригинальную модель
        if generation_kwargs:
            llm = llm.bind(options=generation_kwargs)

        chain = self.prompt_manager.get_template(prompt_name) | llm
        response = execute_llm_chain(chain, payload, stub={})
        if prompt_name == "":
            with open("train.jsonl", "a", encoding="utf-8") as f:
                json.dump({"input": payload, "output": response}, f, ensure_ascii=False)
                f.write("\n")
        return response, "eos"
