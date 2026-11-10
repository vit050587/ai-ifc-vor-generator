"""Content-validated cache and per-session locking for completed typical VORs."""
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
CACHE_FILENAME = '.typical_formation_cache.json'
VERSION = 1


def session_lock(folder):
    key = str(Path(folder).resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fingerprint(folder, template_path, session):
    from .. import vor_comparison as vc
    from ..works_comparison.config import settings
    from src.core.config import load_config
    folder = Path(folder)
    inputs = [template_path, vc._find_session_dir(str(folder), 'IFC_исходные_параметры.json'),
              vc._find_session_dir(str(folder), vc.VOR_JSON), vc._find_vor_pdf(str(folder))]
    inputs.extend((session or {}).get(f'{kind}_file_path') for kind in ('vor','pos','pz','pd'))
    final_works = vc._find_session_dir(str(folder), vc.FINAL_WORKS_JSON)
    if final_works:
        inputs.extend((final_works, str(Path(final_works).with_suffix('.xlsx'))))
    # Changes to calculation/rendering code must also invalidate the result.
    package = Path(__file__).parent
    inputs.extend(package.glob('*.py'))
    inputs.extend(package.parent / name for name in (
        'comparison_typical.py','comparison_layout.py','comparison_ai_reason.py','comparison_wording.py',
        'doc_pdf_parser.py','vor_parser.py','vor_comparison.py','pd_parser.py','ifc_quantities.py','construction_method.py',
        'estimate_parameter_comparison.py','stair_formwork.py'))
    from ..stair_formwork import find_ifc
    raw = vc._find_session_dir(str(folder), 'IFC_исходные_параметры.json')
    if raw:
        inputs.append(find_ifc(raw, (session or {}).get('ifc_file_path')))
    records = []
    for path in sorted({str(Path(p).resolve()) for p in inputs if p and Path(p).is_file()}):
        records.append((path, digest(path)))
    cfg = load_config()
    payload = {'version':VERSION, 'files':records, 'session':{
        'original_filename':(session or {}).get('original_filename')},
        'llm':{'server':cfg.ollama_url,'model':settings.OLLAMA_MODEL,
               'temperature':settings.TEMPERATURE,'top_k':settings.TOP_K,
               'top_p':settings.TOP_P,'seed':settings.SEED}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def load(folder, key):
    try:
        cached = json.loads((Path(folder) / CACHE_FILENAME).read_text())
        if cached['key'] != key:
            return None
        for path, expected in cached['artifacts'].items():
            if not Path(path).is_file() or digest(path) != expected:
                return None
        result = cached['result']
        result['cacheHit'] = True
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save(folder, key, result):
    # A failed model explanation remains retryable on the next request.
    if result.get('aiReasonSummary', {}).get('failed', 0):
        return
    paths = [result['file'], *[item['file'] for item in result['source_files']]]
    paths.extend(str(Path(path).with_suffix('.model-links.json')) for path in list(paths)
                 if Path(path).with_suffix('.model-links.json').is_file())
    payload = {'key':key,'result':result,'artifacts':{str(path):digest(path) for path in paths}}
    target = Path(folder) / CACHE_FILENAME
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
