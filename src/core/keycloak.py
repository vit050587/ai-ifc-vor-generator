"""
Провайдер access-токенов Keycloak для API справочника работ.

Синхронная адаптация паттерна KeycloakTokenProvider (из других
сервисов проекта) под стек ai-ifc-vor-generator (Flask + requests).

Токен запрашивается по grant_type=client_credentials и кешируется.
Если access-токен истёк или скоро истечёт, при следующем запросе он
обновляется автоматически.
"""

import base64
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from src.core.logger import setup_logger

logger = setup_logger(__name__)

# Обновляем токен заранее, чтобы не упереться в expires_at прямо во время запроса.
TOKEN_EXPIRATION_MARGIN_SECONDS = 30


@dataclass(frozen=True)
class AccessToken:
    value: str
    expires_at: float


class KeycloakTokenProvider:
    """Синхронный провайдер Bearer-токена Keycloak с кешированием.

    Потокобезопасен: получение нового токена защищено блокировкой,
    поэтому при конкурентных вызовах из потоков Flask/gunicorn
    запрос к Keycloak выполнится ровно один раз.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._token: AccessToken | None = None
        self._lock = threading.Lock()

    def get_token(self) -> str:
        """Возвращает актуальный access-токен.

        Если кешированный токен отсутствует или скоро истечёт,
        получает новый токен из Keycloak.
        """
        if self._is_token_valid():
            return self._get_cached_token()

        with self._lock:
            # Пока ожидали блокировку, другой поток мог уже
            # получить новый токен.
            if self._is_token_valid():
                return self._get_cached_token()
            self._token = self._request_token()
            self._log_token_info(self._token.value)
        return self._token.value

    def invalidate(self) -> None:
        """Принудительно удаляет токен из кеша.

        Используется, если backend неожиданно вернул 401 для ещё
        не истёкшего токена.
        """
        self._token = None

    def close(self) -> None:
        """Освобождает ресурсы (здесь ничего не требуется, метод
        оставлен для симметрии с async-версией)."""
        self._token = None

    def _is_token_valid(self) -> bool:
        if self._token is None:
            return False
        return self._token.expires_at > time.time() + TOKEN_EXPIRATION_MARGIN_SECONDS

    def _get_cached_token(self) -> str:
        if self._token is None:
            raise RuntimeError("Access token is not initialized")
        return self._token.value

    def _request_token(self) -> AccessToken:
        response = requests.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=self._timeout,
        )
        response.raise_for_status()

        payload: dict[str, Any] = response.json()

        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")

        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError(
                "Keycloak response does not contain a valid access_token"
            )
        if not isinstance(expires_in, int) or expires_in <= 0:
            raise RuntimeError(
                "Keycloak response does not contain a valid expires_in"
            )

        return AccessToken(
            value=access_token,
            expires_at=time.time() + expires_in,
        )

    @staticmethod
    def _log_token_info(token: str) -> None:
        """Декодирует payload JWT и логирует iat/exp/azp.

        Позволяет по логам однозначно определить, какой именно токен
        был использован (автоматически полученный из Keycloak или
        статичный из WORKS_API_TOKEN).
        """
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            iat = payload.get("iat")
            exp = payload.get("exp")
            azp = payload.get("azp", "?")
            sub = payload.get("sub", "?")
            iat_str = time.strftime("%H:%M:%S", time.localtime(iat)) if iat else "?"
            exp_str = time.strftime("%H:%M:%S", time.localtime(exp)) if exp else "?"
            logger.info(
                f"Используется токен Keycloak: client={azp}, "
                f"sub={sub[:8]}..., iat={iat_str}, exp={exp_str}"
            )
        except Exception:
            # Не должно ломать поток работы — просто логируем хвост токена.
            logger.debug(f"Не удалось декодировать токен для лога: {token[:20]}...")