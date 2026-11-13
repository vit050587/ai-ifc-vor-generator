"""Shared SI geometry fallback for individual IFC elements in both exports."""
from collections import Counter
import logging
import weakref

import ifcopenshell
import ifcopenshell.geom
import numpy as np

log = logging.getLogger(__name__)
_CACHE = weakref.WeakKeyDictionary()
BBOX_FIELDS = ('QTO_bbox::Длина_мм', 'QTO_bbox::Ширина_мм', 'QTO_bbox::Высота_мм',
               'QTO_bbox::Площадь_поверхности_м2', 'QTO_bbox::Габаритный_объём_м3')


def mesh_quantities(vertices, faces, ifc_class):
    """Engine vertices are local metres, independently of project length units."""
    vertices = np.asarray(vertices, dtype=float).reshape(-1, 3)
    faces = np.asarray(faces, dtype=int).reshape(-1, 3)
    if not len(vertices) or not np.isfinite(vertices).all():
        return {}
    dims = np.ptp(vertices, axis=0)
    if ifc_class in ('IfcWall', 'IfcWallStandardCase', 'IfcCurtainWall', 'IfcColumn', 'IfcPile'):
        dimensions = (*sorted(dims[:2], reverse=True), dims[2])
    else:
        dimensions = sorted(dims, reverse=True)
    result = dict(zip(BBOX_FIELDS[:3], (float(v * 1000) for v in dimensions)))
    result[BBOX_FIELDS[4]] = float(np.prod(dims))
    if not len(faces):
        return result
    # Weld face-boundary duplicates before checking whether the mesh is closed.
    _, remap = np.unique(np.round(vertices, 9), axis=0, return_inverse=True)
    welded = remap[faces]
    valid = (welded[:, 0] != welded[:, 1]) & (welded[:, 1] != welded[:, 2]) & (welded[:, 2] != welded[:, 0])
    faces, welded = faces[valid], welded[valid]
    triangles = vertices[faces]
    crosses = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    result[BBOX_FIELDS[3]] = float(np.linalg.norm(crosses, axis=1).sum() / 2)
    edges = Counter(tuple(sorted((int(a), int(b)))) for face in welded
                    for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])))
    oriented = Counter((int(a), int(b)) for face in welded
                      for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])))
    closed = bool(edges) and all(count == 2 and oriented[(a, b)] == oriented[(b, a)] == 1
                                for (a, b), count in edges.items())
    if closed:
        # Shift the origin to avoid cancellation for distant representation coordinates.
        relative = triangles - vertices.mean(axis=0)
        volume = abs(float(np.einsum('ij,ij->i', relative[:, 0],
                                    np.cross(relative[:, 1], relative[:, 2])).sum() / 6))
        if volume > 0 and np.isfinite(volume):
            result['QTO_geom::Объём_м3'] = volume
            # Stable alias for existing geometry mappings; never a box product.
            result['QTO_bbox::Объём_м3'] = volume
    result['QTO_geom::Метод'] = ('Замкнутая геометрия IFC, вырезы учтены' if closed
                               else 'Незамкнутая геометрия: рассчитаны только габариты и площадь')
    return result


def compute(element):
    if not getattr(element, 'Representation', None):
        return {}
    model = getattr(element, 'file', None)
    cache = _CACHE.setdefault(model, {}) if isinstance(model, ifcopenshell.file) else None
    key = (element.id(), element.Representation.id(),
           tuple(rel.id() for rel in getattr(element, 'HasOpenings', ()))) if cache is not None else None
    if cache is not None and key in cache:
        return dict(cache[key])
    try:
        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, False)
        settings.set(settings.CONVERT_BACK_UNITS, False)
        shape = ifcopenshell.geom.create_shape(settings, element)
        result = mesh_quantities(shape.geometry.verts, shape.geometry.faces, element.is_a())
    except (RuntimeError, ValueError, TypeError, AttributeError, IndexError) as exc:
        log.debug('Не удалось вычислить геометрию %s: %s', getattr(element, 'GlobalId', '?'), exc)
        result = {}
    if cache is not None:
        cache[key] = result
    return dict(result)


_FIELDS = {
    'volume': ('QTO_geom::Объём_м3', 'QTO_bbox::Объём_м3'),
    'area': ('QTO_bbox::Площадь_поверхности_м2',),
    'length': ('QTO_bbox::Длина_мм',),
    'width': ('QTO_bbox::Ширина_мм',),
    'height': ('QTO_bbox::Высота_мм',),
}

