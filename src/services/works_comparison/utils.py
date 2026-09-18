
from typing import Any
from decimal import Decimal, InvalidOperation
import re
import demjson3
import json
from json_repair import repair_json
from src.core.logger import setup_logger

logger = setup_logger(__name__)

def group_positions(prefix, merge_keys, positions) -> tuple[list[dict], list[str]]:
    result = {}

    add_normalized_keys(prefix, merge_keys, positions)

    normalized_merge_keys = [prefix + v["name"] for v in merge_keys]

    keys = set()

    for position in positions:
        key = tuple(position[k] for k in normalized_merge_keys)
        keys.add(key)

        result.setdefault(key, []).append(position)

    list_result = []
    for group in result:
        list_result.append({
            **dict(zip(normalized_merge_keys, group)),
            "positions": result[group],
        })
    return list_result, normalized_merge_keys

    

def add_normalized_keys(prefix, merge_keys, positions: list[dict]):
    for position in positions:
        for key_object in merge_keys:
            position[prefix + key_object["name"]] = key_object["norm_func"](position[key_object["name"]])


def sum_positions_quantity(positions: list[dict[str, Any]]):
    result = {}
    total = Decimal("0")
    valid_count = 0
    missing_count = 0

    for position in positions:
        quantity = parse_quantity(position["quantity"])

        if quantity is None:
            missing_count += 1
            continue

        total += quantity
        valid_count += 1

    result["totalQuantity"] = (
        float(total) if valid_count else None
    )
    result["positionsWithQuantity"] = valid_count
    result["positionsWithoutQuantity"] = missing_count

    return result


def parse_quantity(value):
        if value is None or value == "":
            return None

        if isinstance(value, str):
            value = value.strip().replace(",", ".")

        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None

def execute_llm_chain(chain, params=None, stub=None):
    response = chain.invoke(params or {})
    return _get_json_from_response(response, stub=stub)

def _get_json_from_response(response, stub = {}):
    """
    Получает json из ответа llm
    """
    def _parse_text(text: str):
        try:
            parsed_json, errors, stats = demjson3.decode(text, strict=False, return_errors=True)
            # Пытаемся конвертировать в json чтобы выявить ошибку. И отлавливаем в try
            json.dumps(parsed_json, ensure_ascii=False, indent=2)
            if errors:
                # Были ошибки
                logger.warning("Во время обработки json исправлены ошибки:\n" + "\n".join(str(e) for e in errors))
                logger.debug(f"Входной текст: \n{text}")

            return parsed_json, None

        except (json.JSONDecodeError, TypeError) as e:
            return None, e
    # Очистка json
    clean_text = response.strip().replace("```json", "").replace("```", "").strip()
    #Убираем висячие запятые
    clean_text = re.sub(r',\s*([\]}])', r'\1', clean_text)

    parsed_json, error = _parse_text(clean_text)
    if parsed_json is None:
        # Пытаемся исправить json
        clean_text = repair_json(clean_text)
        parsed_json, error = _parse_text(clean_text)
        if parsed_json is None:
            logger.error(f"Ошибка парсинга json: {error}")
            return stub
        else:
            return parsed_json
    else:
        return parsed_json