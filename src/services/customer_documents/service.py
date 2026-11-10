"""Validated, code-first import, isolated from VOR-versus-work-list comparison."""
import json
import re
from pathlib import Path
from time import monotonic

from .readers import read_tables, clean, embedded_grid
from .schema import infer_vor, header, fallback, units, norm, is_number, revision
from .ollama_client import OllamaFallback

VERSION=2


class DocumentParseError(ValueError):
    def __init__(self, message, issues=()):
        self.issues=list(issues)
        super().__init__(message)


def numeric(value):
    if isinstance(value,bool): return None
    text=re.sub(r'\s+','',clean(value)).replace(',','.')
    if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?',text): return None
    from math import isfinite
    result=float(text)
    return result if isfinite(result) else None


def substantive(table):
    # Avoid drawing frames, signatures and title sheets. A table must have at
    # least two rows with numerical data and document-related text.
    text=' '.join(clean(c) for row in table.rows for c in row if c)
    if re.search(r'содержание\s+тома|ведомость\s+ссылочных|состав\s+проектной\s+документации',text,re.I):
        return False
    if header(table) is None and sum(bool(re.search(word,text,re.I)) for word in ('Подпись','Разработал','Стадия','Листов','Главный конструктор'))>=2:
        return False
    if not re.search(r'наименован|марка|материал|количество|кол-во|объем|объём|шифр|обоснован|описание',text,re.I):
        return False
    return any(any(is_number(c) for c in row) and (sum(is_number(c) for c in row)>=2 or
               any(re.fullmatch(r'(?:\d+\s*)?(?:м[²³23]|куб\.?м\.?|кв\.?м\.?|кг|т|шт\.?|м|п\.?м\.?)',clean(c),re.I) for c in row))
               for row in table.rows)


def _pieces(unit, now, was, delta):
    unit=clean(unit)
    components=re.split(r'\s*/\s*',unit)
    if len(components)==1:
        yield unit,numeric(now),numeric(was),numeric(delta),None
        return
    # Compound units are separate quantities, not the first number in "шт/кг".
    values=[]
    for value in (now,was,delta):
        parts=clean(value).split('/')
        if len(parts)==1 and numeric(value)==0: parts=['0']*len(components)
        if not clean(value): parts=['']*len(components)
        if len(parts)!=len(components):
            raise ValueError(f'Составная единица {unit} не соответствует значению {clean(value)}')
        values.append([numeric(part) for part in parts])
    for index,component in enumerate(components):
        yield component,values[0][index],values[1][index],values[2][index],index


