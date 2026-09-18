"""
Финальный подбор подходящих работ через LLM (режим АР).

Выполняется после works_table_selector (Подобранные_таблицы_работ.json) и
works_fetcher (Подобранные_работы.json):

  Элементы объединяются в группы по дереву группировки АР
  (filtered_elements_grouped_AR.json: МССК → Материал → Наименование).
  Для каждой группы LLM (та же, что разбирает файл ПОС —
  pd_parser.LLMClient, Ollama) выбирает из кандидатов работы, действительно
  нужные для выполнения работ по группе элементов, и объясняет выбор.
  Логика подбора:

    * LLM-запрос строится только по ПЕРВОМУ элементу группы (представителю):
      его таблицы работ становятся источником работ-кандидатов, его описание
      (тип, материал, технология, этаж) — контекстом запроса;
    * объём работ считается по ВСЕЙ группе: значения объёмов (volume_m3 /
      area_m2) каждого элемента группы суммируются и приводятся к
      нормализованному виду по единице измерения расценки («100 м2» →
      суммарная площадь / 100, «м2» → как есть, «100 м3» → объём / 100 и
      т.д.).

  Результаты:
    - run_<NNN>/Финальный_перечень_работ.json — структурированный перечень
      выбранных работ по каждой группе элементов;
    - run_<NNN>/Финальный_перечень_работ.xlsx — итоговая таблица в структуре
      режима КР (ОБЩИЙ_Финальный_перечень_работ.xlsx): колонки «Шифр ТСН /
      Наименование расценки/ресурса / Ед. изм. / Объём работ / Стоимость за
      Ед. Изм. / Стоимость», строки-заголовки групп элементов, итоговая
      строка «ИТОГО:».

При ошибке LLM для группы элементов в перечень попадают все работы-кандидаты
группы (с пометкой в note) — шаг не должен обнулять результат запуска.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from src.core.config import load_config
from src.core.logger import setup_logger
from src.services.works_cost import add_total_row, format_money, safe_float

logger = setup_logger(__name__)

# Имена выходных файлов (в папке запуска run_<NNN>/)
FINAL_WORKS_JSON_FILENAME = "Финальный_перечень_работ.json"
FINAL_WORKS_XLSX_FILENAME = "Финальный_перечень_работ.xlsx"

# Дерево группировки АР (group_excel.process_ifc_excel_ar) в папке запуска:
# листовые группы (МССК → Материал → Наименование) с индексами элементов
GROUPED_JSON_FILENAME = "filtered_elements_grouped_AR.json"

# Ограничение числа работ-кандидатов в одном запросе LLM (защита контекста)
_MAX_CANDIDATE_WORKS = 200

SYSTEM_PROMPT = """Ты - инженер-сметчик ПТО. Твоя задача - из списка работ-кандидатов (норм цифрового сборника) выбрать только те, которые действительно нужны для выполнения работ по заданной группе элементов здания.

Правила:
1. Отвечай строго в формате JSON, без пояснений вне JSON.
2. Выбирай работы только из приведённого списка работ-кандидатов (поле "pressmark"). Ничего не выдумывай и не добавляй работы с другими шифрами.
3. Ориентируйся на тип элемента (стена, плита, окно, пол и т.д.), материал, технологию возведения (монолит/сборные), расположение в здании и объёмы работ. Ненужные для этой группы элементы работы отбрасывай.
4. В поле "reason" кратко (одним предложением) объясни, почему работа нужна для этой группы элементов.
5. Если подходят все работы-кандидаты - верни их все.
6. Если ни одна работа не подходит - верни пустой список "selected".

