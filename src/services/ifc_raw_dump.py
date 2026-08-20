"""
Выгрузка СЫРЫХ данных всех элементов IFC в XLSX.

Формирует таблицу, где каждая строка — один элемент (IfcElement),
а колонки — всевозможные параметры элемента в том виде, в котором
они присутствуют в IFC-файле:

  * все прямые атрибуты элемента (GlobalId, Name, ObjectType, Tag, ...);
  * свойства из Property Sets  ->  "Свойство::{PsetName}::{PropName}";
  * количественные характеристики QTO  ->  "QTO::{QtoName}::{QtyName}";
  * материал и этаж из связей.

Значения НЕ нормализуются и НЕ округляются: числа остаются числами,
текст — текстом, а ссылки на другие сущности приводятся к виду
``#123=IfcType(Атрибут=значение, ...)`` (с ограничением глубины,
чтобы избежать бесконечной рекурсии по циклическим ссылкам).
"""

import os

import ifcopenshell
import pandas as pd

from src.core.logger import setup_logger

logger = setup_logger(__name__)

# Имя файла выгрузки в корне папки сессии.
RAW_DUMP_FILENAME = "IFC_исходные_параметры.xlsx"
# JSON-копия сырого дампа — используется для группировки в режиме АР
# (быстрее читать, чем XLSX, и содержит колонку материалов
#  «Свойство::IfcMaterialLayer::Name»).
RAW_DUMP_JSON_FILENAME = "IFC_исходные_параметры.json"

# Максимальная глубина раскрытия вложенных сущностей в строковом виде.
# 0 — только ``#id=IfcType``; 1 — атрибуты первого уровня; и т.д.
_RAW_DEPTH_LIMIT = 2

# Лимит длины текста в одной ячейке Excel (с запасом под лимит openpyxl/Excel).
_CELL_LIMIT = 32000


# =====================================================================
#  СЕРИАЛИЗАЦИЯ ЗНАЧЕНИЙ
# =====================================================================

def _truncate(value):
    """Обрезает слишком длинные строки, чтобы не превышать лимит ячейки Excel."""
    if isinstance(value, str) and len(value) > _CELL_LIMIT:
        return value[:_CELL_LIMIT] + "…(обрезано)"
    return value


def _serialize_raw(value, depth=0, seen=None):
    """Приводит значение IFC к «сырому» представлению без нормализации.

    - None                          -> ""
    - число/строка/bool             -> как есть
    - IfcLabel/IfcLengthMeasure/... -> распакованный wrappedValue
    - прочая сущность               -> "#id=IfcType(Атрибут=..., ...)"
    - list/tuple                    -> "(v1, v2, ...)"
    """
    if value is None:
        return ""

    # bool — отдельная ветка, т.к. bool является подклассом int
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value

    if isinstance(value, str):
        return value

    if isinstance(value, ifcopenshell.entity_instance):
        wrapped = getattr(value, "wrappedValue", None)
        if wrapped is not None:
            return _serialize_raw(wrapped, depth + 1, seen)

        eid = value.id()
        if seen is None:
            seen = set()
        # Циклическая ссылка или достигнут лимит глубины — обрезаем.
        if eid in seen or depth >= _RAW_DEPTH_LIMIT:
            return f"#{eid}={value.is_a()}"

        parts = []
        new_seen = seen | {eid}
        try:
            nested_info = value.get_info()
        except Exception:
            nested_info = {}
        for attr_name, attr_value in nested_info.items():
            # 'id' и 'type' — служебные поля get_info(), не атрибуты IFC.
            if attr_name in ("id", "type"):
                continue
            if attr_value is None:
                continue
            serialized = _serialize_raw(attr_value, depth + 1, new_seen)
            if serialized == "" or serialized is None:
                continue
            parts.append(f"{attr_name}={serialized}")

        if parts:
            return f"#{eid}={value.is_a()}(" + ", ".join(parts) + ")"
        return f"#{eid}={value.is_a()}"

    if isinstance(value, (list, tuple)):
        parts = [
            str(_serialize_raw(item, depth + 1, seen))
            for item in value
            if _serialize_raw(item, depth + 1, seen) != ""
        ]
        return "(" + ", ".join(parts) + ")" if parts else ""

    return str(value)


# =====================================================================
#  СВОЙСТВА И КОЛИЧЕСТВА (Property Sets / QTO)
# =====================================================================

