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

import math
from collections import deque
import os
import weakref

import ifcopenshell
import ifcopenshell.geom
import numpy as np
import pandas as pd

from src.core.logger import setup_logger

logger = setup_logger(__name__)
_FORMWORK_SURFACE_CACHE = weakref.WeakKeyDictionary()

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


def _iter_type_propsets(element):
    """Property Sets, привязанные к типу элемента (IfcTypeObject).

    В части моделей параметры (например ``ExpCheck_*::MGE_ElementCode``,
    ``MGE_MaterialCode``) размещены на типе (``IfcWallType``, ``IfcSlabType``,
    ...), а не на экземпляре элемента. Их нужно добавить в дамп, иначе
    downstream-модули не увидят код МССК и материал.

    Yields:
        IfcPropertySet / IfcElementQuantity типа элемента.
    """
    seen = set()
    try:
        for rel in getattr(element, "IsTypedBy", None) or []:
            if not rel.is_a("IfcRelDefinesByType"):
                continue
            type_obj = getattr(rel, "RelatingType", None)
            if type_obj is None:
                continue
            for pset in getattr(type_obj, "HasPropertySets", None) or []:
                if pset is None:
                    continue
                try:
                    key = pset.id()
                except Exception:
                    key = id(pset)
                if key in seen:
                    continue
                seen.add(key)
                yield pset
    except Exception as exc:
        logger.debug(f"Не удалось прочитать свойства типа элемента: {exc}")


def _collect_propsets(element):
    """Собирает свойства (Pset) и количества (QTO) элемента в виде словаря.

    Ключи: ``Свойство::{PsetName}::{PropName}`` и ``QTO::{QtoName}::{QtyName}``.

    Свойства читаются с экземпляра элемента, затем дополняются свойствами
    его типа (``IsTypedBy`` → ``RelatingType`` → ``HasPropertySets``) — только
    для отсутствующих ключей (приоритет у экземпляра). Так покрываются
    модели, где код МССК и материал заданы на типе элемента.
    """
    result = {}
    try:
        if hasattr(element, "IsDefinedBy"):
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

    # Свойства типа элемента — fallback для отсутствующих ключей.
    try:
        for pset in _iter_type_propsets(element):
            set_name = getattr(pset, "Name", None)
            set_name = str(set_name) if set_name else ""

            if pset.is_a("IfcPropertySet"):
                for prop in getattr(pset, "HasProperties", []) or []:
                    prop_name = getattr(prop, "Name", None)
                    if not prop_name:
                        continue
                    key = f"Свойство::{set_name}::{prop_name}"
                    if key not in result:
                        result[key] = _extract_property_value(prop)
            elif pset.is_a("IfcElementQuantity"):
                for qty in getattr(pset, "Quantities", []) or []:
                    qty_name = getattr(qty, "Name", None)
                    if not qty_name:
                        continue
                    key = f"QTO::{set_name}::{qty_name}"
                    if key not in result:
                        result[key] = _extract_quantity_value(qty)
    except Exception as exc:
        logger.debug(f"Ошибка сбора Pset/QTO типа элемента: {exc}")

    return result


# =====================================================================
#  МАТЕРИАЛ И ЭТАЖ
# =====================================================================

def _material_names(items) -> str:
    """Собирает имена материалов из коллекции IFC-сущностей.

    items — список элементов вида IfcMaterialLayer / IfcMaterialConstituent /
    IfcMaterialProfile / IfcMaterial, у которых есть атрибут Material (или сам
    элемент — IfcMaterial). Возвращает строку имён через запятую.
    """
    names = []
    for item in items or []:
        mat = item
        if hasattr(item, "Material"):
            mat = item.Material
        if mat is None:
            continue
        nm = getattr(mat, "Name", None)
        if nm:
            names.append(str(nm))
    return ", ".join(names)


def _get_material(element):
    """Извлекает материал элемента (без нормализации названий).

    Поддерживаются все способы привязки материала в IFC:
      * IfcMaterial (одиночный материал);
      * IfcMaterialLayerSet / IfcMaterialLayerSetUsage (слои);
      * IfcMaterialList;
      * IfcMaterialConstituentSet (состав — двери, окна и т.п.);
      * IfcMaterialProfileSet / IfcMaterialProfileSetUsage (профили — металлопрокат).
    """
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
                return _material_names(getattr(layerset, "MaterialLayers", None) if layerset else None)

            if mat.is_a("IfcMaterialLayerSet"):
                return _material_names(getattr(mat, "MaterialLayers", []))

            if mat.is_a("IfcMaterialList"):
                return _material_names(getattr(mat, "Materials", []))

            if mat.is_a("IfcMaterialConstituentSet"):
                return _material_names(getattr(mat, "MaterialConstituents", []))

            if mat.is_a("IfcMaterialProfileSet"):
                return _material_names(getattr(mat, "MaterialProfiles", []))

            if mat.is_a("IfcMaterialProfileSetUsage"):
                profset = getattr(mat, "ForProfileSet", None)
                return _material_names(getattr(profset, "MaterialProfiles", None) if profset else None)
    except Exception as exc:
        logger.debug(f"Ошибка извлечения материала: {exc}")
    return ""


def find_storey(element):
    """Find direct or inherited storey containment, guarding against assembly cycles."""
    pending = deque([element])
    visited = set()
    while pending:
        current = pending.popleft()
        if current is None:
            continue
        identity = current.id() if callable(getattr(current, 'id', None)) else id(current)
        if identity in visited:
            continue
        visited.add(identity)
        for relation in getattr(current, 'ContainedInStructure', ()) or ():
            container = getattr(relation, 'RelatingStructure', None)
            if container is not None and container.is_a('IfcBuildingStorey'):
                return container
        for attribute in ('Decomposes', 'Nests'):
            for relation in getattr(current, attribute, ()) or ():
                parent = getattr(relation, 'RelatingObject', None)
                if parent is not None:
                    pending.append(parent)
    return None


def _opening_storey(element):
    """Этаж проёма по его элементу-основанию.

    ``IfcOpeningElement`` не входит в пространственную структуру здания
    (``ContainedInStructure``), поэтому его этаж определяется через связь
    ``IfcRelVoidsElement`` → ``RelatingBuildingElement`` (стена/плита).
    Используется только для проёмов, которые попадают в дамп проверки ЦИМ.
    """
    try:
        for relation in getattr(element, "VoidsElements", ()) or ():
            host = getattr(relation, "RelatingBuildingElement", None)
            if host is None:
                continue
            container = find_storey(host)
            if container is not None:
                return container
    except Exception as exc:
        logger.debug(f"Ошибка извлечения этажа проёма: {exc}")
    return None


def _get_storey(element):
    """Возвращает (имя_этажа, отметка_этажа) без нормализации."""
    try:
        container = find_storey(element)
        if container is None and element.is_a("IfcOpeningElement"):
            container = _opening_storey(element)
        if container is not None:
            name = getattr(container, "Name", None)
            elevation = getattr(container, "Elevation", None)
            return (str(name) if name is not None else "",
                    _serialize_raw(elevation) if elevation is not None else "")
    except Exception as exc:
        logger.debug(f"Ошибка извлечения этажа: {exc}")
    return "", ""


# =====================================================================
#  ВЫЧИСЛЕНИЕ КОЛИЧЕСТВ ИЗ ГЕОМЕТРИИ (bbox-замена QTO)
# =====================================================================
#
# Если в IFC-файле отсутствуют IfcElementQuantity (QTO), количества
# (объём, габариты, площадь поверхности) вычисляются из тесселированной
# геометрии элемента (IfcPolygonalFaceSet / IfcTriangulatedFaceSet).
#
# Все вычисленные колонки имеют префикс ``QTO_bbox::`` — это маркер,
# указывающий, что значения получены из bbox/меша, а не из оригинальных
# QTO-наборов IFC-файла.