Формат ответа:
{
  "selected": [
    {"pressmark": "шифр работы из списка", "reason": "краткое пояснение"}
  ]
}
"""


def _build_user_prompt(
    element_payload: Dict[str, Any],
    tables: List[Dict[str, str]],
    candidate_works: List[Dict[str, Any]],
    quantity: Optional[Dict[str, Any]] = None,
    group_count: Optional[int] = None,
) -> str:
    """Формирует пользовательский запрос LLM по группе элементов и кандидатам.

    element_payload — запись ПЕРВОГО элемента группы (представителя): по нему
    LLM понимает тип/материал/технологию работ. quantity — суммарные объёмы
    по всей группе (если None — объёмы представителя), group_count — число
    элементов в группе.
    """
    element = element_payload.get("element", {}) or {}
    if quantity is None:
        quantity = {}
        for work in element_payload.get("works", []) or []:
            q = work.get("quantity") or {}
            if q:
                for key, value in q.items():
                    if value:
                        quantity[key] = value
                break

    lines = ["## Группа элементов"]
    if group_count:
        lines.append(f"Количество элементов в группе: {group_count}")
    if element.get("name"):
        lines.append(f"Наименование: {element['name']}")
    if element.get("ifc_class"):
        lines.append(f"IFC-класс: {element['ifc_class']}")
    if element.get("predefined_type"):
        lines.append(f"Тип (PredefinedType): {element['predefined_type']}")
    mssk = element_payload.get("mssk_context") or {}
    if mssk.get("code") or mssk.get("name"):
        lines.append(f"Код МССК: {mssk.get('code', '')} ({mssk.get('name', '')})")
    if element.get("material"):
        lines.append(f"Материал: {element['material']}")
    if element.get("construction_method"):
        lines.append(f"Технология: {element['construction_method']}")
    if element.get("storey"):
        storey = element["storey"]
        if element.get("storey_type"):
            storey += f" ({element['storey_type']})"
        lines.append(f"Этаж: {storey}")
    if quantity:
        qty_text = ", ".join(f"{k}={v}" for k, v in quantity.items())
        lines.append(f"Объёмы: {qty_text}")
    if tables:
        lines.append("")
        lines.append("## Подобранные таблицы работ")
        for table in tables:
            lines.append(f"- {table['code']} — {table['name']}")

    lines.append("")
    lines.append("## Работы-кандидаты (нормы цифрового сборника)")
    for work in candidate_works:
        cost = work.get("directCosts")
        cost_text = f", прямые затраты: {cost}" if cost is not None else ""
        lines.append(
            f"- {work['pressmark']} | {work.get('title', '')} "
            f"| ед. изм.: {work.get('unitOfMeasure', '')}{cost_text}"
        )

    lines.append("")
    lines.append(
        "## Задача\nВыбери из работ-кандидатов только те, которые нужны для "
        "выполнения работ по этой группе элементов. Верни JSON "
        '{"selected": [{"pressmark": "...", "reason": "..."}]}.'
    )
    return "\n".join(lines)


def _match_works_by_pressmark(
    selected: List[Dict[str, Any]],
    candidate_works: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Сопоставляет ответ LLM (шифры) с работами-кандидатами."""
    by_pressmark = {str(w.get("pressmark")): w for w in candidate_works}
    reasons = {
        str(item.get("pressmark")): str(item.get("reason") or "")
        for item in selected
        if isinstance(item, dict) and item.get("pressmark")
    }
    matched: List[Dict[str, Any]] = []
    for pressmark, reason in reasons.items():
        work = by_pressmark.get(pressmark)
        if work is None:
            logger.warning(f"LLM выбрал неизвестный шифр работы: {pressmark}")
            continue
        matched.append({"work": work, "reason": reason, "llm_selected": True})
    return matched


def _load_leaf_groups(grouped_json_path: str) -> List[Dict[str, Any]]:
    """Листовые группы элементов из filtered_elements_grouped_AR.json.

    Обход дерева группировки АР (МССК → Материал → Наименование) в глубину:
    листовые узлы (без children) содержат indices — 0-based индексы строк
    filtered_elements.xlsx, они же — позиции записей elements в
    Подобранные_таблицы_работ.json (файлы строятся по одним и тем же строкам
    в одном порядке). Возвращает список листовых групп в порядке дерева.
    """
    if not os.path.isfile(grouped_json_path):
        logger.warning(
            f"Нет файла группировки {os.path.basename(grouped_json_path)} — "
            "LLM-подбор выполняется по каждому элементу отдельно"
        )
        return []
    try:
        with open(grouped_json_path, "r", encoding="utf-8") as fh:
            tree = json.load(fh)
    except Exception as exc:
        logger.warning(f"Не удалось прочитать файл группировки: {exc}")
        return []

    leaves: List[Dict[str, Any]] = []

    def walk(nodes: List[Dict[str, Any]], path: List[str]) -> None:
        for node in nodes or []:
            current_path = path + [str(node.get("name", ""))]
            children = node.get("children") or []
            if children:
                walk(children, current_path)
            else:
                indices = [
                    int(i) for i in node.get("indices", []) or []
                    if isinstance(i, (int, str)) and str(i).strip().lstrip("-").isdigit()
                ]
                if indices:
                    leaves.append({
                        "name": str(node.get("name", "")),
                        "path": current_path,
                        "indices": sorted(indices),
                    })

    if isinstance(tree, list):
        walk(tree, [])
    return leaves


