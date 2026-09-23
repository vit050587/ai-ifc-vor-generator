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
     totalMeasure соответствующей группы элементов.
4. Полученные работы отправляются на эндпоинт стоимости
      works/resources (POST), откуда берутся показатели стоимости
      одной измерительной единицы работы (за 100 м², тонну и т. д. в
      зависимости от okeiValue): curSalary — зарплата рабочих (ЗП),
      curOperationOfMachines — эксплуатация машин (ЭМ),
      curCostOfMaterialResources — материальные ресурсы (МР).
      Колонки «ЗП» / «ЭМ» / «МР» = соответствующий показатель ×
      Объём работ; «Стоимость» = ЗП + ЭМ + МР.
"""

import base64
import json
import os
import re
import time
from typing import Any, Dict, List, Tuple

import pandas as pd
import requests

from src.core.config import load_config
from src.core.keycloak import KeycloakTokenProvider
from src.core.logger import setup_logger
from src.services.works_cost import (
    _get_corrected_volume,
    _add_cost_column,
    _parse_money,
    format_money,
)

logger = setup_logger(__name__)

_cfg = load_config()

API_URL = _cfg.WORKS_API_URL
RESOURCES_API_URL = _cfg.WORKS_RESOURCES_API_URL
API_TOKEN = _cfg.WORKS_API_TOKEN

# Провайдер для автоматического обновления токена (Keycloak, client_credentials).
# Если KEYCLOAK_CLIENT_ID/KEYCLOAK_CLIENT_SECRET не заданы — используется
# статичный WORKS_API_TOKEN как fallback.
if _cfg.KEYCLOAK_CLIENT_ID and _cfg.KEYCLOAK_CLIENT_SECRET:
    _token_provider = KeycloakTokenProvider(
        token_url=_cfg.KEYCLOAK_TOKEN_URL,
        client_id=_cfg.KEYCLOAK_CLIENT_ID,
        client_secret=_cfg.KEYCLOAK_CLIENT_SECRET,
    )
    logger.info(
        "Токен API справочника работ будет получаться автоматически "
        "(Keycloak client_credentials)"
    )
else:
    _token_provider = None
    if not API_TOKEN:
        logger.warning(
            "WORKS_API_TOKEN не задан и Keycloak-клиент не сконфигурирован "
            "(KEYCLOAK_CLIENT_ID/KEYCLOAK_CLIENT_SECRET) — "
            "запросы к API работ будут недоступны"
        )
    else:
        logger.warning(
            "Keycloak-клиент не сконфигурирован (KEYCLOAK_CLIENT_ID/"
            "KEYCLOAK_CLIENT_SECRET) — используется статичный WORKS_API_TOKEN"
        )


def _get_token() -> str:
    """Возвращает актуальный Bearer-токен.

    Если настроен Keycloak-клиент — токен получается и обновляется
    автоматически, иначе возвращается статичный WORKS_API_TOKEN.
    """
    if _token_provider is not None:
        return _token_provider.get_token()
    if not API_TOKEN:
        raise RuntimeError(
            "Не задан Bearer-токен для API подбора работ "
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

# Максимальный размер страницы — чтобы получить все позиции за один запрос.
_PAGE_SIZE = 1000

# Причина пропуска групп сборного железобетона: в цифровом сборнике (ЦС)
# содержатся только монолитные конструкции, поэтому для сборных элементов
# запрос к API подбора работ не отправляется. Причина фиксируется в
# api_works_response.json и в заголовке группы финального перечня.
PRECAST_SKIP_REASON = (
    "Сборный железобетон — в цифровом сборнике только монолитные "
    "конструкции, работы из ЦС не подбираются"
)


def _is_precast_group(element: Dict[str, Any]) -> bool:
    """Признак сборного железобетона у группы элементов.

    Признак ``_isPrecast`` устанавливается в
    ifc_reference_builder.build_reference_output по сырому материалу
    элемента («Железобетон сборный»), ConstructionMethod = Precast и
    уровню материала в пути группировки. Для страховки (группы из
    других источников) дублируется по нормализованному материалу
    характеристики «Материал».
    """
    if element.get("_isPrecast"):
        return True
    for char in element.get("characteristics") or []:
        if isinstance(char, dict) and char.get("name") == "Материал":
            values = char.get("values") or []
            material = str(
                (values[0] or {}).get("strValue", "")
            ).lower() if values else ""
            return "сборн" in material
    return False


# =====================================================================
#  POST-ЗАПРОС К API
# =====================================================================

def _fetch_one(element: Dict[str, Any]) -> Dict[str, Any]:
    """POST-запрос для одного элемента.

    API принимает один объект (не массив), поэтому каждый элемент
    отправляется отдельным запросом. Служебные внутренние поля
    (ключи с префиксом '_', например _reinforcementVolumeRatio)
    в запрос не включаются.
    """
    headers = {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }
    url = f"{API_URL}?pageSize={_PAGE_SIZE}"

    # Полезная нагрузка без внутренних служебных полей группы
    payload = {
        key: value for key, value in element.items()
        if not key.startswith('_')
    }

    def _post() -> requests.Response:
        try:
            return requests.post(url, json=payload, headers=headers, timeout=300)
        except requests.RequestException as exc:
            raise RuntimeError(f"Ошибка вызова API подбора работ: {exc}") from exc

    response = _post()

    # Токен мог быть отозван Keycloak раньше, чем наступил рассчитанный
    # expires_at. В таком случае сбрасываем кеш, получаем новый токен
    # и повторяем запрос ровно один раз.
    if response.status_code == 401 and _token_provider is not None:
        _token_provider.invalidate()
        headers["Authorization"] = f"Bearer {_get_token()}"
        response = _post()

    if response.status_code == 401:
        raise RuntimeError(
            "API подбора работ вернул 401 — Bearer-токен недействителен "
            "(проверьте WORKS_API_TOKEN / KEYCLOAK_CLIENT_ID и "
            "KEYCLOAK_CLIENT_SECRET)"
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


# =====================================================================
#  ФИЛЬТРАЦИЯ ГРУПП РАБОТ ПО ВЫСОТЕ ЗДАНИЯ
# =====================================================================

def _height_in_range(height: float, range_obj: Any) -> bool:
    """Проверяет, попадает ли высота здания в диапазон группы работ.

    Диапазон приходит от API в виде
        {"min": 57.0, "minMathNotation": ">", "max": 75.0,
         "maxMathNotation": "<="}
    Группа без диапазона (например, «Уход за бетоном») применяется
    при любой высоте.

    Аргументы:
        height     — высота здания, м.
        range_obj  — словарь range из workGroups (или None/{} — любая высота).

    Возвращает:
        True, если высота попадает в диапазон группы.
    """
    if not isinstance(range_obj, dict) or not range_obj:
        return True

    minimum = range_obj.get("min")
    maximum = range_obj.get("max")
    min_notation = str(range_obj.get("minMathNotation") or ">").strip()
    max_notation = str(range_obj.get("maxMathNotation") or "<=").strip()

    if minimum is not None:
        try:
            if min_notation == ">=":
                if not height >= float(minimum):
                    return False
            elif not height > float(minimum):
                return False
        except (ValueError, TypeError):
            pass

    if maximum is not None:
        try:
            if max_notation == "<":
                if not height < float(maximum):
                    return False
            elif not height <= float(maximum):
                return False
        except (ValueError, TypeError):
            pass

    return True


def _filter_response_by_height(
    response: Dict[str, Any],
    building_height: float | None,
) -> Dict[str, Any]:
    """Оставляет в ответе API только группы работ, подходящие высоте здания.

    API возвращает работы позиции справочника, сгруппированные по высоте
    здания («до 57», «более 57 до 75 м», «более 75 до 105 м» …), —
    фильтрацию по высоте выполняет клиент. Без фильтрации в финальный
    перечень попадают расценки сразу всех диапазонов высоты.

    Группы без диапазона (например, «Уход за бетоном») не зависят от
    высоты и сохраняются всегда. Если building_height не задана —
    ответ возвращается без изменений (прежнее поведение).

    Аргументы:
        response        — разобранный JSON-ответ API подбора работ.
        building_height — высота здания, м (или None).

    Возвращает:
        Ответ API, в котором data[].workGroups оставлены только
        подходящие по высоте группы.
    """
    if building_height is None:
        logger.warning(
            "Высота здания не задана — группы работ не отфильтрованы "
            "по высоте (в перечень могут попасть расценки всех диапазонов)"
        )
        return response

    try:
        height = float(building_height)
    except (ValueError, TypeError):
        logger.warning(
            f"Некорректная высота здания ({building_height!r}) — "
            "группы работ не отфильтрованы по высоте"
        )
        return response

    filtered = dict(response)
    data = response.get("data") or []
    new_data = []
    for position in data:
        if not isinstance(position, dict) or not position.get("workGroups"):
            new_data.append(position)
            continue
        kept_groups = [
            group for group in position["workGroups"]
            if not isinstance(group, dict)
            or _height_in_range(height, group.get("range"))
        ]
        if kept_groups:
            new_position = dict(position)
            new_position["workGroups"] = kept_groups
            new_data.append(new_position)
    filtered["data"] = new_data
    return filtered


def _select_best_positions(
    element: Dict[str, Any],
    positions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Отбирает позиции, наиболее точно соответствующие группе элементов.

    API подбора возвращает все позиции, у которых есть все характеристики
    запроса, — включая позиции с ДОПОЛНИТЕЛЬНЫМИ характеристиками, которых
    в запросе нет. Например, для группы «Фундаментная плита» (в запросе
    только «Материал») API возвращает также позиции «Фундаментная плита
    под оборудование объемом до 5 м3» и т.п. с характеристикой «Объем».
    Работы всех этих позиций не должны попадать в перечень: группа без
    характеристики «Объем» — обычная фундаментная плита здания.

    Правило: остаются позиции, у которых нет характеристик, отсутствующих
    в запросе группы (имена характеристик позиции ⊆ имён характеристик
    группы). Если таких позиций нет — возвращаются все (fallback, прежнее
    поведение).
    """
    if len(positions) <= 1:
        return positions
    requested = {
        str(char.get("name", "")).strip().lower()
        for char in element.get("characteristics", []) or []
        if isinstance(char, dict)
    }
    if not requested:
        return positions
    exact: List[Dict[str, Any]] = []
    for pos in positions:
        pos_char_names = [
            str(char.get("name", "")).strip().lower()
            for char in pos.get("characteristics", []) or []
            if isinstance(char, dict)
        ]
        if all(name in requested for name in pos_char_names):
            exact.append(pos)
    return exact if exact else positions


