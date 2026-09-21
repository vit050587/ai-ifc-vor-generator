"""
Заполнение универсального шаблона параметров подбора работ для каждого элемента.

Шаблон — перечень параметров из ``data/selection_parameters.json`` (3 глава
перечня ВОР из perechen_kr_1.xlsx: геометрия, положение в здании, технология,
оборудование). Соответствие «параметр шаблона → ключи сырого дампа IFC» задаётся
расширяемым справочником ``data/selection_parameters_mapping.json``.

Вход — сырой дамп параметров элементов ``IFC_исходные_параметры.json``
(создаётся на этапе 0 модулем ``ifc_raw_dump`` в корне сессии).

Выход — ``Параметры_подбора_элементов.json`` в корне сессии: массив заполненных
шаблонов (по одному на каждый элемент модели) + метаданные и константы проекта.
Раздел ``constants`` содержит **только константы схемы** подбора работ
(``works_classification.json`` — те же 9 констант, что отображаются в
веб-интерфейсе в блоке «Параметры влияющие на расценки»; ненайденные —
``null``), значения записываются в виде **выбранных вариантов** из списков
``values`` схемы (числовые значения переводятся в диапазоны — как
автоподстановка в веб-интерфейсе).

Приоритет заполнения параметра (см. ``_resolution`` в карте соответствия):

  1. ``raw_keys`` — явные ключи сырого дампа (по порядку, первое непустое);
  2. ``keywords`` — fallback-поиск первого непустого значения по подстроке
     в ключе (служебные ключи OwnerHistory/ObjectPlacement/Representation
     игнорируются, ``exclude_keywords`` отсекают ложные совпадения);
  3. ``computed`` — вычисление из других параметров элемента
     (``building_part``, ``wall_location``, ``layers_count``,
     ``min_section_side``);
  4. ``constant`` — константа проекта: переданные константы (UI/globalConstants)
      → IFC (``IFC_глобальные_константы.json``; для ``floor_height`` — fallback
      на ``height.txt`` в старых сессиях) → ПОС
      (``ПОС_глобальные_константы.json``) → ПЗ
      (``ПЗ_глобальные_константы.json``).

Значения из ключей ``QTO_bbox::*_мм`` автоматически переводятся в метры;
значения QTO берутся как есть (единицы следуют единицам проекта IFC).
"""

import json
import os
from datetime import datetime

from src.core.logger import setup_logger

logger = setup_logger(__name__)

# Справочники (data/ — только чтение).
DATA_DIR = os.path.join("data")
TEMPLATE_SCHEMA_PATH = os.path.join(DATA_DIR, "selection_parameters.json")
MAPPING_PATH = os.path.join(DATA_DIR, "selection_parameters_mapping.json")

# Файлы сессии (вход и выход) — корень сессии outputs/<session_id>/.
RAW_DUMP_JSON_FILENAME = "IFC_исходные_параметры.json"
POS_CONSTANTS_FILENAME = "ПОС_глобальные_константы.json"
PZ_CONSTANTS_FILENAME = "ПЗ_глобальные_константы.json"
IFC_CONSTANTS_FILENAME = "IFC_глобальные_константы.json"
HEIGHT_FILENAME = "height.txt"
OUTPUT_FILENAME = "Параметры_подбора_элементов.json"

# Служебные ключи сырого дампа — не участвуют в fallback-поиске по ключевым
# словам (длинные сериализованные сущности, не параметры подбора).
_SERVICE_KEYS = {"OwnerHistory", "ObjectPlacement", "Representation",
                 "GlobalId", "Tag"}


# =====================================================================
#  ЗАГРУЗКА СПРАВОЧНИКОВ
# =====================================================================

def _load_json(path):
    """Читает JSON-файл в кодировке UTF-8."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_template_schema(path=None):
    """Перечень параметров шаблона из data/selection_parameters.json.

    Возвращает список имён параметров в порядке следования категорий.
    """
    data = _load_json(path or TEMPLATE_SCHEMA_PATH)
    names = []
    for category in data.get("categories", []):
        for param in category.get("parameters", []):
            if param.get("name"):
                names.append(param["name"])
    return names


def load_mapping(path=None):
    """Карта соответствия «параметр шаблона → источники» (data/selection_parameters_mapping.json)."""
    return _load_json(path or MAPPING_PATH).get("parameters", {})


# =====================================================================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================================

def _is_empty(value):
    """Пустое значение (None или пустая строка)."""
    return value is None or value == ""


def _to_number(value):
    """Приводит значение к float (в т.ч. строку с запятой-разделителем).

    Возвращает None, если значение не числовое.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def _convert_bbox_mm(value, source_key):
    """Ключи QTO_bbox::*_мм содержат миллиметры — переводит значение в метры."""
    num = _to_number(value)
    if num is None or "QTO_bbox" not in source_key or not source_key.endswith("_мм"):
        return value
    return round(num / 1000.0, 4)