def _sum_group_quantities(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Суммарные объёмы по всем элементам группы.

    У каждого элемента quantity одинаков для всех его таблиц (QTO элемента),
    поэтому берётся первое непустое quantity записи элемента. Возвращает
    {volume_m3, area_m2, count} — сумма по группе + число элементов.
    """
    total: Dict[str, Any] = {"volume_m3": 0.0, "area_m2": 0.0, "count": len(payloads)}
    for payload in payloads:
        for work in payload.get("works", []) or []:
            q = work.get("quantity") or {}
            if not any(q.get(k) for k in ("volume_m3", "area_m2")):
                continue
            for key in ("volume_m3", "area_m2"):
                value = safe_float(q.get(key), default=0.0)
                if value > 0:
                    total[key] += value
            break
    return total


def _element_quantity_for_table(
    element_payload: Dict[str, Any], table_code: str,
) -> Dict[str, Any]:
    """Объёмы (quantity) группы элементов для конкретной таблицы работ."""
    for work in element_payload.get("works", []) or []:
        if str(work.get("code")) == str(table_code):
            return dict(work.get("quantity") or {})
    return {}


def _build_work_row(
    element_payload: Dict[str, Any],
    table: Dict[str, str],
    work: Dict[str, Any],
    reason: str,
    llm_selected: bool,
    quantity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Строка выбранной работы в итоговом перечне.

    quantity — объёмы для расчёта (сумма по группе либо объёмы отдельного
    элемента); если None — берётся quantity таблицы из записи элемента.
    """
    element = element_payload.get("element", {}) or {}
    if quantity is None:
        quantity = _element_quantity_for_table(element_payload, table["code"])
    return {
        "pressmark": work.get("pressmark"),
        "title": work.get("title"),
        "unit_of_measure": work.get("unitOfMeasure"),
        "table_code": table["code"],
        "table_name": table["name"],
        "direct_costs": work.get("directCosts"),
        "cur_direct_costs": work.get("curDirectCosts"),
        "quantity": quantity,
        "reason": reason,
        "llm_selected": llm_selected,
    }


def select_final_works(
    tables_json_path: str,
    works_json_path: str,
    run_dir: str,
    llm_config: Optional[Any] = None,
) -> Optional[str]:
    """Финальный подбор работ через LLM для каждой группы элементов.

    Args:
        tables_json_path: путь к Подобранные_таблицы_работ.json запуска.
        works_json_path: путь к Подобранные_работы.json запуска (кандидаты).
        run_dir: папка запуска run_<NNN>/ (сюда пишутся итоговые файлы).
        llm_config: конфигурация LLM (pd_parser.Config); по умолчанию —
            из src.core.config.load_config (OLLAMA_BASE_URL / NORMS_LLM_MODEL).

    Returns:
        Путь к Финальный_перечень_работ.json либо None, если данных нет.
    """
    if not os.path.isfile(tables_json_path):
        logger.warning(f"Нет файла подобранных таблиц: {tables_json_path}")
        return None
    if not os.path.isfile(works_json_path):
        logger.warning(
            f"Нет файла работ цифрового сборника: {works_json_path} — "
            "финальный подбор работ через LLM не выполняется"
        )
        return None

    with open(tables_json_path, "r", encoding="utf-8") as fh:
        tables_payload = json.load(fh)
    with open(works_json_path, "r", encoding="utf-8") as fh:
        works_payload = json.load(fh)

    # Карта «шифр таблицы → работы» из Подобранные_работы.json
    works_by_table: Dict[str, Dict[str, Any]] = {}
    for table in works_payload.get("tables", []) or []:
        works_by_table[str(table.get("code"))] = table

    # LLM-клиент (тот же, что для разбора ПОС)
    from src.services.pd_parser import LLMClient

    if llm_config is None:
        cfg = load_config()
        from src.services.pd_parser import Config as LLMConfig

        llm_config = LLMConfig(
            llm_base_url=cfg.ollama_url,
            llm_model=cfg.model_ollama,
        )
    llm = LLMClient(llm_config)

    result_elements: List[Dict[str, Any]] = []
    total_selected_works = 0

    elements_payload = tables_payload.get("elements", []) or []

    # Единицы обработки: листовые группы элементов (МССК → Материал →
    # Наименование) из filtered_elements_grouped_AR.json. Подбор работ через
    # LLM выполняется только по ПЕРВОМУ элементу группы (представителю) —
    # по его таблицам и описанию; объёмы работ считаются по ВСЕЙ группе
    # (сумма объёмов элементов). Элементы вне групп (файл группировки
    # отсутствует/повреждён или индексы вне диапазона) обрабатываются
    # поодиночно, как раньше.
    leaf_groups = _load_leaf_groups(os.path.join(run_dir, GROUPED_JSON_FILENAME))

    processing_units: List[Dict[str, Any]] = []
    if leaf_groups:
        covered: set = set()
        for group in leaf_groups:
            indices = sorted(
                i for i in group["indices"] if 0 <= i < len(elements_payload)
            )
            if not indices:
                continue
            covered.update(indices)
            processing_units.append({
                "first": elements_payload[indices[0]],
                "payloads": [elements_payload[i] for i in indices],
                "group": group,
            })
        for idx, payload in enumerate(elements_payload):
            if idx not in covered:
                processing_units.append({
                    "first": payload, "payloads": [payload], "group": None,
                })
        logger.info(
            f"LLM-подбор работ: {len(leaf_groups)} групп элементов, "
            f"{len(elements_payload)} элементов (подбор — по первому элементу "
            "группы, объёмы — по всей группе)"
        )
    else:
        processing_units = [
            {"first": payload, "payloads": [payload], "group": None}
            for payload in elements_payload
        ]

    for unit in processing_units:
        element_payload = unit["first"]
        group = unit["group"]
        # Суммарные объёмы по всем элементам группы (для отдельного элемента —
        # объёмы берутся из его записи, как раньше)
        group_quantity = (
            _sum_group_quantities(unit["payloads"]) if group is not None else None
        )

        # Таблицы группы (уникальные шифры, порядок как в файле) — только по
        # первому элементу группы (представителю)
        seen: set = set()
        tables: List[Dict[str, str]] = []
        candidate_works: List[Dict[str, Any]] = []
        for work in element_payload.get("works", []) or []:
            code = str(work.get("code") or "").strip()
            if not code:
                continue
            if code not in seen:
                seen.add(code)
                tables.append({"code": code, "name": work.get("name") or ""})
                for w in (works_by_table.get(code, {}) or {}).get("works", []) or []:
                    candidate_works.append(w)

        if not candidate_works:
            entry = {
                "element": element_payload.get("element", {}),
                "mssk_context": element_payload.get("mssk_context"),
                "selected_collection": element_payload.get("selected_collection"),
                "tables": tables,
                "selected_works": [],
                "note": "Нет работ-кандидатов в цифровом сборнике",
            }
            if group is not None:
                entry["group"] = {
                    "name": group.get("name", ""),
                    "path": group.get("path", []),
                    "element_count": len(unit["payloads"]),
                    "element_indices": group["indices"],
                }
                entry["group_quantity"] = group_quantity
            result_elements.append(entry)
            continue

        # Ограничение кандидатов (защита контекста LLM)
        candidate_works = candidate_works[:_MAX_CANDIDATE_WORKS]

        user_prompt = _build_user_prompt(
            element_payload, tables, candidate_works,
            quantity=group_quantity,
            group_count=len(unit["payloads"]) if group is not None else None,
        )
        selected_works: List[Dict[str, Any]] = []
        note = ""
        try:
            answer = llm.complete_json(
                system=SYSTEM_PROMPT, user=user_prompt,
            )
            selected = answer.get("selected")
            if not isinstance(selected, list):
                selected = []
            matched = _match_works_by_pressmark(selected, candidate_works)
            if matched:
                # Таблица работы определяется по её шифру (префикс шифра таблицы)
                def _table_for(work: Dict[str, Any]) -> Dict[str, str]:
                    pressmark = str(work.get("pressmark") or "")
                    for table in tables:
                        if pressmark.startswith(table["code"]):
                            return table
                    return tables[0]

                for item in matched:
                    table = _table_for(item["work"])
                    selected_works.append(
                        _build_work_row(
                            element_payload, table, item["work"],
                            item["reason"], item["llm_selected"],
                            quantity=group_quantity,
                        )
                    )
            else:
                note = "LLM не выбрал работы — включены все работы-кандидаты"
                logger.warning(
                    f"LLM не выбрал работы для группы "
                    f"«{(element_payload.get('element') or {}).get('name')}» — "
                    "fallback на полный список кандидатов"
                )
        except Exception as exc:
            note = f"Ошибка LLM ({exc}) — включены все работы-кандидаты"
            logger.error(
                f"Ошибка LLM при подборе работ для группы "
                f"«{(element_payload.get('element') or {}).get('name')}»: {exc}",
                exc_info=True,
            )

        if not selected_works:
            # Fallback: все кандидаты, сгруппированные по таблицам
            for table in tables:
                for work in (works_by_table.get(table["code"], {}) or {}).get("works", []) or []:
                    selected_works.append(
                        _build_work_row(
                            element_payload, table, work, "", False,
                            quantity=group_quantity,
                        )
                    )

        total_selected_works += len(selected_works)
        entry = {
            "element": element_payload.get("element", {}),
            "mssk_context": element_payload.get("mssk_context"),
            "selected_collection": element_payload.get("selected_collection"),
            "tables": tables,
            "selected_works": selected_works,
            "note": note,
        }
        if group is not None:
            # Метаданные группы: подбор выполнен по первому элементу
            # (представителю), объёмы — сумма по всем элементам группы
            entry["group"] = {
                "name": group.get("name", ""),
                "path": group.get("path", []),
                "element_count": len(unit["payloads"]),
                "element_indices": group["indices"],
            }
            entry["group_quantity"] = group_quantity
        result_elements.append(entry)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "processing_type": "AR",
        "period": works_payload.get("period"),
        "period_id": works_payload.get("period_id"),
        "source": os.path.basename(works_json_path),
        "total_elements": len(result_elements),
        "total_works": total_selected_works,
        "elements": result_elements,
    }

    # JSON
    json_path = os.path.join(run_dir, FINAL_WORKS_JSON_FILENAME)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    logger.info(
        f"Финальный перечень работ сохранён: {json_path} "
        f"({len(result_elements)} групп, {total_selected_works} работ)"
    )

    # XLSX — итоговая таблица в структуре режима КР
    xlsx_path = os.path.join(run_dir, FINAL_WORKS_XLSX_FILENAME)
    build_final_works_xlsx(result_elements, xlsx_path)

    return json_path