def _extract_property_value(prop):
    """Извлекает значение из свойства IfcProperty* без нормализации."""
    try:
        if prop.is_a("IfcPropertySingleValue"):
            if prop.NominalValue is not None:
                return _serialize_raw(prop.NominalValue)
            return ""

        if prop.is_a("IfcPropertyEnumeratedValue"):
            vals = prop.EnumerationValues
            if vals:
                parts = [str(_serialize_raw(v)) for v in vals]
                return "; ".join(p for p in parts if p)
            return ""

        if prop.is_a("IfcPropertyListValue"):
            vals = prop.ListValues
            if vals:
                parts = [str(_serialize_raw(v)) for v in vals]
                return "; ".join(p for p in parts if p)
            return ""

        if prop.is_a("IfcPropertyBoundedValue"):
            lo = _serialize_raw(getattr(prop, "LowerBoundValue", None))
            hi = _serialize_raw(getattr(prop, "UpperBoundValue", None))
            sp = _serialize_raw(getattr(prop, "SetPointValue", None))
            res = []
            if lo != "":
                res.append(f"min={lo}")
            if hi != "":
                res.append(f"max={hi}")
            if sp != "":
                res.append(f"set={sp}")
            return ", ".join(res)

        if prop.is_a("IfcPropertyTableValue"):
            defining = prop.DefiningValues or []
            defined = prop.DefinedValues or []
            pairs = []
            for d, v in zip(defining, defined):
                pairs.append(f"{_serialize_raw(d)}={_serialize_raw(v)}")
            return "; ".join(pairs)

        if prop.is_a("IfcPropertyReferenceValue"):
            ref = getattr(prop, "PropertyReference", None)
            return _serialize_raw(ref)

        if prop.is_a("IfcComplexProperty"):
            # Вложенные свойства разворачиваем в "Name=value; ...".
            parts = []
            for sub in getattr(prop, "HasProperties", []) or []:
                sub_name = getattr(sub, "Name", None) or ""
                parts.append(f"{sub_name}={_extract_property_value(sub)}")
            return "; ".join(parts)
    except Exception as exc:
        logger.debug(f"Ошибка извлечения значения свойства {getattr(prop, 'Name', '?')}: {exc}")

    return ""


_QTY_ATTRS = {
    "IfcQuantityLength": "LengthValue",
    "IfcQuantityArea": "AreaValue",
    "IfcQuantityVolume": "VolumeValue",
    "IfcQuantityCount": "CountValue",
    "IfcQuantityWeight": "WeightValue",
    "IfcQuantityTime": "TimeValue",
}


def _extract_quantity_value(qty):
    """Извлекает значение из IfcQuantity* без нормализации."""
    try:
        for qtype, attr in _QTY_ATTRS.items():
            if qty.is_a(qtype):
                val = getattr(qty, attr, None)
                return _serialize_raw(val)
    except Exception as exc:
        logger.debug(f"Ошибка извлечения значения количества {getattr(qty, 'Name', '?')}: {exc}")
    return ""


def _collect_propsets(element):
    """Собирает свойства (Pset) и количества (QTO) элемента в виде словаря.

    Ключи: ``Свойство::{PsetName}::{PropName}`` и ``QTO::{QtoName}::{QtyName}``.
    """
    result = {}
    try:
        if not hasattr(element, "IsDefinedBy"):
            return result

        for rel in element.IsDefinedBy:
            if not rel.is_a("IfcRelDefinesByProperties"):
                continue
            pset = rel.RelatingPropertyDefinition
            if pset is None:
                continue

            set_name = getattr(pset, "Name", None)
            set_name = str(set_name) if set_name else ""

            # Property Set
            if pset.is_a("IfcPropertySet"):
                for prop in getattr(pset, "HasProperties", []) or []:
                    prop_name = getattr(prop, "Name", None)
                    if not prop_name:
                        continue
                    key = f"Свойство::{set_name}::{prop_name}"
                    result[key] = _extract_property_value(prop)

            # Element Quantity (QTO)
            elif pset.is_a("IfcElementQuantity"):
                for qty in getattr(pset, "Quantities", []) or []:
                    qty_name = getattr(qty, "Name", None)
                    if not qty_name:
                        continue
                    key = f"QTO::{set_name}::{qty_name}"
                    result[key] = _extract_quantity_value(qty)
    except Exception as exc:
        logger.debug(f"Ошибка сбора Pset/QTO для элемента: {exc}")

    return result


# =====================================================================
#  МАТЕРИАЛ И ЭТАЖ
# =====================================================================

def _get_material(element):
    """Извлекает материал элемента (без нормализации названий)."""
    try:
        if not hasattr(element, "HasAssociations"):
            return ""
        for rel in element.HasAssociations:
            if not rel.is_a("IfcRelAssociatesMaterial"):
                continue
            mat = rel.RelatingMaterial
            if mat is None:
                continue

            if mat.is_a("IfcMaterial"):
                return str(getattr(mat, "Name", "") or "")

            if mat.is_a("IfcMaterialLayerSetUsage"):
                layerset = getattr(mat, "ForLayerSet", None)
                if layerset and getattr(layerset, "MaterialLayers", None):
                    names = []
                    for layer in layerset.MaterialLayers:
                        if layer.Material:
                            nm = getattr(layer.Material, "Name", None)
                            if nm:
                                names.append(str(nm))
                    if names:
                        return ", ".join(names)
                return ""

            if mat.is_a("IfcMaterialLayerSet"):
                names = []
                for layer in getattr(mat, "MaterialLayers", []) or []:
                    if layer.Material:
                        nm = getattr(layer.Material, "Name", None)
                        if nm:
                            names.append(str(nm))
                return ", ".join(names) if names else ""

            if mat.is_a("IfcMaterialList"):
                names = []
                for m in getattr(mat, "Materials", []) or []:
                    nm = getattr(m, "Name", None)
                    if nm:
                        names.append(str(nm))
                return ", ".join(names) if names else ""
    except Exception as exc:
        logger.debug(f"Ошибка извлечения материала: {exc}")
    return ""


