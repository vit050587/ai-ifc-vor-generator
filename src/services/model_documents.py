"""Exact document rows used by the model inspector, including older exports."""
import json
import math
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=8)
def _sheet(path, sheet, modified, size):
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        return list(book[sheet].values)
    finally:
        book.close()


def sheet_rows(path, sheet):
    path = Path(path)
    stamp = path.stat()
    return _sheet(str(path), sheet, stamp.st_mtime_ns, stamp.st_size)


def number(value):
    try:
        result = float(str(value).replace('\xa0', '').replace(' ', '').replace(',', '.'))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def same_quantity(left, right):
    left, right = number(left), number(right)
    return left == right if left is None or right is None else math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-8)


def _position(position, guids, group_id, group_name):
    source = position.get('quantityCalculation') or {}
    return {'id': f'per:{source["file"]}:{source["row"]}', 'kind': 'perechen',
            'groupId': group_id, 'groupName': group_name, 'name': position['name'],
            'code': position['code'], 'unit': position.get('unit'), 'quantity': position.get('quantity'),
            'guids': guids, 'file': source['file'], 'sheet': source['sheet'], 'row': source['row'],
            'relation': 'group_position'}


def saved_perechen(folder, mode):
    folder = Path(folder)
    if mode == 'KR':
        from .cs_final_reader import read_final_works, FINAL_WORKBOOK
        from .cs_model_members import exact_members
        path = folder / 'selected_elements_grouped.json'
        if not path.is_file() or not (folder / FINAL_WORKBOOK).is_file():
            return None
        selected = json.loads(path.read_text(encoding='utf-8'))
        members = exact_members(folder, selected)
        if members is None or len(members) != len(selected):
            raise ValueError('Не подтверждён состав групп ЦС')
        works = read_final_works(folder, selected)
        return [_position(work, list(dict.fromkeys(row['GlobalId'] for row in rows if row.get('GlobalId'))),
                          f'group-{index+1:04d}', selected[index].get('_typeKey'))
                for index, rows in enumerate(members) for work in works[index]]
    path = folder / 'Финальный_перечень_работ.json'
    workbook = folder / 'Финальный_перечень_работ.xlsx'
    frame_path = folder / 'filtered_elements.xlsx'
    if not all(p.is_file() for p in (workbook, frame_path)):
        return None
    if not path.is_file():
        return legacy_perechen(folder)
    data = json.loads(path.read_text(encoding='utf-8'))
    if not data.get('groups'):
        return selected_elements_perechen(folder, data) if data.get('elements') else legacy_perechen(folder)
    import pandas as pd
    from .work_height import label
    frame = pd.read_excel(frame_path, sheet_name='Данные', usecols=['GlobalId']).fillna('')
    groups = [group for part in ('Подземная', 'Цоколь', 'Надземная')
              for group in data['groups'] if group.get('part') == part]
    if len(groups) != len(data['groups']):
        raise ValueError('Не определён порядок групп перечня')
    prepared = []
    for group in groups:
        indices = group.get('indices')
        if indices is None or any(type(i) is not int or not 0 <= i < len(frame) for i in indices):
            raise ValueError('В перечне не сохранён точный состав группы')
        guids = list(dict.fromkeys(str(frame.iloc[i]['GlobalId']) for i in indices if frame.iloc[i]['GlobalId']))
        title = group['element'] + (f" ({group['material']})" if group.get('material') else '') + f"  Кол-во: {group.get('count')}"
        if group.get('subsection'):
            title += f"  [{group['subsection']}]"
        if group.get('height_context'):
            title += '  [' + label(group['height_context']) + ']'
        prepared.append((title, guids))
    active, seen, result = None, 0, []
    for row_number, values in enumerate(sheet_rows(workbook, 'Данные')[1:], 2):
        code, title, unit, quantity = (*values[:4],)
        if not code:
            if title and 'Кол-во:' in str(title):
                if seen >= len(prepared) or str(title) != prepared[seen][0]:
                    raise ValueError('Группы Excel и сохранённого перечня не совпали')
                active = seen
                seen += 1
            continue
        if active is None:
            raise ValueError('Работа находится вне группы перечня')
        source = {'file':workbook.name, 'sheet':'Данные', 'row':row_number}
        result.append(_position({'name':str(title or ''), 'code':str(code), 'unit':str(unit or ''),
                                 'quantity':number(quantity), 'quantityCalculation':source},
                                prepared[active][1], str(active), groups[active]['element']))
    if seen != len(prepared):
        raise ValueError('Не все группы перечня найдены в Excel')
    return result