def _placement_to_matrix(placement):
    """Строит матрицу трансформации 4x4 из IfcAxis2Placement3D (или 2D).

    Возвращает (4x4) однородную матрицу, переводящую локальные координаты
    в координаты родительского представления.
    """
    if placement is None:
        return np.eye(4, dtype=float)

    loc = placement.Location
    location = np.array(loc.Coordinates, dtype=float) if loc else np.zeros(3)

    # IfcAxis2Placement3D
    if placement.is_a("IfcAxis2Placement3D"):
        axis = placement.Axis
        ref_dir = placement.RefDirection

        if axis is not None and ref_dir is not None:
            z = np.array(axis.DirectionRatios, dtype=float)
            x = np.array(ref_dir.DirectionRatios, dtype=float)
            z = z / np.linalg.norm(z)
            # Убеждаемся, что x перпендикулярен z (по спецификации IFC это так,
            # но на всякий случай ортогонализуем).
            x = x - np.dot(x, z) * z
            x = x / np.linalg.norm(x)
            y = np.cross(z, x)
        elif ref_dir is not None:
            # Нет Axis — подразумевается z=(0,0,1).
            x = np.array(ref_dir.DirectionRatios, dtype=float)
            x = x / np.linalg.norm(x)
            y = np.cross(np.array([0.0, 0.0, 1.0]), x)
            z = np.array([0.0, 0.0, 1.0])
        else:
            x = np.array([1.0, 0.0, 0.0])
            y = np.array([0.0, 1.0, 0.0])
            z = np.array([0.0, 0.0, 1.0])

        # Дополняем location до 3D (IfcCartesianPoint может быть 2D).
        if len(location) == 2:
            location = np.append(location, 0.0)

    # IfcAxis2Placement2D
    elif placement.is_a("IfcAxis2Placement2D"):
        ref_dir = placement.RefDirection
        if ref_dir is not None:
            x = np.array(ref_dir.DirectionRatios, dtype=float)
            if len(x) == 2:
                x = np.append(x, 0.0)
            x = x / np.linalg.norm(x)
            y = np.cross(np.array([0.0, 0.0, 1.0]), x)
        else:
            x = np.array([1.0, 0.0, 0.0])
            y = np.array([0.0, 1.0, 0.0])
        z = np.array([0.0, 0.0, 1.0])
        if len(location) == 2:
            location = np.append(location, 0.0)
    else:
        return np.eye(4, dtype=float)

    M = np.eye(4, dtype=float)
    M[:3, 0] = x
    M[:3, 1] = y
    M[:3, 2] = z
    M[:3, 3] = location
    return M


def _local_placement_to_matrix(placement):
    """Рекурсивно строит мировую матрицу из цепочки IfcLocalPlacement.

    IfcLocalPlacement содержит PlacementRelTo (ссылка на родительский
    placement) и RelativePlacement (IfcAxis2Placement3D). Матрицы
    перемножаются от корня (IfcLocalPlacement без PlacementRelTo)
    до текущего placement.
    """
    if placement is None:
        return np.eye(4, dtype=float)

    # Если есть родительский placement — сначала его матрица.
    parent = getattr(placement, "PlacementRelTo", None)
    if parent is not None and parent.is_a("IfcLocalPlacement"):
        parent_m = _local_placement_to_matrix(parent)
    else:
        parent_m = np.eye(4, dtype=float)

    own = getattr(placement, "RelativePlacement", None)
    own_m = _placement_to_matrix(own)
    return parent_m @ own_m


def _mapped_item_to_matrix(item):
    """Строит матрицу трансформации из IfcMappedItem (MappingTarget).

    IfcMappedItem задаёт трансформацию через MappingTarget
    (IfcCartesianTransformationOperator3D), которая включает смещение,
    масштаб и поворот.
    """
    try:
        target = getattr(item, "MappingTarget", None)
        if target is None:
            return np.eye(4, dtype=float)

        # Точка начала (Axis1/LocalOrigin)
        origin = getattr(target, "LocalOrigin", None)
        loc = np.array(origin.Coordinates, dtype=float) if origin else np.zeros(3)
        if len(loc) == 2:
            loc = np.append(loc, 0.0)

        # Направления осей
        axis1 = getattr(target, "Axis1", None)   # X
        axis2 = getattr(target, "Axis2", None)   # Y
        axis3 = getattr(target, "Axis3", None)   # Z

        def _dir(d, default):
            if d is not None:
                v = np.array(d.DirectionRatios, dtype=float)
                n = np.linalg.norm(v)
                return v / n if n > 1e-12 else np.array(default, dtype=float)
            return np.array(default, dtype=float)

        x = _dir(axis1, [1.0, 0.0, 0.0])
        y = _dir(axis2, [0.0, 1.0, 0.0])
        z = _dir(axis3, [0.0, 0.0, 1.0])

        # Масштаб
        scale = getattr(target, "Scale", 1.0) or 1.0

        M = np.eye(4, dtype=float)
        M[:3, 0] = x * scale
        M[:3, 1] = y * scale
        M[:3, 2] = z * scale
        M[:3, 3] = loc
        return M
    except Exception:
        return np.eye(4, dtype=float)


def _apply_transform(verts, matrix):
    """Применяет матрицу 4x4 к массиву вершин (N×3)."""
    if matrix is None or np.allclose(matrix, np.eye(4)):
        return verts
    n = len(verts)
    homog = np.hstack([verts, np.ones((n, 1))])  # N×4
    transformed = homog @ matrix.T               # N×4
    return transformed[:, :3]


def _polygon_area_2d(points):
    """Площадь 2D-многоугольника (формула шнурков/Gauss).

    ``points`` — список (x, y). Первая и последняя точки могут совпадать
    (замкнутый контур) или нет — оба варианта обрабатываются корректно.
    """
    n = len(points)
    if n < 3:
        return 0.0
    # Если контур замкнут (первая == последняя) — убираем последнюю.
    if points[0] == points[-1]:
        pts = points[:-1]
    else:
        pts = points
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _profile_net_area(contours):
    """Площадь сечения профиля с учётом отверстий.

    ``contours`` — список контуров: первый — внешний, остальные —
    внутренние (отверстия). Площадь отверстий вычитается.
    """
    if not contours:
        return 0.0
    area = _polygon_area_2d(contours[0])
    for inner in contours[1:]:
        area -= _polygon_area_2d(inner)
    return max(area, 0.0)


def _fan_triangulate(face):
    """Веерная триангуляция многоугольника (fallback)."""
    n = len(face)
    if n < 3:
        return []
    if n == 3:
        return [list(face)]
    return [[face[0], face[i], face[i + 1]] for i in range(1, n - 1)]