def _get_storey(element):
    """Возвращает (имя_этажа, отметка_этажа) без нормализации."""
    try:
        if not hasattr(element, "ContainedInStructure"):
            return "", ""
        for rel in element.ContainedInStructure:
            if not rel.is_a("IfcRelContainedInSpatialStructure"):
                continue
            container = rel.RelatingStructure
            if container is None or not container.is_a("IfcBuildingStorey"):
                continue
            name = getattr(container, "Name", None)
            elevation = getattr(container, "Elevation", None)
            return (
                str(name) if name is not None else "",
                _serialize_raw(elevation) if elevation is not None else "",
            )
    except Exception as exc:
        logger.debug(f"Ошибка извлечения этажа: {exc}")
    return "", ""


# =====================================================================
#  СБОР ДАННЫХ ЭЛЕМЕНТА
# =====================================================================

def _collect_element_data(element):
    """Собирает все параметры одного элемента в словарь (колонка -> значение)."""
    row = {}

    try:
        element_info = element.get_info()
    except Exception:
        element_info = {}

    # Служебная колонка: конкретный IFC-класс элемента.
    row["IfcClass"] = element_info.get("type", element.is_a())

    # Все прямые атрибуты элемента — как в IFC.
    for attr_name, attr_value in element_info.items():
        # 'id' и 'type' — служебные поля get_info(), не атрибуты IFC.
        if attr_name in ("id", "type"):
            continue
        row[attr_name] = _truncate(_serialize_raw(attr_value))

    # Материал (из IfcRelAssociatesMaterial).
    row["Материал"] = _get_material(element)

    # Этаж и отметка этажа (из IfcRelContainedInSpatialStructure).
    storey_name, storey_elev = _get_storey(element)
    row["Этаж"] = storey_name
    row["Отметка_этажа"] = storey_elev

    # Свойства (Pset) и количества (QTO).
    for key, value in _collect_propsets(element).items():
        row[key] = _truncate(value)

    return row


# =====================================================================
#  ГЛАВНАЯ ФУНКЦИЯ
# =====================================================================

def dump_ifc_elements_raw(ifc_file: str, output_folder: str) -> str:
    """Сохраняет XLSX со всеми элементами IFC и их сырыми параметрами.

    Args:
        ifc_file: путь к IFC-файлу.
        output_folder: папка, куда сохранить результат (корень сессии).

    Returns:
        Путь к созданному XLSX-файлу или пустая строка, если элементов нет.
    """
    logger.info(f"Выгрузка сырых параметров элементов IFC: {ifc_file}")

    if not os.path.exists(ifc_file):
        logger.error(f"IFC файл не найден: {ifc_file}")
        return ""

    if not output_folder:
        output_folder = os.getcwd()
    os.makedirs(output_folder, exist_ok=True)

    model = ifcopenshell.open(ifc_file)

    elements = model.by_type("IfcElement")
    logger.info(f"Найдено элементов IfcElement: {len(elements)}")

    if not elements:
        logger.warning("В IFC-файле не найдено ни одного элемента IfcElement")
        return ""

    rows = []
    all_columns = []
    seen_columns = set()

    for element in elements:
        row = _collect_element_data(element)
        rows.append(row)
        # Накапливаем колонки в порядке первого появления.
        for key in row.keys():
            if key not in seen_columns:
                seen_columns.add(key)
                all_columns.append(key)

    # Строим полные строки: у элементов, у которых параметр отсутствует,
    # ставим пустую строку. Так DataFrame не приводит bool/числа к float
    # и не подставляет NaN вместо отсутствующих значений.
    full_rows = [
        {col: row.get(col, "") for col in all_columns}
        for row in rows
    ]
    df = pd.DataFrame(full_rows)
    # Пустые значения приводим к пустой строке (а не NaN).
    df = df.fillna("").map(_truncate)

    output_path = os.path.join(output_folder, RAW_DUMP_FILENAME)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Сырые_данные", index=False)

    # JSON-копия дампа: используется для группировки в режиме АР.
    try:
        json_path = os.path.join(output_folder, RAW_DUMP_JSON_FILENAME)
        df.to_json(json_path, orient="records", force_ascii=False, index=False)
        logger.info(f"Сохранён JSON-дамп сырых данных: {json_path} "
                    f"({os.path.getsize(json_path) / 1024:.0f} KB)")
    except Exception as exc:
        logger.warning(f"Не удалось сохранить JSON-дамп сырых данных: {exc}")

    logger.info(
        f"Сохранён файл сырых данных: {output_path} "
        f"(элементов: {len(df)}, колонок: {len(df.columns)})"
    )
    return output_path
