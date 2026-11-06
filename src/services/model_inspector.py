"""Read individual IFC elements for the existing GLB viewer."""

import json
import math
from functools import lru_cache
from pathlib import Path


def _present(value):
    return value is not None and value != "" and not (
        isinstance(value, float) and not math.isfinite(value)
    )


@lru_cache(maxsize=2)
def _element_index(path, modified, size):
    # File stamps invalidate the index when a session's dump is regenerated.
    with open(path, encoding="utf-8") as source:
        rows = json.load(source)
    return {
        row["GlobalId"]: {key: value for key, value in row.items() if _present(value)}
        for row in rows if isinstance(row, dict) and row.get("GlobalId")
    }


def _unit_label(unit):
    from ifcopenshell.util.unit import get_unit_symbol
    try:
        symbol = get_unit_symbol(unit)
    except (AttributeError, KeyError):
        return str(getattr(unit, 'Name', '') or '')
    if symbol == '?':
        return str(getattr(unit, 'Name', '') or '')
    replacements = {'m':'м', 'mm':'мм', 'cm':'см', 'dm':'дм', 'km':'км',
                    'g':'г', 'kg':'кг', 's':'с', 'ft':'фут', 'in':'дюйм'}
    power = symbol[-1:] if symbol[-1:] in ('2', '3') else ''
    base = symbol[:-1] if power else symbol
    return replacements.get(base, base) + {'2':'²', '3':'³'}.get(power, '')


@lru_cache(maxsize=2)
def _ifc_unit_index(path, modified, size):
    """Read units once per source revision, without keeping the IFC model in memory."""
    import ifcopenshell
    from ifcopenshell.util.unit import get_property_unit, get_project_unit
    model = ifcopenshell.open(path)
    result = {}

    def units_for(definition):
        if definition.is_a('IfcElementQuantity'):
            prefix, properties = 'QTO', definition.Quantities or ()
        elif definition.is_a('IfcPropertySet'):
            prefix, properties = 'Свойство', definition.HasProperties or ()
        else:
            return {}
        units = {}
        for prop in properties:
            try:
                unit = get_property_unit(prop, model, use_cache=True)
            except (AttributeError, RuntimeError, TypeError, IndexError):
                unit = None
            label = _unit_label(unit) if unit else 'шт.' if prop.is_a('IfcQuantityCount') else ''
            units[f'{prefix}::{definition.Name or ""}::{prop.Name}'] = label
        return units

    # Instance values override type values, matching the raw dump reader.
    for relationship in model.by_type('IfcRelDefinesByProperties'):
        definition = relationship.RelatingPropertyDefinition
        definitions = [definition] if hasattr(definition, 'is_a') else definition
        units = {}
        for item in definitions or ():
            units.update(units_for(item))
        if units:
            for element in relationship.RelatedObjects or ():
                if getattr(element, 'GlobalId', None):
                    result.setdefault(element.GlobalId, {}).update(units)
    for relationship in model.by_type('IfcRelDefinesByType'):
        units = {}
        for definition in relationship.RelatingType.HasPropertySets or ():
            units.update(units_for(definition))
        if units:
            for element in relationship.RelatedObjects or ():
                if getattr(element, 'GlobalId', None):
                    target = result.setdefault(element.GlobalId, {})
                    for key, value in units.items():
                        target.setdefault(key, value)
    length = get_project_unit(model, 'LENGTHUNIT', use_cache=True) if model.by_type('IfcProject') else None
    return result, _unit_label(length) if length else ''


def element_details(session_dir, guid, ifc_path=None):
    """Use the unfiltered dump; fall back to IFC for older sessions/other products."""
    path = Path(session_dir) / "IFC_исходные_параметры.json"
    row = None
    if path.is_file():
        stamp = path.stat()
        row = _element_index(str(path), stamp.st_mtime_ns, stamp.st_size).get(guid)
    if row is None and ifc_path:
        import ifcopenshell
        from src.services.ifc_raw_dump import (
            _serialize_raw, _get_material, _get_storey, _collect_propsets,
        )

        model = ifcopenshell.open(ifc_path)
        try:
            element = model.by_guid(guid)
        except RuntimeError:
            element = None
        if element is not None:
            row = {key: _serialize_raw(value) for key, value in element.get_info().items()
                   if key not in ("id", "type")}
            row.update(IfcClass=element.is_a(), Материал=_get_material(element))
            row["Этаж"], row["Отметка_этажа"] = _get_storey(element)
            row.update(_collect_propsets(element))
    if row is None:
        return None
    fields = {key: value for key, value in row.items() if _present(value)}
    units = {}
    if ifc_path and Path(ifc_path).is_file():
        stamp = Path(ifc_path).stat()
        index, length_unit = _ifc_unit_index(str(ifc_path), stamp.st_mtime_ns, stamp.st_size)
        units = dict(index.get(guid, {}))
        if length_unit:
            units['Отметка_этажа'] = length_unit
    return {"guid": guid, "fields": fields, "units":{key:value for key,value in units.items() if key in fields and value}}