# ======================================================================
#  XLSX в структуре режима КР (ОБЩИЙ_Финальный_перечень_работ.xlsx)
# ======================================================================

def _clean_element_header_name(name: Any) -> str:
    """Убирает цифровой ID в конце имени элемента (после последнего двоеточия).

    Аналогично обработке «Имя элемента» из Revit в api_works_lookup (КР):
    «Фундаментная плита3:Фундаментная плита:2176357» → «Фундаментная плита3:Фундаментная плита».
    """
    text = str(name or "").strip()
    if ":" in text:
        parts = text.split(":")
        if parts[-1].strip().isdigit():
            text = ":".join(parts[:-1])
    return text


def _element_header(element_entry: Dict[str, Any]) -> str:
    """Заголовок группы элементов для строки-заголовка в таблице.

    Формат как в КР: «Имя (материал, МССК)» — например
    «Фундаментная плита3:Фундаментная плита (Железобетон монолитный, Фундаментная плита)».
    """
    element = element_entry.get("element", {}) or {}
    header = _clean_element_header_name(element.get("name")) or str(
        element.get("ifc_class") or "Элемент"
    )
    info_parts: List[str] = []
    material = str(element.get("material") or "").strip()
    if material:
        info_parts.append(material)
    mssk_name = str((element_entry.get("mssk_context") or {}).get("name") or "").strip()
    if mssk_name and mssk_name.lower() != header.lower():
        info_parts.append(mssk_name)
    if info_parts:
        header = f"{header} ({', '.join(info_parts)})"
    return header


