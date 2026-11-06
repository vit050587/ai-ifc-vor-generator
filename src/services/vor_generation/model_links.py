"""Persist exact element contributions alongside the rendered VOR workbook."""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4


def workbook_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(workbook, sheet, rows):
    workbook = Path(workbook)
    target = workbook.with_suffix('.model-links.json')
    payload = {'version': 1, 'workbook': workbook.name, 'sha256': workbook_digest(workbook),
               'sheet': sheet, 'rows': rows}
    temporary = target.with_name(target.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
