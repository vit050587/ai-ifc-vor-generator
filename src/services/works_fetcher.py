"""
Получение работ из цифрового сборника (larix) по подобранным таблицам (режим АР).

Выполняется после works_table_selector (Подобранные_таблицы_работ.json):

  1. GET catalog/period/filter — получение списка периодов; из него выбирается
     самый актуальный период ТСН (baseTypeCode == "TSN", в title есть «индекс»,
     максимальный dateStart). Ключевое слово «индекс» отличает сам период
     («221 индекс/дополнение 82 …») от технических дополнений
     («Технические правки к Дополнению 82 …»).
     Период сохраняется в корне сессии в файле period.json.
  2. GET catalog/work-process/list — по каждому шифру таблицы (поле "code"
     работ из Подобранные_таблицы_работ.json, например "3.6-71") запрашивается
     список работ цифрового сборника за найденный период (параметр per=<title>).
     Все полученные работы сохраняются в папке запуска run_<NNN>/ в файле
     Подобранные_работы.json.

Авторизация — Bearer-токен Keycloak (client_credentials, тот же провайдер,
что и для API ТСН в режиме КР); fallback — статичный WORKS_API_TOKEN.
"""

import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from src.core.config import load_config
from src.core.keycloak import KeycloakTokenProvider
from src.core.logger import setup_logger

logger = setup_logger(__name__)

_cfg = load_config()

# Эндпоинты цифрового сборника (larix)
PERIOD_FILTER_URL = _cfg.WORKS_PERIOD_FILTER_URL
WORK_PROCESS_URL = _cfg.WORKS_WORK_PROCESS_URL
WORK_PROCESS_DETAIL_URL = _cfg.WORKS_WORK_PROCESS_DETAIL_URL
API_TOKEN = _cfg.WORKS_API_TOKEN

# Имена выходных файлов
# period.json — в корне сессии (общий для всех запусков)
PERIOD_JSON_FILENAME = "period.json"
# Подобранные_работы.json — в папке запуска run_<NNN>/
WORKS_JSON_FILENAME = "Подобранные_работы.json"

# Размер страницы запроса списка работ (как в спецификации API)
_PAGE_SIZE = 10
# Ограничение на количество страниц одного запроса (защита от зацикливания)
_MAX_PAGES = 100
_REQUEST_TIMEOUT = 60

# Провайдер Bearer-токена Keycloak (как в api_works_lookup, режим КР).
if _cfg.KEYCLOAK_CLIENT_ID and _cfg.KEYCLOAK_CLIENT_SECRET:
    _token_provider = KeycloakTokenProvider(
        token_url=_cfg.KEYCLOAK_TOKEN_URL,
        client_id=_cfg.KEYCLOAK_CLIENT_ID,
        client_secret=_cfg.KEYCLOAK_CLIENT_SECRET,
    )
else:
    _token_provider = None
    if not API_TOKEN:
        logger.warning(
            "WORKS_API_TOKEN не задан и Keycloak-клиент не сконфигурирован — "
            "запросы к цифровому сборнику (larix) будут недоступны"
        )


def _get_token() -> str:
    """Возвращает актуальный Bearer-токен (Keycloak либо статичный fallback)."""
    if _token_provider is not None:
        return _token_provider.get_token()
    if not API_TOKEN:
        raise RuntimeError(
            "Не задан Bearer-токен для цифрового сборника "
            "(переменная окружения WORKS_API_TOKEN)"
        )
    try:
        payload_b64 = API_TOKEN.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) if exp else "?"
    except Exception:
        exp_str = "?"
    logger.info(f"Используется статичный WORKS_API_TOKEN (exp={exp_str})")
    return API_TOKEN


