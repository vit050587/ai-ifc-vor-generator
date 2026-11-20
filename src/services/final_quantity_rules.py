"""Document reinforcement and summer curing rules shared by final exporters."""
import re

from .vor_generation import documents, mapping, template
from .vor_generation.source_rebar import copy_rebar


def is_rebar(work):
    return bool(re.search(r'арматур|стержн|каркас', str(work.get('title') or ''), re.I)) and not re.search(
        r'закладн|стекло|композит', str(work.get('title') or ''), re.I)


def allocations(groups, run_dir, raw_rows=None, *, return_pending=False):
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
    pending = copy_rebar(output, rows, rebar, 'ВОР заказчика: исходный тоннаж без множителей', preserve_unmatched=False)
    result = [{'tonnes': None, 'specs': [], 'categories': {}} for _ in groups]
    bucket = 'bars'
    for item in output:
        if item.volume is None or item.src_index % 2 == 0:
            continue
        target = result[item.src_index // 2]
        if '.' in str(item.label or ''):
            target['specs'].append((item.name_override, float(item.volume)))
            target['categories'][bucket]['specs'].append((item.name_override, float(item.volume)))
        else:
            bucket = mapping.rebar_bucket(item.name_override)
            target['categories'].setdefault(bucket, {'tonnes':0, 'specs':[]})['tonnes'] += float(item.volume)
            target['tonnes'] = (target['tonnes'] or 0) + float(item.volume)
    for target in result:
        totals = {}
        for name, mass in target['specs']:
            totals[name] = totals.get(name, 0) + mass
        target['specs'] = list(totals.items())
    return (result, pending) if return_pending else result


def group_scope(element, name, part):
    from .vor_generation.elements import _element_kind
    cls = element.get('ifc_class') or element.get('ifcClass') or (element.get('row') or {}).get('Тип элемента') or ''
    actual = element.get('name') or name
    name = actual + (' ' + name if actual != name else '')
    kind = _element_kind(cls, '', name, element.get('predefined_type') or '')
    return name, element.get('source_kind') or kind or mapping.detect_kind(name), mapping.detect_part(part)


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


def _category_work(bucket):
    title = {'bars':'Устройство арматурных стержней', 'frames':'Устройство арматурных каркасов',
             'releases':'Устройство арматурных выпусков'}[bucket]
    # A missing installation rate is not replaced with another category's rate.
    code = 'ВОР: '+{'bars':'стержни', 'frames':'каркасы', 'releases':'выпуски'}[bucket]
    return dict(pressmark=code, title=title, unit='т', unit_of_measure='т',
                is_resource=False, source_only=True, quantity={})


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
        categories = amount['categories']
        for bucket in categories:
            if not any(mapping.rebar_bucket(r['title']) == bucket for r in installation):
                row = _category_work(bucket);rows.append(row);installation.append(row)
        used = set()
        for row in installation:
            bucket = mapping.rebar_bucket(row['title'])
            tonnes = categories.get(bucket, {}).get('tonnes') if bucket not in used else None
            used.add(bucket)
            from .perechen_pipeline import _divisor
            row['volume'] = tonnes / _divisor(row['unit']) if tonnes is not None else None
            row['volume_source'] = 'ВОР заказчика: исходный тоннаж без множителей' if tonnes is not None else ''
            row['zp'], row['em'], row['mr'], row['total'] = _costs(row, row['volume'])
        for work in installation:
            if work.get('volume') is None:
                continue
            bucket = mapping.rebar_bucket(work['title'])
            resources = [_resource(name, mass) for name, mass in categories[bucket]['specs']]
            for row in resources:
                row['work'] = work['title'];row['rebar_bucket'] = bucket
                row['zp'], row['em'], row['mr'], row['total'] = _costs(row, row['volume']) if row['pressmark'].startswith('1.') else (None,) * 4
            at = rows.index(work)+1;rows[at:at] = resources


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
        categories = amount['categories']
        for bucket in categories:
            if not any(mapping.rebar_bucket(w['title']) == bucket for w in installation):
                work = _category_work(bucket);works.append(work);installation.append(work)
        used = set()
        for work in installation:
            bucket = mapping.rebar_bucket(work['title'])
            mass = categories.get(bucket, {}).get('tonnes') if bucket not in used else None
            used.add(bucket)
            work['customer_rebar_tonnes'] = mass
            work['volume_source'] = 'ВОР заказчика: исходный тоннаж без множителей' if mass is not None else ''
            if mass is None:
                continue
            resources = []
            for name, mass in categories[bucket]['specs']:
                resource = _resource(name, mass)
                previous = prior.get((resource['pressmark'], resource['title']), {})
                resource = {**resource, **previous, 'volume':mass, 'customer_rebar_tonnes':mass,
                            'volume_source':resource['volume_source'], 'unit':'т', 'unit_of_measure':'т',
                            'is_resource':True, 'rebar_bucket':bucket, 'parent_work':work['title']}
                resources.append(resource)
            at = works.index(work)+1;works[at:at] = resources
        entry['selected_works'] = works


def summer_curing(entries):
    """Compatibility entry point: preserve the project-selected curing norm."""
    for entry in entries:
        for work in entry.get('selected_works') or []:
            code = str(work.get('pressmark') or '')
            if code not in ('3.6-61-1', '3.6-98-1'):
                continue
            quantity = dict(work.get('quantity') or {})
            if code == '3.6-61-1':
                # Covered surface is not concrete volume or formwork surface.
                work.setdefault('unit_of_measure', '1 м2')
                quantity.pop('volume_m3', None)
                if 'covered_area_m2' in quantity:
                    quantity['area_m2'] = quantity['covered_area_m2']
            else:
                work.setdefault('unit_of_measure', '1 м3')
                quantity.pop('area_m2', None)
                if 'volume_m3' not in quantity:
                    source = entry.get('group_quantity') or {}
                    if 'volume_m3' in source:
                        quantity['volume_m3'] = source['volume_m3']
            work['quantity'] = quantity
            work['reason'] = (work.get('reason') or '') + '; ТСН п. 2.12: ' + (
                'м² укрываемой поверхности' if code == '3.6-61-1' else 'м³ бетона по проекту; п. 1.2.9.2: температура от +5 °C')


def refresh_concrete(entries, rows):
    """Refresh concrete from the same project/net rule used by the VOR."""
    from .ifc_quantities import resolve_volume
    for entry in entries:
        indices = entry.get('source_indices') or []
        if not indices or any(i < 0 or i >= len(rows) for i in indices):
            continue
        if not all(rows[i].get('GlobalId') for i in indices):
            continue
        representative = str((entry.get('element') or {}).get('global_id') or '')
        if representative and representative not in {str(rows[i].get('GlobalId')) for i in indices}:
            continue
        measures = [resolve_volume(rows[i]) for i in indices]
        total = sum(m.value for m in measures if m.value is not None)
        source = '; '.join(dict.fromkeys(m.source for m in measures if m.source))
        missing = any(m.value is None for m in measures)
        estimated = any(m.status == 'estimated' for m in measures)
        entry.setdefault('group_quantity', {})['volume_m3'] = total
        entry.setdefault('quantity_sources', {})['volume_m3'] = source
        for work in entry.get('selected_works') or []:
            unit = str(work.get('unit_of_measure') or work.get('unit') or '').replace('³', '3')
            if 'м3' not in unit and 'm3' not in unit:
                continue
            if work.get('is_resource'):
                continue
            work.setdefault('quantity', {})['volume_m3'] = total
            if missing:
                work['quantityStatus'] = 'partial_data' if total else 'missing_data'
                work['reason'] = 'Неполные данные: ' + source
            elif estimated:
                work['quantityStatus'] = 'estimated'
                work['reason'] = 'Оценка: ' + source


def _special_formwork(row, code):
    from .contact_formwork import contact_area
    from .stair_formwork import floor_projection
    if code.startswith(('3.6-116-', '3.6-117-')) or code in (
            '3.6-119-3', '3.6-119-4', '3.6-130-2', '3.6-130-3', '3.6-140-2', '3.6-140-3'):
        return contact_area(row, without_openings=True)
    if code.startswith('3.6-122-'):
        return floor_projection(row)
    return None


def refresh_formwork(entries, rows):
    """Recalculate saved group hints from TSN projection/contact rules."""
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
                code = str(work.get('pressmark') or '')
                special = [_special_formwork(rows[i], code) for i in indices]
                selected = special if special and special[0] is not None else measures
                work_area = sum(value for value, _ in selected if value is not None)
                work['normative_formwork_area_m2'] = work_area
                work['normative_formwork_source'] = '; '.join(dict.fromkeys(source for _, source in selected if source))
                work.setdefault('quantity', {})['area_m2'] = work_area
                if any(not source for _, source in selected):
                    work['quantityStatus'] = 'partial_data' if work_area else 'missing_data'
                    work['reason'] = 'Недостаточно данных: для перекрытий нет горизонтальной проекции без отверстий, для лестниц — проекции; общая площадь не подставляется'
                elif any('Оценка' in source for _, source in selected):
                    work['quantityStatus'] = 'estimated'
                    work['reason'] = '; '.join(source for _, source in selected)
            if str(work.get('pressmark') or '') == '3.6-136-1':
                from .ifc_quantities import number
                # A project schedule is a group quantity, not one count per IFC wall.
                shields = number(rows[indices[0]].get('ТСН::Щитов_на_этаже'))
                floors = number(rows[indices[0]].get('ТСН::Этажей_с_ограждением'))
                if shields is None or floors is None:
                    work['quantity'] = {}
                    work['quantityStatus'] = 'missing_data'
                    work['reason'] = 'ТСН п. 2.17: нужны количество щитов на этаже и число этажей'
                else:
                    work['quantity'] = {'count': shields * floors}


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
    """Retain source totals with missing or ambiguous model correspondence.

    Such rows belong to the existing template subsection, with an explicit
    missing-model reason; no geometry or installation rate is invented.
    """
    from .work_sections import catalog
    from .vor_generation.source_rebar import source_scope, shared_heading
    scopes = [group_scope(e.get('element') or {}, e.get('group_base_name') or '', e.get('part') or '') for e in entries]
    _, pending = allocations(scopes, '', raw_rows, return_pending=True)
    existing = {e.get('group_base_name') for e in entries}
    sections = catalog().sections
    for row in pending:
        kind, part, _ = source_scope(row)
        if not part and kind in ('foundation_slab', 'crane_foundation', 'equipment_foundation'):
            part = 'underground'
        elif not part and kind == 'parapet':
            part = 'above'
        name = shared_heading(row)
        if name in existing:
            continue
        section_kind = 'foundation_slab' if kind == 'crane_foundation' else kind
        section = next((s for s in sections if s.kind == section_kind and s.part == part), None)
        part_name = {'underground':'Подземная','above':'Надземная'}.get(part,'Часть здания не указана')
        path = list(section.path) if section else [part_name, row.external_scope or 'Исходные объёмы арматуры']
        entries.append({'element': {'name': name, 'source_kind':kind}, 'part':part_name,
                        'group_base_name': name, 'group_header': name,
                        'group_path':path, 'group_quantity': {}, 'selected_works': [],
                        'source_only': True, 'note': 'Общий объём ВОР заказчика: однозначная привязка к одной группе IFC отсутствует; тоннаж не распределён'})
        existing.add(name)


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
