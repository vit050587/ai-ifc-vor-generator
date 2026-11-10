"""Cache complete, validated imports by content and parser/model versions."""
import hashlib
import json
import os
import threading
from pathlib import Path


def target_for(path,allow_llm):
    from src.core.config import load_config
    from .. import perechen_kb as kb
    root=Path(__file__).parent
    cfg=load_config()
    code=hashlib.sha256(b''.join(p.read_bytes() for p in sorted(root.glob('*.py')))+b''.join((root.parent/name).read_bytes() for name in ('perechen_kb.py','vor_generation/thickness.py','vor_generation/mapping.py'))).hexdigest()
    content=hashlib.sha256(Path(path).read_bytes()).hexdigest()
    key=hashlib.sha256(json.dumps([code,content,bool(allow_llm),kb.OLLAMA_URL,kb.KB_MODEL,kb.KB_NUM_PREDICT,kb.KB_TIMEOUT],sort_keys=True).encode()).hexdigest()
    return Path(cfg.CACHE_PATH)/'customer_document_imports'/(key+'.json')


def load(target):
    try:
        value=json.loads(target.read_text(encoding='utf-8'))
        return value if isinstance(value,dict) and isinstance(value.get('facts'),list) and value['facts'] else None
    except (OSError,ValueError):
        return None


def save(target,report):
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        temporary.write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