def model_quantities(session_dir, ifc_path=None):
    """Read numeric quantity fields in one batch, retaining explicit IFC units."""
    path = Path(session_dir) / 'IFC_исходные_параметры.json'
    if not path.is_file():
        return {}
    stamp = path.stat()
    rows = _element_index(str(path), stamp.st_mtime_ns, stamp.st_size)
    units = {}
    if ifc_path and Path(ifc_path).is_file():
        stamp = Path(ifc_path).stat()
        units, _ = _ifc_unit_index(str(ifc_path), stamp.st_mtime_ns, stamp.st_size)
    result = {}
    for guid, row in rows.items():
        fields = {}
        for key, value in row.items():
            if not (key.startswith(('QTO::', 'QTO_bbox::', 'QTO_geom::'))
                    or key.startswith(('Объём', 'Объем', 'Площадь'))):
                continue
            if isinstance(value, bool):
                continue
            try:
                numeric = float(str(value).replace(',', '.'))
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric):
                fields[key] = numeric
        if fields:
            result[guid] = {'fields': fields, 'units': {
                key: value for key, value in units.get(guid, {}).items() if key in fields}}
    return result


def model_groups(run_folder, processing_type):
    """Resolve membership only from exact GUIDs or indices into this run's rows."""
    import pandas as pd

    folder = Path(run_folder)
    tree_path = folder / ("filtered_elements_grouped_mssk.json" if processing_type == "KR"
                          else "filtered_elements_grouped_AR.json")
    rows_path = folder / "filtered_elements.xlsx"
    if not tree_path.is_file() or not rows_path.is_file():
        return {"status": "not_ready", "groups": []}
    tree = json.loads(tree_path.read_text(encoding="utf-8"))
    selected_path = folder / "selected_elements_grouped.json"
    if processing_type == "KR" and selected_path.is_file():
        from src.services.cs_model_members import exact_members

        selected = json.loads(selected_path.read_text(encoding="utf-8"))
        members = exact_members(folder, selected)
        if members is None or len(members) != len(selected):
            raise ValueError("Не удалось восстановить точный состав групп запуска")
        groups = []
        for group, rows in zip(selected, members):
            path = [group.get("_buildingPart"), group.get("buildingElementName"),
                    group.get("_rawMaterial")]
            for characteristic in group.get("characteristics", []):
                values = [str(value["strValue"]) for value in characteristic.get("values", [])
                          if value.get("strValue") is not None]
                if values and characteristic.get("name") != "Материал":
                    path.append(f'{characteristic.get("name", "Параметр")}: {", ".join(values)}')
            path.append(group.get("_typeKey"))
            groups.append(_group_record(len(groups), [str(part) for part in path if part], rows))
    else:
        frame = pd.read_excel(rows_path, sheet_name="Данные", usecols=lambda col: col in (
            "GlobalId", "Имя", "Тип элемента", "Этаж")).fillna("")
        rows = frame.to_dict("records")
        groups = []

        def visit(nodes, path):
            for group in nodes:
                names = path + [str(group.get("name", "Группа"))]
                if group.get("children"):
                    visit(group["children"], names)
                else:
                    indices = group.get("indices", [])
                    if any(type(index) is not int or not 0 <= index < len(rows) for index in indices):
                        raise ValueError("Индексы группы не соответствуют строкам выбранного запуска")
                    groups.append(_group_record(len(groups), names, [rows[index] for index in indices]))

        visit(tree, [])
    return {"status": "ready", "groups": groups}


