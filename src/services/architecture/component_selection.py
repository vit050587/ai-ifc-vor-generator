"""Select rates per actual construction component, without a global coverage list."""
import copy
import json
import logging
import re

logger = logging.getLogger(__name__)


def select(payload, works_by_table, llm, build_row, quantity=None, group_count=None):
    from .ai_selection import _work_context, _assumptions, apply_quantity, VERSION
    from .table_matching import resolve
    from .rate_constraints import compatible, adjustments
    components = payload.get('architecture_components')
    if components is None:
        # Old saved runs are narrowed too; their 27 table-derived operations
        # must never revive the old, oversized global selection request.
        from .works_table_selector import _table_index
        element, kind, components, excluded, assumptions = resolve(payload['element'], _table_index())
        payload.update(element=element, family=kind, architecture_components=components,
                       excluded_components=excluded, assumptions=assumptions)
        available = {w['code'] for w in payload.get('works') or []}
        for scope in components:
            for code in scope['table_codes']:
                if code not in available:
                    payload.setdefault('works', []).append(dict(_table_index()[code], quantity={}))
    selected, notes, decisions = [], [], []
    for scope in components:
        candidates = []
        for code in scope['table_codes']:
            for work in (works_by_table.get(code) or {}).get('works') or []:
                if str(work.get('pressmark') or '').startswith(code + '-'):
                    from .catalogue_context import enrich as enrich_catalogue
                    candidates.append(dict(enrich_catalogue(work), _table_code=code))
        candidates = list({w['pressmark']: w for w in candidates}.values())
        complete_pool = list(candidates)
        candidates = [w for w in candidates if compatible(scope, w, complete_pool, payload['element'])]
        common = {'element': _compact_element(payload['element']), 'component': scope,
                  'previously_selected': [{k: w.get(k) for k in ('pressmark', 'title', 'reason', 'architecture_operation')}
                                          for w in selected if w.get('pressmark')],
                  'constants': {k: v for k, v in (payload.get('applied_constants') or {}).items()
                                if k != 'document_facts'}}
        if scope.get('kind') == 'wall':
            from .ai_selection import wall_reference
            common['wall_reference_examples_only'] = wall_reference()
        # Long *rate* tables are examined in bounded batches; the winner set
        # competes for the same component, rather than spawning more operations.
        shortlist_failure = None
        if len(candidates) > 24 or len(json.dumps([_work_context(w) for w in candidates], ensure_ascii=False)) > 18000:
            finalists = []
            for start in range(0, len(candidates), 20):
                batch = candidates[start:start+20]
                allowed = {w['pressmark'] for w in batch}
                from .ai_selection import _validate_pressmark
                answer = _ask(llm, 'Сравни расценки только для данного слоя/компонента. '
                              'Выбери одну наиболее вероятную; если все несовместимы, pressmark=null. '
                              'Не создавай новые операции. Верни {"pressmark": "шифр либо null", "reason": "..."}.',
                              dict(common, rates=[_work_context(w) for w in batch]),
                              required_fields=('pressmark',),
                              field_schemas={'pressmark': {'type': ['string', 'null'], 'enum': sorted(allowed)+[None]}},
                              validate=lambda answer: _validate_pressmark(answer, allowed))
                if answer.get('decision') == 'unresolved':
                    shortlist_failure = answer['reason']
                    finalists = []
                    break
                if answer['pressmark'] is not None:
                    finalists.append(next(w for w in batch if w['pressmark'] == answer['pressmark']))
            candidates = finalists
        included_parent = next((w for w in selected
                                if scope.get('role') == 'adhesive' and w.get('pressmark')
                                and re.search(r'на кле[ею]', w.get('title') or '', re.I)), None)
        if included_parent:
            answer = dict(decision='included', pressmark=None, included_in=included_parent['pressmark'],
                          reason='Клей учтён составом выбранной расценки: ' + included_parent['title'],
                          quantity_basis='unknown', assumptions=[])
        elif not candidates:
            answer = dict(decision='unresolved', pressmark=None,
                          reason=shortlist_failure or scope.get('mapping_note') or 'В API нет применимой расценки для компонента после проверки материала, основания, поверхности и толщины',
                          quantity_basis=scope.get('quantity_basis') or 'unknown', assumptions=[])
        else:
            allowed = {w['pressmark'] for w in candidates}
            prior = {w['pressmark'] for w in selected if w.get('pressmark')}
            schema = {
                'decision': {'type': 'string', 'enum': ['selected', 'included', 'unresolved', 'not_applicable']},
                'pressmark': {'type': ['string', 'null'], 'enum': sorted(allowed)+[None]},
                'reason': {'type': 'string', 'minLength': 1},
                'included_in': {'type': ['string', 'null'], 'enum': sorted(prior)+[None]},
                'quantity_basis': {'type': 'string', 'enum': ['element', 'area', 'layer_volume', 'length', 'count', 'unknown']},
                'assumptions': {'type': 'array', 'items': {'type': 'string'}}}
            answer = _ask(llm, '''Выбирай работу только для одного исходного слоя/компонента.
Таблицы и строки здесь — альтернативы, а не обязательные работы.
Сравни материал, способ устройства, толщину и состав расценок. Сам выбери одну
наиболее вероятную подходящую расценку, допущения запиши. Не выбирай технологически
несовместимую расценку только ради заполнения. Если точной нет, допустим обоснованный
аналог с явной пометкой в assumptions; если аналога нет — unresolved.
Если операция уже входит в ранее выбранную комплексную расценку, верни included
с included_in=её шифр и объясни, какая операция учтена. Не дублируй её стоимость.
Ранее выбранный шифр нельзя выбирать повторно для другого компонента: проверь
включение в его состав; если включение не доказано — unresolved, сохрани компонент.
not_applicable допустим, если исходный текст не является самостоятельной работой.
pressmark заполняй только при selected; при остальных решениях pressmark=null.
Для included указывай шифр ранее выбранной расценки только в included_in.
Для крепежа/сетки/швов не бери количество, площадь или объём всей конструкции
автоматически: quantity_basis=unknown, если нет их собственного измерителя.
Для отдельного слоя volume допускается только как layer_volume с его явной толщиной.
Используй document_evidence и project_conditions: это сведения ВОР, ПД, ПОС и ПЗ.
Если документ повлиял на выбор, укажи источник и страницу в reason. При противоречии
IFC и документа сам выбери наиболее обоснованный вариант и явно опиши допущение.
Общий объём ВОР не является объёмом каждого элемента и не распределяется автоматически.
Ответ: {"decision":"selected|included|unresolved|not_applicable", "pressmark":"шифр либо null",
"reason":"...", "included_in":null, "quantity_basis":"area|layer_volume|element|length|count|unknown",
"assumptions":["..."]}. Не составляй список решений для других компонентов.''',
                          dict(common, rates=[_work_context(w) for w in candidates]),
                          required_fields=('decision', 'pressmark', 'reason'), field_schemas=schema,
                          validate=lambda answer: _validate_decision(answer, allowed, prior))
        decisions.append(dict(component_id=scope['id'], **answer))
        if answer['decision'] in ('included', 'not_applicable'):
            if included_parent:
                code = included_parent['table_code']
                pool = [w for w in (works_by_table.get(code) or {}).get('works') or []
                        if str(w.get('pressmark') or '').startswith(code + '-')]
                for adjustment, factor, calculation in adjustments(scope, included_parent, pool, payload['element']):
                    if any(w.get('pressmark') == adjustment['pressmark'] for w in selected):
                        continue
                    table = next(t for t in payload['works'] if t['code'] == code)
                    extra = build_row(payload, table, adjustment, calculation, False, quantity=quantity)
                    extra.update(architecture_operation=dict(scope, quantity_basis='area'),
                                 architecture_quantity_factor=factor, quantity_factor_source=calculation,
                                 selection_method=VERSION, selection_status='catalogue_adjustment',
                                 parent_pressmark=included_parent['pressmark'], assumptions=[],
                                 okeiValue=adjustment.get('okeiValue'))
                    apply_quantity(extra, payload, quantity, group_count)
                    selected.append(extra)
            notes.append(scope['name'] + ': ' + answer['reason'])
            continue
        if answer['decision'] == 'selected':
            work = next(w for w in candidates if w['pressmark'] == answer['pressmark'])
            table = next(t for t in payload['works'] if t['code'] == work['_table_code'])
        else:
            # Preserve unresolved scope visibly; never lose it or fabricate a
            # norm code just to complete an otherwise successful estimate.
            work = dict(title=scope['name'], unitOfMeasure='м2')
            table = dict(code='', name='Без подобранной расценки')
            notes.append(scope['name'] + ': ' + answer['reason'])
        row = build_row(payload, table, work, answer['reason'], answer['decision'] == 'selected', quantity=quantity)
        row['architecture_operation'] = dict(scope, component=scope['name'], table=table['code'],
                                             quantity_basis=_quantity_basis(scope, work, len(components)))
        row['assumptions'] = list(dict.fromkeys(_assumptions(payload) + _assumptions(answer)))
        if answer['decision'] == 'selected' and scope.get('role') in ('paint', 'plaster'):
            from .table_matching import has
            if has(work.get('title') or '', 'высококачеств', 'улучшенн') and not has(str(payload['element'].get('material') or ''), 'высококачеств', 'улучшенн'):
                row['assumptions'].append('Класс качества отделки не указан в исходном составе; выбранный класс требует подтверждения проектом')
        row['document_evidence'] = copy.deepcopy(payload['element'].get('document_evidence') or [])
        row['project_conditions'] = copy.deepcopy(payload['element'].get('project_conditions') or [])
        if scope.get('mapping_note'):
            row['assumptions'].append(scope['mapping_note'])
        if answer['decision'] == 'selected' and 'winal' in scope.get('evidence', '').lower():
            row['assumptions'].append('Основа панели Winal не подтверждена исходным описанием; выбранная расценка — аналог, материал основы и состав подсистемы требуют проверки')
        row.update(selection_method=VERSION, selection_status=answer['decision'],
                   source_only=answer['decision'] == 'unresolved', okeiValue=work.get('okeiValue'))
        repetitions = scope.get('component_repetitions', 1)
        if repetitions > 1 and answer['decision'] == 'selected':
            row.update(architecture_quantity_factor=repetitions,
                       quantity_factor_source=f'В составе конструкции явно указано {repetitions} одинаковых нанесения: ' + scope['evidence'])
        apply_quantity(row, payload, quantity, group_count)
        selected.append(row)
        if answer['decision'] == 'selected':
            for adjustment, factor, calculation in adjustments(scope, work, complete_pool, payload['element']):
                if any(w.get('pressmark') == adjustment['pressmark'] for w in selected):
                    message = 'Повторная добавка ' + adjustment['pressmark'] + ': требуется проверка объёма по компонентам'
                    logger.warning('АР: %s', message)
                    notes.append(message)
                    decisions[-1].setdefault('adjustment_conflicts', []).append(adjustment['pressmark'])
                    continue
                extra = build_row(payload, table, adjustment, calculation, False, quantity=quantity)
                extra.update(architecture_operation=dict(row['architecture_operation']),
                             architecture_quantity_factor=factor, quantity_factor_source=calculation,
                             selection_method=VERSION, selection_status='catalogue_adjustment',
                             parent_pressmark=work['pressmark'], assumptions=[],
                             okeiValue=adjustment.get('okeiValue'))
                apply_quantity(extra, payload, quantity, group_count)
                selected.append(extra)
    payload['component_decisions'] = decisions
    return selected, '; '.join(notes)