def _api_get(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """GET-запрос к цифровому сборнику с Bearer-токеном.

    При 401 (токен отозван раньше expires_at) токен обновляется и запрос
    повторяется ровно один раз.
    """
    headers = {
        "Authorization": f"Bearer {_get_token()}",
        "Accept": "application/json",
    }

    def _get() -> requests.Response:
        try:
            return requests.get(
                url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Ошибка вызова цифрового сборника: {exc}") from exc

    response = _get()

    if response.status_code == 401 and _token_provider is not None:
        _token_provider.invalidate()
        headers["Authorization"] = f"Bearer {_get_token()}"
        response = _get()

    if response.status_code == 401:
        raise RuntimeError(
            "Цифровой сборник вернул 401 — Bearer-токен недействителен"
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"Цифровой сборник вернул {response.status_code}: "
            f"{response.text[:300]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Цифровой сборник вернул не-JSON ответ: {response.text[:300]}"
        ) from exc


# =====================================================================
#  ШАГ 1. АКТУАЛЬНЫЙ ПЕРИОД ТСН
# =====================================================================

def fetch_actual_period() -> Dict[str, Any]:
    """Возвращает самый актуальный период ТСН из цифрового сборника.

    Фильтрация записей периода:
      - baseTypeCode == "TSN";
      - в title есть слово «индекс» (сам период, а не технические правки
        или тестовые дополнения);
      - максимальный dateStart (при равенстве — поздний createdOn).
    """
    payload = _api_get(
        PERIOD_FILTER_URL,
        params={
            "page_number": 0,
            "page_size": 50,
            "sort_column": "DATE_START",
            "sort_direction": "DESC",
            "deleted": "false",
            "search_mode": "CONTAINS",
        },
    )

    records = payload.get("data") or []
    if not isinstance(records, list):
        raise RuntimeError("Неожиданный формат ответа period/filter (data не список)")

    candidates = [
        rec for rec in records
        if rec.get("baseTypeCode") == "TSN"
        and "индекс" in str(rec.get("title", "")).lower()
    ]
    if not candidates:
        raise RuntimeError(
            "В списке периодов не найден актуальный период ТСН "
            "(baseTypeCode=TSN, «индекс» в названии)"
        )

    period = max(
        candidates,
        key=lambda rec: (str(rec.get("dateStart") or ""), str(rec.get("createdOn") or "")),
    )
    logger.info(
        f"Актуальный период ТСН: {period.get('title')} "
        f"(dateStart={period.get('dateStart')}, id={period.get('id')})"
    )
    return period


def save_period_json(period: Dict[str, Any], session_dir: str) -> str:
    """Сохраняет актуальный период в корне сессии (period.json)."""
    payload = {
        "period": period.get("title"),
        "id": period.get("id"),
        "date_start": period.get("dateStart"),
        "base_type_code": period.get("baseTypeCode"),
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path = os.path.join(session_dir, PERIOD_JSON_FILENAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    logger.info(f"Период ТСН сохранён: {path}")
    return path


# =====================================================================
#  ШАГ 2. РАБОТЫ ПО ШИФРАМ ТАБЛИЦ
# =====================================================================

def _collect_table_codes(tables_payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """Уникальные таблицы (код + наименование) из Подобранные_таблицы_работ.json.

    Порядок сохраняется как в исходном файле (первое вхождение).
    """
    seen: set = set()
    tables: List[Dict[str, str]] = []
    for element in tables_payload.get("elements", []) or []:
        for work in element.get("works", []) or []:
            code = str(work.get("code") or "").strip()
            if code and code not in seen:
                seen.add(code)
                tables.append({"code": code, "name": work.get("name") or ""})
    return tables


# =====================================================================
#  ШАГ 3. ДЕТАЛЬНЫЕ ПАРАМЕТРЫ ПОЗИЦИЙ (разбивка стоимости)
# =====================================================================

def fetch_work_details(
    work_ids: List[int], period_id: int,
) -> Dict[int, Dict[str, Any]]:
    """Детальные параметры позиций (полная разбивка стоимости) из larix.

    GET catalog/work-process/detail?id=<ID>&period=<periodId> — по id каждой
    работы возвращает показатели, которых нет в списке работ
    (catalog/work-process/list): curSalary (ЗП), curOperationOfMachines (ЭМ),
    curCostOfMaterialResources (МР), curDirectCosts и т.д.

    Ошибка отдельной позиции (404 и т.п.) не прерывает обработку — позиция
    пропускается с предупреждением (стоимость остаётся по данным списка).

    Аргументы:
        work_ids  — уникальные id работ (поле "id" работ из work-process/list).
        period_id — id актуального периода ТСН (period.json / period_id
                    Подобранные_работы.json).

    Возвращает словарь {work_id: детальные_параметры_позиции}.
    """
    details: Dict[int, Dict[str, Any]] = {}
    for wid in work_ids:
        try:
            detail = _api_get(
                WORK_PROCESS_DETAIL_URL,
                params={"id": int(wid), "period": int(period_id)},
            )
        except Exception as exc:
            logger.warning(
                f"Не получены детальные параметры позиции id={wid}: {exc}"
            )
            continue
        if isinstance(detail, dict) and detail:
            details[int(wid)] = detail
    logger.info(
        f"Детальные параметры позиций (стоимость): получено "
        f"{len(details)} из {len(work_ids)}"
    )
    return details


def fetch_works_for_table(table_code: str, period_title: str) -> List[Dict[str, Any]]:
    """Все работы таблицы за период (постраничная выборка work-process/list)."""
    works: List[Dict[str, Any]] = []
    page = 0
    while page < _MAX_PAGES:
        payload = _api_get(
            WORK_PROCESS_URL,
            params={
                "per": period_title,
                "page_number": page,
                "page_size": _PAGE_SIZE,
                "sort_column": "PRESSMARK_SORT",
                "sort_direction": "ASC",
                "pressmark": table_code,
                "search_mode": "CONTAINS",
                "deleted": 0,
            },
        )
        data = payload.get("data") or []
        if not isinstance(data, list):
            raise RuntimeError(
                "Неожиданный формат ответа work-process/list (data не список)"
            )
        works.extend(data)

        paging = payload.get("paging") or {}
        total_pages = paging.get("totalPages")
        page += 1
        if not data or not isinstance(total_pages, int) or page >= total_pages:
            break
    return works


def fetch_and_save_works(
    tables_json_path: str,
    session_dir: str,
    works_output_dir: Optional[str] = None,
) -> Optional[str]:
    """Полный шаг: период → period.json, работы таблиц → Подобранные_работы.json.

    Args:
        tables_json_path: путь к Подобранные_таблицы_работ.json запуска.
        session_dir: корень сессии outputs/<session_id>/ (сюда пишется
            period.json — период общий для всех запусков сессии).
        works_output_dir: папка для Подобранные_работы.json (по умолчанию —
            session_dir; в пайплайне АР — папка запуска run_<NNN>/, чтобы
            результаты разных запусков не перезаписывали друг друга).

    Returns:
        Путь к Подобранные_работы.json либо None, если работы не запрашивались
        (нет подобранных таблиц).
    """
    with open(tables_json_path, "r", encoding="utf-8") as fh:
        tables_payload = json.load(fh)

    # Шаг 1: актуальный период ТСН
    period = fetch_actual_period()
    period_title = str(period.get("title") or "")
    if not period_title:
        raise RuntimeError("У актуального периода ТСН пустое название (title)")
    save_period_json(period, session_dir)

    tables = _collect_table_codes(tables_payload)
    if not tables:
        logger.warning(
            "В Подобранные_таблицы_работ.json нет таблиц — "
            "запрос работ к цифровому сборнику не выполняется"
        )
        return None

    # Шаг 2: работы по каждому шифру таблицы
    result_tables: List[Dict[str, Any]] = []
    total_works = 0
    for table in tables:
        code = table["code"]
        logger.info(f"Запрос работ цифрового сборника: таблица {code} ({table['name']})")
        works = fetch_works_for_table(code, period_title)
        total_works += len(works)
        result_tables.append({
            "code": code,
            "name": table["name"],
            "works_count": len(works),
            "works": works,
        })
        logger.info(f"Таблица {code}: получено {len(works)} работ")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "processing_type": "AR",
        "period": period_title,
        "period_id": period.get("id"),
        "source": os.path.basename(tables_json_path),
        "total_tables": len(tables),
        "total_works": total_works,
        "tables": result_tables,
    }

    path = os.path.join(works_output_dir or session_dir, WORKS_JSON_FILENAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    logger.info(
        f"Работы цифрового сборника сохранены: {path} "
        f"({len(tables)} таблиц, {total_works} работ)"
    )
    return path


# =====================================================================
#  CLI для отладки: python -m src.services.works_fetcher <tables.json>
#      <session_dir> [works_output_dir]
# =====================================================================

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Использование: python -m src.services.works_fetcher "
            "<путь_к_Подобранные_таблицы_работ.json> <папка_сессии> "
            "[папка_для_Подобранные_работы.json]"
        )
        sys.exit(1)

    _tables_path = sys.argv[1]
    _session_dir = sys.argv[2]
    _works_dir = sys.argv[3] if len(sys.argv) > 3 else _session_dir
    if not os.path.isfile(_tables_path):
        print(f"Файл не найден: {_tables_path}")
        sys.exit(1)
    os.makedirs(_session_dir, exist_ok=True)
    os.makedirs(_works_dir, exist_ok=True)

    _result = fetch_and_save_works(_tables_path, _session_dir, _works_dir)
    if _result:
        print(f"OK: {_result}")
    else:
        print("Таблицы не найдены — работы не запрашивались")