# Множитель нормы в начале единицы измерения: «100 м2» → 100, «1000 шт.» → 1000
_UNIT_MULTIPLIER_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(.*)$")


def _volume_for_unit(quantity: Dict[str, Any], unit_of_measure: Any) -> str:
    """Объём работ по единице измерения расценки, нормализованный по норме.

    Базовое значение выбирается по размерности единицы (м² → площадь, м³ →
    объём, шт → количество элементов) и делится на множитель нормы из
    единицы измерения: «100 м2» → площадь / 100, «100 м3» → объём / 100,
    «1000 шт.» → количество / 1000, «м2»/«м3»/«шт» → как есть. Для прочих
    единиц (т, м и т.п.) объём не заполняется (нет данных в quantity).
    """
    unit = str(unit_of_measure or "").strip().lower().replace("²", "2").replace("³", "3")

    divisor = 1.0
    m = _UNIT_MULTIPLIER_RE.match(unit)
    if m:
        try:
            mult = float(m.group(1).replace(",", "."))
            if mult > 0:
                divisor = mult
        except ValueError:
            pass

    value = None
    if "м2" in unit:
        value = (quantity or {}).get("area_m2")
    elif "м3" in unit:
        value = (quantity or {}).get("volume_m3")
    elif "шт" in unit:
        value = (quantity or {}).get("count")

    num = safe_float(value, default=0.0)
    if num > 0:
        num = num / divisor
        return f"{num:.4f}"
    return ""


