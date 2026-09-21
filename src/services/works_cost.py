"""
Расчёт и форматирование стоимости работ финального перечня (режим КР).

Модуль выделен из legacy fourth_etap.py: содержит только те функции,
которые используются в api_works_lookup.py (подбор работ через API ТСН).
Код функций перенесён без изменений — поведение режима КР не меняется.
"""

import math
import re

import pandas as pd

from src.core.config import load_config
from src.core.logger import setup_logger

logger = setup_logger(__name__)

_cfg = load_config()

KOEFS_FILE = _cfg.KOEFS_PATH
PRICE_COST_FILE = _cfg.PRICE_COST_PATH

_price_cost_lookup_cache = None
_price_material_lookup_cache = None


def safe_float(value, default=0.0):
    """
    Безопасное преобразование значения во float.
    Поддерживает: 123.45, 123,45, 1 234,56 мм, 1.62 м³
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return default
        return float(value)
    if isinstance(value, (list, tuple, dict, set)):
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    if not text or text.lower() in {"nan", "inf", "-inf"}:
        return default
    text = text.replace("\xa0", " ").replace("\t", " ")
    if "," in text and "." in text:
        last_dot = text.rfind(".")
        last_comma = text.rfind(",")
        if last_dot > last_comma:
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        parts = text.split(",")
        if len(parts) == 2:
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
    pattern = r"(-?[\d]+(?:\.[\d]+)?(?:[eE][+-]?[\d]+)?)"
    match = re.search(pattern, text)
    if match:
        try:
            return float(match.group(1))
        except (ValueError, TypeError):
            pass
    return default


def _get_corrected_volume(df):
    koefs = pd.read_excel(KOEFS_FILE)
    df_copy = df.copy()

    if "Шифр ТСН" not in df_copy.columns:
        return df_copy
    if "Наименование расценки/ресурса" not in df_copy.columns:
        return df_copy

    koefs_filtered = koefs[koefs["Шифр ТСН"].isin(df_copy["Шифр ТСН"])].copy()
    resource_col = "Наименование открытой группы ресурсов/\nресурса в составе открытой группы"

    if resource_col not in koefs_filtered.columns:
        return df_copy

    koefs_filtered_by_material = koefs_filtered[
        koefs_filtered[resource_col].isin(df_copy["Наименование расценки/ресурса"])
    ].copy()

    for _, koef_row in koefs_filtered_by_material.iterrows():
        resource_name = koef_row[resource_col]
        matching_indices = df_copy[
            df_copy["Наименование расценки/ресурса"] == resource_name
        ].index

        if len(matching_indices) == 0:
            continue

        df_idx = matching_indices[0]

        if "Объём работ" not in df_copy.columns:
            continue

        norm = safe_float(koef_row.get("Норма расхода", 0))
        volume = safe_float(df_copy.loc[df_idx, "Объём работ"])

        if norm > 100:
            df_copy.loc[df_idx, "Объём работ"] = volume * norm / 100
        elif norm:
            df_copy.loc[df_idx, "Объём работ"] = volume * norm

    return df_copy


def _get_price_material_cost():
    global _price_material_lookup_cache

    if _price_material_lookup_cache is not None:
        return _price_material_lookup_cache

    try:
        df = pd.read_excel(PRICE_COST_FILE, sheet_name="_Связаанные_ресурсы_select_wp_p")
        _price_material_lookup_cache = dict(
            zip(df["Шифр ресурса"], df["Сметная цена текущая"])
        )
        logger.info("Загружено %s расценок из price_cost.xlsx (ресурсы)", len(_price_material_lookup_cache))
    except Exception as e:
        logger.error("Ошибка загрузки price_cost.xlsx (ресурсы): %s", e)
        _price_material_lookup_cache = {}

    return _price_material_lookup_cache


def _get_price_cost_lookup():
    global _price_cost_lookup_cache

    if _price_cost_lookup_cache is not None:
        return _price_cost_lookup_cache

    try:
        df = pd.read_excel(PRICE_COST_FILE, sheet_name="_Получние_параметры_позиции_sel")
        _price_cost_lookup_cache = dict(
            zip(df["Шифр расценки"], df["Текущие прямые затраты/Всего затр"])
        )
        logger.info("Загружено %s расценок из price_cost.xlsx (расценки)", len(_price_cost_lookup_cache))
    except Exception as e:
        logger.error("Ошибка загрузки price_cost.xlsx (расценки): %s", e)
        _price_cost_lookup_cache = {}

    return _price_cost_lookup_cache


def _add_cost_column(df):
    lookup = _get_price_cost_lookup()
    lookup_material = _get_price_material_cost()

    if "Шифр ТСН" not in df.columns or "Объём работ" not in df.columns:
        logger.warning("Не найдены колонки 'Шифр ТСН' или 'Объём работ'")
        df["Стоимость за Ед. Изм."] = ""
        df["Стоимость"] = ""
        return df

    def lookup_price(shifr_clean):
        # Проверяем основную таблицу расценок
        if shifr_clean in lookup:
            return lookup[shifr_clean]
        
        # Проверяем таблицу ресурсов
        if shifr_clean in lookup_material:
            return lookup_material[shifr_clean]
        
        # Пробуем с префиксом "3."
        if not shifr_clean.startswith("3."):
            alt = "3." + shifr_clean
            if alt in lookup:
                return lookup[alt]
            if alt in lookup_material:
                return lookup_material[alt]
        
        # Пробуем без префикса "3."
        if shifr_clean.startswith("3."):
            alt = shifr_clean[2:]
            if alt in lookup:
                return lookup[alt]
            if alt in lookup_material:
                return lookup_material[alt]
        
        # Специальные случаи (если нужно)
        if "1.7-4-2" in shifr_clean:
            return 342.51
        if "1.7-4-3" in shifr_clean:
            return 290.82
        if "1.1-1-3" in shifr_clean:
            return 104.98
        if "1.1-1-37" in shifr_clean:
            return 104.98
        
        return None

    def calculate_cost(row):
        shifr = row.get("Шифр ТСН")
        volume = row.get("Объём работ")

        if pd.isna(shifr) or pd.isna(volume):
            return "", ""

        try:
            shifr_clean = str(shifr).strip()
            price = lookup_price(shifr_clean)
            
            if price is None:
                return "", ""
            
            # Цена за единицу измерения
            unit_price = float(price)
            
            # Общая стоимость
            cost = unit_price * float(volume)
            
            return round(unit_price, 2), round(cost, 2)
        except (ValueError, TypeError):
            return "", ""

    # Создаем две колонки
    df[["Стоимость за Ед. Изм.", "Стоимость"]] = df.apply(
        lambda row: pd.Series(calculate_cost(row)), axis=1
    )
    
    # Заполняем пустые значения
    df["Стоимость за Ед. Изм."] = df["Стоимость за Ед. Изм."].fillna("")
    df["Стоимость"] = df["Стоимость"].fillna("")
    
    return df


def format_money(value) -> str:
    """Форматирует денежное значение для финального перечня работ.

    Разряды тысяч разделяются пробелом, после точки — ровно 2 знака.
    Пример: 392458.206 -> '392 458.21'. Для пустых/невалидных/нулевых
    значений возвращается пустая строка.
    """
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return ""
        num = float(str(value).replace(" ", "").replace("\u00a0", ""))
    except (ValueError, TypeError):
        return ""
    if num != num or num in (float("inf"), float("-inf")):
        return ""
    if num <= 0:
        return ""
    return f"{num:,.2f}".replace(",", " ")


def _parse_money(value) -> float:
    """Преобразует отформатированное денежное значение обратно во float."""
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return 0.0
        num = float(str(value).replace(" ", "").replace("\u00a0", ""))
        if num != num or num in (float("inf"), float("-inf")):
            return 0.0
        return num
    except (ValueError, TypeError):
        return 0.0


def add_total_row(df):
    """Добавляет последней строкой 'ИТОГО:' с суммами денежных колонок.

    Суммируются колонки 'ЗП', 'ЭМ', 'МР' и 'Стоимость' (те, что есть
    в таблице).

    Аргументы:
        df — DataFrame финального перечня работ (денежные колонки уже
             отформатированы format_money).

    Возвращает:
        DataFrame с добавленной итоговой строкой (или исходный, если
        ни одной денежной колонки нет).
    """
    money_columns = [col for col in ("ЗП", "ЭМ", "МР", "Стоимость") if col in df.columns]
    if not money_columns:
        return df

    total_row = {col: "" for col in df.columns}
    label_col = (
        "Наименование расценки/ресурса"
        if "Наименование расценки/ресурса" in df.columns
        else df.columns[0]
    )
    total_row[label_col] = "ИТОГО:"
    for col in money_columns:
        total = sum(_parse_money(v) for v in df[col])
        total_row[col] = format_money(total)

    return pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)