def _parse_vor_table(table, schema):
    facts,issues,unparsed=[],[],[]
    section=schema.get('initial_section','');parent='';block=schema.get('initial_block','')
    caption=next((clean(c) for row in table.rows[:schema['start']] for c in row
                  if c and 'ведомость' in clean(c).lower()),'')
    context=caption if any(w in caption.lower() for w in ('монолит','сборн','огражден','подпорн')) else ''
    for ri,row in enumerate(table.rows):
        numbering=[clean(cell) for cell in row if clean(cell)]
        if len(numbering)>=3 and numbering==[str(i) for i in range(1,len(numbering)+1)]:
            continue
        if ri < schema['start'] and not (len([c for c in row if clean(c)]) == 1 and re.match(r'^\d+[.]',next((clean(c) for c in row if clean(c)),''))):
            continue
        title=clean(row[schema['name']])
        pos=clean(row[schema['pos']]) if schema.get('pos') is not None else ''
        selected=[row[col['now']] for col in schema['columns']]
        old=[row[col['was']] for col in schema['columns'] if col.get('was') is not None]
        has_values=any(is_number(v) for v in selected+old)
        if not has_values:
            text=' '.join(dict.fromkeys(clean(c) for c in row if c))
            if title and pos and re.fullmatch(r'\d+',pos) and not any(is_number(v) for v in selected):
                parent=title
                section=title
            elif text and not any(is_number(c) for c in row) and not re.search(r'было|стало|дельта|наименован',text,re.I):
                section=(section+' '+text) if section and re.match(r'^част[ьи]',text,re.I) else text
                parent=''
            if title and sum(is_number(c) for c in row)>=2:
                unparsed.append(ri)
            continue
        if not title:
            # Nested summaries in wide drawings retain the same physical grid.
            # They are control tables, never extra construction quantities.
            if any(re.search(r'итого|всего|глухие|проемами|фризовые|до\s*\d+\s*м',clean(c),re.I) for c in row if c):
                issues.append(dict(kind='control_row',location=table.location,row=ri+1))
            continue
        if norm(title).startswith(('итого','всего','наименован')): continue
        source_name=f'{parent}: {title}' if '.' in pos and parent and parent not in title else title
        from ..vor_generation.thickness import source_column
        measurement = source_column(table,schema,ri)
        emitted=0
        for col in schema['columns']:
            now=row[col['now']];was=row[col['was']] if col.get('was') is not None else None
            delta=row[col['delta']] if col.get('delta') is not None else None
            if not any(is_number(v) for v in (now,was,delta)): continue
            unit=col.get('unit') or (clean(row[schema['unit']]) if schema.get('unit') is not None else '')
            if not unit or not (units(unit) or '/' in unit):
                if col.get('role')=='unit_measure': continue
                unparsed.append(ri);continue
            row_unit=clean(row[schema['unit']]) if schema.get('unit') is not None else ''
            if '/' in row_unit and any('/' in clean(v) for v in (now,was,delta)):
                unit=row_unit
            try:
                pieces=list(_pieces(unit,now,was,delta))
            except ValueError as exc:
                issues.append(dict(kind='invalid_compound_quantity',message=str(exc),source=table.source(ri,col['now'])))
                unparsed.append(ri);continue
            for actual_unit,current,previous,difference,component in pieces:
                if current is None and previous is None: continue
                label=col.get('label') or ''
                if label.startswith('Бетон '):
                    for ci,path in enumerate(schema.get('paths', [])):
                        if any('марка бетона' in clean(v).lower() for v in path) and any(revision(v)=='now' for v in path):
                            spec=clean(row[ci])
                            if re.fullmatch(r'[FfФф]\s*\d+',spec): label += ', '+spec.upper().replace('Ф','F')
                name=f'{label} — {source_name}' if label else source_name
                role=col.get('role','quantity')
                source=table.source(ri,col['now'])
                source['header']=schema.get('paths',[[]]*len(row))[col['now']]
                source['table_headers']=' '.join(dict.fromkeys(clean(c) for h in table.rows[:schema['start']] for c in h if c))
                source['was']=table.source(ri,col['was']) if col.get('was') is not None else None
                source['delta']=table.source(ri,col['delta']) if col.get('delta') is not None else None
                if component is not None: source['component']=component
                facts.append(dict(name=name,construction=source_name,thickness_measurement=measurement,unit=actual_unit,volume_now=current,volume_was=previous,
                                  delta=difference,has_current=True,role=role,section=' / '.join(filter(None,[context,section])),
                                  block=block,pos=pos,page=table.page,sheet=table.sheet,source=source,
                                  construction_method='precast' if 'сборн' in context.lower() else 'monolithic' if 'монолит' in context.lower() else 'unknown'))
                emitted+=1
                if current is not None and previous is not None and difference is not None:
                    # Integer-rounded source values may differ by up to 1.5.
                    raw=[clean(v).replace(' ','').replace(',','.') for v in (now,was,delta)]
                    tolerance=sum(.5*10**(-len(v.split('.')[1]) if '.' in v else 0) for v in raw if '/' not in v)
                    if abs(current-previous-difference)>max(tolerance,1e-8):
                        issues.append(dict(kind='delta_mismatch',source=source,printed=difference,calculated=current-previous))
        if not emitted: unparsed.append(ri)
    return facts,issues,sorted(set(unparsed))


def _legacy(facts):
    result=[]
    # Per-floor quantities and unit quantities are preserved in the audit data.
    # For existing typical builders feed the corresponding whole-product totals.
    def row_key(fact):
        source=fact['source']
        return (source.get('page'),source.get('sheet'),source['table'],source['row'])
    def steel(fact):
        match=re.search(r'[АA]\s*(240|400|500)[СC]?',fact['name'])
        return match.group(1) if match else None
    detailed={(row_key(f),steel(f)) for f in facts if f['role']=='quantity' and f['name'].startswith('Арматура ')}
    for index,fact in enumerate(facts):
        if fact['role'] in ('unit_measure','floor_count','part_quantity'): continue
        if fact['role']=='reinforcement_total' and any(k==row_key(fact) and (steel(fact) is None or cls==steel(fact)) for k,cls in detailed):
            continue
        row=dict(fact)
        from .context import enrich
        enrich(row)
        row['original_pos']=fact['pos']
        row['pos']=f"{fact['source']['cell']}:{fact['source']['table']}:{index}"
        # An explicit current column left blank does not mean "use the old value".
        if fact.get('has_current') and fact['volume_now'] is None:
            row['volume_was']=None
        from ..works_comparison.units import unit_and_factor
        canonical,factor=unit_and_factor(row['unit'])
        if factor is not None:
            factor=float(factor)
            if canonical=='кг': canonical,factor='т',factor/1000
            canonical={'м³':'м3','м²':'м2'}.get(canonical,canonical)
            if canonical!=row['unit'] or factor!=1:
                row['input_unit']=row['unit'];row['unit']=canonical
                for measure in ('volume_now','volume_was','delta'):
                    if row.get(measure) is not None: row[measure]=row[measure]*factor
                row['conversion']=f"{row['input_unit']} × {factor:g} → {canonical}"
        result.append(row)
    return result


