"""Клиент локальной LLM (Ollama) для проверки ЦИМ модели.

Модель по умолчанию берётся из ``CHECK_MODEL_LLM_MODEL``, затем из
``LLM_MODEL`` (в рабочей копии это ``qwen3.8:27b``), затем из конфигурации
сервиса. Ответ ожидается в формате JSON.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

import requests

from src.core.config import load_config
from src.core.logger import setup_logger

logger = setup_logger(__name__)


class CheckModelLLMError(RuntimeError):
    """Ошибка обращения к LLM."""


class CheckModelLLM:
    """Небольшая обёртка над ``/api/chat`` Ollama."""

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        config = load_config()
        self.model = (
            model
            or os.getenv("CHECK_MODEL_LLM_MODEL")
            or os.getenv("LLM_MODEL")
            or config.model_ollama
        )
        self.base_url = (
            base_url
            or os.getenv("LLM_BASE_URL")
            or config.ollama_url
        ).rstrip("/")
        self.timeout = float(
            timeout or os.getenv("CHECK_MODEL_LLM_TIMEOUT", "900")
        )

    def chat_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        *,
        num_ctx: int = 32768,
        num_predict: int = 4096,
    ) -> Dict[str, Any]:
        """Отправить запрос и вернуть разобранный JSON-объект.

        Raises:
            CheckModelLLMError: сеть/сервер недоступны или ответ не JSON.
        """
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": "json",
            "messages": messages,
            "options": {
                "temperature": 0,
                "num_ctx": num_ctx,
                "num_predict": num_predict,
            },
        }
        try:
            response = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = (response.json().get("message") or {}).get("content") or ""
        except requests.RequestException as exc:
            raise CheckModelLLMError(f"LLM недоступна: {exc}") from exc
        except (ValueError, AttributeError) as exc:
            raise CheckModelLLMError(f"Некорректный ответ LLM: {exc}") from exc

        parsed = _extract_json(content)
        if parsed is None:
            raise CheckModelLLMError(
                f"LLM вернула не JSON: {content[:300]!r}"
            )
        return parsed


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Извлечь JSON-объект из ответа модели (с запасом на ```json-обёртки)."""
    if not text:
        return None
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped)
    try:
        data = json.loads(stripped)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass

    # Ищем первый сбалансированный JSON-объект.
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(stripped[start : index + 1])
                    return data if isinstance(data, dict) else None
                except json.JSONDecodeError:
                    return None
    return None
