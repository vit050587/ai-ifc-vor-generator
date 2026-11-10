"""Keep the existing estimate reader; recover only unrecognized table schemas."""
from pathlib import Path
from tempfile import TemporaryDirectory
from openpyxl import Workbook

from .readers import read_tables,clean
from .schema import fallback
from .service import substantive,DocumentParseError
from .ollama_client import OllamaFallback

HEADERS={'code':'Шифр','name':'Наименование','unit':'Ед. изм.','quantity':'Количество',
         'total':'Стоимость','correction':'Поправочные коэффициенты','index':'Индекс',
         'winter_current':'Зимний коэффициент в текущих ценах','winter_base':'Зимний коэффициент в базисных ценах',
         'labor':'ЗП','machines':'ЭМ','materials':'МР'}


def _schema(table):
    from ..estimate_comparison import _schema as recognize
    for ri in range(min(50,len(table.rows))):
        for depth in (1,2,3,4):
            if ri+depth>len(table.rows): continue
            labels=[' '.join(dict.fromkeys(clean(table.expanded[r][ci]) for r in range(ri,ri+depth)))
                    for ci in range(len(table.rows[0]))]
            result=recognize(labels)
            if result:
                return {'start':ri+depth,'fields':{k:v for k,v in result.items() if k!='priceLevel'},'priceLevel':result['priceLevel']}
    return None


def read(path, *, llm=None, allow_llm=True):
    from ..estimate_comparison import read_xlsx
    book=Workbook();book.remove(book.active)
    locations={};calls=0;model=llm; requests=[];contexts=[]
    for table in read_tables(path):
        if not substantive(table): continue
        schema=_schema(table);request=None
        if schema is None:
            if not allow_llm:
                raise DocumentParseError(f'{table.location}: не распознаны обязательные столбцы сметы')
            model=model or OllamaFallback()
            try:
                before=getattr(model,'calls',None)
                schema,request=fallback(table,'estimate',model)
                after=getattr(model,'calls',None)
                calls += max(0,after-before) if isinstance(before,int) and isinstance(after,int) else 1
            except Exception as exc:
                raise DocumentParseError(f'{table.location}: не удалось разобрать структуру сметы: {exc}') from exc
        fields=schema['fields']
        title=table.sheet or 'Смета PDF'
        sheet=book[title] if title in book.sheetnames else book.create_sheet(title)
        contexts.append(table.context)
        width=len(table.rows[0]);labels=['']*width
        for key,col in fields.items():
            if col is not None: labels[col]=HEADERS[key]
        if schema.get('priceLevel')=='current': labels[fields['total']]='Стоимость в текущих ценах'
        for preamble in table.rows[:schema['start']]:
            sheet.append(preamble)
        sheet.append(labels)
        mapping=locations.setdefault(sheet.title,{})
        for ri,row in enumerate(table.rows[schema['start']:],schema['start']):
            sheet.append(row)
            mapping[sheet.max_row]=dict(table.source(ri,fields['quantity']),
                                       quantityCell=table.source(ri,fields['quantity']),costCell=table.source(ri,fields['total']))
        locations[sheet.title]=mapping
        if request: requests.append((request,schema))
    if not book.sheetnames:
        raise DocumentParseError('В документе не найдены таблицы сметы.')
    try:
        with TemporaryDirectory(prefix='customer_estimate_') as temporary:
            converted=Path(temporary)/'estimate.xlsx';book.save(converted)
            result=read_xlsx(converted)
            if not validate_native_coverage(converted,result):
                raise DocumentParseError('Часть позиций сметы не распознана; неполный результат не используется.')
    finally:
        book.close()
    for position in result['positions']:
        for item in [position,*position.get('resources',[])]:
            source=item.get('source') or {}
            mapping=locations.get(source.get('sheet'),{})
            item['source']=dict(mapping.get(source.get('row'),source))
            if source.get('costRow') in mapping:
                item['source']['costCell']=mapping[source['costRow']]['costCell']
    if not result.get('period'):
        import re
        periods={m.group(1).strip() for text in contexts for m in re.finditer(r'(?:текущий период тсн|период цен|период расчета)\s*:\s*([^\n]+)',text,re.I)}
        if len(periods)==1: result['period']=next(iter(periods))
    result['file']=Path(path).name
    result['importReport']={'llmCalls':calls,'tables':len(locations),'kind':'estimate'}
    if requests and hasattr(model,'save_validated'):
        for request,schema in requests: model.save_validated(*request)
    return result


def validate_native_coverage(path, document):
    """A successful header match alone is not proof that all rates were read."""
    import re
    from openpyxl import load_workbook
    consumed={(p['source'].get('sheet'),p['source'].get('row'))
              for parent in document['positions'] for p in [parent,*parent.get('resources',[])]}
    book=load_workbook(path,read_only=True,data_only=True)
    try:
        for sheet in book:
            if sheet.sheet_state!='visible': continue
            for index,row in enumerate(sheet.iter_rows(values_only=True),1):
                if (sheet.title,index) in consumed: continue
                code=any(re.fullmatch(r'\d+(?:[.-]\d+){1,}(?:[а-яa-z])?',clean(v),re.I) for v in row if v is not None)
                if code and any(isinstance(v,str) and re.search(r'[а-яa-z]{3}',v,re.I) for v in row):
                    return False
        return True
    finally:
        book.close()
