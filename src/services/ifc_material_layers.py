"""Flatten ordered IFC material layers, keeping thickness in explicit mm."""
def layer_properties(element):
    try:
        from ifcopenshell.util.element import get_material
        from ifcopenshell.util.unit import calculate_unit_scale
        material = get_material(element, should_inherit=True)
        if material is None:
            return {}
        if material.is_a('IfcMaterialLayerSetUsage'):
            material = material.ForLayerSet
        if not material.is_a('IfcMaterialLayerSet'):
            return {}
        scale = calculate_unit_scale(element.file) * 1000
        result = {}
        for index, layer in enumerate(material.MaterialLayers or [], 1):
            prefix = f'Материальный_слой_{index}'
            result[prefix + '_Материал'] = str(getattr(layer.Material, 'Name', '') or '')
            if layer.LayerThickness is not None:
                result[prefix + '_Толщина_мм'] = float(layer.LayerThickness) * scale
        return result
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return {}