def _group_record(index, path, rows):
    members = {}
    for row in rows:
        guid = row.get("GlobalId")
        if guid and isinstance(guid, str):
            members[guid] = {"guid": guid, "name": str(row.get("Имя") or guid),
                             "ifcClass": str(row.get("Тип элемента") or ""),
                             "storey": str(row.get("Этаж") or "")}
    return {"id": str(index), "path": path, "members": list(members.values())}


def model_relations(run_folder, processing_type):
    """Read saved evidence only; opening the viewer never launches calculations."""
    from .vor_generation.model_links import workbook_digest

    folder = Path(run_folder)
    from .model_documents import saved_perechen, saved_estimate, recover_vor, sheet_rows, same_quantity, document_groups
    links, unconfirmed, notes = [], 0, []
    try:
        canonical = saved_perechen(folder, processing_type)
    except (ValueError, KeyError, OSError) as error:
        canonical = None
        notes.append('Перечень: ' + str(error))
    final_path = folder / f"final_result_{processing_type}.json"
    if canonical is not None:
        links.extend(canonical)
    elif final_path.is_file():
        final = json.loads(final_path.read_text(encoding='utf-8'))
        for group in final.get('elementGroups', []):
            element = group.get('element') or {}
            guids = list(dict.fromkeys(element.get('guids') or []))
            # Older JSONs could contain the broader, pre-split membership.
            # A viewer must not present those as exact links.
            if not guids or element.get('count') != len(guids):
                unconfirmed += 1
                continue
            for index, position in enumerate(group.get('positions') or []):
                source = position.get('quantityCalculation')
                source = source if isinstance(source, dict) else {}
                links.append({'id': f'per:{group.get("groupId")}:{index}', 'kind': 'perechen',
                              'groupId': group.get('groupId'), 'groupName': element.get('typeName'),
                              'name': position.get('name'), 'code': position.get('code'),
                              'unit': position.get('unit'), 'quantity': position.get('quantity'),
                              'quantityStatus': position.get('quantityStatus'), 'guids': guids,
                              'file': source.get('file') or final_path.name,
                              'sheet': source.get('sheet'), 'row': source.get('row'),
                              'relation': 'group_position'})
    for trace_path in sorted(folder.glob('*.model-links.json')):
        trace = json.loads(trace_path.read_text(encoding='utf-8'))
        name = trace.get('workbook', '')
        workbook = folder / name
        if not name or Path(name).name != name or not workbook.is_file():
            unconfirmed += 1
            continue
        if workbook_digest(workbook) != trace.get('sha256'):
            unconfirmed += 1
            continue
        for item in trace.get('rows', []):
            if not item.get('guids'):
                continue
            links.append({**item, 'id': f'vor:{name}:{item["row"]}', 'kind': 'vor',
                          'file': name, 'sheet': trace.get('sheet')})
    links = [item for item in links if item.get('guids')]
    try:
        links.extend(saved_estimate(folder, [item for item in links if item['kind'] == 'perechen']))
    except (ValueError, KeyError, OSError) as error:
        notes.append('Смета: ' + str(error))
    target = folder / 'Сформированный_ВОР_типовой.xlsx'
    if target.is_file() and not target.with_suffix('.model-links.json').is_file():
        raw = folder / 'IFC_исходные_параметры.json'
        if not raw.is_file():
            raw = folder.parent / 'IFC_исходные_параметры.json'
        try:
            links.extend(recover_vor(folder, raw))
        except (ValueError, KeyError, OSError) as error:
            notes.append('Типовая ВОР: ' + str(error))
    combined = folder / 'Типовая_ВОР_сверка.xlsx'
    if combined.is_file():
        try:
            rows = sheet_rows(combined, 'Сформированная ВОР')
            for item in links:
                if item['kind'] != 'vor' or not 1 <= item['row'] <= len(rows):
                    continue
                row = rows[item['row']-1]
                if str(row[1] or '') == item['name'] and same_quantity(row[3], item['quantity']):
                    item.update(originFile=item['file'], file=combined.name, sheet='Сформированная ВОР')
        except (KeyError, ValueError, OSError):
            pass
    return {'items': links, 'groups':document_groups(folder, links), 'unconfirmed': unconfirmed, 'notes':notes,
            'counts':{kind:sum(item['kind'] == kind for item in links) for kind in ('perechen','vor','estimate')},
            'vorTraced': any(link['kind'] == 'vor' for link in links)}