def unresolved_rows(payload, build_row, reason, quantity=None, group_count=None):
    """Last-resort export for an element whose selection could not be completed."""
    from .ai_selection import VERSION, apply_quantity
    scopes = payload.get('architecture_components') or [dict(
        id='element', name=payload['element'].get('name') or 'Работа элемента')]
    rows, decisions = [], []
    for scope in scopes:
        row = build_row(payload, dict(code='', name='Без подобранной расценки'),
                        dict(title=scope['name'], unitOfMeasure=''), reason, False, quantity=quantity)
        row.update(selection_method=VERSION, selection_status='unresolved', source_only=True,
                   architecture_operation=dict(scope, quantity_basis='unknown'), assumptions=[],
                   document_evidence=copy.deepcopy(payload['element'].get('document_evidence') or []),
                   project_conditions=copy.deepcopy(payload['element'].get('project_conditions') or []))
        apply_quantity(row, payload, quantity, group_count)
        rows.append(row)
        decisions.append(dict(component_id=scope['id'], decision='unresolved', pressmark=None,
                              reason=reason, selection_failed=True))
    payload['component_decisions'] = decisions
    return rows, reason


def _ask(*args, **kwargs):
    from .ai_selection import _ask as ask
    try:
        return ask(*args, **kwargs)
    except RuntimeError as exc:
        # A failed selection request, including an unavailable model, leaves
        # an explicit unpriced component rather than aborting the estimate.
        component = (args[2].get('component') or {}) if len(args) > 2 else {}
        logger.error('ИИ-подбор АР: компонент «%s» сохранён без расценки: %s',
                     component.get('name', '?'), exc)
        return dict(decision='unresolved', pressmark=None, included_in=None,
                    reason=str(exc), quantity_basis='unknown', assumptions=[],
                    response_validation_failed=True)


