"""Fallback through the existing native Ollama client used for rate selection."""
import hashlib
import json
import os
import threading
from pathlib import Path

_LOCK = threading.BoundedSemaphore(1)
VERSION = 2


class OllamaFallback:
    def __init__(self):
        from src.core.config import load_config
        from .. import perechen_kb as kb
        cfg = load_config()
        self.parameters = {'base_url':kb.OLLAMA_URL,'model':kb.KB_MODEL,
                           'temperature':0,'num_predict':kb.KB_NUM_PREDICT,
                           'timeout':kb.KB_TIMEOUT,'think':False}
        self.cache = Path(cfg.CACHE_PATH) / 'customer_document_schemas'
        self.calls = 0
        self.max_calls = int(os.getenv('CUSTOMER_DOCUMENT_MAX_CALLS','4'))
    def _key(self, system,user,schema):
        return hashlib.sha256(json.dumps([VERSION,self.parameters,system,user,schema],
                                       ensure_ascii=False,sort_keys=True).encode()).hexdigest()

    def complete_json(self,system,user,schema):
        target=self.cache/(self._key(system,user,schema)+'.json')
        try:
            return json.loads(target.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            pass
        if self.calls>=self.max_calls:
            raise ValueError('Достигнут лимит обращений к модели для одного документа.')
        from ..perechen_kb import _ask_llm
        prompt = system+'\nJSON Schema:\n'+json.dumps(schema,ensure_ascii=False)+'\n'+user
        with _LOCK:
            self.calls+=1
            return _ask_llm(prompt, think=False)

    def save_validated(self,system,user,schema,result):
        self.cache.mkdir(parents=True,exist_ok=True)
        target=self.cache/(self._key(system,user,schema)+'.json')
        temporary=target.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
        try:
            temporary.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
