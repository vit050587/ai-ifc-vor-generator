"""Exclude geometry-free IFC stair containers when their children are indexed."""
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _containers(path, size, modified):
    import ifcopenshell
    model = ifcopenshell.open(path)
    containers = {}
    def leaves(entity, visited):
        if entity.id() in visited:
            return None
        children = [child for rel in getattr(entity, 'IsDecomposedBy', ()) for child in rel.RelatedObjects]
        if not children:
            return {entity.GlobalId} if entity.is_a() in ('IfcStair', 'IfcStairFlight', 'IfcSlab') else None
        result = set()
        for child in children:
            found = leaves(child, visited | {entity.id()})
            if not found:
                return None
            result.update(found)
        return result
    for entity in model.by_type('IfcStair'):
        if entity.Representation or not entity.IsDecomposedBy:
            continue
        children = leaves(entity, set())
        if children:
            containers[entity.GlobalId] = children
    return containers


def hydrate_index(index, raw_path, ifc_path=None):
    if not any(element.ifc_class == 'IfcStair' for element in index.elements):
        return
    from ..stair_formwork import find_ifc
    source = find_ifc(raw_path, ifc_path)
    if source is None:
        return
    try:
        info = source.stat()
        containers = _containers(str(source.resolve()), info.st_size, info.st_mtime_ns)
    except (OSError, RuntimeError, ValueError):
        logger.warning('Cannot inspect stair decomposition in original IFC: %s', source)
        return
    indexed = {element.guid for element in index.elements if element.guid}
    removable = {guid for guid, children in containers.items() if children.issubset(indexed)}
    if removable:
        index.elements[:] = [element for element in index.elements if element.guid not in removable]
        index._names_cache.clear()
        logger.info('Excluded geometry-free stair containers with indexed children: %d', len(removable))
