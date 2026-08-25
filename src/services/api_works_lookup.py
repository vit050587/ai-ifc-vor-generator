"""
Подбор работ через API справочника ТСН (режим КР).

Заменяет фильтрацию работ из data/perechen_kr.xlsx и LLM-отбор
(этапы 1-4) на POST-запросы к эндпоинту
digital-collection/building-elements/positions.

Поток работы:
  1. Выбранные в веб-интерфейсе элементы группируются и преобразуются
     в формат ifc_raw_elements_grouped.json (buildingElementName,
     totalMeasure, characteristics, additionalCharacteristics).
  2. Каждый элемент отправляется отдельным POST-запросом на эндпоинт
     (API принимает один объект, не массив).
  3. Из ответов (data[].works) формируется финальный перечень работ:
     Шифр ТСН = works/code, Наименование расценки/ресурса = works/name,
     Ед. изм. = works/unitOfMeasure. Объём работ рассчитывается по
     totalMeasure соответствующей группы элементов, Стоимость — по
     price_cost.xlsx (Шифр ТСН × Объём работ).
"""

import json
import os
from typing import Any, Dict, List, Tuple

import pandas as pd
import requests

from src.core.config import load_config
from src.core.logger import setup_logger
from src.services.fourth_etap import _get_corrected_volume, _add_cost_column

logger = setup_logger(__name__)

_cfg = load_config()

API_URL = _cfg.WORKS_API_URL
API_TOKEN = _cfg.WORKS_API_TOKEN

# Максимальный размер страницы — чтобы получить все позиции за один запрос.
_PAGE_SIZE = 1000


# =====================================================================
#  POST-ЗАПРОС К API
# =====================================================================

def _fetch_one(element: Dict[str, Any]) -> Dict[str, Any]:
    """POST-запрос для одного элемента.

    API принимает один объект (не массив), поэтому каждый элемент
    отправляется отдельным запросом.
    """
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }
    url = f"{API_URL}?pageSize={_PAGE_SIZE}"

    try:
        response = requests.post(url, json=element, headers=headers, timeout=300)
    except requests.RequestException as exc:
        raise RuntimeError(f"Ошибка вызова API подбора работ: {exc}") from exc

    if response.status_code == 401:
        raise RuntimeError(
            "API подбора работ вернул 401 — Bearer-токен недействителен "
            "(проверьте WORKS_API_TOKEN)"
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"API подбора работ вернул статус {response.status_code}: "
            f"{response.text[:500]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"API подбора работ вернул не-JSON ответ: {response.text[:500]}"
        ) from exc

    if not isinstance(data, dict) or "data" not in data:
        raise RuntimeError(
            f"Некорректный ответ API подбора работ: {str(data)[:500]}"
        )

    return data


