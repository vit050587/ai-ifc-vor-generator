import re


def unit_text_normalize(text: str | None) -> str:
    text = text_normalize(text)
    text = re.sub(r"^1 ", "", text)
    return text

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