def _ear_clip_triangulate(verts, face):
    """Триангуляция простого многоугольника методом отрезания «ушек».

    Корректно обрабатывает вогнутые многоугольники. Возвращает список
    треугольников (тройки индексов в ``verts``).
    """
    n = len(face)
    if n < 3:
        return []
    if n == 3:
        return [list(face)]

    # Нормаль грани для определения направления обхода и проецирования.
    v0 = verts[face[0]]
    v1 = verts[face[1]]
    v2 = verts[face[2]]
    normal = np.cross(v1 - v0, v2 - v0)
    norm_len = np.linalg.norm(normal)
    if norm_len < 1e-12:
        return _fan_triangulate(face)
    normal = normal / norm_len

    # Выбираем плоскость проецирования (отбрасываем координату с
    # наибольшей компонентой нормали — для устойчивости).
    abs_n = np.abs(normal)
    if abs_n[0] >= abs_n[1] and abs_n[0] >= abs_n[2]:
        drop = 0
    elif abs_n[1] >= abs_n[2]:
        drop = 1
    else:
        drop = 2

    def _proj(idx):
        v = verts[idx]
        return tuple(np.delete(v, drop))

    pts2d = [_proj(i) for i in face]

    # Определяем направление обхода (по знаку площади в 2D).
    signed_area = 0.0
    for i in range(n):
        x1, y1 = pts2d[i]
        x2, y2 = pts2d[(i + 1) % n]
        signed_area += x1 * y2 - x2 * y1
    ccw = signed_area > 0

    # Индексы активных вершин (кольцо).
    indices = list(range(n))

    def _cross2d(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def _is_convex(prev, curr, nxt):
        # Для CCW: cross > 0 → выпуклый. Для CW: cross < 0 → выпуклый.
        c = _cross2d(pts2d[prev], pts2d[curr], pts2d[nxt])
        return c > 0 if ccw else c < 0

    def _point_in_tri(p, a, b, c):
        # Проверка: точка p внутри треугольника (a, b, c) — барицентрически.
        d1 = _cross2d(a, b, p)
        d2 = _cross2d(b, c, p)
        d3 = _cross2d(c, a, p)
        has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
        has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
        return not (has_neg and has_pos)

    triangles = []
    # Защита от бесконечного цикла.
    max_iter = n * n
    it = 0

    while len(indices) > 3 and it < max_iter:
        it += 1
        ear_found = False
        m = len(indices)
        for ii in range(m):
            prev_i = indices[(ii - 1) % m]
            curr_i = indices[ii]
            next_i = indices[(ii + 1) % m]

            if not _is_convex(prev_i, curr_i, next_i):
                continue

            # Ни одна другая вершина не должна быть внутри этого треугольника.
            a, b, c = pts2d[prev_i], pts2d[curr_i], pts2d[next_i]
            ok = True
            for jj in indices:
                if jj in (prev_i, curr_i, next_i):
                    continue
                if _point_in_tri(pts2d[jj], a, b, c):
                    ok = False
                    break
            if not ok:
                continue

            triangles.append([face[prev_i], face[curr_i], face[next_i]])
            indices.pop(ii)
            ear_found = True
            break

        if not ear_found:
            # Не удалось найти «ушко» — fallback на веер для оставшихся.
            remaining = [face[i] for i in indices]
            triangles.extend(_fan_triangulate(remaining))
            break

    if len(indices) == 3:
        triangles.append([face[indices[0]], face[indices[1]], face[indices[2]]])

    return triangles if triangles else _fan_triangulate(face)


def _is_mesh_closed(verts, faces):
    """Проверяет, является ли меш замкнутой (manifold) поверхностью.

    Меш считается замкнутым, если каждое ребро принадлежит ровно
    двум граням. Возвращает True для замкнутого меша.
    """
    edges = {}
    for f in faces:
        n = len(f)
        for j in range(n):
            a, b = f[j], f[(j + 1) % n]
            key = (min(a, b), max(a, b))
            edges[key] = edges.get(key, 0) + 1
    # Допускаем небольшое количество не-манифолдных рёбер (<= 2),
    # т.к. тесселяции из Revit иногда имеют мелкие дефекты.
    non_manifold = sum(1 for v in edges.values() if v != 2)
    return non_manifold <= 2


def _extract_profile_points_2d(profile):
    """Извлекает 2D-точки контура профиля.

    Поддерживает:
      * IfcArbitraryClosedProfileDef — OuterCurve (IfcIndexedPolyCurve);
      * IfcArbitraryProfileDefWithVoids — OuterCurve + InnerCurves (voids);
      * IfcRectangleProfileDef — задаётся шириной/высотой;
      * IfcIShapeProfileDef, IfcLShapeProfileDef и др. — упрощённо через bbox.

    Возвращает список списков точек: первый — внешний контур,
    остальные — внутренние (отверстия). Каждая точка — (x, y).
    """
    contours = []

    def _curve_to_points(curve):
        """Извлекает точки из IfcIndexedPolyCurve / IfcPolyline."""
        if curve is None:
            return []
        if curve.is_a("IfcIndexedPolyCurve"):
            pts_node = curve.Points
            if pts_node is None:
                return []
            if pts_node.is_a("IfcCartesianPointList2D"):
                return [tuple(p) for p in pts_node.CoordList]
            if pts_node.is_a("IfcCartesianPointList3D"):
                return [(p[0], p[1]) for p in pts_node.CoordList]
        elif curve.is_a("IfcPolyline"):
            return [(p.Coordinates[0], p.Coordinates[1])
                    for p in curve.Points if p is not None]
        return []

    if profile is None:
        return contours

    if profile.is_a("IfcArbitraryClosedProfileDef") or \
            profile.is_a("IfcArbitraryProfileDefWithVoids"):
        outer = _curve_to_points(getattr(profile, "OuterCurve", None))
        if outer:
            contours.append(outer)
        # Внутренние контуры (отверстия) — для bbox не критичны,
        # но для объёма их можно игнорировать (упрощённо).
        if profile.is_a("IfcArbitraryProfileDefWithVoids"):
            for inner_curve in getattr(profile, "InnerCurves", []) or []:
                inner = _curve_to_points(inner_curve)
                if inner:
                    contours.append(inner)

    elif profile.is_a("IfcRectangleProfileDef"):
        w = float(getattr(profile, "XDim", 0) or 0)
        h = float(getattr(profile, "YDim", 0) or 0)
        # IfcRectangleProfileDef может иметь Position (IfcAxis2Placement2D)
        pos = getattr(profile, "Position", None)
        ox, oy = 0.0, 0.0
        if pos and pos.Location:
            ox = float(pos.Location.Coordinates[0])
            oy = float(pos.Location.Coordinates[1])
        contours.append([
            (ox - w / 2, oy - h / 2),
            (ox + w / 2, oy - h / 2),
            (ox + w / 2, oy + h / 2),
            (ox - w / 2, oy + h / 2),
            (ox - w / 2, oy - h / 2),
        ])

    elif profile.is_a("IfcCircleProfileDef"):
        r = float(getattr(profile, "Radius", 0) or 0)
        if r > 0:
            import math
            n = 32
            pts = [(r * math.cos(2 * math.pi * i / n),
                    r * math.sin(2 * math.pi * i / n)) for i in range(n)]
            pts.append(pts[0])
            contours.append(pts)

    elif profile.is_a("IfcCircleHollowProfileDef"):
        r = float(getattr(profile, "Radius", 0) or 0)
        if r > 0:
            import math
            n = 32
            pts = [(r * math.cos(2 * math.pi * i / n),
                    r * math.sin(2 * math.pi * i / n)) for i in range(n)]
            pts.append(pts[0])
            contours.append(pts)

    else:
        # Для других типов профилей (IShape, LShape, TShape и т.д.)
        # пытаемся извлечь через OuterCurve если есть.
        outer_curve = getattr(profile, "OuterCurve", None)
        if outer_curve:
            outer = _curve_to_points(outer_curve)
            if outer:
                contours.append(outer)

    return contours


def _extruded_solid_to_mesh(item):
    """Преобразует IfcExtrudedAreaSolid в (verts, faces, profile_area, depth, surface_area).

    Извлекает 2D-профиль, трансформирует через Position в 3D,
    затем экструдирует вдоль ExtrudedDirection на Depth.

    ``profile_area`` — площадь сечения с учётом отверстий (мм²),
    используется для точного вычисления объёма (profile_area × depth).

    ``surface_area`` — площадь поверхности тела с учётом отверстий (мм²):
    ``2×(S_внеш − ΣS_внутр) + P_внеш×depth + ΣP_внутр×depth``.

    Возвращает (verts, faces, profile_area, depth, surface_area)
    или (None, None, 0, 0, 0).
    """
    swept_area = item.SweptArea
    if swept_area is None:
        return None, None, 0.0, 0.0, 0.0

    contours = _extract_profile_points_2d(swept_area)
    if not contours:
        return None, None, 0.0, 0.0, 0.0

    outer = contours[0]
    if len(outer) < 3:
        return None, None, 0.0, 0.0, 0.0

    # Площадь сечения с учётом отверстий (для точного объёма).
    profile_area = _profile_net_area(contours)

    # Матрица трансформации Position (IfcAxis2Placement3D).
    pos = getattr(item, "Position", None)
    M = _placement_to_matrix(pos)

    # Направление и глубина экструзии.
    ext_dir = item.ExtrudedDirection
    depth = float(item.Depth or 0)
    if ext_dir is None or depth <= 0:
        return None, None, 0.0, 0.0, 0.0

    # Точная площадь поверхности экструзии (с учётом отверстий).
    # 2 × (S_внеш − ΣS_внутр) — основания; P × depth — боковины.
    def _perimeter(points):
        p = 0.0
        n = len(points)
        if points[0] == points[-1]:
            n -= 1
        for i in range(n):
            x1, y1 = points[i]
            x2, y2 = points[(i + 1) % n]
            p += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        return p

    outer_per = _perimeter(outer)
    surface_area = 2.0 * profile_area + outer_per * depth
    for inner in contours[1:]:
        surface_area += _perimeter(inner) * depth

    direction = np.array(ext_dir.DirectionRatios, dtype=float)
    direction = direction / np.linalg.norm(direction)
    # Вектор экструзии в мировых координатах.
    ext_vec = M[:3, :3] @ direction * depth

    # Нижнее основание: 2D-точки профиля → 3D через M.
    bottom_3d = []
    for (x, y) in outer:
        local = np.array([x, y, 0.0, 1.0])
        world = M @ local
        bottom_3d.append(world[:3])
    bottom_3d = np.array(bottom_3d, dtype=float)

    n = len(bottom_3d)
    top_3d = bottom_3d + ext_vec

    verts = np.vstack([bottom_3d, top_3d])
    faces = []

    # Нижняя грань (обратный порядок для правильной нормали).
    faces.append(list(range(n - 1, -1, -1)))
    # Верхняя грань.
    faces.append(list(range(n, 2 * n)))
    # Боковые грани (четырёхугольники).
    for i in range(n - 1):
        faces.append([i, i + 1, n + i + 1, n + i])
    # Замыкающая боковая грань.
    faces.append([n - 1, 0, n, 2 * n - 1])

    return verts, faces, profile_area, depth, surface_area


def _collect_geom_from_item(item, parts, transform=None):
    """Рекурсивно собирает геометрические части (verts, faces) из элемента-представления.

    Обрабатывает:
      * IfcMappedItem  — раскрывает через MappingSource.MappedRepresentation
        с применением трансформации MappingTarget;
      * IfcPolygonalFaceSet / IfcTriangulatedFaceSet — тесселяция;
      * IfcFacetedBrep — граненая B-Rep модель;
      * IfcExtrudedAreaSolid — тело выдавливания (профиль + глубина);
      * IfcBooleanClippingResult — раскрывает через Operand (рекурсивно);
      * IfcBooleanResult — раскрывает первый операнд.

    ``parts`` — список накопленных кортежей:
      (np.ndarray вершин, list граней, kind, profile_area, surface_area)
      где kind = 'extruded' | 'surface',
      profile_area — площадь сечения экструзии (мм²),
      surface_area — точная площадь поверхности экструзии (мм²).

    ``transform`` — опциональная матрица 4×4, применяемая к вершинам.
    Индексы в гранях — 0-based, указывают на локальный массив вершин части.
    """
    try:
        if item.is_a("IfcMappedItem"):
            # IfcMappedItem: трансформируем через MappingTarget.
            mrep = item.MappingSource.MappedRepresentation
            mapped_m = _mapped_item_to_matrix(item)
            combined = mapped_m if transform is None else transform @ mapped_m
            for it in mrep.Items:
                _collect_geom_from_item(it, parts, combined)

        elif item.is_a("IfcPolygonalFaceSet") or item.is_a("IfcTriangulatedFaceSet"):
            coords = item.Coordinates.CoordList
            if not coords:
                return
            verts = np.array(coords, dtype=float)
            # Применяем трансформацию (MappingTarget / placement).
            verts = _apply_transform(verts, transform)
            faces = []

            # IfcPolygonalFaceSet: грани — IfcIndexedPolygonalFace.
            if hasattr(item, "Faces") and item.Faces:
                for f in item.Faces:
                    idx = list(f.CoordIndex)
                    faces.append([i - 1 for i in idx])
            # IfcTriangulatedFaceSet: CoordIndex — список треугольников.
            elif hasattr(item, "CoordIndex") and item.CoordIndex:
                for idx in item.CoordIndex:
                    faces.append([i - 1 for i in idx])

            if len(verts) > 0 and faces:
                parts.append((verts, faces, 'surface', None, None))

        elif item.is_a("IfcFacetedBrep"):
            outer = item.Outer
            if outer:
                verts = []
                faces = []
                for f in getattr(outer, "CfsFaces", []):
                    for bound in f.Bounds:
                        poly = bound.Bound
                        if poly:
                            face_idx = []
                            for p in poly.Polygon:
                                face_idx.append(len(verts))
                                verts.append(list(p.Coordinates))
                            faces.append(face_idx)
                if verts:
                    verts = np.array(verts, dtype=float)
                    verts = _apply_transform(verts, transform)
                    parts.append((verts, faces, 'surface', None, None))

        elif item.is_a("IfcExtrudedAreaSolid"):
            verts, faces, profile_area, depth, surface_area = _extruded_solid_to_mesh(item)
            if verts is not None and len(verts) > 0 and faces:
                verts = _apply_transform(verts, transform)
                parts.append((verts, faces, 'extruded', profile_area, surface_area))

        elif item.is_a("IfcBooleanClippingResult") or item.is_a("IfcBooleanResult"):
            # Boolean result: раскрываем первый операнд (тело).
            # Для bbox-оценки это достаточно — обрезка обычно уменьшает
            # габариты незначительно или не влияет на порядок величин.
            operand = getattr(item, "FirstOperand", None)
            if operand is None:
                operand = getattr(item, "Operand1", None)
            if operand is not None:
                _collect_geom_from_item(operand, parts, transform)

    except Exception as exc:
        logger.debug(f"Ошибка извлечения геометрии из {item.is_a()}: {exc}")


def _compute_mesh_volume(verts, faces):
    """Вычисляет объём меша методом сигнатур тетраэдров.

    Для каждой грани (триангулированной корректно) считается
    signed volume тетраэдра (0, a, b, c). Сумма по модулю — объём.
    Координаты в мм, результат — в мм³.

    Для устойчивости результат не зависит от начала координат, если меш
    замкнут. Если меш не замкнут (тесселяции из Revit часто имеют
    boundary-рёбра / мелкие «дыры»), метод даёт приближённое значение:
    вклад «дыр» в сумму обычно мал по сравнению с объёмом тела.
    """
    total = 0.0
    for face in faces:
        triangles = _ear_clip_triangulate(verts, face)
        for (ia, ib, ic) in triangles:
            a = verts[ia]
            b = verts[ib]
            c = verts[ic]
            total += np.dot(a, np.cross(b, c)) / 6.0
    return abs(total)


def _compute_surface_area(verts, faces):
    """Вычисляет площадь поверхности меша (сумма площадей треугольников).

    Координаты в мм, результат — в мм². Используется корректная
    триангуляция (ear-clipping) для вогнутых многоугольников.
    """
    total = 0.0
    for face in faces:
        triangles = _ear_clip_triangulate(verts, face)
        for (ia, ib, ic) in triangles:
            a = verts[ia]
            b = verts[ib]
            c = verts[ic]
            total += 0.5 * np.linalg.norm(np.cross(b - a, c - a))
    return total


def _compute_bbox_quantities(element):
    """Shared per-element engine geometry, in mm/m²/m³, with opening cuts."""
    from .ifc_geometry_quantities import compute
    return compute(element)


# =====================================================================
#  BBOX-ФОЛБЭК ПЕРИМЕТРА/ТОЛЩИНЫ ДЛЯ РАСЧЁТА ОПАЛУБКИ (РЕЖИМ КР)
# =====================================================================

# Классы элементов, для которых выполняется bbox-фолбэк периметра/толщины:
#   * IfcSlab — плиты, у которых QTO не содержит Perimeter/Depth
#     (неполная выгрузка Revit: приямки, часть фундаментных плит);
#   * IfcStairFlight — лестничные марши: Qto_StairFlightBaseQuantities
#     в принципе не содержит Perimeter/Depth (только Length/Volume).
_FORMWORK_BBOX_CLASSES = ("IfcSlab", "IfcStairFlight")

# Типы геометрии, для которых ручной обход вершин (без ядра
# ifcopenshell.geom) даёт габариты bbox, совпадающие с триангуляцией ядра.
# Это «простые» представления: экструзия, тесселяция, граненая B-Rep.
_SIMPLE_GEOM_TYPES = {
    "IfcExtrudedAreaSolid",
    "IfcPolygonalFaceSet",
    "IfcTriangulatedFaceSet",
    "IfcFacetedBrep",
}
# Контейнеры, которые ручной обход раскрывает с трансформацией.
_SIMPLE_GEOM_CONTAINERS = {"IfcMappedItem"}


def _item_is_simple(item) -> bool:
    """True, если геометрию примитива ручной обход строит без потери точности.

    Простые типы (экструзия, тесселяция, граненая B-Rep без отверстий) и
    ``IfcMappedItem``, ссылающийся только на простые примитивы. Для остальных
    типов (boolean/CSG/swept/advanced brep и т.п.) ручной bbox не используется —
    габариты считает ядро (поведение не меняется).
    """
    t = item.is_a()
    if t == "IfcFacetedBrep":
        # Отверстия (Voids) ручной обход игнорирует — не простой случай.
        return not getattr(item, "Voids", None)
    if t in _SIMPLE_GEOM_TYPES:
        return True
    if t in _SIMPLE_GEOM_CONTAINERS:
        mrep = getattr(getattr(item, "MappingSource", None), "MappedRepresentation", None)
        if mrep is None:
            return False
        return all(_item_is_simple(it) for it in mrep.Items)
    return False


def _geometry_is_simple(element) -> bool:
    """True, если всё представление элемента покрыто ручным обходом вершин."""
    rep = getattr(element, "Representation", None)
    if not rep:
        return False
    seen = False
    for r in getattr(rep, "Representations", []) or []:
        rep_type = getattr(r, "RepresentationType", None) or ""
        if rep_type in ("Curve2D", "Curve3D", "Point", "GeometricSet"):
            continue
        for item in getattr(r, "Items", []) or []:
            seen = True
            if not _item_is_simple(item):
                return False
    return seen


def _fast_bbox_mm(element):
    """Габариты (dx, dy, dz) в мировых координатах без ядра ifcopenshell.geom.

    Использует ручной обход представления (``_collect_geom_from_item``) и
    мировую матрицу размещения (``_local_placement_to_matrix``) — те же
    вершины, что и триангуляция ядра, но без построения треугольников
    (в сотни раз быстрее). Возвращает None, если вершины собрать не удалось.
    """
    parts = []
    for r in getattr(getattr(element, "Representation", None), "Representations", []) or []:
        rep_type = getattr(r, "RepresentationType", None) or ""
        if rep_type in ("Curve2D", "Curve3D", "Point", "GeometricSet"):
            continue
        for item in getattr(r, "Items", []) or []:
            _collect_geom_from_item(item, parts)
    if not parts:
        return None
    verts = np.vstack([p[0] for p in parts])
    matrix = _local_placement_to_matrix(getattr(element, "ObjectPlacement", None))
    verts = _apply_transform(verts, matrix)
    dims = verts.max(axis=0) - verts.min(axis=0)
    return float(dims[0]), float(dims[1]), float(dims[2])


def _compute_formwork_bbox_quantities(element):
    """Вычисляет периметр/толщину элемента из геометрии (bbox, режим КР).

    Вызывается для ОТДЕЛЬНЫХ элементов с неполным QTO — когда QTO есть у
    файла в целом, но у конкретного элемента нет QTO-периметра/толщины,
    необходимых для расчёта площади опалубки плит (периметр × толщина).
    Габариты вычисляются в МИРОВЫХ координатах (USE_WORLD_COORDS):
    вертикальный габарит Z — подъём марша, горизонтальные X/Y — заложение
    и ширина марша.

    Возвращает словарь с ключами 'perimeter_mm' и 'depth_mm':
      * IfcSlab — периметр = 2 × (два наибольших габарита bbox),
        толщина = наименьший габарит (плита горизонтальна);
      * IfcStairFlight — периметр = 2 × длина по склону (два боковых
        ребра марша), толщина = ширина марша: произведение
        периметр × толщина даёт площадь ДВУХ боковых граней марша
        (опалубка монолитного марша);
      * прочие классы — пустой словарь.

    Оценка по bbox приближённая (периметр прямоугольного следа). Если
    геометрия недоступна — пустой словарь.

    Производительность: для «простых» представлений (экструзия, тесселяция,
    граненая B-Rep) габариты считаются ручным обходом вершин без ядра
    ``ifcopenshell.geom`` (в сотни раз быстрее); для сложной геометрии
    используется ядро — результат тот же.
    """
    result = {}
    try:
        ifc_class = element.is_a() if hasattr(element, "is_a") else ""
        if ifc_class not in _FORMWORK_BBOX_CLASSES:
            return result

        # Быстрый путь: для «простых» представлений габариты берутся ручным
        # обходом вершин (те же вершины, что и у ядра, но без триангуляции —
        # на порядки быстрее). Для сложной геометрии (boolean/CSG/swept/…)
        # используется ядро — поведение не меняется.
        dims = None
        if _geometry_is_simple(element):
            dims = _fast_bbox_mm(element)

        if dims is None:
            settings = ifcopenshell.geom.settings()
            settings.set(settings.USE_WORLD_COORDS, True)
            # CONVERT_BACK_UNITS: значения возвращаются в единицах длины
            # проекта (обычно мм) — как и в остальном конвейере, где
            # координаты/габариты IFC считаются в миллиметрах.
            settings.set(settings.CONVERT_BACK_UNITS, True)
            shape = ifcopenshell.geom.create_shape(settings, element)
            verts = shape.geometry.verts
            if len(verts) < 3:
                return result
            dims = (
                max(verts[0::3]) - min(verts[0::3]),
                max(verts[1::3]) - min(verts[1::3]),
                max(verts[2::3]) - min(verts[2::3]),
            )

        dx, dy, dz = dims
        if min(dx, dy, dz) <= 0:
            return result

        if ifc_class == "IfcSlab":
            # Плита горизонтальна: два наибольших габарита — план,
            # наименьший — толщина. Координаты в IFC — в миллиметрах.
            dims = sorted((dx, dy, dz), reverse=True)
            result["perimeter_mm"] = round(float(2.0 * (dims[0] + dims[1])), 1)
            result["depth_mm"] = round(float(dims[2]), 1)
        else:
            # Лестничный марш: Z — подъём (вертикальный габарит),
            # горизонтальные габариты X/Y — заложение и ширина марша.
            # Заложение — больший из двух горизонтальных габаритов.
            run = max(dx, dy)
            width = min(dx, dy)
            slope = math.hypot(run, dz)
            result["perimeter_mm"] = round(float(2.0 * slope), 1)
            result["depth_mm"] = round(float(width), 1)
    except Exception as exc:
        gid = getattr(element, "GlobalId", "?")
        logger.debug(
            f"Ошибка bbox-фолбэка периметра/толщины для элемента {gid}: {exc}"
        )

    return result


# =====================================================================
#  ПРОВЕРКА НАЛИЧИЯ QTO В ФАЙЛЕ
# =====================================================================

def _has_any_qto(elements):
    """Проверяет, есть ли хотя бы у одного элемента IfcElementQuantity (QTO).

    Возвращает True, если хотя бы один элемент из списка имеет QTO.
    Используется для логирования и решения о вычислении bbox-количеств.
    """
    for el in elements:
        if not hasattr(el, "IsDefinedBy"):
            continue
        for rel in el.IsDefinedBy:
            if not rel.is_a("IfcRelDefinesByProperties"):
                continue
            pset = rel.RelatingPropertyDefinition
            if pset is not None and pset.is_a("IfcElementQuantity"):
                return True
    return False


# =====================================================================
#  СБОР ДАННЫХ ЭЛЕМЕНТА
# =====================================================================

def _is_foundation_plate(name, object_type=''):
    text = f'{name or ""} {object_type or ""}'.lower()
    return 'фундамент' in text and 'приям' not in text


def _surface_formwork_kind(ifc_class, name, object_type=''):
    text = f'{name or ""} {object_type or ""}'.lower()
    if 'сборн' in text:
        return None
    if ifc_class == 'IfcColumn' or (ifc_class in ('IfcWall', 'IfcWallStandardCase') and any(word in text for word in ('пилон', 'колонн'))):
        return 'column'
    if ifc_class == 'IfcBeam':
        return 'beam'
    if ifc_class == 'IfcStairFlight':
        return 'flight'
    if ifc_class == 'IfcSlab' and not _is_foundation_plate(name, object_type):
        if any(word in text for word in ('перекрыт', 'покрыти', 'площадк', 'landing')):
            return 'slab'
    return None


def _surface_formwork_from_mesh(verts, faces, kind):
    vertices = np.asarray(verts).reshape(-1, 3)
    triangles = vertices[np.asarray(faces).reshape(-1, 3)]
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    lengths = np.linalg.norm(cross, axis=1)
    underside = float(lengths[cross[:, 2] < -lengths * 1e-6].sum() / 2)
    edges = float(lengths[np.abs(cross[:, 2]) <= lengths * 1e-6].sum() / 2)
    if kind == 'column':
        underside = 0.0
    formwork = underside + edges
    if formwork <= 0:
        return {}
    result = {'underside_m2': underside,
              'edges_m2': edges, 'formwork_m2': formwork}
    if kind in ('slab', 'flight'):
        from .stair_formwork import horizontal_projection
        result.update(horizontal_projection(vertices, np.asarray(faces).reshape(-1, 3)))
    return result


def warm_formwork_surface_cache(model, elements=None):
    """Batch IFC geometry on the engine's workers; reuse it in both exports."""
    if not isinstance(model, ifcopenshell.file):
        return
    cache = _FORMWORK_SURFACE_CACHE.setdefault(model, {})
    selected = [e for e in (elements if elements is not None else model.by_type('IfcElement'))
                if e.id() not in cache and _surface_formwork_kind(e.is_a(), getattr(e, 'Name', ''), getattr(e, 'ObjectType', ''))]
    if not selected:
        return
    kinds = {e.id(): _surface_formwork_kind(e.is_a(), e.Name, e.ObjectType) for e in selected}
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    try:
        iterator = ifcopenshell.geom.iterator(settings, model, min(4, os.cpu_count() or 1), include=selected)
        if not iterator.initialize():
            return
        count = 0
        while True:
            shape = iterator.get()
            result = _surface_formwork_from_mesh(shape.geometry.verts, shape.geometry.faces, kinds[shape.id])
            if result:
                cache[shape.id] = result
            count += 1
            if count % 500 == 0:
                logger.info('Геометрия опалубки: %d из %d элементов', count, len(selected))
            if not iterator.next():
                break
    except Exception as exc:
        logger.debug('Пакетная геометрия опалубки: %s; оставшиеся элементы вычисляются отдельно', exc)


def _compute_surface_formwork_quantities(element):
    """Mesh underside and vertical faces; walking/top faces are excluded.

    Columns use vertical faces. Floors, beams and flights also include
    underside faces. Precast elements and foundation slabs use other rules.
    """
    kind = _surface_formwork_kind(element.is_a(), getattr(element, 'Name', ''), getattr(element, 'ObjectType', ''))
    if kind is None:
        return {}
    model = getattr(element, 'file', None)
    cache = _FORMWORK_SURFACE_CACHE.setdefault(model, {}) if isinstance(model, ifcopenshell.file) else None
    entity_id = element.id() if cache is not None else None
    if cache is not None and entity_id in cache:
        return dict(cache[entity_id])
    try:
        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        shape = ifcopenshell.geom.create_shape(settings, element)
        result = _surface_formwork_from_mesh(shape.geometry.verts, shape.geometry.faces, kind)
        if cache is not None:
            cache[entity_id] = result
        return dict(result)
    except Exception as exc:
        logger.debug('Не удалось получить грани опалубки %s: %s', getattr(element, 'GlobalId', '?'), exc)
        return {}


def _compute_formwork_contour_quantities(element):
    """Outer footprint perimeter of a foundation slab, excluding inner openings.

    Project the actual IFC mesh onto XY and dissolve its triangles. Bounding
    boxes change the outline of rotated and irregular plates; vertical faces
    also include recesses and openings, so neither gives the outer perimeter.
    Geometry engine coordinates are in metres, independently of IFC units.
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    try:
        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        shape = ifcopenshell.geom.create_shape(settings, element)
        vertices = np.asarray(shape.geometry.verts).reshape(-1, 3)
        faces = np.asarray(shape.geometry.faces).reshape(-1, 3)
        triangles = vertices[faces]
        cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
        projected = [Polygon(triangle[:, :2]) for triangle, normal in zip(triangles, cross)
                     if abs(normal[2]) > 1e-10]
        if not projected:
            return {}
        footprint = unary_union(projected)
        polygons = [footprint] if footprint.geom_type == 'Polygon' else list(footprint.geoms)
        perimeter = sum(p.exterior.length for p in polygons if p.geom_type == 'Polygon')
        depth = float(np.ptp(vertices[:, 2]))
        if perimeter <= 0 or depth <= 0:
            return {}
        return {'perimeter_mm': perimeter * 1000, 'depth_mm': depth * 1000, 'method': 'outer_contour'}
    except Exception as exc:
        logger.debug('Не удалось получить внешний контур плиты %s: %s', getattr(element, 'GlobalId', '?'), exc)
        return {}


def _compute_formwork_quantities(element):
    if (getattr(element, 'is_a', lambda: '')() == 'IfcSlab'
            and _is_foundation_plate(getattr(element, 'Name', ''), getattr(element, 'ObjectType', ''))):
        contour = _compute_formwork_contour_quantities(element)
        if contour:
            return contour
    return _compute_formwork_bbox_quantities(element)


def enrich_formwork_quantities(row, element):
    """Use the same per-element perimeter/depth fallback as zero_step."""
    if row.get("IfcClass") not in ("IfcSlab", "IfcStairFlight", "IfcWall", "IfcWallStandardCase", "IfcBeam", "IfcColumn"):
        return row
    if (row.get('IfcClass') in ('IfcWall', 'IfcWallStandardCase')
            and _surface_formwork_kind(row.get('IfcClass'), row.get('Name'), row.get('ObjectType')) != 'column'):
        for key in ('QTO_geom::Площадь_опалубки_м2', 'QTO_geom::Площадь_низа_м2', 'QTO_geom::Площадь_торцов_м2'):
            row.pop(key, None)
        return row
    if (_surface_formwork_kind(row.get('IfcClass'), row.get('Name'), row.get('ObjectType'))
            and (not row.get('QTO_geom::Площадь_опалубки_м2')
                 or (_surface_formwork_kind(row.get('IfcClass'), row.get('Name'), row.get('ObjectType')) in ('slab', 'flight')
                     and 'QTO_geom::Горизонтальная_проекция_м2' not in row))):
        surface = _compute_surface_formwork_quantities(element)
        if surface:
            row['QTO_geom::Площадь_опалубки_м2'] = surface['formwork_m2']
            row['QTO_geom::Площадь_низа_м2'] = surface['underside_m2']
            row['QTO_geom::Площадь_торцов_м2'] = surface['edges_m2']
            if 'projection_m2' in surface:
                row['QTO_geom::Горизонтальная_проекция_м2'] = surface['projection_m2']
                row['QTO_geom::Горизонтальная_проекция_без_отверстий_м2'] = surface['gross_projection_m2']
    if row.get('IfcClass') not in ('IfcSlab', 'IfcStairFlight'):
        return row
    def positive(key):
        try:
            return float(row.get(key) or 0) > 0
        except (ValueError, TypeError):
            return False
    foundation = _is_foundation_plate(row.get('Name'), row.get('ObjectType'))
    perimeter = positive("QTO::Qto_SlabBaseQuantities::Perimeter") or positive('QTO_geom::Внешний_периметр_мм') or (not foundation and positive("QTO_bbox::Периметр_мм"))
    depth = positive("QTO::Qto_SlabBaseQuantities::Depth") or positive("QTO_bbox::Толщина_мм")
    if not perimeter or not depth:
        values = _compute_formwork_quantities(element)
        if not perimeter and values.get("perimeter_mm"):
            key = 'QTO_geom::Внешний_периметр_мм' if values.get('method') == 'outer_contour' else 'QTO_bbox::Периметр_мм'
            row[key] = values["perimeter_mm"]
        if not depth and values.get("depth_mm"):
            row["QTO_bbox::Толщина_мм"] = values["depth_mm"]
    return row


_WALL_OPENING_CLASSES = ('IfcWall', 'IfcWallStandardCase', 'IfcWallElementedCase')

# Колонка торцов проёмов стены: площадь вертикальных щёк вырезов, м².
OPENING_ENDS_COLUMN = 'QTO_geom::Площадь_торцов_проёмов_м2'
# Колонка торцов самой стены: два торца на каждое тело меша, м².
WALL_ENDS_COLUMN = 'QTO_geom::Площадь_торцов_стены_м2'


def _number(value):
    """Число из значения дампа (``None``, если не читается)."""
    if isinstance(value, bool) or value in (None, ''):
        return None
    try:
        return float(str(value).strip().replace(' ', '').replace(',', '.'))
    except (TypeError, ValueError):
        return None


def _opening_quantities_mm(opening):
    """Высота и глубина проёма (мм) из QTO проёма, если они выгружены."""
    height = depth = None
    try:
        relations = opening.IsDefinedBy
    except Exception:
        relations = []
    for relation in relations or []:
        try:
            if not relation.is_a('IfcRelDefinesByProperties'):
                continue
            definition = relation.RelatingPropertyDefinition
            if not definition.is_a('IfcElementQuantity'):
                continue
            for quantity in definition.Quantities:
                name = str(getattr(quantity, 'Name', '') or '')
                if name == 'Height' and height is None:
                    height = getattr(quantity, 'LengthValue', None)
                elif name == 'Depth' and depth is None:
                    depth = getattr(quantity, 'LengthValue', None)
        except Exception:
            continue
    return height, depth


def _body_items(element):
    """Элементы представления Body (по ним — число отдельных тел меша)."""
    items = []
    representation = getattr(element, 'Representation', None)
    for rep in getattr(representation, 'Representations', ()) or ():
        if rep.RepresentationIdentifier not in (None, 'Body'):
            continue
        items.extend(getattr(rep, 'Items', ()) or ())
    return items


def wall_ends_area_m2(row, element):
    """Площадь торцов стены, м²: ``2 × толщина × высота`` на каждое тело.

    Стена может быть собрана из нескольких отдельных тел (разрыв на
    деформационном шве, вставки). У каждого тела свои два торца, поэтому
    число торцов — ``2 × число элементов Body``. Число элементов Body
    совпадает с числом компонентов меша (проверено на модели: 5 161 стена).

    Returns:
        Площадь в м² или ``None``, если толщина/высота неизвестны.
    """
    thickness = _number(row.get('QTO::Qto_WallBaseQuantities::Width'))
    if thickness is None:
        thickness = _number(row.get('QTO_bbox::Ширина_мм'))
    height = _number(row.get('QTO::Qto_WallBaseQuantities::Height'))
    if height is None:
        height = _number(row.get('QTO_bbox::Высота_мм'))
    if not thickness or not height or thickness <= 0 or height <= 0:
        return None
    bodies = len(_body_items(element)) or 1
    return 2.0 * (thickness / 1000.0) * (height / 1000.0) * bodies


def opening_ends_area_m2(row, element):
    """Площадь торцов (вертикальных щёк) проёмов стены, м².

    При вырезании проёма в стене в геометрии остаются четыре поверхности
    откоса; две из них — вертикальные щёки размером «глубина проёма × высота
    проёма». Площадь лицевых граней стены они не уменьшают
    (``NetSideArea`` — площадь по срединной плоскости), торцами стены
    (``2 × толщина × высота``) тоже не являются, поэтому в требуемые
    параметры стены не входят. Для опалубки это реальные щиты, поэтому сумма
    ``2 × глубина × высота`` по всем проёмам элемента сохраняется отдельной
    колонкой (``OPENING_ENDS_COLUMN``).

    Высота берётся как вертикальный габарит проёма (``dz`` в мировых
    координатах): QTO проёма бывает недостоверным — в модели встречаются
    проёмы, у которых ``Height`` в QTO равен толщине стены, а ``Depth`` —
    её высоте (перепутаны оси). QTO ``Height`` используется, только если
    геометрию собрать не удалось. Глубина — QTO ``Depth``, но не больше
    толщины стены (``Depth`` тоже бывает завышен); при отсутствии — толщина
    стены.

    Args:
        row: строка сырого дампа элемента (нужна толщина стены).
        element: элемент IFC (стена).

    Returns:
        Площадь в м² или ``None``, если у элемента нет проёмов.
    """
    try:
        relations = element.HasOpenings
    except Exception:
        return None
    if not relations:
        return None
    wall_thickness = _number(row.get('QTO::Qto_WallBaseQuantities::Width'))
    if wall_thickness is None:
        wall_thickness = _number(row.get('QTO_bbox::Ширина_мм'))
    total = 0.0
    for relation in relations:
        opening = getattr(relation, 'RelatedOpeningElement', None)
        if opening is None:
            continue
        qto_height, qto_depth = _opening_quantities_mm(opening)
        height = None
        box = _fast_bbox_mm(opening)
        if box is not None:
            height = box[2]
        if not height or height <= 0:
            height = qto_height
        depth = qto_depth
        if wall_thickness and (depth is None or depth > wall_thickness):
            depth = wall_thickness
        height = _number(height)
        depth = _number(depth)
        if not height or not depth or height <= 0 or depth <= 0:
            continue
        total += 2.0 * (depth / 1000.0) * (height / 1000.0)
    return total or None


def _height_geometry_complete(element):
    """No height conclusions from a partially supported representation.

    The first operand bounds a DIFFERENCE/INTERSECTION conservatively. UNION
    needs both operands and is deliberately left unresolved by this parser.
    """
    def supported(item):
        if item.is_a('IfcMappedItem'):
            return all(supported(child) for child in item.MappingSource.MappedRepresentation.Items)
        if item.is_a('IfcBooleanResult'):
            return item.Operator in ('DIFFERENCE','INTERSECTION') and supported(item.FirstOperand)
        return any(item.is_a(kind) for kind in ('IfcExtrudedAreaSolid','IfcPolygonalFaceSet',
                                                'IfcTriangulatedFaceSet','IfcFacetedBrep'))
    rep = getattr(element,'Representation',None)
    bodies = [r for r in getattr(rep,'Representations',()) or ()
              if r.RepresentationIdentifier in (None,'Body')]
    return bool(bodies) and all(supported(item) for r in bodies for item in r.Items)


def enrich_work_height(row, element):
    """Reuse parsed vertices; no additional geometry-engine/tessellation call."""
    from .work_height import ifc_fields
    if 'Отметка_низа_м' in row:
        return row
    parts = []
    try:
        if not _height_geometry_complete(element):
            row.update(ifc_fields(element))
            return row
        rep = getattr(element, 'Representation', None)
        for representation in getattr(rep, 'Representations', ()) or ():
            if representation.RepresentationIdentifier not in (None, 'Body'):
                continue
            for item in representation.Items:
                _collect_geom_from_item(item, parts)
        vertices = np.vstack([part[0] for part in parts]) if parts else None
        row.update(ifc_fields(element, vertices))
    except Exception as exc:
        logger.debug('Не определены отметки работ IFC: %s', exc)
        row.update(ifc_fields(element))
    return row


def _collect_element_data(element, compute_bbox_qto=True, compute_dimensions=False,
                          opening_ends=False):
    """Собирает все параметры одного элемента в словарь (колонка -> значение).

    Args:
        element: элемент IFC (IfcElement).
        compute_bbox_qto: вычислять отсутствующие параметры этого элемента
            из геометрии независимо от наличия других QTO.
        compute_dimensions: дополнительно вычислять из геометрии недостающие
            габариты (длину/ширину/высоту). Нужно проверке ЦИМ: в моделях
            QTO может содержать объём и площадь, но не содержать размеров
            сечения (балки, пластины, колонны).
        opening_ends: сохранять у стен площадь торцов проёмов
            (``OPENING_ENDS_COLUMN``) — вертикальных щёк вырезов. Нужно
            проверке ЦИМ для расчёта опалубки по всем четырём боковым
            поверхностям; по умолчанию выключено, поэтому поведение режимов
            ЦС/ИИ не меняется.
    """
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
    propsets = _collect_propsets(element)
    for key, value in propsets.items():
        row[key] = _truncate(value)

    if compute_bbox_qto:
        from .ifc_geometry_quantities import fill_missing
        required = ("volume", "area")
        if compute_dimensions:
            required = ("volume", "area", "length", "width", "height")
        fill_missing(row, element, compute_fn=_compute_bbox_quantities,
                     required=required)

    from .ifc_quantities import stamp_ifc_volume
    stamp_ifc_volume(row, element)
    enrich_work_height(row, element)
    row = enrich_formwork_quantities(row, element)
    if opening_ends and element.is_a() in _WALL_OPENING_CLASSES:
        ends = wall_ends_area_m2(row, element)
        if ends:
            row[WALL_ENDS_COLUMN] = round(ends, 6)
        opening_ends_value = opening_ends_area_m2(row, element)
        if opening_ends_value:
            row[OPENING_ENDS_COLUMN] = round(opening_ends_value, 6)
    return row


# =====================================================================
#  ГЛАВНАЯ ФУНКЦИЯ
# =====================================================================

def dump_ifc_elements_raw(
    ifc_file: str,
    output_folder: str,
    model=None,
    exclude_classes=("IfcOpeningElement", "IfcBuildingElementProxy"),
    compute_dimensions: bool = False,
    dimension_classes=None,
    opening_ends: bool = False,
) -> str:
    """Сохраняет XLSX со всеми элементами IFC и их сырыми параметрами.

    Args:
        ifc_file: путь к IFC-файлу.
        output_folder: папка, куда сохранить результат (корень сессии).
        model: необязательная уже открытая модель ifcopenshell — если задана,
            повторное открытие файла не выполняется (экономия на больших IFC).
        exclude_classes: IFC-классы, которые не нужно включать в дамп. По
            умолчанию исключаются вырезы и прокси-элементы (поведение режимов
            ЦС/ИИ). Режим «Проверка ЦИМ модели» передаёт пустой кортеж, чтобы
            проверить и проёмы (``IfcOpeningElement``).
        compute_dimensions: вычислять из геометрии недостающие габариты
            (длину/ширину/высоту) для классов из ``dimension_classes``.
            Используется проверкой ЦИМ; по умолчанию выключено, поэтому
            поведение режимов ЦС/ИИ не меняется.
        dimension_classes: IFC-классы, для которых нужны габариты (например,
            классы требований ЦИМ). ``None`` — для всех элементов (не
            рекомендуется для больших моделей).
        opening_ends: сохранять у стен площадь торцов проёмов
            (``OPENING_ENDS_COLUMN``) — вертикальных щёк вырезов. Включается
            проверкой ЦИМ для расчёта опалубки стен по всем четырём боковым
            поверхностям; по умолчанию выключено, поэтому состав колонок
            дампа режимов ЦС/ИИ не меняется.

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

    if model is None:
        model = ifcopenshell.open(ifc_file)

    elements = model.by_type("IfcElement")
    logger.info(f"Найдено элементов IfcElement: {len(elements)}")

    if not elements:
        logger.warning("В IFC-файле не найдено ни одного элемента IfcElement")
        return ""

    warm_formwork_surface_cache(model, elements)

    # В режимах ЦС/ИИ вырезы (IfcOpeningElement) и прокси-элементы
    # (IfcBuildingElementProxy) не нужны в сыром дампе.
    # Важно: ifcopenshell entity_instance.is_a() принимает только одну строку
    # с именем типа, поэтому проверяем каждый класс отдельно.
    excluded = tuple(exclude_classes or ())
    if excluded:
        elements = [el for el in elements if not any(el.is_a(c) for c in excluded)]
        logger.info(f"Элементов после исключения {', '.join(excluded)}: {len(elements)}")

    if not elements:
        logger.warning("После исключения классов не осталось элементов")
        return ""

    # Missing geometry fields are filled independently for every element.
    compute_bbox_qto = True
    logger.info('Сначала используются QTO элемента; геометрия вычисляется только для недостающих величин.')

    rows = []
    all_columns = []
    seen_columns = set()

    for element in elements:
        element_class = element.is_a()
        needs_dimensions = bool(
            compute_dimensions
            and (dimension_classes is None or element_class in dimension_classes)
        )
        row = _collect_element_data(
            element,
            compute_bbox_qto=compute_bbox_qto,
            compute_dimensions=needs_dimensions,
            opening_ends=opening_ends,
        )
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
