from prompt_manager import PromptManager
from typing import Any, Optional
from functools import lru_cache
from langchain_ollama import OllamaLLM
import json
from utils import _get_json_from_response, execute_llm_chain
from joblib import Memory
from config import settings

memory = Memory("cache", verbose=0)

class OllamaServiceTg:
    def __init__(self, prompts_path):
        self.prompt_manager = PromptManager(prompts_path)
        self.prompt_manager.load_all()

        self.llm = OllamaLLM(
            model=settings.OLLAMA_MODEL_TG_NAME,
            base_url=settings.OLLAMA_BASE_URL,
            temperature=settings.TEMPERATURE,
            top_k=settings.TOP_K,
            top_p=settings.TOP_P,
            seed=settings.SEED,
            num_ctx=settings.OLLAMA_NUM_CTX,
            num_predict=settings.OLLAMA_NUM_PREDICT,
            keep_alive=settings.OLLAMA_KEEP_ALIVE,
        )

    def get_tg_model_answer(self,
                prompt_name: str,
                payload: dict[str, str],
                generation_kwargs: Optional[dict[str, Any]] = None):
        generation_kwargs = generation_kwargs or {}

        parameters_for_cache = {
            "model": self.llm.model,
            "temperature": self.llm.temperature,
            "top_k": self.llm.top_k,
            "top_p": self.llm.top_p,
            "seed": self.llm.seed,
            "num_ctx": self.llm.num_ctx,
            "num_predict": self.llm.num_predict,

            "prompt_text": self.prompt_manager.get_prompt(prompt_name)
        }
        return self._get_tg_model_answer_cached(self.llm, self.prompt_manager, prompt_name, payload, parameters_for_cache, generation_kwargs)


    @staticmethod
    @memory.cache(ignore=["llm", "prompt_manager"])
    def _get_tg_model_answer_cached(
            llm,
            prompt_manager,
            prompt_name: str,
            payload: dict[str, str],
            parameters_for_cache: dict[str, Any],
            generation_kwargs: Optional[dict[str, Any]] = None
            ):
        """generation_kwargs заменяет все аргументы на переданные"""
        # Если есть аргументы то заменяем не трогая оригинальную модель
        if generation_kwargs:
            llm = llm.bind(options=generation_kwargs)

        chain = prompt_manager.get_template(prompt_name) | llm
        response = execute_llm_chain(chain, payload, stub={})
        return response