def fetch_works_from_api(
    elements_json: List[Dict[str, Any]],
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Отправляет элементы в API и собирает ответы.

    API принимает один объект за запрос, поэтому каждый элемент
    из elements_json отправляется отдельным POST-запросом.

    Аргументы:
        elements_json — массив групп элементов в формате
            ifc_raw_elements_grouped.json.

    Возвращает:
        Список пар (element, api_response), где element — исходная
        группа запроса, api_response — ответ API для неё
        ({"data": [...], "paging": {...}}).
    """
    if not elements_json:
        raise RuntimeError("Нет данных для отправки в API подбора работ")

    if not API_TOKEN:
        raise RuntimeError(
            "Не задан Bearer-токен для API подбора работ "
            "(переменная окружения WORKS_API_TOKEN)"
        )

    results: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    total = len(elements_json)

    for index, element in enumerate(elements_json, 1):
        name = element.get("buildingElementName", "?")
        logger.info(f"API запрос {index}/{total}: {name}")
        response = _fetch_one(element)
        positions = response.get("data", []) or []
        works_count = sum(len(p.get("works", []) or []) for p in positions)
        logger.info(
            f"API ответ {index}/{total}: {name} — "
            f"позиций={len(positions)}, работ={works_count}"
        )
        results.append((element, response))

    return results


# =====================================================================
#  РАСЧЁТ ОБЪЁМА РАБОТ
# =====================================================================

def safe_float(value, default: float = 0.0) -> float:
    """Безопасное преобразование значения во float."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
        if num != num or num in (float("inf"), float("-inf")):  # NaN/Inf
            return default
        return num
    except (ValueError, TypeError):
        return default


def _divisor(okei_value) -> int:
    try:
        okei = int(okei_value)
    except (ValueError, TypeError):
        okei = 1
    return okei if okei and okei > 1 else 1


def _calculate_work_volume(work: Dict[str, Any], total_measure: Dict[str, Any]) -> str:
    """Рассчитывает объём работ для расценки из API.

    Аргументы:
        work — расценка из ответа API (unitOfMeasure, okeiValue).
        total_measure — totalMeasure группы элементов запроса
            ({type: volume|area|count, value, unit}).

    Возвращает строку с объёмом (или пустую строку, если посчитать нельзя).
    """
    measure_type = (total_measure or {}).get("type", "")
    measure_value = safe_float((total_measure or {}).get("value", 0))

    if measure_value <= 0:
        return ""

    unit = str(work.get("unitOfMeasure", "") or "").lower().replace(" ", "")
    okei_value = work.get("okeiValue")

    is_volume = ("м3" in unit or "m3" in unit or "м[3" in unit or "m[3" in unit)
    is_area = ("м2" in unit or "m2" in unit or "м[2" in unit or "m[2" in unit)
    is_count = "шт" in unit

    if is_volume and measure_type == "volume":
        vol = measure_value / _divisor(okei_value)
        decimals = 4 if _divisor(okei_value) > 1 else 3
        return f"{vol:.{decimals}f}"
    if is_area and measure_type == "area":
        vol = measure_value / _divisor(okei_value)
        decimals = 4 if _divisor(okei_value) > 1 else 2
        return f"{vol:.{decimals}f}"
    if is_count and measure_type == "count":
        vol = measure_value / _divisor(okei_value)
        decimals = 4 if _divisor(okei_value) > 1 else 0
        return f"{vol:.{decimals}f}"

    # Массовые (т) и прочие измерители посчитать из totalMeasure нельзя.
    return ""


# =====================================================================
#  ФОРМИРОВАНИЕ ФИНАЛЬНОГО ПЕРЕЧНЯ РАБОТ
# =====================================================================

def build_final_works_from_api(
    api_results: List[Tuple[Dict[str, Any], Dict[str, Any]]],
    output_folder: str,
    save_raw: bool = True,
) -> str:
    """Формирует финальный Excel-перечень работ из ответов API.

    Колонки:
        Шифр ТСН              = works/code
        Наименование расценки/ресурса = works/name
        Ед. изм.              = works/unitOfMeasure
        Объём работ           = по totalMeasure группы элементов запроса
        Стоимость             = цена из price_cost.xlsx × Объём работ

    Аргументы:
        api_results   — список пар (element, api_response) из fetch_works_from_api.
        output_folder — папка для сохранения результата.
        save_raw      — сохранять ли сырые ответы API (api_works_response.json).

    Возвращает:
        Путь к созданному файлу ОБЩИЙ_Финальный_перечень_работ.xlsx.
    """
    rows: List[Dict[str, Any]] = []

    for element, response in api_results:
        total_measure = element.get("totalMeasure", {})
        positions = response.get("data", []) or []

        for position in positions:
            pos_label = position.get("fullName") or position.get("name", "")

            for work in position.get("works", []) or []:
                volume = _calculate_work_volume(work, total_measure)
                rows.append({
                    "Шифр ТСН": work.get("code", ""),
                    "Наименование расценки/ресурса": work.get("name", ""),
                    "Ед. изм.": work.get("unitOfMeasure", ""),
                    "Объём работ": volume,
                })

    if not rows:
        logger.warning("API подбора работ не вернул ни одной расценки")
        rows.append({
            "Шифр ТСН": "",
            "Наименование расценки/ресурса": "Работы не подобраны",
            "Ед. изм.": "",
            "Объём работ": "",
        })

    df = pd.DataFrame(rows, columns=[
        "Шифр ТСН", "Наименование расценки/ресурса", "Ед. изм.", "Объём работ",
    ])

    # Корректировка объёмов по нормам расхода (koefs.xlsx)
    try:
        df = _get_corrected_volume(df)
    except Exception as exc:
        logger.error(f"Ошибка при корректировке объёма работ: {exc}")

    # Стоимость = цена из price_cost.xlsx × Объём работ
    try:
        df = _add_cost_column(df)
    except Exception as exc:
        logger.error(f"Ошибка при расчёте стоимости: {exc}")

    output_path = os.path.join(output_folder, "ОБЩИЙ_Финальный_перечень_работ.xlsx")
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Данные", index=False)

    logger.info(
        f"Финальный перечень работ из API сохранён: {output_path} "
        f"({len(df)} строк)"
    )

    if save_raw:
        raw_path = os.path.join(output_folder, "api_works_response.json")
        combined = {
            "results": [
                {
                    "element": element,
                    "response": response,
                }
                for element, response in api_results
            ],
        }
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump(combined, fh, ensure_ascii=False, indent=2, default=str)
        logger.info(f"Сырые ответы API сохранены: {raw_path}")

    return output_path