def _read_vor(path, *, llm=None, allow_llm=True, report=None, llm_first=False):
    from .code_recovery import recover
    start=monotonic(); facts=[];issues=[];calls=0;tables_count=0;recoveries=0
    model=llm
    carried={}
    legacy=None
    def legacy_loader():
        nonlocal legacy
        if legacy is None:
            from ..doc_pdf_parser import extract_vor_rows
            legacy=extract_vor_rows(path) if Path(path).suffix.lower()=='.pdf' and Path(path).is_file() else []
        return legacy
    for table in read_tables(path):
        if embedded_grid(table.rows):
            issues.append(dict(kind='unparsed_table',location=table.location,table=table.index,
                               message='Извлечена рамка листа вместо сетки таблицы; LLM не вызывается'))
            continue
        schema=infer_vor(table)
        previous=carried.get(len(table.rows[0]))
        continuation=False
        if previous and previous.get('unit') is not None:
            limit=schema['start'] if schema else len(table.rows)
            continuation=any(units(row[previous['unit']]) and re.search('[а-яa-z]',clean(row[previous['name']]),re.I)
                             and any(is_number(row[column['now']]) for column in previous['columns'])
                             for row in table.rows[:limit])
            if schema and (schema['name'],schema.get('unit'),[c['now'] for c in schema['columns']]) != (previous['name'],previous.get('unit'),[c['now'] for c in previous['columns']]):
                continuation=False
            if continuation:
                schema=dict(previous,start=0)
        if not continuation and not substantive(table): continue
        tables_count+=1
        parsed,diagnostics,missing=_parse_vor_table(table,schema) if schema else ([],[],[0])
        if llm_first and allow_llm:
            # The original parser already failed on this PDF. Ask for its
            # schema before using the broader deterministic recovery.
            parsed,diagnostics,missing=[],[],[0]
        if missing or not parsed:
            failure=None
            if allow_llm:
                model=model or OllamaFallback()
                before=getattr(model,'calls',None)
                try:
                    llm_schema,request=fallback(table,'vor',model)
                    parsed_llm,diagnostics_llm,missing_llm=_parse_vor_table(table,llm_schema)
                    if missing_llm or not parsed_llm:
                        raise ValueError('Схема модели оставила неразобранные строки')
                    parsed,diagnostics,missing=parsed_llm,diagnostics_llm,missing_llm
                    if hasattr(model,'save_validated'):
                        try:
                            model.save_validated(*request)
                        except OSError:
                            issues.append(dict(kind='cache_unavailable',location=table.location,
                                               message='Проверенная схема использована без записи в кеш'))
                except Exception as exc:
                    failure=str(exc)
                finally:
                    after=getattr(model,'calls',None)
                    calls+=max(0,after-before) if isinstance(before,int) and isinstance(after,int) else 1
            if failure is not None or not allow_llm:
                recoveries+=1
                parsed,diagnostics,missing=recover(table,schema,legacy_loader)
                issues.append(dict(kind='code_fallback',location=table.location,table=table.index,
                                   message='Повторный разбор ВОР кодом',llm_error=failure,
                                   parsed_facts=len(parsed),unparsed_rows=[ri+1 for ri in missing]))
                if (missing or not parsed) and not allow_llm:
                    raise DocumentParseError(f'{table.location}, таблица {table.index+1}: структура ВОР распознана не полностью',missing)
            if missing or not parsed:
                issues.append(dict(kind='unparsed_rows' if parsed else 'unparsed_table',location=table.location,
                                   table=table.index,rows=[ri+1 for ri in missing],
                                   message='Кодом извлечена только подтверждённая часть таблицы' if parsed else 'Кодом не найдены подтверждённые величины таблицы'))
        facts.extend(parsed);issues.extend(diagnostics)
        if parsed and schema:
            carried[len(table.rows[0])]=dict(schema,initial_section=parsed[-1].get('section',''),
                                           initial_block=parsed[-1].get('block',''))
    if not facts:
        raise DocumentParseError('Повторный разбор кодом не нашёл подтверждённых величин ВОР. Проверьте извлечение таблиц из документа.',issues)
    if allow_llm:
        from .context import resolve_unknown
        model = model or OllamaFallback()
        before=getattr(model,'calls',None)
        try:
            semantic_calls=resolve_unknown(facts,model)
        except Exception as exc:
            semantic_calls=0
            issues.append(dict(kind='semantic_fallback_failed',message='Контекст конструкций определяется только кодом',llm_error=str(exc)))
        after=getattr(model,'calls',None)
        calls+=max(0,after-before) if isinstance(before,int) and isinstance(after,int) else semantic_calls
    from .validation import validate
    checks=validate(facts)
    result=_legacy(facts)
    details={'version':VERSION,'kind':'vor','status':'partial' if any(issue['kind'] in ('unparsed_rows','unparsed_table','semantic_fallback_failed') for issue in issues) else 'ready','tables':tables_count,'facts':facts,'issues':issues,'checks':checks,
             'stats':{'llmCalls':calls,'codeFallbacks':recoveries,'seconds':round(monotonic()-start,3),'facts':len(facts),'rows':len(result)}}
    if report is not None: report.update(details)
    return result