def _validate_decision(answer, allowed, prior):
    from .ai_selection import QUANTITY_BASES
    if answer['decision'] not in ('selected', 'included', 'unresolved', 'not_applicable'):
        raise ValueError('Неизвестное решение для компонента')
    if not isinstance(answer.get('reason'), str) or not answer['reason'].strip():
        raise ValueError('Нужно обоснование решения по компоненту')
    if answer['decision'] == 'selected':
        if answer['pressmark'] in prior:
            raise ValueError('Повторный шифр уже выбран для этой конструкции: укажи included с обоснованием или unresolved')
        if answer['pressmark'] not in allowed:
            raise ValueError('Шифр вне справочника этого компонента. Допустимы: ' + ', '.join(sorted(allowed)))
    elif answer['pressmark'] is not None:
        # The decision takes precedence: a rejected candidate must never become
        # a priced work just because the model echoed its code. Keep the audit.
        answer['ignored_pressmark'] = answer['pressmark']
        answer['pressmark'] = None
    if answer['decision'] == 'included' and (not isinstance(answer.get('included_in'), str)
                                            or answer['included_in'] not in prior):
        raise ValueError('included_in должен ссылаться на ранее выбранную комплексную расценку')
    if answer.get('quantity_basis', 'area') not in QUANTITY_BASES:
        raise ValueError('Неизвестный измеритель компонента')
    return answer


