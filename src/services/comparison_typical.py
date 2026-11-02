"""Compare completed standard VOR workbooks without rebuilding their rows."""
from collections import OrderedDict, defaultdict
from pathlib import Path
import json
import re
import unicodedata
from openpyxl import load_workbook
from .works_comparison.units import normalize_positions
from .vor_generation.quantity import source_summary

VERSION = 18
FILES = ('typical_generated.xlsx', 'typical_customer.xlsx')
NATIVE_FILES = ('Сформированный_ВОР.xlsx', 'ВОР_заказчика.xlsx')


def norm(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(value or '')).lower().replace('ё', 'е')).strip()


def read_typical_workbook(path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook['ВОР КР_ЖС']
        part = section = subsection = group = parent = parking = component = ''
        result = []
        for index, values in enumerate(sheet.iter_rows(min_row=4, max_col=6, values_only=True), 4):
            number, name, unit, quantity, calculation, reason = values
            number = str(number).strip() if number is not None else ''
            name = str(name).strip() if name is not None else ''
            if number and not name:
                heading = norm(number)
                parent = component = ''
                if heading.startswith('подраздел '):
                    subsection, group = number, ''
                elif heading.startswith('раздел '):
                    section, subsection, group = number, '', ''
                elif heading.startswith(('подземная часть', 'надземная часть')) or heading == 'автостоянка':
                    if heading == 'автостоянка':
                        parking = number
                        part, section = number, ''
                    else:
                        part = ' / '.join(filter(None, [parking, number]))
                        if not parking:
                            section = ''
                    subsection = group = ''
                else:
                    group = number
                continue
            context = dict(part=part, section=section, subsection=subsection, elementGroup=group)
            if not number and not name and not unit and quantity is None:
                # Preserve separators after the header, including completely empty template rows.
                result.append({**context, 'sourceRow': index, 'isSeparator': True})
                continue
            kind = 'resource' if re.fullmatch(r'\d+[.,]\d+', number) else 'work' if re.fullmatch(r'\d+', number) else 'variant'
            if kind == 'work':
                parent = name
                if 'лестниц' in norm(subsection):
                    from .vor_generation.mapping import stair_component
                    detected = stair_component(name)
                    if detected:
                        component = {'flight':'Лестничные марши','landing':'Лестничные площадки'}[detected]
            if component:
                context['elementGroup'] = ' → '.join(filter(None,[group,component]))
            value = None
            normalized_unit = str(unit or '').strip()
            if quantity is not None and str(quantity).strip() not in ('', '-', '—', '…'):
                if isinstance(quantity, str):
                    quantity = float(quantity.replace('\u00a0', '').replace(' ', '').replace(',', '.'))
                normalized = normalize_positions([{'unit': normalized_unit, 'quantity': quantity}])[0]
                value, normalized_unit = normalized['quantity'], normalized['unit']
            else:
                normalized_unit = normalize_positions([{'unit': normalized_unit, 'quantity': 0}])[0]['unit']
            result.append({**context, 'sourceRow': index, 'code': number, 'work': name,
                           'kind': kind, 'parentWork': parent if kind != 'work' else '',
                           'unit': normalized_unit, 'quantity': value, 'reason': str(reason or ''),
                           'quantityStatus': 'partial_data' if 'Неполные данные:' in str(reason or '') else 'calculated' if value is not None else 'missing_data',
                           'sourceFile': Path(path).name})
            result[-1]['calculation'] = str(calculation or '')
        return result
    finally:
        workbook.close()


def scope(row):
    return tuple(norm(row.get(k)) for k in ('part', 'section', 'subsection', 'elementGroup'))


def identity(row):
    # Resource numbering changes as diameters/grades are inserted; its name and owning work do not.
    return (row.get('kind', 'work'), norm(row.get('parentWork')), norm(row.get('work')), norm(row.get('unit')))


def group_aliases(generated, customer):
    """Match a unique area-labelled slab group within the same subsection.

    Area is a calculated total in the heading, not a slab type or thickness.
    Also pair the unique model crane plate at 1400 mm with the customer's
    combined 1400/1600 mm position, preserving both source scopes and quantities.
    Never guess when either side contains several groups of this family.
    """
    families = {'generated': defaultdict(set), 'customer': defaultdict(set)}
    for side, rows in (('generated', generated), ('customer', customer)):
        for row in rows:
            key = scope(row)
            family = re.fullmatch(r'(плита (?:перекрытия|покрытия)) площадью\s+.+?\s*м[2²]', key[-1])
            if family:
                families[side][key[:3] + (family.group(1),)].add(key)
    aliases = {}
    for family, gkeys in families['generated'].items():
        ckeys = families['customer'].get(family, set())
        if len(gkeys) == len(ckeys) == 1:
            aliases[next(iter(ckeys))] = next(iter(gkeys))
    from .vor_generation import mapping
    cranes = {'generated': defaultdict(set), 'customer': defaultdict(set)}
    for side, rows in (('generated', generated), ('customer', customer)):
        for row in rows:
            key = scope(row)
            if mapping.detect_kind(key[-1]) == 'crane_foundation':
                cranes[side][key[:3]].add(key)
    for parent, gkeys in cranes['generated'].items():
        ckeys = cranes['customer'].get(parent, set())
        if len(gkeys) != 1 or len(ckeys) != 1:
            continue
        gkey, ckey = next(iter(gkeys)), next(iter(ckeys))
        model_thickness = mapping.thickness_values(gkey[-1])
        if not model_thickness and re.search(r'(?:^|[\s_:])1400\s*мм\b', gkey[-1]):
            model_thickness = (1400,)
        combined = re.search(r'толщин\w*\s+(1400)\s*(?:/\s*(1600)|\(\s*(1600)\s*\))\s*мм', ckey[-1])
        if model_thickness == (1400,) and combined:
            aliases[ckey] = gkey
    return aliases


def compare_rows(generated, customer):
    aliases = group_aliases(generated, customer)
    groups = OrderedDict()
    for side, rows in (('generated', generated), ('customer', customer)):
        for row in rows:
            key = scope(row)
            if side == 'customer':
                key = aliases.get(key, key)
            groups.setdefault(key, {'generated': [], 'customer': []})[side].append(row)
    tree = OrderedDict()
    for key in groups:
        branch = tree
        for component in key:
            branch = branch.setdefault(component, OrderedDict())
    def ordered_scopes(branch, prefix=()):
        if len(prefix) == 4:
            yield prefix
        else:
            for component, children in branch.items():
                yield from ordered_scopes(children, prefix + (component,))
    result = []
    for key in ordered_scopes(tree):
        members = groups[key]
        lookup = defaultdict(list)
        for index, row in enumerate(members['customer']):
            if not row.get('isSeparator'):
                lookup[identity(row)].append(index)
        display_group = (members['generated'] or members['customer'])[0].get('elementGroup', '')
        used = set()
        pairs = []
        for row in members['generated']:
            if row.get('isSeparator'):
                pairs.append((row, None))
                continue
            candidates = lookup[identity(row)]
            index = candidates.pop(0) if candidates else None
            if index is not None:
                used.add(index)
            pairs.append((row, members['customer'][index] if index is not None else None))
        # Insert customer-only rows next to the preceding customer work, not into a different group.
        last = -1
        for index, row in enumerate(members['customer']):
            if index in used:
                last = next(i for i, (_, c) in enumerate(pairs) if c is row)
            elif row.get('isSeparator'):
                if not members['generated']:
                    pairs.insert(last + 1, (None, row)); last += 1
            else:
                pairs.insert(last + 1, (None, row)); last += 1
        for g, c in pairs:
            source = g or c
            if source.get('isSeparator'):
                result.append({**source, 'generatedWork': '', 'customerWork': '', 'status': 'separator', 'detail': {}})
                continue
            gv, cv = (g or {}).get('quantity'), (c or {}).get('quantity')
            partial_g = (g or {}).get('quantityStatus') == 'partial_data'
            partial_c = (c or {}).get('quantityStatus') == 'partial_data'
            crane_alias = bool(g and c and scope(g) != scope(c)
                               and 'кран' in norm(g.get('elementGroup')))
            comments = source_summary((c or {}).get('reason', ''))
            if crane_alias:
                comments = '. '.join(filter(None, [comments,
                    'Сравнение фундамента башенного крана: модель — 1400 мм; ВОР заказчика — общая позиция 1400/1600 мм. Объём заказчика не распределён по толщинам']))
            difference = None if gv is None or cv is None or partial_g or partial_c else round(cv - gv, 6)
            status = 'only_project' if g is None else 'only_ifc' if c is None else 'ifc_quantity_incomplete' if partial_g else 'project_quantity_incomplete' if partial_c else 'no_quantity' if gv is None or cv is None else 'matched' if abs(difference) < 1e-6 else 'quantity_mismatch'
            result.append({**source, 'elementGroup': display_group,
                           'isDifferent': status != 'matched' and not (g and c and gv is None and cv is None and not partial_g and not partial_c),
                           'generatedCalculation': (g or {}).get('calculation', ''),
                           'customerCalculation': (c or {}).get('calculation', ''),
                           'generatedElementGroup': (g or {}).get('elementGroup'),
                           'customerElementGroup': (c or {}).get('elementGroup'),
                           'generatedWork': g['work'] if g else '', 'customerWork': c['work'] if c else '',
                           'generatedQuantity': gv, 'customerQuantity': cv, 'difference': difference,
                           'differencePercent': None if difference is None or gv == 0 else difference / abs(gv) * 100,
                           'generatedSourceRow': (g or {}).get('sourceRow'), 'customerSourceRow': (c or {}).get('sourceRow'),
                           'modelReason': source_summary((g or {}).get('reason', '')), 'comments': comments,
                           'status': status, 'comparisonConfidence': 'Одинаковая работа; единственная группа фундамента башенного крана' if crane_alias else 'Одинаковая работа в одной группе' if g and c else 'Пара отсутствует', 'detail': {}})
    return result


def compare_workbooks(generated_path, customer_path):
    generated = read_typical_workbook(generated_path)
    customer = read_typical_workbook(customer_path)
    rows = compare_rows(generated, customer)
    from .comparison_similarity import typical_composition_similarity
    result = {'version': VERSION, 'status': 'ready', 'rows': rows,
            'compositionSimilarity': typical_composition_similarity(rows),
            'sourceFiles': [str(generated_path), str(customer_path)],
            'explanation': 'На первой странице показаны только различающиеся строки готовых типовых ВОР. ВОР заказчика и проектный ВОР — один источник. Единственные группы плит одного вида внутри подраздела сопоставляются независимо от площади в заголовке; площадь — итоговый показатель. При нескольких группах неоднозначная пара не назначается. Сравнение: часть здания → раздел → подраздел → группа → работа. Материалы сопоставляются внутри своей работы. Пустое значение не равно нулю. Разность рассчитывается только при наличии обоих полных объёмов.'}

    return result


def template_input(positions, generated=False):
    """Adapt parsed customer input only when a completed customer workbook is unavailable."""
    rows = []
    for index, p in enumerate(normalize_positions(positions)):
        number = p.get('positionNumber')
        if number is None:
            number = p.get('rowNumber')
        rows.append({'pos': str(number if number is not None else index + 1), 'name': p.get('name') or p.get('workAndCostName') or '',
                     'unit': p.get('unit') or '', 'volume_now': p.get('quantity'),
                     'page': (p.get('sourceLocation') or {}).get('page'),
                     'sheet': (p.get('sourceLocation') or {}).get('sheet'),
                     'section': p.get('sectionName') or '', 'block': (p.get('_construction') or {}).get('name') or ''})
    return rows


def build_typical_comparison(folder, models=None, projects=None, raw_paths=(), document_paths=None, ifc_names=(), customer_path=None, ifc_paths=()):
    folder = Path(folder)
    native = [folder / name for name in NATIVE_FILES]
    if all(p.is_file() for p in native):
        return compare_workbooks(*native)
    # Use the same builder as the Formation tab, never reconstruct model quantities from selected rates.
    if not raw_paths and not native[0].is_file():
        raise ValueError('Нет исходных элементов IFC для формирования типового ВОР')
    from .vor_generation import builder, customer_vor, documents
    from .works_comparison.positions_extractor import PositionsExtractor
    records = []
    for index, path in enumerate(raw_paths):
        rows = json.loads(Path(path).read_text(encoding='utf-8'))
        def missing_geometry(row):
            if row.get('IfcClass') not in ('IfcSlab', 'IfcStairFlight', 'IfcWall', 'IfcWallStandardCase', 'IfcBeam', 'IfcColumn'):
                return False
            def positive(*keys):
                for key in keys:
                    try:
                        if float(row.get(key) or 0) > 0:return True
                    except (ValueError, TypeError):pass
                return False
            from .ifc_raw_dump import _is_foundation_plate, _surface_formwork_kind
            if (_surface_formwork_kind(row.get('IfcClass'), row.get('Name'), row.get('ObjectType'))
                    and not positive('QTO_geom::Площадь_опалубки_м2')):
                return True
            if row.get('IfcClass') not in ('IfcSlab', 'IfcStairFlight'):
                return False
            if (_is_foundation_plate(row.get('Name'), row.get('ObjectType'))
                    and not positive('QTO::Qto_SlabBaseQuantities::Perimeter', 'QTO_geom::Внешний_периметр_мм')):
                return True
            return not (positive('QTO::Qto_SlabBaseQuantities::Perimeter', 'QTO_geom::Внешний_периметр_мм', 'QTO_bbox::Периметр_мм')
                        and positive('QTO::Qto_SlabBaseQuantities::Depth', 'QTO_bbox::Толщина_мм'))
        missing = [row for row in rows if row.get('GlobalId') and missing_geometry(row)]
        source = Path(ifc_paths[index]) if index < len(ifc_paths) and ifc_paths[index] else None
        if missing and source and source.suffix.lower() == '.ifc' and source.is_file():
            import ifcopenshell
            from .ifc_raw_dump import enrich_formwork_quantities, warm_formwork_surface_cache
            model = ifcopenshell.open(str(source))
            warm_formwork_surface_cache(model)
            for row in missing:
                try:
                    element = model.by_guid(row['GlobalId'])
                except RuntimeError:
                    continue
                if element is not None:
                    enrich_formwork_quantities(row, element)
            del model
        records.extend(rows)
    raw = folder / 'typical_ifc_elements.json'
    raw.write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
    paths = document_paths or {}
    paths = dict(paths)
    if customer_path and Path(customer_path).suffix.lower() == '.pdf':
        paths['vor'] = customer_path
    docs = documents.load_documents(paths, use_llm=False)
    if not docs.vor_rows and projects:
        docs.vor_rows = template_input(PositionsExtractor(projects).run())
        docs.rebar = documents.extract_rebar(docs.vor_rows)
        docs.file_names['vor'] = Path(customer_path).name if customer_path else 'ВОР заказчика'
    generated_path = native[0] if native[0].is_file() else folder / FILES[0]
    if not native[0].is_file():
        builder.build_vor(str(raw), docs, str(generated_path), ifc_name=', '.join(ifc_names))
    else:
        from shutil import copyfile
        copyfile(generated_path, folder / FILES[0])
    ready_customer = False
    if customer_path and Path(customer_path).suffix.lower() == '.xlsx':
        book = load_workbook(customer_path, read_only=True, data_only=True)
        try:
            ready_customer = 'ВОР КР_ЖС' in book.sheetnames
        finally:
            book.close()
    if native[1].is_file():
        from shutil import copyfile
        copyfile(native[1], folder / FILES[1])
    elif ready_customer:
        from shutil import copyfile
        if Path(customer_path).resolve() != (folder / FILES[1]).resolve():
            copyfile(customer_path, folder / FILES[1])
    elif customer_path and Path(customer_path).suffix.lower() == '.pdf':
        customer_vor.build_customer_vor(customer_path, str(folder / FILES[1]), elements_json_path=str(raw),
                                      raw_rows=docs.vor_rows or None)
    else:
        customer_vor.build_customer_vor(FILES[1], str(folder / FILES[1]), elements_json_path=str(raw),
                                      raw_rows=template_input(PositionsExtractor(projects or []).run()))
    return compare_workbooks(generated_path, native[1] if native[1].is_file() else folder / FILES[1])
