import re


def unit_text_normalize(text: str | None) -> str:
    from .units import unit_and_factor
    return unit_and_factor(text)[0]

def text_normalize(text: str | None) -> str:
    if text is None:
        return ""
    return (
        " ".join(text.lower().strip().split())
        .replace(".", "")
        .replace("²", "2")
        .replace("³", "3")
    )

def code_normalize(text: str | None):
    if text is None:
        return ""
    text = text.replace("3.6-", "6-")
    return text