def _compact_element(element):
    row = element.get('row') or {}
    compact = {k: v for k, v in element.items() if k not in ('row', 'project_conditions')}
    compact['project_conditions'] = [condition for condition in element.get('project_conditions') or []
                                     if not any(s in str(condition.get('key') or '').lower()
                                                for s in ('concrete', 'formwork', 'rebar', 'reinforcement'))]
    return compact | {
        'dimensions_and_layers': {k: v for k, v in row.items()
                                 if any(s in k.lower() for s in ('толщина', 'ширина', 'длина', 'высота', 'материальный_слой', 'ар_слой'))}}


def _quantity_basis(scope, work, component_count):
    """Unit and measured scope determine arithmetic, not a model's suggestion."""
    from src.services.quantity_measure import work_unit
    unit = work_unit(work).lower().replace('²', '2').replace('³', '3')
    from .quantity_fields import is_piece_unit
    if is_piece_unit(unit) and not scope.get('document_source') and scope.get('role') not in ('junction', 'adhesive'):
        return 'count'
    if scope.get('quantity_basis') == 'unknown' or scope.get('role') in ('junction', 'drainage', 'adhesive'):
        return 'unknown'
    if 'м3' in unit or 'm3' in unit:
        if scope.get('thickness_field'):
            return 'layer_volume'
        return 'element' if component_count == 1 and scope.get('kind') == 'wall' else 'unknown'
    if 'м2' in unit or 'm2' in unit:
        return 'area'
    if is_piece_unit(unit) and scope.get('kind') in ('door', 'window', 'furniture'):
        return 'count'
    return 'unknown'