def saved_estimate(folder, positions):
    folder = Path(folder)
    path = folder / 'Смета_по_перечню_данные.json'
    workbook = folder / 'Смета_по_сформированному_перечню.xlsx'
    if not path.is_file() or not workbook.is_file():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    by_source = {(row['file'], row.get('row'), row.get('code')):row for row in positions if row.get('row')}
    sheet = 'Смета по перечню'
    rows = sheet_rows(workbook, sheet)
    # The group heading is exported verbatim from the source workbook. Repeated
    # codes are resolved inside that group, never matched globally by code/name.
    candidates, group = {}, ''
    for index, values in enumerate(rows, 1):
        if str(values[0] or '').startswith('Раздел: '):
            group = str(values[0])[len('Раздел: '):]
        elif index >= 19 and values[0] and not values[1]:
            title = str(values[0])
            if not title.startswith(('Раздел ', 'Подраздел ', 'Итого', 'ИТОГО')) and 'часть здания' not in title.lower():
                group = title
        if len(values) > 4 and values[1] and values[2]:
            key = (group, str(values[1]), str(values[2]), number(values[4]))
            candidates.setdefault(key, []).append(index)
    source_name = Path(data.get('source', '')).name
    result = []
    for record in data.get('rows', []):
        if not record.get('code'):
            continue
        source = by_source.get((source_name, record.get('source_row'), record['code']))
        if not source or source.get('name') != record.get('title') or not same_quantity(source.get('quantity'), record.get('quantity_exact')):
            continue
        index = record.get('estimate_row')
        if index is None:
            matches = candidates.get((record.get('group',''), record['code'], record['title'], number(record.get('quantity_exact'))), [])
            if len(matches) != 1:
                continue
            index = matches[0]
        if type(index) is not int or not 1 <= index <= len(rows):
            continue
        values = rows[index-1]
        if str(values[1]) != record['code'] or str(values[2]) != record['title'] or not same_quantity(values[4], source.get('quantity')):
            continue
        result.append({**source, 'id':f'estimate:{index}', 'kind':'estimate', 'file':workbook.name,
                       'sheet':sheet, 'row':index, 'total':number(record.get('total')),
                       'sourceFile':source_name, 'sourceRow':record['source_row'], 'relation':'source_position'})
    return result


def recover_vor(folder, raw_path):
    """Recover exact unique references already written in saved calculation cells."""
    folder, raw_path = Path(folder), Path(raw_path)
    workbook = folder / 'Сформированный_ВОР_типовой.xlsx'
    if not workbook.is_file() or not raw_path.is_file():
        return []
    sources = {}
    for row in json.loads(raw_path.read_text(encoding='utf-8')):
        guid = row.get('GlobalId')
        name = row.get('Name') or row.get('ObjectType')
        if not guid or not name:
            continue
        source = ', '.join(str(v) for v in (name, row.get('Этаж')) if v)
        sources.setdefault(source, set()).add(guid)
    sheet = 'ВОР КР_ЖС'
    rows = sheet_rows(workbook, sheet)
    result = []
    for index, values in enumerate(rows, 1):
        if len(values) < 5 or number(values[3]) is None:
            continue
        calculation = str(values[4] or '')
        if calculation.startswith('Расчёты объёмов, строки '):
            extra = sheet_rows(workbook, 'Расчёты объёмов')
            calculation = ''.join(str(row[2] or '') for row in extra if row[0] == index and row[1] == 'Расчёт')
        total = re.search(r'=\s*([-+\d.eE]+)\s*$', calculation)
        markers = re.findall(r'\[([^\[\]]+)\]', calculation)
        matches_total = total and (same_quantity(total[1], values[3]) or total[1] == format(number(values[3]), 'g'))
        if not matches_total or not markers:
            continue
        if any(len(sources.get(marker, ())) != 1 for marker in markers):
            continue
        guids = list(dict.fromkeys(next(iter(sources[marker])) for marker in markers))
        result.append({'id':f'vor:{workbook.name}:{index}', 'kind':'vor', 'file':workbook.name,
                       'sheet':sheet, 'row':index, 'number':str(values[0] or ''), 'name':str(values[1] or ''),
                       'unit':str(values[2] or ''), 'quantity':number(values[3]), 'guids':guids,
                       'relation':'saved_calculation'})
    return result


