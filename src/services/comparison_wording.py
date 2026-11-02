"""Neutral display terminology for explanations saved by earlier app versions."""
import re


def comparison_text(value, *, typical=False):
    if not isinstance(value, str):
        return value
    noun = 'сформированной ВОР' if typical else 'сформированного перечня'
    location = 'в сформированной ВОР' if typical else 'в сформированном перечне'
    phrases = (
        ('вероятно верно у нас', 'вероятно верен сформированный объём'),
        ('нашим перечнем', 'сформированным перечнем'),
        ('нашего перечня', 'сформированного перечня'),
        ('нашем перечне', 'сформированном перечне'),
        ('нашему перечню', 'сформированному перечню'),
        ('наш перечень', 'сформированный перечень'),
        ('нашего ВОР', 'сформированной ВОР'),
        ('нашей ВОР', 'сформированной ВОР'),
        ('нашем ВОР', 'сформированной ВОР'),
        ('нашу ВОР', 'сформированную ВОР'),
        ('наша ВОР', 'сформированная ВОР'),
        ('наш ВОР', 'сформированная ВОР'),
        ('наш объём', 'объём ' + noun),
        ('наш объем', 'объём ' + noun),
        ('у нас', location),
    )
    for phrase, replacement in phrases:
        def replace(match):
            return replacement[0].upper() + replacement[1:] if match[0][0].isupper() else replacement
        value = re.sub(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', replace, value, flags=re.IGNORECASE)
    return value
