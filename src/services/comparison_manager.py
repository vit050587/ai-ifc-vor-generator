"""Persistent multi-source comparisons using the existing VOR and comparison pipelines."""
import copy
import json
import logging
import threading
import time
import uuid
import shutil
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.utils import secure_filename
from werkzeug.datastructures import FileStorage
from .vor_parser import parse_vor, QuantityColumnRequired
from .comparison_exports import EXPORT_FILES, build_comparison_exports

logger = logging.getLogger(__name__)


class ComparisonManager:
    ACTIVE = {'models_processing', 'comparing'}

    def __init__(self, sessions):
        self.sessions = sessions
        self.root = Path(sessions.output_folder)
        self.lock = sessions._state_lock
        self.jobs = {}
        with self.lock:
            for sid, session in sessions._sessions.items():
                if session.get('session_type') == 'comparison':
                    self.jobs[sid] = session['comparison']
            # Import the old layout once, preserving IDs and report paths.
            legacy = self.root / 'comparisons'
            for path in legacy.glob('*/state.json'):
                try:
                    job = json.loads(path.read_text(encoding='utf-8'))
                    sid = job['id']
                    if sid in self.jobs:
                        continue
                    if str(uuid.UUID(sid)) != sid:
                        raise ValueError('Invalid comparison ID')
                    old_dir, new_dir = path.parent, self.root / sid
                    if new_dir.exists():
                        raise ValueError(f'Comparison destination already exists: {new_dir}')
                    created = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                    shutil.move(str(old_dir), str(new_dir))
                    def relocate(value):
                        if isinstance(value, str):
                            return str(new_dir) + value[len(str(old_dir)):] if value.startswith(str(old_dir) + '/') else value
                        if isinstance(value, dict):
                            return {k: relocate(v) for k, v in value.items()}
                        if isinstance(value, list):
                            return [relocate(v) for v in value]
                        return value
                    job = relocate(job)
                    job.setdefault('created_at', created)
                    self.jobs[sid] = job
                    self._update(sid)
                    (new_dir / 'state.json').rename(new_dir / 'legacy-comparison-state.json')
                except (ValueError, KeyError, OSError):
                    logger.exception('Cannot import comparison state %s', path)
            if legacy.is_dir() and not any(legacy.iterdir()):
                legacy.rmdir()
            for sid, job in list(self.jobs.items()):
                if job['status'] in self.ACTIVE:
                    job.update(status='error', message='Обработка прервана перезапуском сервиса. Создайте новое сравнение.', error='Сервис перезапущен')
                self._update(sid)

    def _write(self, job_id, name, payload):
        path = self.root / job_id / name
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)

    def _read_models(self, job_id):
        """Refresh CS inputs independently of the saved report version."""
        models=json.loads((self.root/job_id/'models.json').read_text(encoding='utf-8'))
        changed=False
        job=self.jobs[job_id]
        if job.get('mode')=='KR':
            from .cs_final_reader import synchronize_cs_model
            for index,sid in enumerate(job.get('sessions',[])):
                runs=sorted((self.root/sid).glob('run_*/ОБЩИЙ_Финальный_перечень_работ.xlsx'))
                if index<len(models) and runs:
                    updated=synchronize_cs_model(models[index],runs[-1].parent)
                    if updated is not models[index]:models[index]=updated;changed=True
        if changed:self._write(job_id,'models.json',models)
        return models,changed

    def _update(self, job_id, **fields):
        with self.lock:
            job = self.jobs[job_id]
            # A deleted session must not be recreated by a background worker.
            if job_id not in self.sessions._sessions and job.get('_registered'):
                raise KeyError('Сессия сравнения удалена')
            job.update(fields)
            job.setdefault('created_at', datetime.now(timezone.utc).isoformat())
            files = []
            for name in ('models.json', 'estimates.json', 'model_analytics.json', 'semantic_match_diagnostics.jsonl', 'result.json', 'result.xlsx', *EXPORT_FILES):
                path = self.root / job_id / name
                if path.is_file() and (name not in ('result.json', 'result.xlsx') or job['status'] == 'completed'):
                    files.append({'path': str(path), 'filename': name, 'size': path.stat().st_size})
            from .comparison_documents import DOC_FILENAMES
            for kind, document in job.get('documents', {}).items():
                for path in (Path(document['path']), self.root / job_id / DOC_FILENAMES[kind], self.root / job_id / Path(DOC_FILENAMES[kind]).with_suffix('.xlsx')):
                    if path.is_file():
                        files.append({'path':str(path),'filename':path.name,'size':path.stat().st_size})
            self.sessions._sessions[job_id] = {
                'session_id': job_id, 'session_type': 'comparison', 'source_type': 'comparison',
                'created_at': job['created_at'], 'status': job['status'],
                'processing_type': job['mode'], 'ifc_file_name': ', '.join(f['name'] for f in job['sources']),
                'progress': job.get('progress', 0), 'progress_message': job.get('message', ''),
                'error': job.get('error'), 'files': files, 'has_results': job['status'] == 'completed',
                'comparison': job,
            }
            for child_id in job.get('sessions', []):
                child = self.sessions._sessions.get(child_id)
                if child:
                    child['parent_session_id'] = job_id
            job['_registered'] = True
            self.sessions._save()

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs or job_id not in self.sessions._sessions:
                raise KeyError('Сравнение не найдено')
            job = copy.deepcopy(self.jobs[job_id])
            job['has_matching_diagnostics'] = (self.root / job_id / 'semantic_match_diagnostics.jsonl').is_file()
            job['input_exports'] = [name for name in EXPORT_FILES if (self.root/job_id/name).is_file() or (self.root/job_id/('models.json' if name=='generated_vor.xlsx' else 'estimates.json')).is_file()]
            job['reuse_estimates'] = (self.root / job_id / 'estimates.json').is_file()
            job['can_retry'] = (self.root / job_id / 'models.json').is_file() and (job['reuse_estimates'] or (bool(job.get('estimates')) and all(Path(f['path']).is_file() for f in job['estimates'])))
            if job.get('mode') == 'AR' and job.get('sessions') and job['status'] in ('models_ready','completed','error'):
                from .works_table_selector import get_constants_schema
                from .comparison_documents import resolve_constants, material_choices
                job['parameter_schema'] = get_constants_schema()
                if not job.get('parameter_preview') or any('material_choices' not in m for m in job['parameter_preview']):
                    preview = []
                    for sid, source in zip(job['sessions'], job['sources']):
                        _, rows = resolve_constants(job.get('documents', {}), self.root / sid, job['parameter_schema'])
                        _, model_rows = resolve_constants({}, self.root / sid, job['parameter_schema'])
                        preview.append({'session_id':sid,'source_file':source['name'],'constants':rows,'model_constants':model_rows, 'material_choices':material_choices(self.sessions, sid)})
                    job['parameter_preview'] = preview
            return job

    def list(self):
        with self.lock:
            return [copy.deepcopy(j) for sid, j in reversed(list(self.jobs.items())) if sid in self.sessions._sessions]

    def _save_files(self, job_id, files, prefix):
        saved = []
        for index, file in enumerate(files):
            name = secure_filename(file.filename) or 'file'
            path = self.root / job_id / f'{prefix}_{index}_{name}'
            file.save(path)
            saved.append({'name': file.filename, 'path': str(path)})
        return saved

    def create(self, files, mode, documents=None):
        if mode not in ('KR', 'AR'):
            raise ValueError('Выберите режим ЦС или ИИ')
        if not files or any(Path(f.filename or '').suffix.lower() not in ('.ifc', '.pdf') for f in files):
            raise ValueError('Загрузите IFC-модели или PDF-чертежи')
        if len({Path(f.filename).suffix.lower() for f in files}) != 1:
            raise ValueError('В одном сравнении используйте либо IFC, либо PDF')
        documents = {kind: file for kind, file in (documents or {}).items() if file and file.filename}
        if documents and mode != 'AR':
            raise ValueError('ПОС, ПЗ, ВОР и ПД доступны только в режиме ИИ')
        if any(kind not in ('pos','pz','vor','pd') or Path(file.filename).suffix.lower() != '.pdf' for kind,file in documents.items()):
            raise ValueError('ПОС, ПЗ, ВОР и ПД должны быть PDF-файлами')
        job_id = str(uuid.uuid4())
        (self.root / job_id).mkdir()
        sources = self._save_files(job_id, files, 'model')
        saved_documents = {kind: self._save_files(job_id, [file], kind)[0] for kind,file in documents.items()}
        with self.lock:
            self.jobs[job_id] = {'id': job_id, 'mode': mode, 'sources': sources, 'estimates': [],
                                 'documents': saved_documents, 'applied_constants': [], 'sessions': [], 'status': 'models_processing', 'progress': 0,
                                 'message': 'Обработка всех элементов загруженных файлов', 'error': None}
            self._update(job_id)
        threading.Thread(target=self._models, args=(job_id,), daemon=True).start()
        return self.get(job_id)

    def compare_saved_vor(self, job_id):
        job = self.get(job_id)
        source = job.get('documents', {}).get('vor')
        if not source:
            raise ValueError('ВОР не загружен')
        with open(source['path'], 'rb') as file:
            return self.compare(job_id, [FileStorage(stream=file, filename=source['name'])])

    def _stage(self, job_id, key, progress=None, message='', **fields):
        with self.lock:
            stages = copy.deepcopy(self.jobs[job_id].get('stages', {}))
            stages[key] = dict(progress=progress, message=message, **fields)
            self._update(job_id, stages=stages, message=message)

    def _wait(self, getter, complete, timeout=14400, on_progress=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = getter()
            if not state:
                raise RuntimeError('Сессия обработки удалена')
            if on_progress:
                on_progress(state)
            if state.get('status') in ('error', 'failed'):
                raise RuntimeError(state.get('error') or state.get('progress_message') or 'Ошибка обработки')
            if state.get('status') in complete:
                return state
            time.sleep(2)
        raise TimeoutError('Превышено время ожидания обработки')

    def _parse_documents(self, job_id):
        job = self.get(job_id)
        documents = job.get('documents', {})
        if not documents:
            return documents
        from . import pd_parser
        from src.core.config import load_config
        from .comparison_documents import DOC_FILENAMES
        cfg = load_config()
        config = pd_parser.Config(llm_base_url=cfg.ollama_url, llm_model=cfg.model_ollama)
        for kind, document in documents.items():
            if document.get('status') == 'completed' and document.get('result') is not None:
                continue
            label = {'pos':'ПОС','pz':'ПЗ','vor':'ВОР','pd':'ПД'}[kind]
            document['status'] = 'processing'
            self._update(job_id, documents=documents)
            def progress(message):
                self._stage(job_id, 'documents', None, f'{label}: {message}', active=True)
            progress('Разбор документа')
            try:
                if kind in ('vor', 'pd'):
                    from . import doc_pdf_parser
                    parser = doc_pdf_parser.parse_vor if kind == 'vor' else doc_pdf_parser.parse_pd
                    result = parser(document['path'], use_llm=True, config=config, progress=progress)
                else:
                    result = pd_parser.parse_pos_constants(document['path'], use_llm=True, config=config,
                                                           progress=progress, document_label=label)
                result['file_name'] = document['name']
                self._write(job_id, DOC_FILENAMES[kind], result)
                if kind in ('vor','pd'):
                    exporter = doc_pdf_parser.vor_to_xlsx if kind == 'vor' else doc_pdf_parser.pd_to_xlsx
                    exporter(result.get('items', []), self.root / job_id / Path(DOC_FILENAMES[kind]).with_suffix('.xlsx'))
                document.update(status='completed', result=result)
                self._update(job_id, documents=documents)
            except Exception as exc:
                document.update(status='error', error=str(exc))
                self._update(job_id, documents=documents)
                raise
        if any(kind in documents for kind in ('pos','pz')) and 'pd_full' not in job:
            from . import pd_full
            self._stage(job_id, 'documents', None, 'Полный разбор ПОС и ПЗ', active=True)
            try:
                full = pd_full.extract(documents.get('pos', {}).get('path'), documents.get('pz', {}).get('path'),
                                       log=lambda message:self._stage(job_id, 'documents', None, str(message), active=True))
            except Exception as exc:
                logger.warning('Дополнительный разбор ПОС/ПЗ не выполнен: %s', exc)
                full = []
                self._update(job_id, document_warnings=[f'Полный разбор ПОС/ПЗ не выполнен: {exc}. Основные параметры сохранены.'])
            self._write(job_id, 'pd_full.json', full)
            self._update(job_id, pd_full=full)
        if any(kind in documents for kind in ('vor','pd')):
            from .doc_facts import Facts
            facts = Facts(documents.get('vor', {}).get('result', {}).get('items', []),
                          documents.get('pd', {}).get('result', {}).get('items', []), session_dir=str(self.root / job_id))
            self._update(job_id, document_facts={'items':facts.summary_items(), 'vor':len(facts.vor), 'pd':len(facts.pd)})
        self._stage(job_id, 'documents', 100, 'Дополнительные документы разобраны', active=False)
        return documents

    def confirm_parameters(self, job_id, overrides, use_documents=True, material_codes=None, use_tsn=True):
        from .works_table_selector import get_constants_schema
        if not isinstance(overrides, dict) or type(use_documents) is not bool or type(use_tsn) is not bool or not isinstance(material_codes or {}, dict):
            raise ValueError('Некорректные параметры расчёта')
        schema = {d['name']: d for d in get_constants_schema()}
        for name, value in overrides.items():
            if name not in schema or value not in schema[name].get('values', []):
                raise ValueError(f'Недопустимое значение параметра: {name}')
        with self.lock:
            job = self.get(job_id)
            if (job['status'] not in ('parameters_required','models_ready','completed','error') or not job.get('sessions')):
                raise ValueError('Параметры доступны перед запуском формирования ВОР')
            if job['mode'] != 'AR' and overrides:
                raise ValueError('Глобальные параметры доступны только в режиме ИИ')
            validated = {}
            for sid, choices in (material_codes or {}).items():
                if sid not in job.get('sessions', []) or not isinstance(choices, dict):
                    raise ValueError('Некорректная модель для уточнения материала')
                available = {c.get('id', c['code']):c for m in job.get('parameter_preview', []) if m['session_id']==sid for c in m.get('material_choices', [])}
                rows = {}
                for parent, value in choices.items():
                    if parent not in available or value not in {o['code'] for o in available[parent]['options']}:
                        raise ValueError('Недопустимый код уточнения материала')
                    rows.update({str(i):value for i in available[parent]['rows']})
                validated[sid] = rows
            self._update(job_id, parameter_overrides=overrides, use_documents=use_documents,
                         material_choices_selected=material_codes or {}, material_code_overrides=validated, use_tsn=use_tsn, parameter_preview=job.get('parameter_preview', []),
                         parameters_confirmed=True, applied_constants=[], model_analytics=[], status='models_processing', error=None, message='Формирование ВОР с выбранными параметрами')
        threading.Thread(target=self._models, args=(job_id,), daemon=True).start()
        return self.get(job_id)

    def _prepare_parameters(self, job_id, documents):
        from .comparison_documents import resolve_constants, material_choices
        from .works_table_selector import get_constants_schema
        job = self.get(job_id)
        sessions, preview = [], []
        schema = get_constants_schema() if job['mode'] == 'AR' else []
        for index, source in enumerate(job['sources']):
            with open(source['path'], 'rb') as file:
                method = self.sessions.process_ifc if Path(source['name']).suffix.lower() == '.ifc' else self.sessions.process_pdf
                sid = method(FileStorage(stream=file, filename=source['name']), source['name'], processing_type=job['mode'])['session_id']
            sessions.append(sid)
            self._update(job_id, sessions=sessions)
            def progress(state):
                value = round((index + (state.get('progress', 0) or 0) / 100) / len(job['sources']) * 40)
                self._stage(job_id, 'models', value, f"{source['name']}: извлечение элементов", active=True)
            self._wait(lambda: self.sessions.get(sid), {'ifc_processed', 'selecting_rows'}, on_progress=progress)
            _, rows = resolve_constants(documents, self.root / sid, schema)
            _, model_rows = resolve_constants({}, self.root / sid, schema)
            preview.append({'session_id':sid, 'source_file':source['name'], 'constants':rows, 'model_constants':model_rows, 'material_choices':material_choices(self.sessions, sid)})
        if job['mode'] == 'KR' and not any(m['material_choices'] for m in preview):
            self._update(job_id, parameters_confirmed=True, parameter_preview=preview)
            return
        self._stage(job_id, 'models', 40, 'Проверьте параметры перед формированием ВОР', active=False)
        self._update(job_id, status='parameters_required', progress=40, parameter_schema=schema,
                     parameter_preview=preview, message='Параметры готовы. Проверьте значения и запустите расчёт всех элементов.')

    def _models(self, job_id):
        try:
            job = self.get(job_id)
            documents = self._parse_documents(job_id)
            job = self.get(job_id)
            if not job.get('parameters_confirmed') and (job['mode'] == 'AR' or hasattr(self.sessions, '_get_original_excel_path')):
                self._prepare_parameters(job_id, documents)
                job = self.get(job_id)
                if job['status'] == 'parameters_required':
                    return
            combined, session_ids = [], []
            for index, source in enumerate(job['sources']):
                self._update(job_id, message=f"Файл {index + 1}/{len(job['sources'])}: {source['name']}")
                existing = job.get('sessions', [])
                if index < len(existing):
                    session_id = existing[index]
                else:
                    with open(source['path'], 'rb') as file:
                        method = self.sessions.process_ifc if Path(source['name']).suffix.lower() == '.ifc' else self.sessions.process_pdf
                        session_id = method(FileStorage(stream=file, filename=source['name']), source['name'], processing_type=job['mode'])['session_id']
                session_ids.append(session_id)
                self._update(job_id, sessions=session_ids)
                def report_model(state, start, weight, label):
                    fraction = max(0, min(100, state.get('progress', 0) or 0)) / 100
                    value = round((index + start + weight * fraction) / len(job['sources']) * 100)
                    message = f"{source['name']} ({index + 1}/{len(job['sources'])}): {state.get('progress_message') or label}"
                    self._stage(job_id, 'models', value, message, active=True)
                    self._update(job_id, progress=value)
                self._wait(lambda: self.sessions.get(session_id), {'ifc_processed', 'selecting_rows'},
                           on_progress=lambda state: report_model(state, 0, .4, 'Извлечение элементов'))
                constants = {}
                if job['mode'] == 'AR':
                    from .comparison_documents import DOC_FILENAMES, resolve_constants
                    from .works_table_selector import get_constants_schema
                    child_dir = self.root / session_id
                    for kind, document in documents.items():
                        shutil.copy2(document['path'], child_dir / f'{kind}_source.pdf')
                        (child_dir / DOC_FILENAMES[kind]).write_text(json.dumps(document['result'], ensure_ascii=False), encoding='utf-8')
                    if 'pd_full' in job:
                        self._write(session_id, 'pd_full.json', job['pd_full'])
                    self._write(session_id, 'pd_mode.json', {'mode':'on' if job.get('use_documents', True) else 'off'})
                    self._write(session_id, 'tsn_mode.json', {'mode':'on' if job.get('use_tsn', True) else 'off'})
                    constants, applied = resolve_constants(documents, child_dir, get_constants_schema(),
                                                           job.get('parameter_overrides'), job.get('use_documents', True))
                    current = self.get(job_id).get('applied_constants', [])
                    current.append({'session_id':session_id,'source_file':source['name'],'constants':applied})
                    self._update(job_id, applied_constants=current)
                    self.sessions._build_selection_parameters_templates(session_id, str(child_dir))
                run = self.sessions.select_rows(session_id, [], all_rows=True, processing_type=job['mode'], **({'global_constants': constants} if job['mode'] == 'AR' else {}),
                                                **({'material_code_overrides':job['material_code_overrides'].get(session_id, {})} if job.get('material_code_overrides') else {}))
                def run_state():
                    state = self.sessions.get_run(session_id, run['run_id'])
                    if state:
                        current = self.sessions.get(session_id) or {}
                        state = dict(state, progress=current.get('progress', 0), progress_message=current.get('progress_message', 'Формирование ВОР'))
                    return state
                self._wait(run_state, {'completed'}, on_progress=lambda state: report_model(state, .4, .6, 'Формирование ВОР'))
                analytics_path = self.root / session_id / f"run_{run['run_number']:03d}" / 'analytic.json'
                if analytics_path.is_file():
                    analytics = self.get(job_id).get('model_analytics', [])
                    analytics.append({'source_file':source['name'], 'session_id':session_id,
                                      'data':json.loads(analytics_path.read_text(encoding='utf-8'))})
                    self._write(job_id, 'model_analytics.json', analytics)
                    self._update(job_id, model_analytics=analytics)
                payload = self.sessions.get_final_json_result_by_number(session_id, run['run_number'])
                if not payload:
                    raise RuntimeError(f"Не сформирован ВОР для {source['name']}")
                records = payload if isinstance(payload, list) else [payload]
                if not any(record.get('elementGroups') for record in records):
                    raise RuntimeError(f"ВОР файла {source['name']} не содержит групп элементов")
                if not any(group.get('positions') for record in records for group in record.get('elementGroups', [])):
                    raise RuntimeError(f"Сформированный ВОР файла {source['name']} пуст: группы элементов найдены, но работы не сформированы. Проверьте этап подбора работ.")
                for record in records:
                    record['sourceFile'] = source['name']
                    combined.append(record)
                self._update(job_id, progress=round((index + 1) / len(job['sources']) * 100))
            self._stage(job_id, 'models', 100, 'Все модели обработаны', active=False)
            self._write(job_id, 'models.json', combined)
            combined,_=self._read_models(job_id)
            build_comparison_exports(self.root/job_id,models=combined)
            self._update(job_id, status='models_ready', message='ВОР готовы. Загрузите ВОР заказчика для сравнения.')
        except Exception as exc:
            logger.exception('Model processing failed: %s', job_id)
            self._update(job_id, status='error', error=str(exc), message='Ошибка обработки моделей')

    def compare(self, job_id, files):
        with self.lock:
            job = self.get(job_id)
            if job['status'] in self.ACTIVE:
                raise ValueError('Обработка уже выполняется')
            if not (self.root / job_id / 'models.json').exists():
                raise ValueError('Сначала дождитесь обработки моделей')
            allowed = {'.pdf', '.xlsx', '.xls', '.xml', '.zip', '.gsfx', '.gge'}
            if not files or any(Path(f.filename or '').suffix.lower() not in allowed for f in files):
                raise ValueError('Выберите файлы ВОР заказчика: PDF, XLSX, XLS, XML, ZIP, GSFX или GGE')
            estimates = self._save_files(job_id, files, 'estimate')
            self._update(job_id, stages={**job.get('stages', {}), 'models': {'progress': 100, 'message': 'ВОР готовы', 'active': False}, 'estimates': {'progress': 0}, 'comparison': {'progress': 0}}, estimates=estimates, status='comparing', progress=0, error=None,
                         message='Отправка ВОР заказчика на обработку')
        threading.Thread(target=self._compare, args=(job_id,), daemon=True).start()
        return self.get(job_id)

    def retry(self, job_id, source_index=None, quantity_column=None):
        with self.lock:
            job = self.get(job_id)
            if job['status'] in self.ACTIVE:
                raise ValueError('Обработка уже выполняется')
            if not job['can_retry']:
                raise ValueError('Для повтора нужны сформированный ВОР и сохранённые файлы ВОР заказчика')
            if job['status'] == 'input_required':
                pending = job.get('pending_input') or {}
                if source_index != pending.get('sourceIndex') or quantity_column not in pending.get('choices', []):
                    raise ValueError('Выберите столбец объёма для указанного файла')
                job['estimates'][source_index].setdefault('quantitySelections', {})[pending['key']] = quantity_column
                job['reuse_estimates'] = False
                self._update(job_id, estimates=job['estimates'], pending_input=None)

            self._update(job_id, status='comparing', error=None, progress=65 if job['reuse_estimates'] else 0, message='Повтор проверки сохранённых результатов' if job['reuse_estimates'] else 'Повтор разбора сохранённого ВОР заказчика')
        threading.Thread(target=self._compare, args=(job_id, job['reuse_estimates']), daemon=True).start()
        return self.get(job_id)

    def _estimate_tasks_client(self):
        """Ленивая загрузка клиента внешнего сервиса разбора смет (только XML/ZIP/GSFX/GGE).

        Клиент нужен исключительно для форматов, которые не разбираются локально;
        PDF/XLSX разбираются `vor_parser.parse_vor` и внешний сервис не используют.
        Импорт остаётся ленивым: если модуль отсутствует в сборке, понятная ошибка
        поднимается только при попытке разбора этих форматов, а не при любом
        сравнении (раньше безусловный импорт на входе `_compare` ронял сравнение
        с `ModuleNotFoundError` даже для PDF/XLSX).
        """
        try:
            from .estimate_tasks_client import EstimateTasksClient
        except ImportError as exc:
            raise RuntimeError(
                'Разбор ВОР заказчика в этом формате требует внешний сервис разбора смет, '
                'но модуль src/services/estimate_tasks_client.py отсутствует в сборке. '
                'Загрузите ВОР заказчика в формате PDF или XLSX.'
            ) from exc
        return EstimateTasksClient()

    def _compare(self, job_id, reuse_estimates=False):
        try:
            from .works_comparison.processor import Processor
            job = self.get(job_id)
            if reuse_estimates:
                saved_projects = json.loads((self.root / job_id / 'estimates.json').read_text(encoding='utf-8'))
                reuse_estimates = not any(str(p.get('result', {}).get('parser', '')).startswith('local_vor_') and p['result']['parser'] != 'local_vor_v3' for p in saved_projects)
            if reuse_estimates:
                projects = json.loads((self.root / job_id / 'estimates.json').read_text(encoding='utf-8'))
            else:
                projects = []
                for index, source in enumerate(job['estimates']):
                    self._update(job_id, message=f"Разбор ВОР заказчика {index + 1}/{len(job['estimates'])}: {source['name']}")
                    self._stage(job_id, 'estimates', round(index / len(job['estimates']) * 100), f"Проверка ВОР заказчика {index + 1}/{len(job['estimates'])}: {source['name']}", active=True, indeterminate=True)
                    if Path(source['path']).suffix.lower() in ('.pdf', '.xlsx'):
                        def local_progress(message):
                            self._stage(job_id, 'estimates', round(index / len(job['estimates']) * 100),
                                        f"{source['name']}: {message}", active=True, indeterminate=True)
                        cached_path = self.root / job_id / f'estimate_{index}_parsed.json'
                        cached = json.loads(cached_path.read_text(encoding='utf-8')) if cached_path.is_file() else {}
                        if cached.get('parser') == 'local_vor_v3' and cached.get('selectedQuantityColumn') == source.get('quantitySelections', source.get('quantityColumn')):
                            response = cached
                        else:
                            response = parse_vor(source['path'], source.get('quantitySelections', source.get('quantityColumn')), progress=local_progress)
                        response['selectedQuantityColumn'] = source.get('quantitySelections', source.get('quantityColumn'))
                        for parsed_document in response.get('resultData', []):
                            parsed_document['estimateNumber'] = source['name']
                        response['filename'] = source['name']
                        source['parser'] = 'local_vor_v3'
                        source['parseDiagnostics'] = response.get('parseDiagnostics', [])
                        source['parsedRows'] = sum(len(position.get('rows', [])) for document in response.get('resultData', []) for section in document.get('sections', []) for position in section.get('positions', []))
                        source.pop('validationWarning', None)
                        self._update(job_id, estimates=job['estimates'])
                    else:
                        client = self._estimate_tasks_client()
                        content = Path(source['path']).read_bytes()
                        validation = client.validate(source['name'], content)
                        self._write(job_id, f'estimate_{index}_validation.json', validation)
                        scans = validation.get('scanResults') or []
                        source.pop('validationWarning', None)
                        if scans and not any(scan.get('hasEstimate') for scan in scans):
                            source['validationWarning'] = 'Предварительная проверка не обнаружила смету. Это не исключает ВОР; требуется подтверждение по результату разбора.'
                            logger.warning('Estimate validation did not recognize %s; attempting authoritative VOR parsing', source['name'])
                        self._update(job_id, estimates=job['estimates'])
                        self._stage(job_id, 'estimates', round(index / len(job['estimates']) * 100), f"Отправка ВОР заказчика: {source['name']}", active=True, indeterminate=True)
                        response = client.upload(source['name'], content, task_id=f'{job_id}-{index}-{uuid.uuid4().hex[:8]}')
                        self._write(job_id, f'estimate_{index}_upload.json', response)
                        if response.get('errorMessage') or str(response.get('status', '')).lower() in ('failed', 'error'):
                            raise RuntimeError(response.get('errorMessage') or 'Ошибка сервиса разбора ВОР заказчика')
                        if not response.get('resultData'):
                            task_id = response.get('taskId') or response.get('id')
                            if not task_id:
                                raise ValueError('Сервис разбора ВОР заказчика не вернул идентификатор задачи')
                            source['remoteTaskId'] = task_id
                            self._update(job_id, estimates=job['estimates'])
                            def remote_progress(state):
                                self._write(job_id, f'estimate_{index}_status.json', state)
                                detail = state.get('progressMessage') or state.get('message') or state.get('status') or 'обработка'
                                self._stage(job_id, 'estimates', round(index / len(job['estimates']) * 100),
                                            f"ВОР заказчика {index + 1}/{len(job['estimates'])} — {source['name']}: {detail}", active=True, indeterminate=True)
                            response = client.wait_for_result(task_id, on_progress=remote_progress)
                    self._write(job_id, f'estimate_{index}_parsed.json', response)
                    if not isinstance(response, dict) or not isinstance(response.get('resultData'), list) or not response['resultData']:
                        raise ValueError(f"Сервис вернул пустой или неизвестный результат: {source['name']}")
                    parsed_rows = [row for document in response['resultData']
                                   for section in document.get('sections', [])
                                   for position in section.get('positions', [])
                                   for row in position.get('rows', [])
                                   if isinstance(row, dict) and str(row.get('workAndCostName') or '').strip()]
                    if not parsed_rows:
                        raise ValueError(f"Сервис разбора не вернул позиции ВОР заказчика: {source['name']}. Предварительная проверка ищет сметы; для этого формата может потребоваться отдельный парсер ВОР.")
                    projects.append({'sourceFile': source['name'], 'result': response})
                    self._update(job_id, progress=round((index + 1) / len(job['estimates']) * 60))
            self._stage(job_id, 'estimates', 100, 'ВОР заказчика разобраны', active=False)
            self._write(job_id, 'estimates.json', projects)
            self._update(job_id, message='Сравнение объёмов и проверка позиций через ИИ', progress=65)
            models,_ = self._read_models(job_id)
            if len(models) == len(job.get('sessions', [])):
                from .ifc_json_builder import enrich_group_levels
                for model, sid in zip(models, job['sessions']):
                    runs = sorted((self.root / sid).glob('run_*/filtered_elements.xlsx'), key=lambda path:path.stat().st_mtime)
                    if runs:
                        enrich_group_levels(model, runs[-1].parent)
            build_comparison_exports(self.root/job_id,models=models,projects=projects)
            self._update(job_id)
            processor = Processor(models, projects, output_path=self.root / job_id / 'result.json')
            self._stage(job_id, 'comparison', None, 'Группировка и сравнение позиций', active=True)
            def validation_progress(done, total):
                value = round(done / total * 100) if total else 100
                self._stage(job_id, 'comparison', value, f'Сопоставление и проверка ИИ: {value}%' , active=True)
                self._update(job_id, progress=65 + round(value * .35))
            result = processor.run(on_progress=validation_progress)
            from .comparison_vor import prepare_report, input_statistics
            result['inputStatistics'] = input_statistics(models,projects)
            if job.get('mode')=='KR':result['csFinalModels']=models
            result['reconcileConstructionLinks']=True
            result = prepare_report(result,models=models)
            self._write(job_id, 'result.json', result)
            from .comparison_report import build_comparison_excel
            build_comparison_excel(str(self.root / job_id), result)
            self._stage(job_id, 'comparison', 100, 'Сравнение завершено', active=False)
            self._update(job_id, status='completed', progress=100, message='Сравнение завершено')
        except QuantityColumnRequired as exc:
            self._update(job_id, status='input_required', error=None,
                         message=f"Выберите столбец объёма: {source['name']}",
                         pending_input={'sourceIndex':index,'filename':source['name'],'choices':exc.choices,'key':exc.key})
        except Exception as exc:
            logger.exception('Comparison failed: %s', job_id)
            self._update(job_id, status='error', error=str(exc), message='Ошибка сравнения')

    def artifact(self, job_id, filename):
        job = self.get(job_id)
        if filename not in ('result.json', 'result.xlsx', 'models.json', 'estimates.json', 'semantic_match_diagnostics.jsonl', *EXPORT_FILES):
            raise KeyError('Файл не найден')
        if filename in ('result.json', 'result.xlsx') and job['status'] != 'completed':
            raise ValueError('Результат ещё не готов')
        path = self.root / job_id / filename
        if filename in EXPORT_FILES and not path.is_file():
            with self.lock:
                folder=self.root/job_id
                models=json.loads((folder/'models.json').read_text(encoding='utf-8')) if (folder/'models.json').is_file() else None
                projects=json.loads((folder/'estimates.json').read_text(encoding='utf-8')) if (folder/'estimates.json').is_file() else None
                build_comparison_exports(folder,models=models,projects=projects)
                self._update(job_id)
        if filename in ('result.json','result.xlsx'):
            from .comparison_vor import prepare_report, input_statistics
            from .comparison_report import build_comparison_excel
            with self.lock:
                report_path = self.root / job_id / 'result.json'
                if not report_path.is_file():
                    raise KeyError('Файл не найден')
                report = json.loads(report_path.read_text(encoding='utf-8'))
                folder=self.root/job_id
                models,refreshed=self._read_models(job_id) if (folder/'models.json').is_file() else (None,False)
                revisions=lambda values:[model.get('workSourceRevision') for model in values or []]
                stale_cs=self.jobs[job_id].get('mode')=='KR' and revisions(report.get('csFinalModels'))!=revisions(models)
                if report.get('reportVersion') != 16 or not report.get('inputStatistics') or refreshed or stale_cs:
                    if models is not None and (folder/'estimates.json').is_file():
                        report['inputStatistics']=input_statistics(models,json.loads((folder/'estimates.json').read_text(encoding='utf-8')))
                    if self.jobs[job_id].get('mode')=='KR' and models is not None:report['csFinalModels']=models
                    report['reconcileConstructionLinks']=True
                    report = prepare_report(report,models=models)
                    if (folder/'models.json').is_file():
                        build_comparison_exports(folder,models=models,projects=json.loads((folder/'estimates.json').read_text(encoding='utf-8')) if (folder/'estimates.json').is_file() else None)
                    self._write(job_id, 'result.json', report)
                if filename == 'result.xlsx':
                    build_comparison_excel(str(self.root / job_id), report)
                    self._update(job_id)
        if not path.is_file():
            raise KeyError('Файл не найден')
        return path