def _iter_works(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Извлекает список работ из ответа API подбора работ.

    Поддерживает форматы ответа эндпоинта:
      * вложенный в workGroups (актуальный):
        {"data": [{..., "workGroups": [{"works": [...]}]}]} —
        позиции справочника, работы сгруппированы внутри workGroups;
      * вложенный (на случай отката): {"data": [{..., "works": [...]}]} —
        позиции справочника с вложенным списком работ;
      * плоский: {"data": [work, ...]} — список работ на верхнем уровне.

    Аргументы:
        response — разобранный JSON-ответ API.

    Возвращает:
        Плоский список работ (пустой, если работ нет).
    """
    data = response.get("data", []) or []
    works: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        nested = item.get("works")
        if isinstance(nested, list):
            works.extend(w for w in nested if isinstance(w, dict))
        elif item.get("workGroups"):
            # Актуальный формат: работы вложены в группы позиции
            for group in item["workGroups"]:
                if not isinstance(group, dict):
                    continue
                group_works = group.get("works")
                if isinstance(group_works, list):
                    works.extend(w for w in group_works if isinstance(w, dict))
        elif item.get("code") is not None or item.get("name") is not None:
            # Плоский формат: элемент data сам является работой
            works.append(item)
    return works


# Ключевая фраза работ по уходу за бетоном в наименовании расценки
# («Уход за бетоном при среднесуточной температуре воздуха +5 гр. и выше»
# и т.п.)
CURING_WORK_KEYWORD = "уход за бетоном"


def _is_curing_work(work: Dict[str, Any]) -> bool:
    """Расценка «Уход за бетоном …» из ответа API подбора работ."""
    return CURING_WORK_KEYWORD in str(work.get("name", "")).lower()


def _iter_all_works(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Все работы ответа API — по всем позициям и их группам работ.

    В отличие от _iter_works (который вызывается для позиций, отобранных
    _select_best_positions) обходит data целиком — включая позиции,
    отсечённые отбором по лишним характеристикам (например,
    «Фундаментная плита под оборудование …»). Используется для
    гарантированного добавления работ «Уход за бетоном», которые могут
    прийти в отсечённых позициях.
    """
    data = response.get("data", []) or []
    works: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        nested = item.get("works")
        if isinstance(nested, list):
            works.extend(w for w in nested if isinstance(w, dict))
        for group in item.get("workGroups") or []:
            if isinstance(group, dict):
                group_works = group.get("works")
                if isinstance(group_works, list):
                    works.extend(w for w in group_works if isinstance(w, dict))
    return works


def fetch_works_from_api(
    elements_json: List[Dict[str, Any]],
    building_height: float | None = None,
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Отправляет элементы в API и собирает ответы.

    API принимает один объект за запрос, поэтому каждый элемент
    из elements_json отправляется отдельным POST-запросом.

    Аргументы:
        elements_json — массив групп элементов в формате
            ifc_raw_elements_grouped.json.
        building_height — высота здания, м; используется для отсечения
            групп работ, не подходящих по высоте (см.
            _filter_response_by_height). None — без фильтрации.

    Возвращает:
        Список пар (element, api_response), где element — исходная
        группа запроса, api_response — отфильтрованный по высоте
        ответ API для неё ({"data": [...], "paging": {...}}).
    """
    if not elements_json:
        raise RuntimeError("Нет данных для отправки в API подбора работ")

    # Убеждаемся, что токен доступен (из Keycloak-провайдера или WORKS_API_TOKEN).
    _get_token()

    results: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    total = len(elements_json)

    for index, element in enumerate(elements_json, 1):
        name = element.get("buildingElementName", "?")

        # Сборный железобетон: в ЦС только монолитные конструкции —
        # запрос к API не отправляется, работы не подбираются.
        # Причина фиксируется в ответе (api_works_response.json) и в
        # заголовке группы финального перечня работ.
        if _is_precast_group(element):
            logger.info(
                f"API запрос {index}/{total}: {name} — ПРОПУЩЕН "
                f"(сборный железобетон: в ЦС только монолитные конструкции)"
            )
            results.append((element, {
                "data": [],
                "_skipReason": PRECAST_SKIP_REASON,
            }))
            continue

        logger.info(f"API запрос {index}/{total}: {name}")
        response = _filter_response_by_height(_fetch_one(element), building_height)
        positions = response.get("data", []) or []
        works_count = len(_iter_works(response))
        logger.info(
            f"API ответ {index}/{total}: {name} — "
            f"позиций={len(positions)}, работ={works_count}"
        )
        results.append((element, response))

    return results


# =====================================================================
#  ЗАПРОС СТОИМОСТИ РАБОТ (works/resources)
# =====================================================================

# Максимальное число работ в одном запросе стоимости.
_COST_BATCH_SIZE = 100

# Показатели стоимости из ответа API (works/resources), извлекаемые
# для каждой работы:
#   curSalary                    — зарплата рабочих (ЗП);
#   curOperationOfMachines       — эксплуатация машин (ЭМ);
#   curCostOfMaterialResources   — материальные ресурсы (МР).
_COST_FIELDS = (
    "curSalary",
    "curOperationOfMachines",
    "curCostOfMaterialResources",
)


def _post_resources(payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST-запрос к эндпоинту стоимости работ (works/resources).

    При 401 сбрасывает кеш токена Keycloak и повторяет запрос один раз.
    """
    headers = {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }

    def _post() -> requests.Response:
        try:
            return requests.post(
                RESOURCES_API_URL, json=payload, headers=headers, timeout=300
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Ошибка вызова API стоимости работ: {exc}") from exc

    response = _post()

    if response.status_code == 401 and _token_provider is not None:
        _token_provider.invalidate()
        headers["Authorization"] = f"Bearer {_get_token()}"
        response = _post()

    if response.status_code == 401:
        raise RuntimeError(
            "API стоимости работ вернул 401 — Bearer-токен недействителен "
            "(проверьте WORKS_API_TOKEN / KEYCLOAK_CLIENT_ID и "
            "KEYCLOAK_CLIENT_SECRET)"
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"API стоимости работ вернул статус {response.status_code}: "
            f"{response.text[:500]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"API стоимости работ вернул не-JSON ответ: {response.text[:500]}"
        ) from exc


def fetch_work_costs(
    works: List[Dict[str, Any]],
) -> Tuple[Dict[int, Dict[str, float]], List[Dict[str, Any]]]:
    """Запрашивает стоимостные показатели для списка работ.

    Работы отправляются на эндпоинт works/resources в формате
    {"works": [...], "wrate": false, "coefficient": true}. В ответе для
    каждой работы возвращаются показатели стоимости одной измерительной
    единицы (за 100 м², тонну и т. д. в зависимости от okeiValue):
      * curSalary — зарплата рабочих (ЗП);
      * curOperationOfMachines — эксплуатация машин (ЭМ);
      * curCostOfMaterialResources — материальные ресурсы (МР).

    Аргументы:
        works — список работ в формате ответа API подбора работ
            (id, code, name, unitOfMeasure, okeiValue, characteristics,
            additionalWorks).

    Возвращает:
        Кортеж (costs, raw_responses), где costs — словарь
        {workId: {curSalary, curOperationOfMachines,
        curCostOfMaterialResources}}, raw_responses — сырые ответы API
        (для дампа).
    """
    costs: Dict[int, Dict[str, float]] = {}
    raw_responses: List[Dict[str, Any]] = []

    # Дедупликация работ по id (одна и та же расценка может прийти
    # в ответах для разных групп элементов)
    unique: Dict[int, Dict[str, Any]] = {}
    for work in works:
        work_id = work.get("id")
        if work_id is None or work_id in unique:
            continue
        unique[work_id] = work

    work_ids = list(unique.keys())
    total = len(work_ids)
    logger.info(f"Запрос стоимости для {total} работ ({RESOURCES_API_URL})")

    for start in range(0, total, _COST_BATCH_SIZE):
        batch_ids = work_ids[start:start + _COST_BATCH_SIZE]
        payload = {
            "works": [unique[wid] for wid in batch_ids],
            "wrate": False,
            "coefficient": True,
        }
        response = _post_resources(payload)
        raw_responses.append(response)

        for cost_work in response.get("works", []) or []:
            work_id = cost_work.get("workId")
            if work_id is None:
                continue
            costs[work_id] = {
                field: safe_float(cost_work.get(field))
                for field in _COST_FIELDS
            }

    logger.info(
        f"Получена стоимость для {len(costs)} из {total} работ"
    )
    return costs, raw_responses


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


def _work_divisor(work: Dict[str, Any]) -> int:
    """Делитель объёма работы (100 для «100 м²», 1 для «м²»/«т»/«шт»).

    Приоритет — okeiValue из ответа API. У части работ okeiValue
    отсутствует (None), а кратность задана только в самой единице
    измерения («100 м2», «1 т», «100 м3») — в этом случае числовой
    префикс разбирается из unitOfMeasure. Без этого объём работ
    в «100 м²» не делился на 100, а стоимость завышалась в 100 раз.
    """
    divisor = _divisor(work.get("okeiValue"))
    if divisor > 1:
        return divisor

    unit = str(work.get("unitOfMeasure", "") or "").strip()
    match = re.match(r"^(\d+)\s", unit)
    if match:
        try:
            prefix = int(match.group(1))
            if prefix > 1:
                return prefix
        except ValueError:
            pass
    return 1


# Приоритет ключей при выборе «рабочей» площади группы из totalAreas.
# «Площадь, м2» — основная площадь элементов (для стен — боковая/опалубочная,
# для плит/балок — площадь опирания), уже выбранная на этапе подготовки таблицы.
# «NetArea» — элементы, у которых площадь задана только в Net-количествах
# (гидроизоляции, утеплитель и т.п.).
_AREA_KEY_PRIORITY = ['Площадь, м2', 'GrossSideArea', 'NetSideArea', 'GrossArea', 'GROSS', 'NetArea']


def _pick_area_value(total_areas: Dict[str, Any]) -> float:
    """Выбирает «рабочую» площадь группы элементов из totalAreas.

    Приоритет ключей: 'Площадь, м2' (основная площадь из сметной таблицы),
    затем боковые/полные площади (GrossSideArea, NetSideArea, GrossArea,
    GROSS). Площадь следа/опирания (Footprint) используется только в крайнем
    случае, если других площадей в группе нет.

    Аргументы:
        total_areas — словарь {имя колонки площади: суммарное значение}.

    Возвращает:
        Число площади (м²) или 0.0, если подходящей площади нет.
    """
    if not total_areas:
        return 0.0
    for priority in _AREA_KEY_PRIORITY:
        for key, value in total_areas.items():
            if priority.lower() in str(key).lower() and 'footprint' not in str(key).lower():
                area_value = safe_float(value)
                if area_value > 0:
                    return area_value
    # Fallback: любая положительная площадь из группы
    for value in total_areas.values():
        area_value = safe_float(value)
        if area_value > 0:
            return area_value
    return 0.0


def _is_formwork_work(work: Dict[str, Any]) -> bool:
    """Расценка монтажа/демонтажа опалубки с единицей измерения «м2».

    Прочие работы с «опалубкой» в наименовании (например, установка
    арматуры «в опалубку», ед. изм. «1 т») не подпадают — несовпадение
    единицы измерения.
    """
    name = str(work.get("name", "")).lower()
    unit = str(work.get("unitOfMeasure", "") or "").lower().replace(" ", "").replace("²", "2")
    return "опалубк" in name and "м2" in unit


def _calculate_work_volume(
    work: Dict[str, Any],
    total_measure: Dict[str, Any],
    total_areas: Dict[str, Any] | None = None,
    formwork_area: float = 0.0,
    slab_group: bool = False,
) -> str:
    """Рассчитывает объём работ для расценки из API.

    Аргументы:
        work — расценка из ответа API (unitOfMeasure, okeiValue).
        total_measure — totalMeasure группы элементов запроса
            ({type: volume|area|count, value, unit}).
        total_areas — суммарные площади группы (totalAreas из
            ifc_reference_builder), используются если расценка в м²,
            а основной измеритель группы — объём (например, опалубка
            стен считается по боковой площади при totalMeasure типа volume).
        formwork_area — площадь опалубки плит группы (м²): фундаментные
            плиты — периметр × толщина, перекрытия — периметр × толщина +
            площадь плиты. Для расценок монтажа/демонтажа опалубки (м²)
            подставляется вместо площади из totalAreas.
        slab_group — группа является плитной (фундаментная плита /
            перекрытие): опалубку таких групп считаем ТОЛЬКО по
            formwork_area, fallback на площадь плиты из totalAreas
            запрещён (иначе объём опалубки завышается в разы).

    Возвращает строку с объёмом (или пустую строку, если посчитать нельзя).
    """
    measure_type = (total_measure or {}).get("type", "")
    measure_value = safe_float((total_measure or {}).get("value", 0))

    if measure_value <= 0:
        return ""

    unit = str(work.get("unitOfMeasure", "") or "").lower().replace(" ", "")

    is_volume = ("м3" in unit or "m3" in unit or "м[3" in unit or "m[3" in unit)
    is_area = ("м2" in unit or "m2" in unit or "м[2" in unit or "m[2" in unit)
    is_count = "шт" in unit

    divisor = _work_divisor(work)

    if is_volume and measure_type == "volume":
        vol = measure_value / divisor
        decimals = 4 if divisor > 1 else 3
        return f"{vol:.{decimals}f}"
    if is_area and measure_type == "area":
        vol = measure_value / divisor
        decimals = 4 if divisor > 1 else 2
        return f"{vol:.{decimals}f}"
    if is_count and measure_type == "count":
        vol = measure_value / divisor
        decimals = 4 if divisor > 1 else 0
        return f"{vol:.{decimals}f}"

    # Расценка в площади (м²), а основной измеритель группы — объём или прочее.
    # Монтаж/демонтаж опалубки плит — по площади опалубки
    # (фундаментные плиты — периметр × толщина, перекрытия —
    # периметр × толщина + площадь плиты).
    if is_area and _is_formwork_work(work) and formwork_area > 0:
        vol = formwork_area / divisor
        decimals = 4 if divisor > 1 else 2
        return f"{vol:.{decimals}f}"

    # Прочие расценки в площади (м²) при измерителе группы «объём»:
    # берём суммарную площадь группы из totalAreas (например,
    # монтаж/демонтаж опалубки стен считается по боковой площади).
    # Для ОПАЛУБКИ плитных групп fallback на площадь плиты запрещён:
    # если formwork_area не рассчитан (нет периметра/толщины), объём
    # остаётся пустым — площадь плиты не является площадью опалубки.
    if is_area and slab_group and _is_formwork_work(work):
        return ""

    if is_area and total_areas:
        area_value = _pick_area_value(total_areas)
        if area_value > 0:
            vol = area_value / divisor
            decimals = 4 if divisor > 1 else 2
            return f"{vol:.{decimals}f}"

    # Массовые (т) и прочие измерители посчитать из totalMeasure нельзя.
    return ""


def _resolve_unit_label(work: Dict[str, Any]) -> str:
    """Определяет единицу измерения для колонки «Ед. изм.» финального перечня.

    Обозначение единицы должно соответствовать делителю объёма:
      * без делителя — базовая единица: «м³», «м²», «шт»;
      * с делителем (например, 100) — «100 м³», «100 м²», «100 шт»,
        т.е. объём в перечне выражен в сотнях соответствующих единиц.

    Делитель определяется по okeiValue, а при его отсутствии — по числовому
    префиксу единицы из API («100 м2» → 100, см. _work_divisor).

    Для прочих измерителей (т, кг, м и т.п.) возвращается исходное
    обозначение из API.
    """
    unit = str(work.get("unitOfMeasure", "") or "").lower().replace(" ", "")
    divisor = _work_divisor(work)

    if "м3" in unit or "m3" in unit or "м[3" in unit or "m[3" in unit:
        base = "м³"
    elif "м2" in unit or "m2" in unit or "м[2" in unit or "m[2" in unit:
        base = "м²"
    elif "шт" in unit:
        base = "шт"
    else:
        return work.get("unitOfMeasure", "") or ""

    return f"{divisor} {base}" if divisor > 1 else base


# =====================================================================
#  ЧАСТИ ЗДАНИЯ В ФИНАЛЬНОМ ПЕРЕЧНЕ РАБОТ
# =====================================================================

# Порядок частей здания в финальном перечне работ
BUILDING_PART_ORDER = ['Подземная', 'Цоколь', 'Надземная']

# Заголовки частей здания в финальном перечне работ
_BUILDING_PART_TITLES = {
    'Подземная': 'ПОДЗЕМНАЯ ЧАСТЬ',
    'Цоколь': 'ЦОКОЛЬ',
    'Надземная': 'НАДЗЕМНАЯ ЧАСТЬ',
}

# Надписи итоговых строк по частям здания
_BUILDING_PART_TOTAL_LABELS = {
    'Подземная': 'ИТОГО по подземной части:',
    'Цоколь': 'ИТОГО по цокольной части:',
    'Надземная': 'ИТОГО по надземной части:',
}


def _resolve_building_part(element: Dict[str, Any]) -> str:
    """Определяет часть здания для группы элементов.

    Приоритет:
      1. Служебное поле ``_buildingPart`` — заполняется в
         ``ifc_reference_builder.build_reference_output`` по path[0]
         группы (у групп без характеристики «Расположение» — например,
         фундаментные плиты — часть здания известна только из него);
      2. Характеристика «Расположение» («Подземная часть здания» и т.п.);
      3. «Надземная» — значение по умолчанию (как в build_reference_output).

    Аргументы:
        element — группа элементов в формате запроса API ТСН.

    Возвращает:
        Одно из значений BUILDING_PART_ORDER.
    """
    part = str(element.get("_buildingPart") or "").strip()
    if part in BUILDING_PART_ORDER:
        return part
    for char in element.get("characteristics") or []:
        if char.get("name") == "Расположение" and char.get("values"):
            location = str(char["values"][0].get("strValue", "")).strip().lower()
            for known in BUILDING_PART_ORDER:
                if location.startswith(known.lower()):
                    return known
    return "Надземная"


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
        ЗП                    = curSalary × Объём работ (зарплата рабочих)
        ЭМ                    = curOperationOfMachines × Объём работ
                                (эксплуатация машин)
        МР                    = curCostOfMaterialResources × Объём работ
                                (материальные ресурсы)
        Стоимость             = ЗП + ЭМ + МР

    Таблица разбивается на части здания (Подземная → Цоколь → Надземная):
    перед работами каждой части — строка-заголовок части, в конце каждой
    части — итоговая строка «ИТОГО по … части:» (суммы ЗП/ЭМ/МР/Стоимость
    по работам части); в конце таблицы — общая строка «ИТОГО:» (сумма по
    всем работам всех частей).

    Аргументы:
        api_results   — список пар (element, api_response) из fetch_works_from_api.
        output_folder — папка для сохранения результата.
        save_raw      — сохранять ли сырые ответы API (api_works_response.json).

    Возвращает:
        Путь к созданному файлу ОБЩИЙ_Финальный_перечень_работ.xlsx.
    """
    # Сначала собираем только строки с работами для обработки
    work_rows: List[Dict[str, Any]] = []
    # Все работы из ответов API — для запроса стоимости (works/resources)
    all_works: List[Dict[str, Any]] = []
    # Сохраняем информацию о заголовках элементов и их позициях
    element_info = []  # список словарей с информацией о каждом элементе

    for element_idx, (element, response) in enumerate(api_results):
        # Получаем название элемента из разных возможных полей
        element_name = element.get("buildingElementName") or element.get("name") or f"Элемент {element_idx + 1}"
        
        # Дополнительная информация об элементе
        element_info_parts = []
        
        # Добавляем материал, если есть
        if element.get("characteristics"):
            for char in element["characteristics"]:
                if char.get("name") == "Материал" and char.get("values"):
                    material = char["values"][0].get("strValue", "")
                    if material:
                        element_info_parts.append(material)
        
        # Добавляем имя элемента из Revit (обрезаем ID после двоеточия)
        if element.get("additionalCharacteristics"):
            for char in element["additionalCharacteristics"]:
                if char.get("name") == "Имя элемента" and char.get("values"):
                    revit_name = char["values"][0].get("strValue", "")
                    # Убираем цифровой ID в конце (после последнего двоеточия)
                    if ":" in revit_name:
                        parts = revit_name.split(":")
                        # Если последний элемент - цифры, убираем его
                        if parts[-1].strip().isdigit():
                            revit_name = ":".join(parts[:-1])
                    if revit_name:
                        element_info_parts.append(revit_name)
        
        # Формируем полное название с дополнительной информацией
        element_full_name = element_name
        if element_info_parts:
            element_full_name += f" ({', '.join(element_info_parts)})"
        
        # Добавляем информацию о количестве
        element_count = element.get("elementCount", 1)
        if element_count > 1:
            element_full_name += f" Кол-во: {element_count}"

        # Сборный железобетон: работы из ЦС не подбираются — причина
        # фиксируется в заголовке группы (в ЦС только монолитные
        # конструкции), чтобы сметчик видел, почему работ нет.
        if _is_precast_group(element):
            element_full_name += f" ({PRECAST_SKIP_REASON})"

        total_measure = element.get("totalMeasure", {})
        total_areas = element.get("totalAreas", {}) or {}
        # Суммарный расход арматуры (ReinforcementVolumeRatio, кг) по всем
        # элементам группы. Для работ по установке арматуры пересчитывается
        # в тонны (÷ 1000) и подставляется в «Объём работ».
        reinforcement_ratio = element.get("_reinforcementVolumeRatio")
        # Площадь опалубки плит группы (м²) — для расценок монтажа/
        # демонтажа опалубки (фундаментные плиты — периметр × толщина,
        # перекрытия — периметр × толщина + площадь плиты).
        formwork_area = safe_float(element.get("_formworkArea"), 0)
        # Признак плитной группы (фундаментная плита / перекрытие):
        # опалубка считается только по _formworkArea, fallback на
        # площадь плиты из totalAreas запрещён.
        is_slab_group = any(
            kw in element_name.lower() for kw in ("плит", "перекрыт")
        )
        # Отбираем позиции, точно соответствующие характеристикам группы
        # (без дополнительных характеристик, которых в запросе нет, —
        # например, «Фундаментная плита под оборудование …»).
        positions = _select_best_positions(
            element, response.get("data", []) or []
        )

        # Сохраняем индекс начала работ этого элемента
        start_index = len(work_rows)
        # id работ, уже добавленных для этой группы (дедупликация работ
        # «Уход за бетоном» при их дополнительном добавлении ниже)
        element_work_ids: set = set()

        for position in positions:
            pos_label = position.get("fullName") or position.get("name", "")

            for work in _iter_works({"data": [position]}):
                volume = _calculate_work_volume(
                    work, total_measure, total_areas,
                    formwork_area=formwork_area,
                    slab_group=is_slab_group,
                )
                work_name = str(work.get("name", ""))
                # Работы по установке арматурных изделий/каркасов/сеток/стержней.
                # API возвращает разные формулировки: «Установка арматурных
                # изделий, каркасов и сеток», «Установка каркасов в опалубку»,
                # «Установка отдельных стержней», «Установка закладных
                # деталей» — все это арматура, но суммарный расход арматуры
                # группы из модели уже включает объёмы всех таких работ.
                # Поэтому объём подставляется только один раз — в расценку
                # «Установка ... отдельных стержней ...», а остальные
                # арматурные расценки (каркасы, сетки, закладные детали)
                # идут с пустым «Объёмом работ», иначе объём задваивается.
                work_name_lower = work_name.lower()
                is_rebar_work = any(
                    kw in work_name_lower
                    for kw in ("арматур", "каркас", "стержн", "сетк", "закладн")
                )
                if is_rebar_work and reinforcement_ratio:
                    if "отдельн" in work_name_lower and "стержн" in work_name_lower:
                        # ReinforcementVolumeRatio в IFC задан в килограммах
                        # (на кубический метр), единица расценки — тонны: кг → т
                        volume = f"{reinforcement_ratio / 1000:.4f}"
                    else:
                        # Каркасы/сетки/закладные детали — без объёма
                        volume = ""
                all_works.append(work)
                element_work_ids.add(work.get("id"))
                work_rows.append({
                    "Шифр ТСН": work.get("code", ""),
                    "Наименование расценки/ресурса": work.get("name", ""),
                    "Ед. изм.": _resolve_unit_label(work),
                    "Объём работ": volume,
                    # id работы для сопоставления со стоимостью из API
                    "_workId": work.get("id"),
                })

        # Работы «Уход за бетоном» добавляются всегда, если они есть
        # в ответе API, — даже если они пришли в позициях, отсечённых
        # отбором _select_best_positions (например, «Фундаментная плита
        # под оборудование …»): уход за бетоном обязателен для любой
        # монолитной конструкции и не зависит от лишних характеристик
        # позиции, в которой пришёл. Дедупликация — по id работы.
        for work in _iter_all_works(response):
            if not _is_curing_work(work) or work.get("id") in element_work_ids:
                continue
            volume = _calculate_work_volume(
                work, total_measure, total_areas,
                formwork_area=formwork_area,
                slab_group=is_slab_group,
            )
            all_works.append(work)
            element_work_ids.add(work.get("id"))
            work_rows.append({
                "Шифр ТСН": work.get("code", ""),
                "Наименование расценки/ресурса": work.get("name", ""),
                "Ед. изм.": _resolve_unit_label(work),
                "Объём работ": volume,
                "_workId": work.get("id"),
            })
        
        # Сохраняем информацию об элементе
        element_info.append({
            "header": element_full_name,
            "start_index": start_index,
            "end_index": len(work_rows),
            "is_last": element_idx == len(api_results) - 1,
            # Часть здания группы (Подземная / Цоколь / Надземная) —
            # по ней финальная таблица разбивается на части
            "part": _resolve_building_part(element),
        })

    if not work_rows:
        logger.warning("API подбора работ не вернул ни одной расценки")
        work_rows.append({
            "Шифр ТСН": "",
            "Наименование расценки/ресурса": "Работы не подобраны",
            "Ед. изм.": "",
            "Объём работ": "",
        })

    # Создаем DataFrame только с работами
    df_works = pd.DataFrame(work_rows, columns=[
        "Шифр ТСН", "Наименование расценки/ресурса", "Ед. изм.", "Объём работ",
        "_workId",
    ])

    # Корректировка объёмов по нормам расхода (koefs.xlsx)
    try:
        df_works = _get_corrected_volume(df_works)
    except Exception as exc:
        logger.error(f"Ошибка при корректировке объёма работ: {exc}")

    # Показатели стоимости из API цифрового сборника
    # (works/resources, поля curSalary/curOperationOfMachines/
    # curCostOfMaterialResources). При сбое — fallback на price_cost.xlsx.
    costs: Dict[int, Dict[str, float]] = {}
    cost_raw: List[Dict[str, Any]] = []
    try:
        works_for_cost = [w for w in all_works if w.get("id") is not None]
        if works_for_cost:
            costs, cost_raw = fetch_work_costs(works_for_cost)
    except Exception as exc:
        logger.error(f"Ошибка при получении стоимости из API: {exc}")

    if costs:
        # ЗП / ЭМ / МР = показатель из API × Объём работ.
        def _calc_component(row, field: str) -> Any:
            info = row.get("_costInfo") or {}
            value = safe_float(info.get(field), default=0.0)
            volume = safe_float(row.get("Объём работ"), default=0.0)
            if value > 0 and volume > 0:
                return round(value * volume, 2)
            return ""

        df_works["_costInfo"] = df_works["_workId"].map(
            lambda wid: costs.get(wid) if wid is not None else None
        )
        df_works["ЗП"] = df_works.apply(
            lambda r: _calc_component(r, "curSalary"), axis=1
        )
        df_works["ЭМ"] = df_works.apply(
            lambda r: _calc_component(r, "curOperationOfMachines"), axis=1
        )
        df_works["МР"] = df_works.apply(
            lambda r: _calc_component(r, "curCostOfMaterialResources"), axis=1
        )
        df_works = df_works.drop(columns=["_costInfo"])

        # Стоимость = ЗП + ЭМ + МР.
        def _calc_total(row) -> Any:
            total = sum(
                safe_float(row.get(col), default=0.0)
                for col in ("ЗП", "ЭМ", "МР")
            )
            return round(total, 2) if total > 0 else ""

        df_works["Стоимость"] = df_works.apply(_calc_total, axis=1)
    else:
        # Fallback: стоимость из price_cost.xlsx (прежнее поведение).
        # Показатели ЗП/ЭМ/МР из API недоступны — колонки остаются пустыми.
        logger.warning(
            "Стоимость из API недоступна — используется price_cost.xlsx"
        )
        df_works["ЗП"] = ""
        df_works["ЭМ"] = ""
        df_works["МР"] = ""
        try:
            df_works = _add_cost_column(df_works)
        except Exception as exc:
            logger.error(f"Ошибка при расчёте стоимости: {exc}")
            df_works["Стоимость"] = ""
        # Колонка «Стоимость за Ед. Изм.» больше не используется
        if "Стоимость за Ед. Изм." in df_works.columns:
            df_works = df_works.drop(columns=["Стоимость за Ед. Изм."])

    # Форматирование денежных колонок: разряды через пробел,
    # 2 знака после точки (например, '392 458.21')
    for col in ("ЗП", "ЭМ", "МР", "Стоимость"):
        if col in df_works.columns:
            df_works[col] = df_works[col].apply(format_money)

    # Теперь формируем строки финального перечня: таблица разбивается
    # на части здания (Подземная → Цоколь → Надземная). Перед работами
    # каждой части — строка-заголовок части, в конце каждой части —
    # итоговая строка по части; в конце таблицы — общий «ИТОГО:».
    final_rows = []

    def _blank_row() -> Dict[str, Any]:
        return {
            "Шифр ТСН": "",
            "Наименование расценки/ресурса": "",
            "Ед. изм.": "",
            "Объём работ": "",
            "ЗП": "",
            "ЭМ": "",
            "МР": "",
            "Стоимость": "",
            "_is_header": False,
            "_is_part_header": False,
            "_is_part_total": False,
        }

    def _sum_money(rows: List[Dict[str, Any]]) -> Dict[str, str]:
        """Суммы денежных колонок по строкам работ.

        Значения уже отформатированы format_money — разбираются
        обратно через _parse_money.
        """
        return {
            col: format_money(sum(_parse_money(r.get(col)) for r in rows))
            for col in ("ЗП", "ЭМ", "МР", "Стоимость")
            if col in df_works.columns
        }

    # Части здания, представленные в перечне (в фиксированном порядке)
    parts_in_order = [
        part for part in BUILDING_PART_ORDER
        if any(info["part"] == part for info in element_info)
    ]

    all_work_rows: List[Dict[str, Any]] = []

    if not parts_in_order:
        # Ни одной группы с частью здания (например, полностью пустой
        # ответ API) — выводим строки работ без разбивки на части.
        for idx in range(len(df_works)):
            row = df_works.iloc[idx].to_dict()
            row["_is_header"] = False
            row["_is_part_header"] = False
            row["_is_part_total"] = False
            final_rows.append(row)
            all_work_rows.append(row)

    for part_pos, part in enumerate(parts_in_order):
        part_elements = [info for info in element_info if info["part"] == part]

        # Заголовок части здания
        part_header = _blank_row()
        part_header["Наименование расценки/ресурса"] = (
            _BUILDING_PART_TITLES.get(part, part)
        )
        part_header["_is_part_header"] = True
        final_rows.append(part_header)

        part_work_rows: List[Dict[str, Any]] = []
        for elem_pos, elem_info in enumerate(part_elements):
            # Заголовок группы элементов
            header_row = _blank_row()
            header_row["Наименование расценки/ресурса"] = elem_info["header"]
            header_row["_is_header"] = True
            final_rows.append(header_row)

            # Работы этой группы
            for idx in range(elem_info["start_index"], elem_info["end_index"]):
                row = df_works.iloc[idx].to_dict()
                row["_is_header"] = False
                row["_is_part_header"] = False
                row["_is_part_total"] = False
                final_rows.append(row)
                part_work_rows.append(row)
                all_work_rows.append(row)

            # Пустая строка после каждой группы (кроме последней в части)
            if elem_pos < len(part_elements) - 1:
                final_rows.append(_blank_row())

        # Итоговая строка по части здания
        part_total = _blank_row()
        part_total["Наименование расценки/ресурса"] = (
            _BUILDING_PART_TOTAL_LABELS.get(part, f"ИТОГО по части ({part}):")
        )
        part_total["_is_part_total"] = True
        for col, value in _sum_money(part_work_rows).items():
            part_total[col] = value
        final_rows.append(part_total)

        # Пустая строка между частями (после последней части — общий итог)
        if part_pos < len(parts_in_order) - 1:
            final_rows.append(_blank_row())

    # Общая итоговая строка по всему перечню (сумма работ всех частей —
    # суммы итоговых строк частей не задваиваются, т.к. суммируются
    # только строки работ).
    if any(col in df_works.columns for col in ("ЗП", "ЭМ", "МР", "Стоимость")):
        total_row = _blank_row()
        total_row["Наименование расценки/ресурса"] = "ИТОГО:"
        for col, value in _sum_money(all_work_rows).items():
            total_row[col] = value
        final_rows.append(total_row)

    # Создаем финальный DataFrame
    df = pd.DataFrame(final_rows)

    # Убедимся, что все нужные колонки есть
    required_columns = [
        "Шифр ТСН", "Наименование расценки/ресурса", "Ед. изм.", "Объём работ",
        "ЗП", "ЭМ", "МР", "Стоимость",
        "_is_header", "_is_part_header", "_is_part_total",
    ]
    for col in required_columns:
        if col not in df.columns:
            df[col] = ""

    df = df[required_columns]

    # Убираем служебные колонки для Excel
    df_for_excel = df.drop(columns=[
        "_is_header", "_is_part_header", "_is_part_total",
    ])

    output_path = os.path.join(output_folder, "ОБЩИЙ_Финальный_перечень_работ.xlsx")

    # Создаем Excel с форматированием
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df_for_excel.to_excel(writer, sheet_name="Данные", index=False)

        # Получаем доступ к листу для форматирования
        worksheet = writer.sheets["Данные"]

        # Форматируем заголовки частей/элементов и итоги по частям
        from openpyxl.styles import Font, PatternFill, Alignment

        part_header_font = Font(bold=True, size=12)
        part_header_fill = PatternFill(
            start_color="A9A9A9", end_color="A9A9A9", fill_type="solid"
        )
        header_font = Font(bold=True, size=11)
        header_fill = PatternFill(start_color="D3D3D3", end_color="D3D3D3", fill_type="solid")
        center_alignment = Alignment(horizontal="center", vertical="center")
        bold_font = Font(bold=True, size=11)

        # Форматируем строки-заголовки и итоговые строки
        for row_idx in range(2, len(df) + 2):  # +2 из-за заголовка таблицы
            row_data = df.iloc[row_idx - 2]
            if row_data["_is_part_header"]:
                for col_idx in range(1, len(df_for_excel.columns) + 1):
                    cell = worksheet.cell(row=row_idx, column=col_idx)
                    cell.font = part_header_font
                    cell.fill = part_header_fill
                    cell.alignment = center_alignment
            elif row_data["_is_header"]:
                for col_idx in range(1, len(df_for_excel.columns) + 1):
                    cell = worksheet.cell(row=row_idx, column=col_idx)
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = center_alignment
            elif row_data["_is_part_total"]:
                for col_idx in range(1, len(df_for_excel.columns) + 1):
                    cell = worksheet.cell(row=row_idx, column=col_idx)
                    cell.font = bold_font
                    cell.alignment = center_alignment
        
        # Настраиваем ширину колонок
        worksheet.column_dimensions['A'].width = 15
        worksheet.column_dimensions['B'].width = 60
        worksheet.column_dimensions['C'].width = 10
        worksheet.column_dimensions['D'].width = 15
        worksheet.column_dimensions['E'].width = 15
        worksheet.column_dimensions['F'].width = 15
        worksheet.column_dimensions['G'].width = 15
        worksheet.column_dimensions['H'].width = 15
        
        # Добавляем автофильтр для удобства
        worksheet.auto_filter.ref = f"A1:{chr(64 + len(df_for_excel.columns))}{len(df_for_excel) + 1}"

    logger.info(
        f"Финальный перечень работ из API сохранён: {output_path} "
        f"({len(df_for_excel)} строк)"
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

        if cost_raw:
            cost_path = os.path.join(output_folder, "api_works_cost_response.json")
            with open(cost_path, "w", encoding="utf-8") as fh:
                json.dump(cost_raw, fh, ensure_ascii=False, indent=2, default=str)
            logger.info(f"Сырые ответы API стоимости сохранены: {cost_path}")

    return output_path