# =====================================================================
#  КОНСТАНТЫ ПРОЕКТА (UI / IFC / ПОС)
# =====================================================================

def _load_pos_constants(session_dir):
    """ПОС_глобальные_константы.json → {имя: значение} (пустые отбрасываются)."""
    return _load_doc_constants(session_dir, POS_CONSTANTS_FILENAME)


def _load_pz_constants(session_dir):
    """ПЗ_глобальные_константы.json → {имя: значение} (пустые отбрасываются)."""
    return _load_doc_constants(session_dir, PZ_CONSTANTS_FILENAME)


def _load_doc_constants(session_dir, filename):
    """JSON-файл глобальных констант (ПОС/ПЗ) → {имя: значение}."""
    path = os.path.join(session_dir, filename)
    if not os.path.isfile(path):
        return {}
    try:
        data = _load_json(path)
    except Exception as exc:
        logger.warning(f"Не удалось прочитать {path}: {exc}")
        return {}

    result = {}
    for name, info in (data.get("constants") or {}).items():
        value = info.get("value") if isinstance(info, dict) else info
        if _is_empty(value) or str(value) in ("None", "False"):
            continue
        result[name] = value
    return result


def _load_ifc_constants(session_dir):
    """Глобальные константы IFC из IFC_глобальные_константы.json (этап 0).

    Возвращает {имя константы: значение} — только непустые числовые значения.
    """
    path = os.path.join(session_dir, IFC_CONSTANTS_FILENAME)
    if not os.path.isfile(path):
        return {}
    try:
        data = _load_json(path)
    except Exception as exc:
        logger.warning(f"Не удалось прочитать {path}: {exc}")
        return {}

    result = {}
    for name, info in (data.get("constants") or {}).items():
        value = info.get("value") if isinstance(info, dict) else info
        num = _to_number(value)
        if num is None or num <= 0:
            continue
        result[name] = value
    return result