def build_final_works_xlsx(
    result_elements: List[Dict[str, Any]],
    xlsx_path: str,
) -> str:
    """Строит Финальный_перечень_работ.xlsx в структуре режима КР.

    Колонки и оформление — как в ОБЩИЙ_Финальный_перечень_работ.xlsx (КР):
        Шифр ТСН              = pressmark работы;
        Наименование расценки/ресурса = title работы;
        Ед. изм.              = unitOfMeasure;
        Объём работ           = объём группы по ед. изм. (м² → площадь, м³ → объём);
        Стоимость за Ед. Изм. = curDirectCosts (текущие прямые затраты цифрового
                                сборника; fallback — directCosts);
        Стоимость             = Объём работ × Стоимость за Ед. Изм.

    Перед работами каждой группы — строка-заголовок группы (жирный шрифт,
    серая заливка), между группами — пустая строка, последняя строка — «ИТОГО:».

    Аргументы:
        result_elements — список групп (элементы «elements» Финальный_перечень_работ.json).
        xlsx_path       — путь к создаваемому файлу.

    Возвращает путь к созданному файлу.
    """
    columns = [
        "Шифр ТСН", "Наименование расценки/ресурса", "Ед. изм.",
        "Объём работ", "Стоимость за Ед. Изм.", "Стоимость", "_is_header",
    ]

    def _empty_row(is_header: bool = False) -> Dict[str, Any]:
        return {col: "" for col in columns[:-1]} | {"_is_header": is_header}

    final_rows: List[Dict[str, Any]] = []
    for idx, element_entry in enumerate(result_elements):
        # Строка-заголовок группы элементов
        header_row = _empty_row(is_header=True)
        header_row["Наименование расценки/ресурса"] = _element_header(element_entry)
        final_rows.append(header_row)

        works = element_entry.get("selected_works") or []
        if not works:
            row = _empty_row()
            row["Наименование расценки/ресурса"] = "Работы не подобраны"
            final_rows.append(row)
        for work in works:
            volume_text = _volume_for_unit(
                work.get("quantity"), work.get("unit_of_measure")
            )
            # Текущие прямые затраты; fallback — базовые прямые затраты
            unit_cost = safe_float(work.get("cur_direct_costs"), default=0.0)
            if unit_cost <= 0:
                unit_cost = safe_float(work.get("direct_costs"), default=0.0)
            volume_num = safe_float(volume_text, default=0.0)
            cost = (
                round(unit_cost * volume_num, 2)
                if unit_cost > 0 and volume_num > 0
                else ""
            )
            final_rows.append({
                "Шифр ТСН": work.get("pressmark") or "",
                "Наименование расценки/ресурса": work.get("title") or "",
                "Ед. изм.": work.get("unit_of_measure") or "",
                "Объём работ": volume_text,
                "Стоимость за Ед. Изм.": round(unit_cost, 2) if unit_cost > 0 else "",
                "Стоимость": cost,
                "_is_header": False,
            })

        # Пустая строка после каждой группы (кроме последней)
        if idx < len(result_elements) - 1:
            final_rows.append(_empty_row())

    if not final_rows:
        final_rows.append(_empty_row())
        final_rows[0]["Наименование расценки/ресурса"] = "Работы не подобраны"

    df = pd.DataFrame(final_rows, columns=columns)

    # Форматирование денежных колонок: разряды через пробел, 2 знака
    # после точки (например, '392 458.21') — как в режиме КР
    df["Стоимость за Ед. Изм."] = df["Стоимость за Ед. Изм."].apply(format_money)
    df["Стоимость"] = df["Стоимость"].apply(format_money)

    # Итоговая строка «ИТОГО:» — сумма колонки «Стоимость»
    df_for_excel = add_total_row(df.drop(columns=["_is_header"]))

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        df_for_excel.to_excel(writer, sheet_name="Данные", index=False)
        worksheet = writer.sheets["Данные"]

        # Форматируем строки-заголовки групп (жирный шрифт, заливка) — как в КР
        from openpyxl.styles import Alignment, Font, PatternFill

        header_font = Font(bold=True, size=11)
        header_fill = PatternFill(
            start_color="D3D3D3", end_color="D3D3D3", fill_type="solid"
        )
        center_alignment = Alignment(horizontal="center", vertical="center")

        for row_idx in range(2, len(df) + 2):  # +2 из-за заголовка таблицы
            if df.iloc[row_idx - 2]["_is_header"]:
                for col_idx in range(1, len(df_for_excel.columns) + 1):
                    cell = worksheet.cell(row=row_idx, column=col_idx)
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = center_alignment

        # Ширина колонок — как в КР
        worksheet.column_dimensions["A"].width = 15
        worksheet.column_dimensions["B"].width = 60
        worksheet.column_dimensions["C"].width = 10
        worksheet.column_dimensions["D"].width = 15
        worksheet.column_dimensions["E"].width = 18
        worksheet.column_dimensions["F"].width = 15

        # Автофильтр
        worksheet.auto_filter.ref = (
            f"A1:{chr(64 + len(df_for_excel.columns))}{len(df_for_excel) + 1}"
        )

    logger.info(f"Финальный перечень работ (Excel) сохранён: {xlsx_path}")
    return xlsx_path


# ======================================================================
#  CLI для отладки:
#      python -m src.services.works_final_selector <tables.json>
#      <works.json> <run_dir>
# ======================================================================

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(
            "Использование: python -m src.services.works_final_selector "
            "<путь_к_Подобранные_таблицы_работ.json> "
            "<путь_к_Подобранные_работы.json> <папка_запуска>"
        )
        sys.exit(1)

    _tables_path = sys.argv[1]
    _works_path = sys.argv[2]
    _run_dir = sys.argv[3]
    if not os.path.isfile(_tables_path):
        print(f"Файл не найден: {_tables_path}")
        sys.exit(1)
    if not os.path.isfile(_works_path):
        print(f"Файл не найден: {_works_path}")
        sys.exit(1)
    os.makedirs(_run_dir, exist_ok=True)

    _result = select_final_works(_tables_path, _works_path, _run_dir)
    if _result:
        print(f"OK: {_result}")
    else:
        print("Данные для подбора не найдены — шаг не выполнен")
