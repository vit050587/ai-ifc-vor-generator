"""Document reinforcement and summer curing rules shared by final exporters."""
import re

from .vor_generation import documents, mapping, template
from .vor_generation.source_rebar import copy_rebar


def is_rebar(work):
    return bool(re.search(r'арматур|стержн|каркас', str(work.get('title') or ''), re.I)) and not re.search(
        r'закладн|стекло|композит', str(work.get('title') or ''), re.I)


def allocations(groups, run_dir, raw_rows=None):
    """Copy each customer row once using the same scopes as the typical VOR.

    Groups are not created or moved. Tonnes are never apportioned by IFC volume.
    """
    if raw_rows is None:
        from .vor_comparison import _load_vor_rows
        raw_rows, _ = _load_vor_rows(str(run_dir))
    rebar = documents.extract_rebar(raw_rows or [])
    rows, output = [], []
    for number, group in enumerate(groups):
        name, kind, part = group
        index = number * 2
        rows.extend([
            template.TemplateRow(index=index, kind='block', number=name, element_kind=kind, part=part),
            template.TemplateRow(index=index + 1, kind='work', number='1', name='Устройство арматурных стержней',
                                 element_kind=kind, part=part, subsection=str(number))])
        output.extend([template.OutputRow(index, label=name), template.OutputRow(index + 1)])
    copy_rebar(output, rows, rebar, 'ВОР заказчика: исходный тоннаж без множителей')
    result = [{'tonnes': None, 'specs': []} for _ in groups]
    for item in output:
        if item.volume is None or item.src_index % 2 == 0:
            continue
        target = result[item.src_index // 2]
        if '.' in str(item.label or ''):
            target['specs'].append((item.name_override, float(item.volume)))
        else:
            target['tonnes'] = (target['tonnes'] or 0) + float(item.volume)
    for target in result:
        totals = {}
        for name, mass in target['specs']:
            totals[name] = totals.get(name, 0) + mass
        target['specs'] = list(totals.items())
    return result


def group_scope(element, name, part):
    from .vor_generation.elements import _element_kind
    cls = element.get('ifc_class') or element.get('ifcClass') or (element.get('row') or {}).get('Тип элемента') or ''
    actual = element.get('name') or name
    name = actual + (' ' + name if actual != name else '')
    kind = _element_kind(cls, '', name, element.get('predefined_type') or '')
    if re.search(r'приям', name, re.I):
        kind = 'foundation_slab'
    return name, kind or mapping.detect_kind(name), mapping.detect_part(part)


def _resource(name, tonnes):
    from . import perechen_kb as kb
    diameter = re.search(r'(?:d|д|ø|Ø|диаметр)\s*[=–-]?\s*(\d+)', name, re.I)
    cls = re.search(r'[АA]\s*\d{3,4}\s*[СC]?', name, re.I)
    code = kb.rebar_pressmark(cls.group(0), int(diameter.group(1))) if cls and diameter else None
    from .local_resources import load
    price = load().get(code) or {}
    return {'salary': 0, 'operation_of_machines': 0,
            'cur_salary': 0, 'cur_operation_of_machines': 0,
            'cost_of_material_resources': price.get('price_base'),
            'cur_cost_of_material_resources': price.get('price'),
            'pressmark': code or 'ВОР:Арматура', 'title': name, 'unit': 'т', 'volume': tonnes,
            'volume_source': 'ВОР заказчика: исходный тоннаж, сумма внутри группы без множителей',
            'is_resource': True, 'customer_rebar_tonnes': tonnes}


def apply_legacy_rebar(groups, run_dir, raw_rows=None):
    from .perechen_pipeline import _costs
    if raw_rows is None:
        from .vor_comparison import _load_vor_rows
        raw_rows, _ = _load_vor_rows(str(run_dir))
    candidates = [{'element': g.get('el') or {'name': g.get('element')},
                   'group_base_name': g.get('element'), 'part': g.get('part')} for g in groups]
    original_count = len(candidates)
    ensure_source_scopes(candidates, raw_rows)
    for entry in candidates[original_count:]:
        groups.append({'element': entry['group_base_name'], 'el': entry['element'], 'part': entry['part'],
                       'path': entry['group_path'], 'material': '', 'count': 0, 'indices': [],
                       'quantity': {}, 'rows': [], 'subsection': '', 'status': 'missing_model',
                       'source_only': True, 'reason': entry['note']})
    scopes = [group_scope(g.get('el') or {}, g.get('element') or '', g.get('part') or '') for g in groups]
    for group, amount in zip(groups, allocations(scopes, run_dir, raw_rows)):
        rows = group['rows']
        rows[:] = [r for r in rows if not (is_rebar(r) and r.get('is_resource'))]
        installation = [r for r in rows if is_rebar(r) and not r.get('is_resource')]
        for i, row in enumerate(installation):
            tonnes = amount['tonnes'] if i == 0 else None
            from .perechen_pipeline import _divisor
            row['volume'] = tonnes / _divisor(row['unit']) if tonnes is not None else None
            row['volume_source'] = 'ВОР заказчика: исходный тоннаж без множителей' if tonnes is not None else ''
            row['zp'], row['em'], row['mr'], row['total'] = _costs(row, row['volume'])
        resources = [_resource(name, mass) for name, mass in amount['specs']]
        for row in resources:
            row['work'] = 'Арматура по ВОР заказчика'
            row['zp'], row['em'], row['mr'], row['total'] = _costs(row, row['volume']) if row['pressmark'].startswith('1.') else (None,) * 4
        at = rows.index(installation[0]) + 1 if installation else len(rows)
        rows[at:at] = resources


def apply_api_rebar(entries, run_dir, raw_rows=None):
    if raw_rows is None:
        from .vor_comparison import _load_vor_rows
        raw_rows, _ = _load_vor_rows(str(run_dir))
        if not raw_rows and any('customer_rebar_tonnes' in w for e in entries for w in e.get('selected_works') or []):
            return
    ensure_source_scopes(entries, raw_rows)
    scopes = [group_scope(e.get('element') or {}, e.get('group_base_name') or
                         (e.get('element') or {}).get('name') or '', e.get('part') or '') for e in entries]
    for entry, amount in zip(entries, allocations(scopes, run_dir, raw_rows)):
        works = entry.get('selected_works') or []
        prior = {(w.get('pressmark'), w.get('title')): w for w in works if w.get('is_resource')}
        works[:] = [w for w in works if not (is_rebar(w) and (w.get('is_resource') or str(w.get('pressmark') or '').startswith('1.')))]
        installation = [w for w in works if is_rebar(w)]
        for i, work in enumerate(installation):
            work['customer_rebar_tonnes'] = amount['tonnes'] if i == 0 else None
            work['volume_source'] = 'ВОР заказчика: исходный тоннаж без множителей' if i == 0 and amount['tonnes'] is not None else ''
        resources = []
        for name, mass in amount['specs']:
            resource = _resource(name, mass)
            previous = prior.get((resource['pressmark'], resource['title']), {})
            resource = {**resource, **previous, 'volume': mass, 'customer_rebar_tonnes': mass}
            resource['unit_of_measure'] = 'т'
            resources.append(resource)
        at = works.index(installation[0]) + 1 if installation else len(works)
        works[at:at] = resources
        entry['selected_works'] = works


def summer_curing(entries):
    """Replace winter rates before price enrichment; do not reuse winter prices."""
    from .perechen_pipeline import _local_rate
    local = _local_rate('3.6-98-1') or {}
    for entry in entries:
        works, seen = [], False
        for work in entry.get('selected_works') or []:
            if str(work.get('pressmark') or '').startswith(('3.6-61-', '3.6-98-')):
                if seen:
                    continue
                seen = True
                if work.get('pressmark') != '3.6-98-1':
                    quantity = dict(entry.get('group_quantity') or work.get('quantity') or {})
                    work = {'pressmark': '3.6-98-1', 'title': local.get('title') or 'Уход за бетоном в летний период',
                            'unit_of_measure': '1 м3', 'work_id': local.get('id'), 'quantity': quantity,
                            **{dst: local.get(src) for src, dst in (('salary', 'salary'), ('curSalary', 'cur_salary'),
                                ('operationOfMachines', 'operation_of_machines'), ('curOperationOfMachines', 'cur_operation_of_machines'),
                                ('costOfMaterialResources', 'cost_of_material_resources'), ('curCostOfMaterialResources', 'cur_cost_of_material_resources'))},
                            'reason': 'Уход за бетоном всегда в летний период (правило проекта)'}
            works.append(work)
        entry['selected_works'] = works


def refresh_formwork(entries, rows):
    """Recalculate saved group hints from current one-side/projection rules."""
    from .group_excel import _element_formwork_measure
    for entry in entries:
        indices = entry.get('source_indices') or []
        if not indices or any(i < 0 or i >= len(rows) for i in indices):
            continue
        if not all(str(rows[i].get('GlobalId') or '') for i in indices):
            continue
        representative = str((entry.get('element') or {}).get('global_id') or '')
        if representative and representative not in {str(rows[i].get('GlobalId')) for i in indices}:
            continue
        measures = []
        for i in indices:
            row = rows[i]
            perim = [k for k in row if 'perimeter' in k.lower()]
            depth = [k for k in row if re.search(r'depth|width|толщина', k, re.I)]
            measures.append(_element_formwork_measure(row, perim, depth))
        area = sum(value for value, _ in measures)
        entry['group_formwork_area_m2'] = area
        entry.setdefault('quantity_sources', {})['formwork_area'] = '; '.join(dict.fromkeys(src for _, src in measures if src))
        for work in entry.get('selected_works') or []:
            if 'опалуб' in str(work.get('title') or '').lower():
                work.setdefault('quantity', {})['area_m2'] = area
                if any(not source for _, source in measures):
                    work['quantityStatus'] = 'partial_data' if area else 'missing_data'
                    work['reason'] = 'Недостаточно данных: для перекрытий нет GrossArea или площади нижней поверхности, для лестниц — проекции; общая площадь не подставляется'


def slab_partitions(indices, rows):
    """Keep slab purpose, type, thickness and material separate across floors."""
    from .vor_generation.elements import _element_kind
    from .construction_method import construction_method
    buckets = {}
    for index in indices:
        row = rows[index]
        name = str(row.get('Имя') or row.get('Name') or '')
        cls = str(row.get('Тип элемента') or row.get('IfcClass') or '')
        kind = _element_kind(cls, '', name)
        if kind in ('slab', 'foundation_slab', 'stair', 'precast'):
            # Remove only Revit instance IDs, preserving dimensions in type names.
            type_name = re.sub(r':\d+(?::\d+)?(?=\s+(?:Run|Landing)\b|$)', '', name)
            if kind in ('stair', 'precast'):
                type_name = mapping.stair_component(name) or cls
            material = str(row.get('Материал') or row.get('Material') or '')
            thickness = tuple(str(row.get(k)) for k in sorted(row) if re.search(r'depth|толщина', k, re.I))
            key = kind, type_name, material, thickness, construction_method(row)
        else:
            key = kind,
        buckets.setdefault(key, []).append(index)
    return list(buckets.values())


def split_source_groups(groups, rows):
    """Separate slab types and effective element kinds before selecting rates."""
    from .ifc_reference_builder import _recompute_group_aggregates, _find_volume_columns
    if not rows:
        return groups
    headers = list(rows[0])
    volumes = _find_volume_columns(headers)
    result = []
    for group in groups:
        indices = group.get('indices') or []
        if not indices or any(i < 0 or i >= len(rows) for i in indices):
            result.append(group)
            continue
        partitions = slab_partitions(indices, rows)
        if len(partitions) == 1:
            result.append(group)
            continue
        for members in partitions:
            raw = [rows[i] for i in members]
            aggregate = _recompute_group_aggregates(raw, headers, volumes, precision=8)
            result.append({**group, **aggregate, 'indices': members, 'count': len(members)})
    return result


def ensure_source_scopes(entries, raw_rows):
    """Retain customer tonnes even when the IFC has no corresponding kind.

    Such rows belong to the existing template subsection, with an explicit
    missing-model reason; no geometry or installation rate is invented.
    """
    from .vor_generation.customer_vor import _section_kind, _detect_part
    from .work_sections import catalog
    rebar = documents.extract_rebar(raw_rows or [])
    existing = {(scope[1], scope[2]) for scope in (
        group_scope(e.get('element') or {}, e.get('group_base_name') or '', e.get('part') or '') for e in entries)}
    sections = catalog().sections
    for row in rebar.rows:
        kind, part = row.source_kind or _section_kind(row.section), row.source_part or _detect_part(row.section, '')
        if not part and kind in ('foundation_slab', 'crane_foundation', 'equipment_foundation'):
            part = 'underground'
        elif not part and kind == 'parapet':
            part = 'above'
        if not kind or not part or (kind, part) in existing:
            continue
        section_kind = 'foundation_slab' if kind == 'crane_foundation' else kind
        section = next((s for s in sections if s.kind == section_kind and s.part == part), None)
        if section is None:
            continue
        name = re.sub(r'^Подраздел\s+\d+[.]?\s*', '', section.path[-1], flags=re.I)
        if kind == 'crane_foundation':
            name = 'Фундамент под башенный кран'
        entries.append({'element': {'name': name}, 'part': 'Подземная' if part == 'underground' else 'Надземная',
                        'group_base_name': name, 'group_header': name + ' (ВОР заказчика; соответствие в IFC не найдено)',
                        'group_path': list(section.path), 'group_quantity': {}, 'selected_works': [],
                        'source_only': True, 'note': 'Проверить соответствие группы ВОР заказчика элементам IFC; тоннаж скопирован без пересчёта'})
        existing.add((kind, part))


def api_concrete_resources(entries):
    """Add the unpriced-in-work concrete mix for each concreting group."""
    from .perechen_pipeline import _concrete_resource
    from .local_resources import load
    for entry in entries:
        works = entry.get('selected_works') or []
        if any(str(w.get('pressmark') or '').startswith('1.') and re.search(
                r'смесь бетон|^бетон', str(w.get('title') or ''), re.I) for w in works):
            continue
        concrete = next((w for w in works if re.search(r'бетонирован', str(w.get('title') or ''), re.I)), None)
        if concrete is None:
            continue
        resource = _concrete_resource(entry.get('element') or {}, [])
        if resource is None:
            continue
        price = load().get(resource['pressmark']) or {}
        resource = {**resource, 'unit_of_measure': resource.get('unit') or 'м3',
                    'quantity': dict(entry.get('group_quantity') or concrete.get('quantity') or {}),
                    'salary': 0, 'cur_salary': 0, 'operation_of_machines': 0, 'cur_operation_of_machines': 0,
                    'cost_of_material_resources': price.get('price_base'), 'cur_cost_of_material_resources': price.get('price'),
                    'reason': 'Смесь по классу бетона; расход по koefs.xlsx от объёма бетонирования'}
        works.insert(works.index(concrete) + 1, resource)
        entry['selected_works'] = works
