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
            z = np.array([0.0, 0.0, 0.0])

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
    """Преобразует IfcExtrudedAreaSolid в (verts, faces).

    Извлекает 2D-профиль, трансформирует через Position в 3D,
    затем экструдирует вдоль ExtrudedDirection на Depth.
    Возвращает (np.ndarray вершин, list граней) или (None, None).
    """
    import math

    swept_area = item.SweptArea
    if swept_area is None:
        return None, None

    contours = _extract_profile_points_2d(swept_area)
    if not contours:
        return None, None

    outer = contours[0]
    if len(outer) < 3:
        return None, None

    # Матрица трансформации Position (IfcAxis2Placement3D).
    pos = getattr(item, "Position", None)
    M = _placement_to_matrix(pos)

    # Направление и глубина экструзии.
    ext_dir = item.ExtrudedDirection
    depth = float(item.Depth or 0)
    if ext_dir is None or depth <= 0:
        return None, None

    direction = np.array(ext_dir.DirectionRatios, dtype=float)
    direction = direction / np.linalg.norm(direction)
    # Вектор экструзии в мировых координатах.
    # Если Position задано, направление экструзии задаётся в локальной системе
    # Position и должно быть трансформировано.
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

    return verts, faces


def _collect_geom_from_item(item, parts):
    """Рекурсивно собирает геометрические части (verts, faces) из элемента-представления.

    Обрабатывает:
      * IfcMappedItem  — раскрывает через MappingSource.MappedRepresentation;
      * IfcPolygonalFaceSet / IfcTriangulatedFaceSet — тесселяция;
      * IfcFacetedBrep — граненая B-Rep модель;
      * IfcExtrudedAreaSolid — тело выдавливания (профиль + глубина);
      * IfcBooleanClippingResult — раскрывает через Operand (рекурсивно);
      * IfcBooleanResult — раскрывает первый операнд.

    ``parts`` — список накопленных кортежей (np.ndarray вершин, list граней).
    Индексы в гранях — 0-based, указывают на локальный массив вершин части.
    """
    try:
        if item.is_a("IfcMappedItem"):
            mrep = item.MappingSource.MappedRepresentation
            for it in mrep.Items:
                _collect_geom_from_item(it, parts)

        elif item.is_a("IfcPolygonalFaceSet") or item.is_a("IfcTriangulatedFaceSet"):
            coords = item.Coordinates.CoordList
            if not coords:
                return
            verts = np.array(coords, dtype=float)
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
                parts.append((verts, faces))

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
                    parts.append((np.array(verts, dtype=float), faces))

        elif item.is_a("IfcExtrudedAreaSolid"):
            verts, faces = _extruded_solid_to_mesh(item)
            if verts is not None and len(verts) > 0 and faces:
                parts.append((verts, faces))

        elif item.is_a("IfcBooleanClippingResult") or item.is_a("IfcBooleanResult"):
            # Boolean result: раскрываем первый операнд (тело).
            # Для bbox-оценки это достаточно — обрезка обычно уменьшает
            # габариты незначительно или не влияет на порядок величин.
            operand = getattr(item, "FirstOperand", None)
            if operand is None:
                operand = getattr(item, "Operand1", None)
            if operand is not None:
                _collect_geom_from_item(operand, parts)

    except Exception as exc:
        logger.debug(f"Ошибка извлечения геометрии из {item.is_a()}: {exc}")


def _compute_mesh_volume(verts, faces):
    """Вычисляет объём замкнутого меша методом сигнатур тетраэдров.

    Для каждой грани (триангулированной веером) считается
    signed volume тетраэдра (0, a, b, c). Сумма по модулю — объём.
    Координаты в мм, результат — в мм³.
    """
    total = 0.0
    for face in faces:
        # Триангуляция веером (fan) для многоугольных граней.
        for i in range(1, len(face) - 1):
            a = verts[face[0]]
            b = verts[face[i]]
            c = verts[face[i + 1]]
            total += np.dot(a, np.cross(b, c)) / 6.0
    return abs(total)


def _compute_surface_area(verts, faces):
    """Вычисляет площадь поверхности меша (сумма площадей треугольников).

    Координаты в мм, результат — в мм².
    """
    total = 0.0
    for face in faces:
        for i in range(1, len(face) - 1):
            a = verts[face[0]]
            b = verts[face[i]]
            c = verts[face[i + 1]]
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

    Все значения округлены: объём — 4 знака, габариты — 1 знак,
    площадь — 4 знака. Если геометрия отсутствует — пустой словарь.
    """
    result = {}
    try:
        rep = getattr(element, "Representation", None)
        if not rep:
            return result

        parts = []
        for r in rep.Representations:
            for item in r.Items:
                _collect_geom_from_item(item, parts)

        if not parts:
            return result

        all_verts = []
        total_volume = 0.0
        total_area = 0.0

        for verts, faces in parts:
            all_verts.append(verts)
            total_volume += _compute_mesh_volume(verts, faces)
            total_area += _compute_surface_area(verts, faces)

        if not all_verts:
            return result

        combined = np.vstack(all_verts)
        # Габариты bbox по осям, отсортированные по убыванию:
        # Длина (наибольший) → Ширина → Высота (наименьший).
        dims = combined.max(axis=0) - combined.min(axis=0)
        dims_sorted = np.sort(dims)[::-1]

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