# Габариты, которые проверка ЦИМ запрашивает дополнительно (compute_dimensions).
_DIMENSION_METRICS = frozenset(('length', 'width', 'height'))

# Плиты (включая наследников) — единственный класс, у которого габариты
# запрашиваются не для самих требований ЦИМ: в требованиях IfcSlab (В.2/В.3)
# есть только толщина, периметр, площадь и объём, а они берутся из
# Qto_SlabBaseQuantities (Depth, Perimeter, Net/GrossArea, Net/GrossVolume).
_SLAB_CLASSES = ('IfcSlab', 'IfcSlabStandardCase', 'IfcSlabElementedCase')


def _available(row, element, metric):
    """Check the requested measure, not the presence of some unrelated QTO."""
    from .ifc_quantities import number, resolve_volume, stamp_ifc_volume, VALUE
    if any(number(row.get(key)) is not None for key in _FIELDS[metric]):
        return True
    if metric == 'volume':
        if isinstance(getattr(element, 'file', None), ifcopenshell.file):
            native = stamp_ifc_volume({}, element)
            return number(native.get(VALUE)) is not None
        return resolve_volume(row).value is not None
    # Raw and normalized exports have different field names. Read the typed
    # IFC quantities so both paths make the same decision, independent of names
    # of the quantity set or project language.
    names = {
        'area': {'area', 'netarea', 'grossarea', 'netsidearea', 'grosssidearea',
                 'netsurfacearea', 'grosssurfacearea', 'surfacearea', 'площадь',
                 'чистаяплощадь', 'площадьнетто', 'площадьбрутто', 'площадьповерхности'},
        'length': {'length', 'netlength', 'grosslength', 'длина'},
        'width': {'width', 'thickness', 'depth', 'ширина', 'толщина'},
        'height': {'height', 'высота'},
    }[metric]
    import re
    def clean(name):
        return re.sub(r'[\s_-]', '', str(name).lower())
    definitions = [getattr(rel, 'RelatingPropertyDefinition', None)
                   for rel in getattr(element, 'IsDefinedBy', ()) or ()
                   if rel.is_a('IfcRelDefinesByProperties')]
    from .ifc_raw_dump import _iter_type_propsets
    if isinstance(getattr(element, 'file', None), ifcopenshell.file):
        definitions.extend(_iter_type_propsets(element))
    for definition in definitions:
        if definition is None or not definition.is_a('IfcElementQuantity'):
            continue
        for quantity in definition.Quantities or ():
            if clean(quantity.Name) not in names:
                continue
            value = (getattr(quantity, 'AreaValue', None) if metric == 'area'
                     else getattr(quantity, 'LengthValue', None))
            if number(value) is not None:
                return True
    return False


def _is_slab(element) -> bool:
    """True для плит, габариты которых не входят в требования ЦИМ."""
    try:
        return any(element.is_a(name) for name in _SLAB_CLASSES)
    except Exception:
        return False


def fill_missing(row, element, compute_fn=None, required=('volume', 'area')):
    """QTO first; calculate and publish only requested missing measures.

    Dimensions are also available when volume itself needs geometry. A missing
    diagnostic bounding-box column is never a reason to recalculate valid QTO.
    Slab dimensions are never calculated when QTO already has volume and area:
    the ЦИМ requirements for ``IfcSlab`` do not contain length/width/height.
    """
    from .ifc_quantities import number
    missing = {metric for metric in required if not _available(row, element, metric)}
    if not missing:
        return row
    if 'volume' in missing:
        missing.update(metric for metric in ('length', 'width', 'height')
                       if not _available(row, element, metric))
    elif missing <= _DIMENSION_METRICS and _is_slab(element) \
            and _available(row, element, 'area'):
        # Плита: объём и площадь уже в QTO, а габариты в требованиях ЦИМ для
        # IfcSlab не используются. create_shape для плиты (вычитание проёмов)
        # стоит до 19 с на элемент — не вызываем его ради неиспользуемых колонок.
        return row
    allowed = {key for metric in missing for key in _FIELDS[metric]}
    if 'volume' in missing:
        allowed.add('QTO_bbox::Габаритный_объём_м3')
    values = (compute_fn or compute)(element)
    for key, value in values.items():
        if key not in allowed:
            continue
        if number(row.get(key)) is None:
            row[key] = value
    if allowed.intersection(values) and 'QTO_geom::Метод' in values:
        row['QTO_geom::Метод'] = values['QTO_geom::Метод']
    return row
