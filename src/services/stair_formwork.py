"""TSN 2001.3-6 §2.8: stair formwork uses horizontal projection, not mesh skin.

Keep the physical skin areas separately. No quantity is inferred from the
customer's total or from the ratio of concrete volume to formwork area.
"""
import hashlib
import json
import logging
from pathlib import Path

NET = 'QTO_geom::Горизонтальная_проекция_м2'
GROSS = 'QTO_geom::Горизонтальная_проекция_без_отверстий_м2'
NORMAL_NET = 'Геометрия_Горизонтальная_проекция_м2'
NORMAL_GROSS = 'Геометрия_Горизонтальная_проекция_без_отверстий_м2'
VERSION = 1
logger = logging.getLogger(__name__)


def horizontal_projection(vertices, triangles):
    """Union projected triangles: rotation, overlapping steps and holes matter."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    polygons = []
    for triangle in triangles:
        polygon = Polygon(vertices[triangle, :2])
        if polygon.area > 1e-12:
            polygons.append(polygon)
    if not polygons:
        return {}
    footprint = unary_union(polygons)
    parts = [footprint] if footprint.geom_type == 'Polygon' else footprint.geoms
    gross = sum(Polygon(part.exterior).area for part in parts if part.geom_type == 'Polygon')
    return {'projection_m2': float(footprint.area), 'gross_projection_m2': float(gross)}


def find_ifc(raw_path, preferred=None):
    if preferred and Path(preferred).is_file():
        return Path(preferred)
    # Only a unique original is safe; do not select an arbitrary IFC project.
    root = Path(raw_path).parent
    directories = (root, root.parent) if root.name.startswith('run_') else (root,)
    for directory in directories:
        candidates = list((directory / 'original').glob('*.ifc')) + list(directory.glob('*.ifc'))
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            return None
    return None


def hydrate_index(index, raw_path, ifc_path=None):
    """Upgrade legacy dumps from their original IFC without altering source data.

    Used by the public ElementIndex loader, so normal formation and subsequent
    comparisons get the same geometry. A content-keyed private cache avoids
    retessellating unchanged originals on each request.
    """
    members = [element for element in index.elements if element.kind == 'stair'
               and 'projection_area' not in element.quantities and element.guid]
    if not members:
        return
    source = find_ifc(raw_path, ifc_path)
    if source is None:
        return
    with source.open('rb') as stream:
        key = (VERSION, hashlib.file_digest(stream, 'sha256').hexdigest())
    path = Path(raw_path).with_name('.stair_projection_cache.json')
    values = {}
    try:
        saved = json.loads(path.read_text())
        if saved.get('key') == list(key):
            values = saved['values']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    missing = [element for element in members if element.guid not in values]
    if missing:
        import ifcopenshell
        from .ifc_raw_dump import warm_formwork_surface_cache, _compute_surface_formwork_quantities
        try:
            model = ifcopenshell.open(str(source))
        except (OSError, RuntimeError):
            logger.warning('Original IFC is unavailable for stair projection: %s', source)
            return
        selected = []
        for element in missing:
            try:
                selected.append(model.by_guid(element.guid))
            except RuntimeError:
                continue
        warm_formwork_surface_cache(model, selected)
        for element in selected:
            result = _compute_surface_formwork_quantities(element)
            if 'projection_m2' in result:
                values[element.GlobalId] = [result['projection_m2'], result['gross_projection_m2']]
        try:
            path.write_text(json.dumps({'key': key, 'values': values}), encoding='utf-8')
        except OSError:
            logger.debug('Could not persist stair projection cache: %s', path)
    for element in members:
        if element.guid in values:
            net, gross = values[element.guid]
            element.quantities.update(projection_area=net, gross_projection_area=gross)
            element.quantity_sources.update(projection_area=NET, gross_projection_area=GROSS)