def read_vor(path, *, llm=None, allow_llm=True, report=None):
    from . import cache
    started=monotonic()
    legacy_failed=False
    legacy_error=None
    if Path(path).suffix.lower()=='.pdf' and Path(path).is_file():
        from ..doc_pdf_parser import extract_vor_rows
        try:
            original=extract_vor_rows(path)
            usable=any(clean(row.get('name')) and clean(row.get('unit')) and
                       any(numeric(row.get(field)) is not None for field in ('volume_now','volume_was'))
                       for row in original)
            if usable:
                # Preserve the original sections, positions and measures exactly.
                # Passing these rows through _legacy/enrich changes their scope.
                if report is not None:
                    report.update(version=VERSION,kind='vor',status='ready',parser='legacy',
                                  facts=original,issues=[],checks=[],
                                  stats=dict(llmCalls=0,codeFallbacks=0,rows=len(original),facts=len(original),
                                             seconds=round(monotonic()-started,3),cacheHit=False,parser='legacy'))
                return original
            legacy_error='Старый алгоритм не извлёк числовых позиций'
        except Exception as exc:
            legacy_error=str(exc)
        legacy_failed=True
    target=cache.target_for(path,allow_llm) if llm is None and Path(path).is_file() else None
    details=cache.load(target) if target else None
    if details is not None:
        def rename(value):
            if isinstance(value,dict):
                if 'file' in value: value['file']=Path(path).name
                for nested in value.values(): rename(nested)
            elif isinstance(value,list):
                for nested in value: rename(nested)
        rename(details)
        details['stats']=dict(details['stats'],cacheHit=True,llmCalls=0,seconds=round(monotonic()-started,3))
        if report is not None: report.update(details)
        return _legacy(details['facts'])
    details={}
    rows=_read_vor(path,llm=llm,allow_llm=allow_llm,report=details,llm_first=legacy_failed)
    if legacy_failed:
        details['issues'].insert(0,dict(kind='legacy_fallback',message=legacy_error))
    details['stats']['cacheHit']=False
    if target and details.get('status')=='ready':
        try: cache.save(target,details)
        except OSError: pass  # Read-only cache storage must not lose parsed quantities.
    if report is not None: report.update(details)
    return rows


def populate_documents(docs, path, *, llm=None, allow_llm=True, raw_rows=None):
    from ..vor_generation import documents
    rows=read_vor(path,llm=llm,allow_llm=allow_llm) if raw_rows is None else raw_rows
    docs.prepared_customer_rows=None
    docs.rows=[r for r in docs.rows if r.source!='ВОР']
    docs.vor_rows=rows
    docs.rebar=documents.extract_rebar(rows)
    docs.file_names['vor']=Path(path).name
    for row in rows:
        docs.add_rows([documents.DocumentRow(name=row['name'],unit=row['unit'],
            volume=documents.pick_volume(row.get('volume_now'),row.get('volume_was')),
            section=row.get('section') or '',source='ВОР',file_name=Path(path).name)])
    return rows


def read_estimate(path, *, llm=None, allow_llm=True):
    from .estimate import read
    return read(path,llm=llm,allow_llm=allow_llm)
