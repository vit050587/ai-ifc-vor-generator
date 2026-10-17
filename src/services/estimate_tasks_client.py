"""Клиент внешнего сервиса обработки смет (/smeta/v2/tasks)."""

import os
from typing import Optional
from urllib.parse import quote
import requests

from src.core.logger import setup_logger

logger = setup_logger(__name__)


class EstimateTasksClient:
    """Клиент для /smeta/v2/tasks/{validate,upload,{id}/status,{id}/result}."""

    # Максимум попыток разрешить конфликт task_id:
    # run-1 → run-1(1) → run-1(2) → ... → run-1(10)
    CONFLICT_RETRY_LIMIT = 10

    def __init__(self, base_url: Optional[str] = None, timeout: float = 300.0):
        # base_url — корень сервиса, например http://host:port/smeta
        # Если в env указан http://host:port без /smeta — добавим сами.
        raw = base_url or os.environ.get("ESTIMATE_TASKS_BASE_URL", "http://82.202.138.45")
        raw = raw.rstrip("/")
        if not raw.endswith("/smeta"):
            raw = f"{raw}/smeta"
        self.base_url = raw  # например http://host:port/smeta
        self.timeout = timeout

    # ---------- PUBLIC API ----------

    def validate(self, filename: str, content: bytes) -> dict:
        """POST /smeta/v2/tasks/validate — валидация сметы.
        Возвращает {parsedFile, scanResults: [...]}.
        """
        url = f"{self.base_url}/v2/tasks/validate"
        files = [("files", (filename, content, "application/octet-stream"))]
        logger.info(f"POST {url} (file={filename}, {len(content)} bytes)")
        resp = requests.post(url, files=files, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def upload(
        self,
        filename: str,
        content: bytes,
        task_id: Optional[str] = None,
    ) -> dict:
        """POST /smeta/v2/tasks/upload.

        При конфликте task_id автоматически повторяет запрос,
        добавляя суффикс: task_id → task_id(1) → task_id(2) → ...
        до CONFLICT_RETRY_LIMIT попыток.
        """
        url = f"{self.base_url}/v2/tasks/upload"
        files = [("files", (filename, content, "application/octet-stream"))]

        attempt = 0
        while True:
            params = {}
            effective_task_id = None
            if task_id:
                suffix = "" if attempt == 0 else f"({attempt})"
                effective_task_id = f"{task_id}{suffix}"
                params["taskId"] = effective_task_id

            logger.info(
                f"POST {url} (taskId={effective_task_id or '-'}, "
                f"file={filename}, {len(content)} bytes)"
            )
            resp = requests.post(url, params=params, files=files, timeout=self.timeout)

            if self._is_url_conflict(resp):
                attempt += 1
                if attempt > self.CONFLICT_RETRY_LIMIT:
                    logger.error(
                        f"Конфликт URL для taskId={task_id} не удалось разрешить "
                        f"после {self.CONFLICT_RETRY_LIMIT} попыток"
                    )
                    resp.raise_for_status()
                logger.warning(
                    f"Конфликт URL (taskId={effective_task_id or '-'}). "
                    f"Повтор с taskId={task_id}({attempt})"
                )
                continue

            resp.raise_for_status()
            return resp.json()

    def get_status(self, task_id: str) -> dict:
        """GET /smeta/v2/tasks/{quote(str(task_id), safe='')}/status — текущий статус задачи."""
        url = f"{self.base_url}/v2/tasks/{quote(str(task_id), safe='')}/status"
        resp = requests.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_result(self, task_id: str, as_file: bool = False) -> dict:
        """GET /smeta/v2/tasks/{quote(str(task_id), safe='')}/result — результат задачи."""
        url = f"{self.base_url}/v2/tasks/{quote(str(task_id), safe='')}/result"
        resp = requests.get(
            url,
            params={"as_file": str(as_file).lower()},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ---------- HELPERS ----------

    def _is_url_conflict(self, response: requests.Response) -> bool:
        """Определяет, является ли ответ конфликтом task_id/URL.

        Считаем конфликтом:
          * HTTP 409 (Conflict)
          * HTTP 400 с текстом про «conflict / already exists / уже существует»
        """
        if response.status_code == 409:
            return True
        if response.status_code != 400:
            return False
        try:
            body = (response.text or "").lower()
        except Exception:
            return False
        markers = (
            "конфликт",
            "conflict",
            "already exists",
            "already exist",
            "уже существует",
            "уже есть",
            "duplicate",
        )
        return any(m in body for m in markers)

    def wait_for_result(
        self,
        task_id: str,
        poll_interval: float = 2.0,
        max_attempts: int = 300,
        on_progress=None,
    ) -> dict:
        """Поллит /status до completed, затем возвращает /result."""
        import time
        for attempt in range(max_attempts):
            status_resp = self.get_status(task_id)
            if on_progress:
                on_progress(status_resp)
            status = str(status_resp.get("status") or "").lower()
            error = status_resp.get('errorMessage')
            if error:
                raise RuntimeError(f'Сервис разбора ВОР заказчика завершил задачу {task_id} с ошибкой: {error}')
            if status == "completed":
                try:
                    return self.get_result(task_id)
                except requests.HTTPError as exc:
                    if exc.response is not None and exc.response.status_code == 404:
                        raise RuntimeError(f'Сервис сообщил о завершении задачи {task_id}, но не создал результат разбора ВОР заказчика (HTTP 404).') from exc
                    raise
            if status in ("error", "failed"):
                raise RuntimeError(
                    status_resp.get("errorMessage")
                    or "Ошибка разбора ВОР заказчика во внешнем сервисе"
                )
            time.sleep(poll_interval)
        raise TimeoutError(
            f"Задача {task_id} не завершилась за {poll_interval * max_attempts} секунд"
        )