def document_excerpt(folder, item):
    name = item.get('file', '')
    if Path(name).name != name or not name.endswith('.xlsx'):
        raise ValueError('Документ недоступен для просмотра')
    rows = sheet_rows(Path(folder) / name, item['sheet'])
    target = item.get('row')
    if type(target) is not int or not 1 <= target <= len(rows):
        raise ValueError('Строка документа не найдена')
    start, end = (item['startRow'], item['endRow']) if 'startRow' in item else (max(1, target-2), min(len(rows), target+2))
    if not 1 <= start <= target <= end <= len(rows):
        raise ValueError('Границы группы документа не найдены')
    width = len(rows[target-1])
    from openpyxl.utils import get_column_letter
    return {'file':name, 'sheet':item['sheet'], 'row':target,
            'startRow':start, 'endRow':end, 'groupName':item.get('name') if 'startRow' in item else None,
            'linkedRows':item.get('linkedRows', [target]),
            'headers':['Строка']+[get_column_letter(i) for i in range(1,width+1)],
            'rows':[[index]+list(rows[index-1]) for index in range(start,end+1)]}


def legacy_perechen(folder):
    """Recreate the original deterministic partitions when its result JSON is missing.

    Require the complete exported header (type/material/count/height), with only
    the selected catalog subsection removed. No name-only or fuzzy matching.
    """
    import pandas as pd
    from . import work_height
    from .perechen_kb import type_key
    from .perechen_pipeline import _leaves, _part_from_path
    folder = Path(folder)
    paths = [folder/name for name in ('Подобранные_таблицы_работ.json', 'filtered_elements_grouped_AR.json',
                                      'filtered_elements.xlsx', 'Финальный_перечень_работ.xlsx')]
    if not all(path.is_file() for path in paths):
        return None
    elements = json.loads(paths[0].read_text(encoding='utf-8')).get('elements', [])
    tree = json.loads(paths[1].read_text(encoding='utf-8'))
    rows = pd.read_excel(paths[2], sheet_name='Данные').to_dict('records')
    candidates = {}
    for node, path in _leaves(tree):
        buckets = {}
        for index in node.get('indices', []):
            if type(index) is not int or not 0 <= index < len(elements):
                continue
            element = elements[index].get('element') or {}
            buckets.setdefault((type_key(element.get('name')), str(element.get('material') or '')), []).append(index)
        for (name, material), indices in buckets.items():
            if not all(index < len(rows) and str(rows[index].get('GlobalId') or '') == str((elements[index].get('element') or {}).get('global_id') or '') for index in indices):
                continue
            for members in work_height.partitions(indices, rows):
                title = name + (f' ({material})' if material else '') + f'  Кол-во: {len(members)}'
                title += '  [' + work_height.label(work_height.context([rows[index] for index in members])) + ']'
                guids = tuple(dict.fromkeys(str(rows[index]['GlobalId']) for index in members))
                candidates.setdefault((_part_from_path(path), title), set()).add(guids)
    part, active, result = None, None, []
    part_titles = {'ПОДЗЕМНАЯ ЧАСТЬ':'Подземная','ЦОКОЛЬНАЯ ЧАСТЬ':'Цоколь','НАДЗЕМНАЯ ЧАСТЬ':'Надземная'}
    for row_number, values in enumerate(sheet_rows(paths[3], 'Данные')[1:], 2):
        code, title, unit, quantity = (*values[:4],)
        if not code:
            if title in part_titles:
                part, active = part_titles[title], None
            elif title and 'Кол-во:' in str(title):
                key = re.sub(r'\s*\[Подраздел[^\]]*\]', '', str(title))
                matches = candidates.get((part, key), set())
                active = (str(title), next(iter(matches))) if len(matches) == 1 else None
            continue
        if active:
            result.append(_position({'name':str(title or ''),'code':str(code),'unit':str(unit or ''),
                                     'quantity':number(quantity),'quantityCalculation':{
                                         'file':paths[3].name,'sheet':'Данные','row':row_number}},
                                    list(active[1]), active[0], active[0]))
    return result


def selected_elements_perechen(folder, data):
    import pandas as pd
    folder = Path(folder)
    rows = pd.read_excel(folder/'filtered_elements.xlsx', sheet_name='Данные', usecols=['GlobalId']).fillna('')
    headers = {}
    for entry in data.get('elements', []):
        indices = entry.get('source_indices')
        title = entry.get('group_header')
        if not title or indices is None or any(type(index) is not int or not 0 <= index < len(rows) for index in indices):
            continue
        guids = tuple(dict.fromkeys(str(rows.iloc[index]['GlobalId']) for index in indices if rows.iloc[index]['GlobalId']))
        headers.setdefault(title, set()).add(guids)
    if not headers:
        return legacy_perechen(folder)
    active, result = None, []
    filename = 'Финальный_перечень_работ.xlsx'
    for row_number, values in enumerate(sheet_rows(folder/filename,'Данные')[1:],2):
        code,title,unit,quantity = (*values[:4],)
        if not code:
            if title in headers:
                matches = headers[title]
                active = (title,next(iter(matches))) if len(matches) == 1 else None
            elif title and 'Кол-во:' in str(title):
                active = None
            continue
        if active:
            result.append(_position({'name':str(title or ''),'code':str(code),'unit':str(unit or ''),
                                     'quantity':number(quantity),'quantityCalculation':{
                                         'file':filename,'sheet':'Данные','row':row_number}},list(active[1]),active[0],active[0]))
    return result


