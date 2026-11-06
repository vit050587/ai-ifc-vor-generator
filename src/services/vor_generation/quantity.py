"""Numeric quantities carrying the actual summands used by the VOR builders."""
import re


def source_summary(reason, sources=()):
    text = '\n'.join(filter(None, [reason, *sources]))
    pages = sorted({int(page) for page in re.findall(r'стр\.\s*(\d+)', text)})
    label = (reason or '').split('\n')[0]
    if pages and not re.search(r'стр\.\s*\d+', label):
        label += '; стр. ' + ', '.join(map(str, pages))
    return label


class Quantity(float):
    def __new__(cls, value, expression='', sources=(), element_guids=()):
        obj = super().__new__(cls, value)
        obj.expression = expression or f'{float(value):g}'
        obj.sources = tuple(dict.fromkeys(sources))
        obj.element_guids = tuple(dict.fromkeys(guid for guid in element_guids if guid))
        return obj

    def __add__(self, other):
        if other == 0 and not isinstance(other, Quantity):
            return self
        expression = getattr(other, 'expression', f'{float(other):g}')
        return Quantity(float(self) + float(other), f'{self.expression} + {expression}',
                        self.sources + getattr(other, 'sources', ()),
                        self.element_guids + getattr(other, 'element_guids', ()))

    __radd__ = __add__

    def __mul__(self, factor):
        return Quantity(float(self) * factor, f'({self.expression}) × {factor:g}', self.sources, self.element_guids)

    __rmul__ = __mul__

    def __round__(self, digits=0):
        return Quantity(round(float(self), digits), self.expression, self.sources, self.element_guids)

    @property
    def calculation(self):
        return f'{self.expression} = {float(self):g}'


def source_quantity(value, row):
    location = ', '.join(str(v) for v in [
        f'стр. {row["page"]}' if row.get('page') is not None else '',
        f'лист {row["sheet"]}' if row.get('sheet') else '',
        row.get('section'), row.get('block'),
        f'поз. {row["pos"]}' if row.get('pos') is not None else '',
        row.get('name'),
    ] if v)
    reference = ', '.join(str(v) for v in [
        f'стр. {row["page"]}' if row.get('page') is not None else '',
        f'лист {row["sheet"]}' if row.get('sheet') else '',
        f'поз. {row["pos"]}' if row.get('pos') is not None else '',
    ] if v) or location
    return Quantity(value, f'{float(value):g} [{reference}]', (location,))
