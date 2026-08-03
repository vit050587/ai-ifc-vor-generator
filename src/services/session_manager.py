import json
import os
import shutil
import threading
import uuid
import re
from datetime import datetime
from typing import Dict, List, Optional, Any
from collections import Counter
import pandas as pd
from werkzeug.utils import secure_filename
from src.core.prompt_manager import PromptManager
from src.core.logger import setup_logger
from src.services.zero_step import zero_step
from src.services.first_etap import first_step
from src.services.second_etap import second_step
from src.services.third_etap import third_step
from src.services.fourth_etap import fourth_step
from src.services.pdf_processor import process_pdf
from src.services.serializer import _make_glb_file
from src.services.group_excel import process_ifc_excel

from openpyxl import load_workbook

logger = setup_logger(__name__)


class SessionManager:
    """Управление сессиями обработки IFC файлов"""
    
    def __init__(self, upload_folder: str, output_folder: str, sessions_file: str, perechen_xlsx: str = None, koefs_xlsx: str = None):
        self.upload_folder = os.path.abspath(upload_folder)
        self.output_folder = os.path.abspath(output_folder)
        self.sessions_file = sessions_file
        self.perechen_xlsx = perechen_xlsx
        self.koefs_xlsx = koefs_xlsx
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._state_lock = threading.RLock()
        self._load()
        
        os.makedirs(upload_folder, exist_ok=True)
        os.makedirs(output_folder, exist_ok=True)
        os.makedirs(os.path.dirname(sessions_file) or ".", exist_ok=True)
        
        # Инициализируем PromptManager
        self.prompt_manager = PromptManager()
        self.prompt_manager.load_all()
    
    def _load(self) -> None:
        if os.path.exists(self.sessions_file):
            try:
                with open(self.sessions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self._sessions = {
                            k: v for k, v in data.items() 
                            if isinstance(v, dict) and "session_id" in v
                        }
                    else:
                        self._sessions = {}
            except json.JSONDecodeError as e:
                logger.error(f"Ошибка парсинга JSON в файле сессий: {e}")
                backup_path = f"{self.sessions_file}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                try:
                    os.rename(self.sessions_file, backup_path)
                except Exception:
                    pass
                self._sessions = {}
            except Exception as e:
                logger.error(f"Ошибка загрузки сессий: {e}")
                self._sessions = {}
    
    def _save(self) -> None:
        try:
            temp_file = f"{self.sessions_file}.tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self._sessions, f, ensure_ascii=False, indent=2, default=str)
            
            if os.name == 'nt':
                if os.path.exists(self.sessions_file):
                    os.remove(self.sessions_file)
                os.rename(temp_file, self.sessions_file)
            else:
                os.replace(temp_file, self.sessions_file)
                
        except Exception as e:
            logger.error(f"Ошибка сохранения sessions.json: {e}")
    
    def _update(self, session_id: str, **fields) -> None:
        with self._state_lock:
            if session_id in self._sessions:
                protected_fields = {'session_id', 'created_at', 'ifc_file_path', 'pdf_file_path'}
                fields = {k: v for k, v in fields.items() if k not in protected_fields or k not in self._sessions[session_id]}
                self._sessions[session_id].update(fields)
                self._save()
    
    def _update_progress(self, session_id: str, progress: int, message: str) -> None:
        progress = max(0, min(100, progress))
        with self._state_lock:
            if session_id in self._sessions:
                self._sessions[session_id]["progress"] = progress
                self._sessions[session_id]["progress_message"] = message
                self._save()
    
    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        if not session_id or not isinstance(session_id, str):
            return None
            
        with self._state_lock:
            s = self._sessions.get(session_id)
            if not s:
                return None
            s = dict(s)
            
            # Если есть runs, подставляем файлы текущего запуска
            current_run_id = s.get("current_run_id")
            if current_run_id:
                runs = s.get("runs", [])
                for run in runs:
                    if run.get("run_id") == current_run_id:
                        # Подставляем файлы текущего запуска в основной список
                        s["files"] = run.get("files", [])
                        break
            
            self._decorate_files(s)
            return s
    
    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._state_lock:
            items = [dict(s) for s in self._sessions.values()]
        
        items.sort(key=lambda s: s.get("created_at", ""), reverse=True)
        
        for s in items:
            self._decorate_files(s)
        return items
    
    def delete(self, session_id: str) -> bool:
        if not session_id or not isinstance(session_id, str):
            return False
            
        with self._state_lock:
            s = self._sessions.pop(session_id, None)
            if not s:
                return False
            self._save()
        
        session_dir = os.path.join(self.output_folder, session_id)
        if os.path.isdir(session_dir):
            try:
                shutil.rmtree(session_dir, ignore_errors=True)
            except Exception as e:
                logger.error(f"Ошибка удаления директории сессии {session_id}: {e}")
        
        upload_session_dir = os.path.join(self.upload_folder, session_id)
        if os.path.isdir(upload_session_dir):
            try:
                shutil.rmtree(upload_session_dir, ignore_errors=True)
            except Exception:
                pass
                
        return True
    
    def _decorate_files(self, session: Dict[str, Any]) -> None:
        sid = session.get("session_id")
        
        # Декорируем файлы текущего запуска
        current_run_id = session.get("current_run_id")
        if current_run_id:
            runs = session.get("runs", [])
            for run in runs:
                if run.get("run_id") == current_run_id:
                    for f in run.get("files", []):
                        if isinstance(f, dict):
                            f["download_url"] = f"/ifc-vor/api/session/{sid}/download/{f.get('filename', '')}"
        
        # Оставляем старую логику для обратной совместимости
        for f in session.get("files", []):
            if isinstance(f, dict):
                f["download_url"] = f"/ifc-vor/api/session/{sid}/download/{f.get('filename', '')}"
    
    def file_path(self, session_id: str, filename: str) -> Optional[str]:
        if not filename or '..' in filename or '/' in filename or '\\' in filename:
            return None
            
        s = self.get(session_id)
        if not s:
            return None
        
        # Сначала ищем в файлах текущего запуска
        current_run_id = s.get("current_run_id")
        if current_run_id:
            runs = s.get("runs", [])
            for run in runs:
                if run.get("run_id") == current_run_id:
                    for f in run.get("files", []):
                        if f.get("filename") == filename:
                            return f.get("path")
        
        # Fallback: ищем в старых файлах сессии
        for f in s.get("files", []):
            if f.get("filename") == filename:
                return f.get("path")
        return None
    
    def _prepare_original_dir(self, session_dir: str) -> str:
        """Перемещает исходные файлы в original/ поддиректорию"""
        original_dir = os.path.join(session_dir, 'original')
        os.makedirs(original_dir, exist_ok=True)
        
        # Файлы, которые нужно переместить в original/
        patterns_to_move = [
            r'^original_.*\.(ifc|pdf)$',
            r'^ДЛЯ_СМЕТЧИКА_.*\.xlsx$',
            r'^IFC_ВСЕ_ДАННЫЕ_.*\.xlsx$',
            r'^.*\.glb$',
            r'^blueprint_painted.*\.png$',
            r'^materials_colors\.md$',
        ]
        
        moved_files = []
        for f in os.listdir(session_dir):
            fpath = os.path.join(session_dir, f)
            if not os.path.isfile(fpath):
                continue
            
            should_move = False
            for pattern in patterns_to_move:
                if re.match(pattern, f):
                    should_move = True
                    break
            
            if should_move:
                dst = os.path.join(original_dir, f)
                shutil.move(fpath, dst)
                moved_files.append(f)
                logger.info(f"Перемещён в original/: {f}")
        
        return original_dir
    
    def _get_original_excel_path(self, session_id: str) -> Optional[str]:
        """Находит путь к исходному Excel файлу в original/ директории"""
        s = self.get(session_id)
        if not s:
            return None
        
        session_dir = os.path.join(self.output_folder, session_id)
        original_dir = os.path.join(session_dir, 'original')
        
        if not os.path.exists(original_dir):
            return s.get("excel_file_path")
        
        # Ищем Excel для сметчика в original/
        # ВАЖНО: предпочитаем 'исправленный' (имеет лист 'Данные'),
        # пропускаем 'сокращенный' (у него дефолтный лист 'Sheet1')
        for f in os.listdir(original_dir):
            if 'ДЛЯ_СМЕТЧИКА' in f and 'исправленный' in f and f.endswith('.xlsx'):
                return os.path.join(original_dir, f)
        
        for f in os.listdir(original_dir):
            if 'ДЛЯ_СМЕТЧИКА' in f and 'сокращенный' not in f.lower() and f.endswith('.xlsx'):
                return os.path.join(original_dir, f)
        
        # Fallback: любой Excel (кроме сокращенного)
        for f in os.listdir(original_dir):
            if 'сокращенный' not in f.lower() and f.endswith('.xlsx'):
                return os.path.join(original_dir, f)
        
        # Last resort: любой Excel
        for f in os.listdir(original_dir):
            if f.endswith('.xlsx'):
                return os.path.join(original_dir, f)
        
        return s.get("excel_file_path")
    
    # ========== Обработка IFC ==========
    
    def process_ifc(self, file, original_name: str) -> Dict[str, Any]:
        if not file or not original_name:
            raise ValueError("Отсутствует файл или имя файла")
        
        safe_name = secure_filename(original_name)
        if not safe_name:
            safe_name = "uploaded_file.ifc"
        
        session_id = str(uuid.uuid4())
        session_dir = os.path.join(self.output_folder, session_id)
        os.makedirs(session_dir, exist_ok=True)
        
        ifc_filename = f"original_{safe_name}"
        ifc_path = os.path.join(session_dir, ifc_filename)
        
        try:
            file.save(ifc_path)
            if not os.path.exists(ifc_path) or os.path.getsize(ifc_path) == 0:
                raise ValueError("Ошибка сохранения файла")
        except Exception as e:
            logger.error(f"Ошибка сохранения IFC файла: {e}")
            raise
        
        session = {
            "session_id": session_id,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "source_type": "ifc",
            "status": "ifc_processing",
            "ifc_file_name": original_name,
            "ifc_file_path": ifc_path,
            "excel_file_name": None,
            "excel_file_path": None,
            "selected_rows": None,
            "construction_types": {},
            "construction_materials": {},
            "grouped_data": {},
            "building_height": None,
            "files": [],
            "runs": [],
            "current_run_id": None,
            "error": None,
            "progress": 0,
            "progress_message": "Начало обработки IFC...",
            "has_results": False,
        }
        
        with self._state_lock:
            self._sessions[session_id] = session
            self._save()
        
        thread = threading.Thread(
            target=self._process_ifc_bg,
            args=(session_id, ifc_path),
            daemon=True,
            name=f"IFC-Processing-{session_id[:8]}"
        )
        thread.start()
        
        return {
            "session_id": session_id,
            "status": "ifc_processing",
            "message": "IFC файл принят, начата обработка",
        }
    
    def _process_ifc_bg(self, session_id: str, ifc_path: str) -> None:
        try:
            self._update_progress(session_id, 5, "Проверка IFC файла...")
            
            if not os.path.exists(ifc_path):
                raise FileNotFoundError(f"IFC файл не найден: {ifc_path}")
            
            self._update_progress(session_id, 10, "Обработка IFC файла...")
            
            session_dir = os.path.join(self.output_folder, session_id)
            
            # Шаг 0: zero_step
            self._update_progress(session_id, 20, "Извлечение элементов из IFC...")
            zero_step(ifc_path, output_folder=session_dir)

            # Формируем ifc_elements_output.json и ifc_raw_elements_grouped.json/.xlsx
            # (аналогично пайплайну PDF из pdf_processor.py)
            try:
                from src.services.ifc_reference_builder import build_reference_from_ifc
                build_reference_from_ifc(ifc_path, session_dir)
            except Exception as e:
                logger.warning(f"Не удалось сформировать JSON-файлы справочника для IFC: {e}", exc_info=True)

            self._update_progress(session_id, 80, "Проверка результатов...")
            
            excel_for_smetchik = os.path.join(session_dir, 'ДЛЯ_СМЕТЧИКА_исправленный.xlsx')
            excel_all_data = os.path.join(session_dir, 'IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx')
            
            if not os.path.exists(excel_for_smetchik):
                excel_files = [f for f in os.listdir(session_dir) if f.endswith(('.xlsx', '.xls'))]
                if excel_files:
                    excel_for_smetchik = os.path.join(session_dir, excel_files[0])
                else:
                    raise RuntimeError("Не удалось найти созданный Excel файл")
            
            # Создаём GLB модель
            try:
                glb_filename = _make_glb_file(ifc_path, session_dir)
            except Exception as e:
                logger.warning(f'Не удалось создать файл 3D модели: {e}')
            
            # Перемещаем исходные файлы в original/
            original_dir = self._prepare_original_dir(session_dir)
            
            # Находим Excel в original/
            excel_path = None
            for f in os.listdir(original_dir):
                if 'ДЛЯ_СМЕТЧИКА' in f and f.endswith('.xlsx'):
                    excel_path = os.path.join(original_dir, f)
                    excel_filename = f
                    break
            
            if not excel_path:
                for f in os.listdir(original_dir):
                    if f.endswith('.xlsx'):
                        excel_path = os.path.join(original_dir, f)
                        excel_filename = f
                        break
            
            if not excel_path:
                raise RuntimeError("Не удалось сохранить Excel файл")
            
            file_size = os.path.getsize(excel_path)
            
            with self._state_lock:
                if session_id in self._sessions:
                    self._sessions[session_id]["excel_file_name"] = excel_filename
                    self._sessions[session_id]["excel_file_path"] = excel_path
                    self._sessions[session_id]["status"] = "ifc_processed"
                    self._sessions[session_id]["progress"] = 100
                    self._sessions[session_id]["progress_message"] = "Обработка завершена. Выберите строки и типы конструкций."
                    self._save()
            
        except Exception as e:
            import traceback
            error_msg = f"{type(e).__name__}: {str(e)}"
            logger.error(f"Ошибка обработки IFC для сессии {session_id}:\n{traceback.format_exc()}")
            self._update(session_id, status="error", error=error_msg)
    
    # ========== Обработка PDF ==========
    
    def process_pdf(self, file, original_name: str) -> Dict[str, Any]:
        """Загрузка PDF-файла и запуск фоновой обработки чертежа"""
        if not file or not original_name:
            raise ValueError("Отсутствует файл или имя файла")
        
        safe_name = secure_filename(original_name)
        if not safe_name:
            safe_name = "uploaded_file.pdf"
        
        session_id = str(uuid.uuid4())
        session_dir = os.path.join(self.output_folder, session_id)
        os.makedirs(session_dir, exist_ok=True)
        
        pdf_filename = f"original_{safe_name}"
        pdf_path = os.path.join(session_dir, pdf_filename)
        
        try:
            file.save(pdf_path)
            if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
                raise ValueError("Ошибка сохранения файла")
        except Exception as e:
            logger.error(f"Ошибка сохранения PDF файла: {e}")
            raise
        
        session = {
            "session_id": session_id,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "source_type": "pdf",
            "status": "pdf_processing",
            "pdf_file_name": original_name,
            "pdf_file_path": pdf_path,
            "ifc_file_name": None,
            "ifc_file_path": None,
            "excel_file_name": None,
            "excel_file_path": None,
            "selected_rows": None,
            "construction_types": {},
            "construction_materials": {},
            "grouped_data": {},
            "building_height": None,
            "files": [],
            "runs": [],
            "current_run_id": None,
            "error": None,
            "progress": 0,
            "progress_message": "Начало обработки PDF...",
            "has_results": False,
        }
    
        with self._state_lock:
            self._sessions[session_id] = session
            self._save()
        
        thread = threading.Thread(
            target=self._process_pdf_bg,
            args=(session_id, pdf_path),
            daemon=True,
            name=f"PDF-Processing-{session_id[:8]}"
        )
        thread.start()
        
        return {
            "session_id": session_id,
            "status": "pdf_processing",
            "message": "PDF файл принят, начата обработка",
        }
    
    def _process_pdf_bg(self, session_id: str, pdf_path: str) -> None:
        try:
            self._update_progress(session_id, 5, "Извлечение элементов из чертежа...")
            
            if not os.path.exists(pdf_path):
                raise FileNotFoundError(f"PDF файл не найден: {pdf_path}")
            
            self._update_progress(session_id, 10, "Извлечение элементов из чертежа...")
            
            session_dir = os.path.join(self.output_folder, session_id)
            
            # Обработка PDF через ai-blueprint-to-ifc пайплайн с обновлением прогресса
            self._update_progress(session_id, 20, "Извлечение элементов из чертежа...")
            result = self._process_pdf_with_progress(session_id, pdf_path, session_dir)
            
            self._update_progress(session_id, 90, "Извлечение элементов из чертежа...")
            
            excel_for_smetchik = result["excel_smetchik_path"]
            excel_all_data = result["excel_all_data_path"]
            
            # Перезаписываем переменные на новые пути в случае нескольких листов с данными
            excel_for_smetchik, excel_all_data = self._check_and_merge_sheets(
                excel_for_smetchik, 
                excel_all_data
            )
            
            if not os.path.exists(excel_for_smetchik):
                raise RuntimeError("Не удалось найти созданный Excel файл")
            
            # Перемещаем исходные файлы в original/
            original_dir = self._prepare_original_dir(session_dir)
            
            # Находим Excel в original/
            excel_path = None
            for f in os.listdir(original_dir):
                if 'ДЛЯ_СМЕТЧИКА' in f and f.endswith('.xlsx'):
                    excel_path = os.path.join(original_dir, f)
                    excel_filename = f
                    break
            
            if not excel_path:
                for f in os.listdir(original_dir):
                    if f.endswith('.xlsx'):
                        excel_path = os.path.join(original_dir, f)
                        excel_filename = f
                        break
            
            if not excel_path:
                raise RuntimeError("Не удалось найти Excel файл в original/")
            
            file_size = os.path.getsize(excel_path)
            
            with self._state_lock:
                if session_id in self._sessions:
                    self._sessions[session_id]["excel_file_name"] = excel_filename
                    self._sessions[session_id]["excel_file_path"] = excel_path
                    self._sessions[session_id]["status"] = "ifc_processed"
                    self._sessions[session_id]["progress"] = 100
                    self._sessions[session_id]["progress_message"] = "Обработка завершена. Выберите строки и типы конструкций."
                    self._save()
                    
        except Exception as e:
            import traceback
            error_msg = f"{type(e).__name__}: {str(e)}"
            logger.error(f"Ошибка обработки PDF для сессии {session_id}:\n{traceback.format_exc()}")
            self._update(session_id, status="error", error=error_msg)
    
    def _check_and_merge_sheets(self, excel_for_smetchik, excel_all_data):
        """
        Проверяет наличие листов формата Данные_0, Данные_1 и т.д.
        Если такие есть - объединяет их в общий файл и возвращает новые пути
        """
        
        def get_numbered_sheets(filepath):
            """Получает список листов формата Данные_ЧИСЛО"""
            wb = load_workbook(filepath, read_only=True)
            sheet_names = wb.sheetnames
            wb.close()
            
            pattern = re.compile(r'^Данные_\d+$')
            return [name for name in sheet_names if pattern.match(name)]
        
        def merge_sheets_to_file(source_filepath, output_filename):
            """Объединяет все листы Данные_* в один файл"""
            numbered_sheets = get_numbered_sheets(source_filepath)
            
            if not numbered_sheets:
                return source_filepath
            
            all_data = []
            for sheet_name in numbered_sheets:
                df = pd.read_excel(source_filepath, sheet_name=sheet_name)
                df['Источник_лист'] = sheet_name
                all_data.append(df)
            
            merged_df = pd.concat(all_data, ignore_index=True)
            
            output_path = os.path.join(os.path.dirname(source_filepath), output_filename)
            merged_df.to_excel(output_path, sheet_name='Данные', index=False)
            
            return output_path
        
        new_for_smetchik = merge_sheets_to_file(
            excel_for_smetchik, 
            'ДЛЯ_СМЕТЧИКА_объединенный.xlsx'
        )
        
        new_all_data = merge_sheets_to_file(
            excel_all_data, 
            'IFC_ВСЕ_ДАННЫЕ_объединенный.xlsx'
        )
        
        return new_for_smetchik, new_all_data
    
    def _process_pdf_with_progress(self, session_id: str, pdf_path: str, session_dir: str) -> Dict[str, str]:
        """
        Обработка PDF с пошаговым обновлением прогресса.
        """
        last_update_time = [0]
        min_interval = 2.0
        
        def progress_callback(stage_name: str, progress_percent: int):
            import time
            current_time = time.time()
            
            if current_time - last_update_time[0] < min_interval and progress_percent < 100:
                return
            
            last_update_time[0] = current_time
            self._update_progress(session_id, progress_percent, stage_name)
        
        result = process_pdf(pdf_path, output_folder=session_dir, progress_callback=progress_callback)
        self._update_progress(session_id, 90, "Проверка результатов...")
        
        return result
    
    # ========== Новый метод: создание повторного запуска ==========
    
    def new_run(self, session_id: str, row_indices: List[int], 
                construction_types: Dict[int, str] = None,
                construction_materials: Dict[int, str] = None,
                building_height: float = None, 
                grouped_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Создать новый запуск обработки без повторной обработки IFC/PDF"""
        
        s = self.get(session_id)
        if not s:
            raise KeyError("Сессия не найдена")
        
        # РАСШИРЯЕМ допустимые статусы — ДОБАВЛЯЕМ completed
        if s["status"] not in ("ifc_processed", "selecting_rows", "completed"):
            raise RuntimeError(f"Неверный статус сессии: {s['status']}. Ожидается: ifc_processed, selecting_rows или completed")
        
        if not row_indices:
            raise ValueError("Необходимо выбрать хотя бы одну строку")
        
        row_indices = [int(i) for i in row_indices if isinstance(i, (int, float)) and i >= 0]
        if not row_indices:
            raise ValueError("Некорректные индексы строк")
        
        # Находим исходный Excel
        excel_path = self._get_original_excel_path(session_id)
        
        # FALLBACK: если original/ нет (старые сессии), используем текущий Excel
        if not excel_path or not os.path.exists(excel_path):
            logger.warning(f"original/ не найден, использую excel_file_path из сессии")
            excel_path = s.get("excel_file_path")
        
        if not excel_path or not os.path.exists(excel_path):
            raise RuntimeError(f"Исходный Excel файл не найден. Проверьте директорию сессии.")
        
        logger.info(f"Новый запуск: сессия={session_id}, Excel={excel_path}, строк={len(row_indices)}")
        
        # Создаём новую поддиректорию для запуска
        run_id = str(uuid.uuid4())
        runs = s.get('runs', [])
        run_number = len(runs) + 1
        session_dir = os.path.join(self.output_folder, session_id)
        run_dir = os.path.join(session_dir, f'run_{run_number:03d}')
        os.makedirs(run_dir, exist_ok=True)
        
        # Копируем исходный Excel в директорию запуска
        run_excel_name = os.path.basename(excel_path)
        run_excel_path = os.path.join(run_dir, run_excel_name)
        shutil.copy2(excel_path, run_excel_path)
        logger.info(f"Excel скопирован в: {run_excel_path}")
        
        # Также копируем IFC_ВСЕ_ДАННЫЕ если есть
        original_dir = os.path.join(session_dir, 'original')
        search_dir = original_dir if os.path.exists(original_dir) else session_dir
        
        for f in os.listdir(search_dir):
            if 'IFC_ВСЕ_ДАННЫЕ' in f and f.endswith('.xlsx'):
                src = os.path.join(search_dir, f)
                dst = os.path.join(run_dir, f)
                shutil.copy2(src, dst)
                logger.info(f"IFC_ВСЕ_ДАННЫЕ скопирован в: {dst}")
                break
            # Также копируем GLB файлы в run_dir
        for f in os.listdir(search_dir):
            if f.endswith('.glb'):
                src = os.path.join(search_dir, f)
                dst = os.path.join(run_dir, f)
                shutil.copy2(src, dst)
                logger.info(f"GLB скопирован в: {dst}")
                break  # Обычно один GLB файл

        # Копируем сокращённый файл для сметчика если есть
        for f in os.listdir(search_dir):
            if 'сокращенный' in f.lower() and f.endswith('.xlsx'):
                src = os.path.join(search_dir, f)
                dst = os.path.join(run_dir, f)
                shutil.copy2(src, dst)
                logger.info(f"Сокращённый файл скопирован в: {dst}")
                break
        
        run = {
            "run_id": run_id,
            "run_number": run_number,
            "status": "processing",
            "selected_rows": row_indices,
            "construction_types": construction_types or {},
            "construction_materials": construction_materials or {},
            "building_height": building_height,
            "grouped_data": grouped_data or {},
            "files": [],
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        
        runs.append(run)
        
        self._update(
            session_id,
            current_run_id=run_id,
            runs=runs,
            status="processing",  # ВАЖНО: меняем статус сессии
            progress=0,
            progress_message=f"Запуск {run_number}: выбрано {len(row_indices)} строк"
        )
        
        # Запускаем пайплайн в поддиректории run_dir
        thread = threading.Thread(
            target=self._run_processing_pipeline_in_run,
            args=(session_id, run_id, run_number, run_dir, run_excel_path, 
                row_indices, construction_types or {}, construction_materials or {}, 
                building_height),
            daemon=True,
            name=f"Pipeline-Run{run_number}-{session_id[:8]}"
        )
        thread.start()
        
        return {
            "session_id": session_id,
            "run_id": run_id,
            "run_number": run_number,
            "status": "processing",
            "selected_rows": len(row_indices),
            "message": f"Запуск {run_number}: выбрано {len(row_indices)} строк, начата обработка"
        }
    
    def switch_run(self, session_id: str, run_id: str) -> Dict[str, Any]:
        """Переключиться на другой запуск"""
        s = self.get(session_id)
        if not s:
            raise KeyError("Сессия не найдена")
        
        runs = s.get('runs', [])
        target_run = None
        for run in runs:
            if run.get('run_id') == run_id:
                target_run = run
                break
        
        if not target_run:
            raise ValueError(f"Запуск {run_id} не найден")
        
        self._update(session_id, current_run_id=run_id)
        
        return {
            "session_id": session_id,
            "run_id": run_id,
            "run_number": target_run.get('run_number'),
            "status": target_run.get('status'),
            "files": target_run.get('files', []),
            "building_height": target_run.get('building_height'),
        }
    
    def get_run(self, session_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        """Получить данные конкретного запуска"""
        s = self.get(session_id)
        if not s:
            return None
        
        runs = s.get('runs', [])
        for run in runs:
            if run.get('run_id') == run_id:
                return dict(run)
        return None
    
    def list_runs(self, session_id: str) -> List[Dict[str, Any]]:
        """Получить список всех запусков сессии"""
        s = self.get(session_id)
        if not s:
            return []
        return s.get('runs', [])
    
    # ========== Этап 1: Выбор строк (старый метод для совместимости) ==========
    
    def select_rows(self, session_id: str, row_indices: List[int], 
                    all_rows: bool = False, row_types: Dict[int, str] = None,
                    row_materials: Dict[int, str] = None,
                    building_height: float = None, grouped_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Выбор строк с автоматическим созданием первого запуска (run_001)"""
        s = self.get(session_id)
        if not s:
            raise KeyError("Сессия не найдена")
        
        if s["status"] not in ("ifc_processed", "selecting_rows"):
            raise RuntimeError(f"Неверный статус сессии: {s['status']}")
        
        if not all_rows and not row_indices:
            raise ValueError("Необходимо выбрать хотя бы одну строку")
        
        if not all_rows:
            row_indices = [int(i) for i in row_indices if isinstance(i, (int, float)) and i >= 0]
            if not row_indices:
                raise ValueError("Некорректные индексы строк")
        
        if all_rows:
            excel_path = self._get_original_excel_path(session_id)
            if not excel_path or not os.path.exists(excel_path):
                raise RuntimeError("Excel файл не найден")
            
            try:
                df = pd.read_excel(excel_path)
                row_indices = list(range(len(df)))
            except Exception as e:
                raise RuntimeError(f"Ошибка чтения Excel файла: {str(e)}")
        
        # Если это первый запуск, используем new_run
        runs = s.get('runs', [])
        if not runs:
            return self.new_run(
                session_id, row_indices, 
                row_types or {}, row_materials or {},
                building_height, grouped_data or {}
            )
        else:
            return self.new_run(
                session_id, row_indices,
                row_types or {}, row_materials or {},
                building_height, grouped_data or {}
            )
    
    def _run_processing_pipeline_in_run(self, session_id: str, run_id: str, 
                                         run_number: int, run_dir: str,
                                         excel_path: str, row_indices: List[int],
                                         construction_types: Dict[int, str],
                                         construction_materials: Dict[int, str],
                                         building_height: float = None) -> None:
        """Запуск полного пайплайна обработки в изолированной директории запуска"""
        try:
            # ===== Применяем материалы к Excel в директории запуска =====
            if construction_materials:
                try:
                    logger.info(f"Применяем материалы к файлу: {excel_path}")
                    
                    wb = load_workbook(excel_path)
                    ws = wb['Данные']
                    
                    material_col = None
                    for col_idx, cell in enumerate(ws[1], 1):
                        if cell.value == 'Материал':
                            material_col = col_idx
                            break
                    
                    if material_col:
                        updated_count = 0
                        for row_idx in range(2, ws.max_row + 1):
                            data_idx = row_idx - 2
                            if str(data_idx) in construction_materials:
                                material = construction_materials[str(data_idx)]
                                if material and material != '-':
                                    ws.cell(row=row_idx, column=material_col).value = material
                                    updated_count += 1
                        
                        if updated_count > 0:
                            wb.save(excel_path)
                            logger.info(f"Excel обновлён: {updated_count} материалов изменено")
                    
                    wb.close()
                    
                    materials_file = os.path.join(run_dir, 'materials.json')
                    with open(materials_file, 'w', encoding='utf-8') as f:
                        json.dump(construction_materials, f, ensure_ascii=False, indent=2)
                        
                except Exception as e:
                    logger.error(f"Ошибка при применении материалов к Excel: {e}")
            
            # ===== Фильтрация + группировка =====
            self._update_progress(session_id, 5, f"Запуск {run_number}: Фильтрация элементов...")
            
            df_original = pd.read_excel(excel_path, sheet_name='Данные')
            unique_indices = sorted(set(row_indices))
            
            df_filtered = df_original.iloc[unique_indices].reset_index(drop=True)

            
            filtered_path = os.path.join(run_dir, 'filtered_elements.xlsx')
            with pd.ExcelWriter(filtered_path, engine='openpyxl') as writer:
                df_filtered.to_excel(writer, sheet_name='Данные', index=False)
            
            logger.info(f"Отфильтровано {len(df_filtered)} элементов из {len(df_original)}")
            
            # ===== Группировка =====
            self._update_progress(session_id, 10, f"Запуск {run_number}: Группировка элементов...")
            
            group_result = process_ifc_excel(filtered_path, run_dir)
            
            # Ищем IFC_ВСЕ_ДАННЫЕ в original/ директории
            session_dir = os.path.join(self.output_folder, session_id)
            original_dir = os.path.join(session_dir, 'original')
            all_data_path = None
            
            if os.path.exists(original_dir):
                for f in os.listdir(original_dir):
                    if 'IFC_ВСЕ_ДАННЫЕ' in f and f.endswith('.xlsx'):
                        all_data_path = os.path.join(original_dir, f)
                        break
            
            if all_data_path and os.path.exists(all_data_path):
                all_project_tree = process_ifc_excel(all_data_path, run_dir)
                whole_tree = all_project_tree['excel']
                whole_tree_dst = os.path.join(run_dir, 'Дерево_проекта.xlsx')
                if os.path.exists(whole_tree) and whole_tree != whole_tree_dst:
                    if os.path.exists(whole_tree_dst):
                        os.remove(whole_tree_dst)
                    os.rename(whole_tree, whole_tree_dst)
            
            tree_excel_src = group_result['excel']
            tree_excel_dst = os.path.join(run_dir, 'Дерево_проекта_выбранные_элементы.xlsx')
            if os.path.exists(tree_excel_src) and tree_excel_src != tree_excel_dst:
                if os.path.exists(tree_excel_dst):
                    os.remove(tree_excel_dst)
                os.rename(tree_excel_src, tree_excel_dst)
            
            # Загружаем JSON с группами
            json_path = group_result['json']
            with open(json_path, 'r', encoding='utf-8') as f:
                groups = json.load(f)
            
            # Собираем листовые группы
            self._update_progress(session_id, 15, f"Запуск {run_number}: Формирование групп для сметчика...")
            
            def collect_leaf_groups(groups_list, result=None):
                if result is None:
                    result = []
                for group in groups_list:
                    if group.get('children') and len(group['children']) > 0:
                        collect_leaf_groups(group['children'], result)
                    else:
                        result.append(group)
                return result
            
            leaf_groups = collect_leaf_groups(groups)
            logger.info(f"Найдено {len(leaf_groups)} групп последнего уровня")
            
            # Создаём ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx
            smetchik_rows = []
            for group in leaf_groups:
                first_element = dict(group.get('first_element', {}))
                row_data = first_element.copy()
                
                row_data['Объём_NetVolume_м3_grouped'] = group.get('total_volume', 0)
                row_data['Количество_в_группе_grouped'] = group.get('count', 1)
                
                for area_name, area_value in group.get('total_areas', {}).items():
                    if area_name.endswith('_grouped'):
                        row_data[area_name] = area_value
                    else:
                        row_data[f'{area_name}_grouped'] = area_value
                
                row_data['Название_группы'] = group.get('name', '')
                row_data['Уровень_группы'] = group.get('level', 0)
                row_data['Индексы_элементов'] = ', '.join(str(i + 1) for i in group.get('indices', []))
                
                smetchik_rows.append(row_data)
            
            df_smetchik = pd.DataFrame(smetchik_rows)
            smetchik_path = os.path.join(run_dir, 'ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx')
            
            grouped_cols = [c for c in df_smetchik.columns if c.endswith('_grouped')]
            info_cols = ['Название_группы', 'Уровень_группы', 'Индексы_элементов']
            other_cols = [c for c in df_smetchik.columns if c not in grouped_cols and c not in info_cols]
            df_smetchik = df_smetchik[other_cols + grouped_cols + info_cols]
            
            with pd.ExcelWriter(smetchik_path, engine='openpyxl') as writer:
                df_smetchik.to_excel(writer, sheet_name='Данные', index=False)
            
            logger.info(f"Создан файл для сметчика: {len(df_smetchik)} строк (групп)")
            
            # Определяем часть здания для каждой группы
            new_construction_types = {}
            for i, group in enumerate(leaf_groups):
                indices = group.get('indices', [])
                parts_in_group = []
                for idx in indices:
                    part = construction_types.get(str(idx), construction_types.get(idx, None))
                    if part:
                        parts_in_group.append(part)
                
                if parts_in_group:
                    part_counts = Counter(parts_in_group)
                    most_common_part = part_counts.most_common(1)[0][0]
                    new_construction_types[str(i)] = most_common_part
                else:
                    new_construction_types[str(i)] = 'Надземная'
            
            parts_file = os.path.join(run_dir, 'building_parts.json')
            with open(parts_file, 'w', encoding='utf-8') as f:
                json.dump(new_construction_types, f, ensure_ascii=False, indent=2)
            
            # Обновляем пути для этапов
            excel_path = smetchik_path
            row_indices = list(range(len(df_smetchik)))
            
            # ===== Этапы 1-4 =====
            self._update_progress(session_id, 20, f"Запуск {run_number}: Этап 1 — Анализ через LLM...")
            
            first_step(
                prompt_manager=self.prompt_manager,
                file=excel_path,
                rows=[i+1 for i in row_indices],
                output_folder=run_dir
            )
            
            self._update_progress(session_id, 40, f"Запуск {run_number}: Этап 2 — Фильтрация по части здания...")
            second_step(input_folder=run_dir)
            
            self._update_progress(session_id, 60, f"Запуск {run_number}: Этап 3 — Фильтрация по высоте...")
            third_step(input_folder=run_dir, building_height=building_height)
            
            self._update_progress(session_id, 90, f"Запуск {run_number}: Этап 4 — Формирование перечня...")
            fourth_step(input_folder=run_dir)
            
            self._update_progress(session_id, 95, f"Запуск {run_number}: Сохранение результатов...")
            
            # Собираем финальные файлы
            final_files = []
            skip_patterns = [
                'filtered_elements.xlsx', 'building_parts.json', 'materials.json',
            ]
            
            for f in os.listdir(run_dir):
                fpath = os.path.join(run_dir, f)
                if os.path.isfile(fpath):
                    # Пропускаем служебные файлы
                    if f in skip_patterns:
                        continue
                    if f.endswith('_grouped.json'):
                        continue
                    if f.startswith('Нормализованные_данные_элемента_') or f.endswith('.ifc'):
                        continue
                    if f.startswith('Промежуточные_работы_') or f.startswith('height') or \
                       f.startswith('Финальный') or f.startswith('Подобранные') or \
                       f.startswith('Все_найденные'):
                        continue
                    
                    final_files.append({
                        "path": fpath,
                        "filename": f,
                        "size": os.path.getsize(fpath)
                    })
            
            final_files.sort(key=lambda x: x['filename'])
            
            # Добавляем справочные файлы из корня сессии
            # (создаются при начальной обработке IFC/PDF в ifc_reference_builder)
            reference_files = [
                ("ifc_elements_output.json", "все элементы.json"),
                ("ifc_raw_elements_grouped.json", "группы элементов.json"),
                ("ifc_raw_elements_grouped.xlsx", "группы элементов.xlsx"),
            ]
            for src_name, display_name in reference_files:
                src_path = os.path.join(session_dir, src_name)
                if os.path.isfile(src_path):
                    final_files.append({
                        "path": src_path,
                        "filename": display_name,
                        "size": os.path.getsize(src_path),
                    })
            
            # Обновляем run в сессии
            with self._state_lock:
                if session_id in self._sessions:
                    runs = self._sessions[session_id].get('runs', [])
                    for run in runs:
                        if run['run_id'] == run_id:
                            run['status'] = 'completed'
                            run['files'] = final_files
                            run['building_height'] = building_height
                            break
                    
                    self._sessions[session_id]['runs'] = runs
                    self._sessions[session_id]['status'] = 'completed'
                    self._sessions[session_id]['has_results'] = True
                    self._sessions[session_id]['progress'] = 100
                    self._sessions[session_id]['progress_message'] = f"Запуск {run_number} завершён"
                    self._save()
            
        except Exception as e:
            import traceback
            error_msg = f"{type(e).__name__}: {str(e)}"
            logger.error(f"Ошибка пайплайна для запуска {run_id}:\n{traceback.format_exc()}")
            
            with self._state_lock:
                if session_id in self._sessions:
                    runs = self._sessions[session_id].get('runs', [])
                    for run in runs:
                        if run['run_id'] == run_id:
                            run['status'] = 'error'
                            run['error'] = error_msg
                            break
                    self._sessions[session_id]['runs'] = runs
                    self._save()
    
    # ========== Фильтрация по высоте (старый метод) ==========
    
    def filter_by_height(self, session_id: str, building_height: float) -> Dict[str, Any]:
        s = self.get(session_id)
        if not s:
            raise KeyError("Сессия не найдена")
        
        if s["status"] not in ("filtering_height", "filtering_type", "processing"):
            raise RuntimeError(f"Неверный статус: {s['status']}")
        
        if not isinstance(building_height, (int, float)) or building_height <= 0:
            raise ValueError("Высота здания должна быть положительным числом")
        
        if building_height > 10000:
            raise ValueError("Слишком большая высота здания")
        
        self._update(session_id, building_height=building_height)
        
        return {
            "session_id": session_id,
            "status": "processing",
            "building_height": building_height,
            "message": f"Высота обновлена: {building_height}м"
        }