def document_groups(folder, items):
    """Keep actual contiguous workbook blocks, independently for each document.

    Membership is the union of verified row contributors, never a title match
    between different documents. Unlinked rows stay visible in their block.
    """
    folder = Path(folder)
    sources = {(item['kind'], item['file'], item.get('sheet')) for item in items
               if item.get('sheet') and item.get('row') and item['file'].endswith('.xlsx')}
    defaults = [('perechen', 'Финальный_перечень_работ.xlsx', 'Данные'),
                ('perechen', 'ОБЩИЙ_Финальный_перечень_работ.xlsx', 'Данные'),
                ('estimate', 'Смета_по_сформированному_перечню.xlsx', 'Смета по перечню')]
    combined = folder/'Типовая_ВОР_сверка.xlsx'
    defaults.append(('vor', combined.name if combined.is_file() else 'Сформированный_ВОР_типовой.xlsx',
                     'Сформированная ВОР' if combined.is_file() else 'ВОР КР_ЖС'))
    sources.update(source for source in defaults if (folder/source[1]).is_file())
    result = []
    for kind, filename, sheet in sorted(sources):
        try:
            rows = sheet_rows(folder/filename, sheet)
        except (OSError, KeyError, ValueError):
            continue
        boundaries, context = [], []
        for index, values in enumerate(rows, 1):
            first, second = (list(values) + [None, None])[:2]
            title = None
            if kind == 'perechen' and not first and second:
                if 'Кол-во:' in str(second):
                    title = str(second)
                elif 'ЧАСТЬ' in str(second).upper():
                    context = [str(second)]
                    boundaries.append((index, None, list(context)))
            elif kind == 'estimate' and str(first or '').startswith('Раздел: '):
                title = str(first)[len('Раздел: '):]
                if 'ЧАСТЬ' in title.upper() and 'Кол-во:' not in title:
                    context = [title]
                    boundaries.append((index, None, list(context)))
                    title = None
            elif kind == 'estimate' and index >= 19 and first and not second:
                value = str(first).strip()
                if 'часть здания' in value.lower():
                    context = [value]
                    boundaries.append((index, None, list(context)))
                elif value.startswith('Раздел '):
                    context = context[:1] + [value]
                    boundaries.append((index, None, list(context)))
                elif value.startswith('Подраздел '):
                    context = context[:2] + [value]
                    boundaries.append((index, None, list(context)))
                elif not value.startswith(('Итого', 'ИТОГО')):
                    title = value
            elif kind == 'vor' and index > 2 and first and not second:
                value = str(first).strip()
                if value.startswith('Раздел ') or 'часть здания' in value.lower():
                    context = [value] if 'часть здания' in value.lower() else context[:1]+[value]
                    boundaries.append((index, None, list(context)))
                elif value.startswith('Подраздел '):
                    context = context[:2]+[value]
                    title = value
                else:
                    title = value
            if title:
                boundaries.append((index, title, list(context)))
        relevant = [item for item in items if (item['kind'], item['file'], item.get('sheet')) == (kind, filename, sheet)]
        # Older exports without headings still have exact, independently linked rows.
        if not boundaries and relevant:
            boundaries = [(min(item['row'] for item in relevant), filename, [])]
        for offset, (start, title, path) in enumerate(boundaries):
            if title is None:
                continue
            end = boundaries[offset+1][0]-1 if offset+1 < len(boundaries) else len(rows)
            if kind == 'vor' and title.startswith('Подраздел '):
                # A subsection contains its named construction blocks as well.
                following = next((entry[0] for entry in boundaries[offset+1:]
                                  if entry[1] is None or entry[1].startswith('Подраздел ')), len(rows)+1)
                end = following-1
            while end > start and not any(value is not None and str(value).strip() for value in rows[end-1]):
                end -= 1
            positions = [item for item in relevant if start <= item['row'] <= end]
            guids = list(dict.fromkeys(guid for item in positions for guid in item['guids']))
            result.append({'id':f'docgroup:{kind}:{filename}:{sheet}:{start}', 'kind':kind,
                           'file':filename, 'sheet':sheet, 'name':title, 'path':path,
                           'row':start, 'startRow':start, 'endRow':end, 'guids':guids,
                           'itemIds':[item['id'] for item in positions],
                           'linkedRows':sorted({item['row'] for item in positions})})
    return result
