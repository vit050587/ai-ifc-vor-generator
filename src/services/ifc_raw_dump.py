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
import numpy as np
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
    """Вычисляет количества элемента из геометрии (bbox-замена QTO).

    Возвращает словарь с ключами вида ``QTO_bbox::{Параметр}``:
      * ``Объём_м3``               — объём меша (м³);
      * ``Длина_мм``               — наибольший габарит bbox (мм);
      * ``Ширина_мм``              — средний габарит bbox (мм);
      * ``Высота_мм``              — наименьший габарит bbox (мм);
      * ``Площадь_поверхности_м2`` — площадь поверхности меша (м²).

    Объём для тел выдавливания (IfcExtrudedAreaSolid) вычисляется точно
    как ``площадь_сечения × глубина`` (с вычетом отверстий). Для
    тесселированных мешей используется метод сигнатур тетраэдров
    (приближённо корректный и для мешей с мелкими дефектами). Если
    полученный объём нулевой или отрицательный — используется
    bbox-объём (Длина × Ширина × Высота) как запасной вариант.

    Все значения округлены: объём — 4 знака, габариты — 1 знак,
    площадь — 4 знака. Если геометрия отсутствует — пустой словарь.
    """
    result = {}
    try:
        rep = getattr(element, "Representation", None)
        if not rep:
            return result

        # ObjectPlacement элемента НЕ применяется: он лишь позиционирует
        # элемент в проекте (сдвиг + поворот), не меняя его реальные
        # размеры. Габариты вычисляются в локальной системе координат
        # элемента. Трансформация IfcMappedItem (MappingTarget) применяется
        # внутри _collect_geom_from_item — она является частью определения
        # геометрии экземпляра (масштаб, зеркалирование и т.п.).

        parts = []
        for r in rep.Representations:
            # Пропускаем негеометрические представления (FootPrint, Axis и т.п.),
            # оставляем только пространственные тела/поверхности.
            rep_type = getattr(r, "RepresentationType", None) or ""
            if rep_type in ("Curve2D", "Curve3D", "Point", "GeometricSet"):
                continue
            for item in r.Items:
                _collect_geom_from_item(item, parts)

        if not parts:
            return result

        all_verts = []
        total_volume = 0.0
        total_area = 0.0

        for verts, faces, kind, profile_area, surface_area in parts:
            all_verts.append(verts)

            if kind == 'extruded':
                # Объём тела выдавливания: площадь сечения (с вычетом
                # отверстий) × глубина. Глубину берём как расстояние
                # между нижним и верхним основаниями.
                n = len(verts)
                half = n // 2
                if half > 0 and profile_area is not None and profile_area > 0:
                    v_bottom = verts[0]
                    v_top = verts[half]
                    depth = float(np.linalg.norm(v_top - v_bottom))
                    total_volume += profile_area * depth
                # Площадь поверхности — точная формула (с учётом отверстий),
                # если она вычислена; иначе падаем на триангуляцию.
                if surface_area is not None and surface_area > 0:
                    total_area += surface_area
                else:
                    total_area += _compute_surface_area(verts, faces)
            else:
                # Поверхностный меш — метод тетраэдров (только замкнутые).
                total_volume += _compute_mesh_volume(verts, faces)
                total_area += _compute_surface_area(verts, faces)

        if not all_verts:
            return result

        combined = np.vstack(all_verts)
        # Габариты bbox по осям, отсортированные по убыванию:
        # Длина (наибольший) → Ширина → Высота (наименьший).
        dims = combined.max(axis=0) - combined.min(axis=0)
        dims_sorted = np.sort(dims)[::-1]

        # Объём bbox (Длина × Ширина × Высота) — fallback на случай,
        # если объём из геометрии получился нулевым или аномально малым
        # (сильно «дырявый» меш, вырожденная геометрия и т.п.).
        bbox_volume = float(dims_sorted[0] * dims_sorted[1] * dims_sorted[2])

        if total_volume <= 0.0:
            logger.debug(
                f"Объём из геометрии для {getattr(element, 'GlobalId', '?')} "
                f"равен {total_volume:.3f} мм³ — использую bbox-объём "
                f"{bbox_volume:.3f} мм³."
            )
            total_volume = bbox_volume

        # Координаты в IFC — в миллиметрах.
        result = {
            "QTO_bbox::Объём_м3": round(float(total_volume) / 1e9, 4),
            "QTO_bbox::Длина_мм": round(float(dims_sorted[0]), 1),
            "QTO_bbox::Ширина_мм": round(float(dims_sorted[1]), 1),
            "QTO_bbox::Высота_мм": round(float(dims_sorted[2]), 1),
            "QTO_bbox::Площадь_поверхности_м2": round(float(total_area) / 1e6, 4),
        }
    except Exception as exc:
        gid = getattr(element, "GlobalId", "?")
        logger.debug(f"Ошибка вычисления bbox-количеств для элемента {gid}: {exc}")

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

def _collect_element_data(element, compute_bbox_qto=False):
    """Собирает все параметры одного элемента в словарь (колонка -> значение).

    Args:
        element: элемент IFC (IfcElement).
        compute_bbox_qto: если True и у элемента нет QTO,
            количества вычисляются из геометрии (bbox).
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
    has_qto = any(k.startswith("QTO::") for k in propsets)
    for key, value in propsets.items():
        row[key] = _truncate(value)

    # Если у элемента нет QTO и включён режим вычисления,
    # вычисляем количества из геометрии (bbox).
    # Колонки помечаются префиксом QTO_bbox:: — это маркер,
    # что значения получены из меша, а не из оригинального IFC.
    if compute_bbox_qto and not has_qto:
        for key, value in _compute_bbox_quantities(element).items():
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

    # В режиме АР вырезы (IfcOpeningElement) и прокси-элементы
    # (IfcBuildingElementProxy) не нужны в сыром дампе.
    # Важно: ifcopenshell entity_instance.is_a() принимает только одну строку
    # с именем типа, поэтому проверяем каждый класс отдельно.
    _EXCLUDE_CLASSES = ("IfcOpeningElement", "IfcBuildingElementProxy")
    elements = [el for el in elements if not any(el.is_a(c) for c in _EXCLUDE_CLASSES)]
    logger.info(f"Элементов после исключения {', '.join(_EXCLUDE_CLASSES)}: {len(elements)}")

    if not elements:
        logger.warning(f"После исключения {', '.join(_EXCLUDE_CLASSES)} не осталось элементов")
        return ""

    # ПРОВЕРКА НАЛИЧИЯ QTO: если ни у одного элемента нет IfcElementQuantity,
    # то количества (объём, габариты, площадь) вычисляются из геометрии (bbox).
    # Колонки с вычисленными значениями получают префикс QTO_bbox::.
    compute_bbox_qto = not _has_any_qto(elements)
    if compute_bbox_qto:
        logger.warning(
            "В IFC-файле не найдено QTO (IfcElementQuantity). "
            "Количества будут вычислены из геометрии (bbox) с префиксом QTO_bbox::."
        )
    else:
        logger.info("В IFC-файле найдены QTO (IfcElementQuantity). Количества из геометрии не вычисляются.")

    rows = []
    all_columns = []
    seen_columns = set()

    for element in elements:
        row = _collect_element_data(element, compute_bbox_qto=compute_bbox_qto)
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
