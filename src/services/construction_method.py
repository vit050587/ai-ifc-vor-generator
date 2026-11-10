"""Construction method is independent of material, IFC class and geometry."""
import re

FIELD = 'Технология конструкции'
LABELS = {'precast': 'Сборные конструкции', 'monolithic': 'Монолитные конструкции',
          'unknown': 'Технология не указана', 'conflicting': 'Противоречивые признаки технологии'}


def construction_method(row):
    explicit = set()
    for key, value in row.items():
        if 'constructionmethod' in str(key).lower() or key == FIELD:
            text = re.sub(r'[\s_-]', '', str(value).lower())
            if text in ('precast', 'prefabricated', 'сборный', 'сборная', 'сборные'):
                explicit.add('precast')
            elif text in ('insitu', 'castinsitu', 'castinplace', 'monolithic', 'монолитный', 'монолитная'):
                explicit.add('monolithic')
            elif text == 'conflicting':
                return 'conflicting'
    if explicit:
        return next(iter(explicit)) if len(explicit) == 1 else 'conflicting'
    text = ' '.join(str(row.get(key) or '') for key in ('Name', 'Имя', 'ObjectType', 'Материал', 'material', 'name'))
    precast = bool(re.search(r'сборн|\bprecast\b|\bprefabricated\b', text, re.I))
    monolithic = bool(re.search(r'монолит|\bcast[ _-]?in[ _-]?(?:situ|place)\b', text, re.I))
    return 'conflicting' if precast and monolithic else 'precast' if precast else 'monolithic' if monolithic else 'unknown'