def _load_floor_height_ifc(session_dir):
    """Высота основного (типового) этажа из height.txt (этап 0, zero_step).

    Legacy-источник: используется только если файла
    IFC_глобальные_константы.json нет (старые сессии).
    """
    path = os.path.join(session_dir, HEIGHT_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return _to_number(f.read().strip())
    except Exception as exc:
        logger.warning(f"Не удалось прочитать {path}: {exc}")
        return None


def _load_constants_schema():
    """Схема глобальных констант подбора работ (works_classification.json).

    Тот же перечень констант, что отображается в веб-интерфейсе в блоке
    «Параметры влияющие на расценки» (эндпоинт works_constants →
    works_table_selector.get_constants_schema).
    """
    from src.services.works_table_selector import get_constants_schema

    try:
        return get_constants_schema()
    except Exception as exc:
        logger.warning(f"Не удалось загрузить схему констант подбора работ: {exc}")
        return []


def _resolve_constants(session_dir, project_constants=None):
    """Собирает константы проекта — только константы схемы подбора работ.

    В раздел ``constants`` входят **все** константы схемы
    ``works_classification.json`` (``global_constants`` — те же константы,
    что отображаются в веб-интерфейсе в блоке «Параметры влияющие на
    расценки»), даже если значение нигде не найдено (тогда ``None``).

    Приоритет значений: переданные константы (UI/globalConstants) → IFC
    (``IFC_глобальные_константы.json``; для floor_height — fallback на
    height.txt в старых сессиях) → ПОС (``ПОС_глобальные_константы.json``)
    → ПЗ (``ПЗ_глобальные_константы.json``; пояснительная записка —
    константы, не найденные в IFC и ПОС).
    """
    # ПЗ — базовый источник (низший приоритет): перекрывается константами
    # ПОС, затем IFC (документированный порядок: UI → IFC → ПОС → ПЗ).
    constants = _load_pz_constants(session_dir)
    constants.update(_load_pos_constants(session_dir))

    # Константы, определённые по модели IFC — приоритет выше ПОС и ПЗ.
    constants.update(_load_ifc_constants(session_dir))

    # Legacy-fallback: height.txt (только высота основного этажа).
    if constants.get("floor_height") is None:
        floor_height = _load_floor_height_ifc(session_dir)
        if floor_height is not None and floor_height > 0:
            constants.setdefault("floor_height", floor_height)

    # Константы от вызывающего кода (UI/globalConstants) — высший приоритет.
    for name, value in (project_constants or {}).items():
        if not _is_empty(value):
            constants[name] = value

    # Полный перечень: константы схемы UI (в порядке схемы), включая
    # не найденные (значение None).
    result = {}
    for const in _load_constants_schema():
        name = const.get("name")
        if name:
            result[name] = constants.get(name)
    return result


# =====================================================================
#  ФОРМАТИРОВАНИЕ ЗНАЧЕНИЙ КОНСТАНТ (как в веб-интерфейсе)
# =====================================================================
# Числовое значение константы → вариант из списка значений схемы
# (values в works_classification.json). Аналоги функций веб-интерфейса
# (heightToRange, floorHeightToValue, craneCapacityToValue,
# bucketCapacityToValue, equipmentPowerToValue в src/templates/index.html).

def _height_to_range(h):
    """Высота здания (м) → диапазон из значений схемы."""
    if h <= 30:
        return "до 30"
    if h <= 40:
        return "30-40"
    if h <= 57:
        return "40-57"
    if h <= 75:
        return "57-75"
    if h <= 105:
        return "75-105"
    if h <= 150:
        return "105-150"
    if h <= 250:
        return "150-250"
    return "свыше 200 (шпиль)"


def _floor_height_to_value(h):
    """Высота этажа (м) → вариант из значений схемы."""
    if h <= 3.6:
        return "до 3,6 м"
    if h <= 4:
        return "до 4 м"
    if h <= 6:
        return "до 6 м"
    return "свыше 6 м"


def _crane_capacity_to_value(t):
    """Грузоподъемность (т) → вариант из значений схемы."""
    if t <= 5:
        return "до 5 т"
    if t <= 10:
        return "до 10 т"
    if t <= 16:
        return "до 16 т"
    if t <= 20:
        return "до 20 т"
    return "свыше 20 т"


def _bucket_capacity_to_value(v):
    """Вместимость ковша (м3) → вариант из значений схемы."""
    if v <= 0.25:
        return "до 0,25 м3"
    if v <= 0.5:
        return "до 0,5 м3"
    if v <= 1:
        return "до 1 м3"
    if v <= 1.8:
        return "до 1,8 м3"
    return "3-15 м3 (скрепер)"


def _equipment_power_to_value(p):
    """Мощность оборудования (кВт) → вариант из значений схемы."""
    if p <= 30:
        return "до 30 кВт"
    if p <= 59:
        return "59 кВт"
    if p <= 79:
        return "79 кВт"
    if p <= 96:
        return "96 кВт"
    if p <= 132:
        return "132 кВт"
    return "свыше 132 кВт"


# Числовые константы: имя → функция перевода числа в вариант схемы.
_NUMERIC_FORMATTERS = {
    "building_height_m": _height_to_range,
    "floor_height": _floor_height_to_value,
    "crane_capacity": _crane_capacity_to_value,
    "bucket_capacity": _bucket_capacity_to_value,
    "equipment_power": _equipment_power_to_value,
}


def _format_constants(constants, schema):
    """Приводит значения констант к вариантам из списков схемы (как в UI).

    Числовые значения переводятся в вариант из ``values`` схемы (аналогично
    автоподстановке в веб-интерфейсе); строковые значения, найденные по
    IFC/ПОС/выбранные в UI, передаются как есть.
    """
    formatted = {}
    for const in schema:
        name = const.get("name")
        if not name:
            continue
        value = constants.get(name)
        if _is_empty(value):
            formatted[name] = None
            continue
        formatter = _NUMERIC_FORMATTERS.get(name)
        if formatter:
            num = _to_number(value)
            if num is not None and num > 0:
                formatted[name] = formatter(num)
                continue
        formatted[name] = value
    return formatted


# =====================================================================
#  ВЫЧИСЛЯЕМЫЕ ПАРАМЕТРЫ (computed)
# =====================================================================

def _compute_building_part(element):
    """Часть здания по этажу элемента: Подземная / Цокольная / Надземная."""
    storey = str(element.get("Этаж") or "").lower()
    if any(word in storey for word in ("подвал", "подзем", "basement")):
        return "Подземная"
    if "цоколь" in storey:
        return "Цокольная"
    return "Надземная"


def _compute_wall_location(element):
    """Стена наружная/внутренняя по Pset_WallCommon::IsExternal."""
    value = element.get("Свойство::Pset_WallCommon::IsExternal")
    if isinstance(value, bool):
        return "наружная" if value else "внутренняя"
    if isinstance(value, str) and value.strip():
        s = value.strip().lower()
        if s in ("true", "да", "1", "наружная", "наружн"):
            return "наружная"
        if s in ("false", "нет", "0", "внутренняя", "внутр"):
            return "внутренняя"
    return None


def _compute_layers_count(element):
    """Число слоёв материала (IfcMaterialLayer / колонка «Материал»)."""
    for key in ("Свойство::IfcMaterialLayer::Name", "Материал"):
        value = element.get(key)
        if _is_empty(value):
            continue
        names = [part.strip() for part in str(value).split(",") if part.strip()]
        if names:
            return len(names)
    return None


def _compute_min_section_side(element):
    """Наименьшая сторона поперечного сечения, мм.

    Сначала — минимум из габаритов QTO_bbox (мм); fallback — минимум из
    ширины/толщины QTO (единицы проекта IFC, обычно мм).
    """
    dims = []
    for key in ("QTO_bbox::Длина_мм", "QTO_bbox::Ширина_мм", "QTO_bbox::Высота_мм"):
        num = _to_number(element.get(key))
        if num is not None and num > 0:
            dims.append(num)
    if dims:
        return round(min(dims), 1)

    sides = []
    for key in ("QTO::Qto_WallBaseQuantities::Width",
                "QTO::Qto_SlabBaseQuantities::Depth",
                "QTO::Qto_PlateBaseQuantities::Width"):
        num = _to_number(element.get(key))
        if num is not None and num > 0:
            sides.append(num)
    if sides:
        return round(min(sides), 1)
    return None


# Вычисляемые параметры: имя в карте соответствия → функция(element).
_COMPUTED = {
    "building_part": _compute_building_part,
    "wall_location": _compute_wall_location,
    "layers_count": _compute_layers_count,
    "min_section_side": _compute_min_section_side,
}


# =====================================================================
#  ЗАПОЛНЕНИЕ ШАБЛОНА
# =====================================================================

def _fill_element_parameters(element, template_params, mapping, constants):
    """Заполняет шаблон параметров для одного элемента сырого дампа.

    Возвращает словарь {имя параметра: {value, unit, origin, source_key}},
    где origin — element | computed | constant | not_found.
    """
    parameters = {}

    for name in template_params:
        rule = mapping.get(name) or {}
        unit = rule.get("unit") or ""
        filled = None

        # 1. Явные ключи сырого дампа (по порядку, первое непустое).
        for key in rule.get("raw_keys", []):
            value = element.get(key)
            if not _is_empty(value):
                filled = {
                    "value": _convert_bbox_mm(value, key),
                    "unit": unit,
                    "origin": "element",
                    "source_key": key,
                }
                break

        # 2. Ключевые слова — fallback-поиск по ключам сырого дампа.
        if filled is None:
            keywords = [k.lower() for k in rule.get("keywords", [])]
            if keywords:
                exclude = [k.lower() for k in rule.get("exclude_keywords", [])]
                for key, value in element.items():
                    if _is_empty(value) or key in _SERVICE_KEYS:
                        continue
                    key_l = key.lower()
                    if any(x in key_l for x in exclude):
                        continue
                    if any(k in key_l for k in keywords):
                        filled = {
                            "value": _convert_bbox_mm(value, key),
                            "unit": unit,
                            "origin": "element",
                            "source_key": key,
                        }
                        break

        # 3. Вычисляемые параметры.
        if filled is None:
            computed_name = rule.get("computed")
            if computed_name and computed_name in _COMPUTED:
                value = _COMPUTED[computed_name](element)
                if value is not None:
                    filled = {
                        "value": value,
                        "unit": unit,
                        "origin": "computed",
                        "source_key": computed_name,
                    }

        # 4. Константы проекта (UI → IFC → ПОС, собраны в constants).
        if filled is None and rule.get("source") == "constant":
            for const_name in rule.get("constant_names", []):
                if const_name in constants and not _is_empty(constants[const_name]):
                    filled = {
                        "value": constants[const_name],
                        "unit": unit,
                        "origin": "constant",
                        "source_key": const_name,
                    }
                    break

        if filled is None:
            filled = {"value": None, "unit": unit, "origin": "not_found",
                      "source_key": ""}

        parameters[name] = filled

    return parameters


# =====================================================================
#  ОСНОВНАЯ ФУНКЦИЯ
# =====================================================================

def build_selection_parameters(session_dir, output_path=None,
                               project_constants=None,
                               mapping_path=None, template_path=None):
    """Строит Параметры_подбора_элементов.json по всем элементам сырого дампа.

    Args:
        session_dir: корень сессии (там лежат IFC_исходные_параметры.json,
            ПОС_глобальные_константы.json и height.txt).
        output_path: путь выходного файла (по умолчанию —
            ``<session_dir>/Параметры_подбора_элементов.json``).
        project_constants: константы проекта от вызывающего кода
            (UI/globalConstants), приоритет выше IFC и ПОС.
        mapping_path / template_path: пути к справочникам (для тестов).

    Returns:
        Путь к созданному файлу или None, если сырой дамп не найден/пуст.
    """
    raw_dump_path = os.path.join(session_dir, RAW_DUMP_JSON_FILENAME)
    if not os.path.isfile(raw_dump_path):
        logger.warning(
            f"Сырой дамп параметров не найден ({raw_dump_path}) — "
            "шаблоны параметров подбора не заполняются"
        )
        return None

    try:
        elements = _load_json(raw_dump_path)
    except Exception as exc:
        logger.warning(f"Не удалось прочитать сырой дамп {raw_dump_path}: {exc}")
        return None
    if not isinstance(elements, list) or not elements:
        logger.warning(f"Сырой дамп {raw_dump_path} пуст — шаблоны не заполняются")
        return None

    template_params = load_template_schema(template_path)
    mapping = load_mapping(mapping_path)
    if not template_params:
        logger.warning("Перечень параметров шаблона пуст "
                       f"({template_path or TEMPLATE_SCHEMA_PATH})")
        return None

    constants = _resolve_constants(session_dir, project_constants)

    # Значения констант для раздела constants — выбранные варианты из
    # списков схемы (как в веб-интерфейсе); параметры элементов заполняются
    # исходными значениями (числа с единицами измерения).
    constants_schema = _load_constants_schema()
    formatted_constants = _format_constants(constants, constants_schema)

    filled_elements = []
    for element in elements:
        parameters = _fill_element_parameters(
            element, template_params, mapping, constants
        )
        filled_elements.append({
            "global_id": element.get("GlobalId", ""),
            "name": element.get("Name", ""),
            "ifc_class": element.get("IfcClass", ""),
            "predefined_type": element.get("PredefinedType", ""),
            "storey": element.get("Этаж", ""),
            "parameters": parameters,
        })

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "template": "data/selection_parameters.json",
        "mapping": "data/selection_parameters_mapping.json",
        "source": RAW_DUMP_JSON_FILENAME,
        "total_elements": len(filled_elements),
        "constants": formatted_constants,
        "elements": filled_elements,
    }

    output_path = output_path or os.path.join(session_dir, OUTPUT_FILENAME)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(
        f"Сохранены заполненные шаблоны параметров подбора: {output_path} "
        f"(элементов: {len(filled_elements)}, "
        f"размер: {os.path.getsize(output_path) / 1024:.0f} KB)"
    )
    return output_path


if __name__ == "__main__":
    # Отладочный запуск: python -m src.services.selection_template_builder <session_dir>
    import sys

    if len(sys.argv) < 2:
        print("Использование: python -m src.services.selection_template_builder "
              "<session_dir> [output.json]")
        sys.exit(1)

    path = build_selection_parameters(
        sys.argv[1],
        output_path=sys.argv[2] if len(sys.argv) > 2 else None,
    )
    print(path or "Файл не создан (сырой дамп не найден)")
