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
                protected_fields = {'session_id', 'created_at', 'ifc_file_path'}
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
        for f in session.get("files", []):
            if isinstance(f, dict):
                f["download_url"] = f"/ifc-vor/api/session/{sid}/download/{f.get('filename', '')}"
    
    def file_path(self, session_id: str, filename: str) -> Optional[str]:
        if not filename or '..' in filename or '/' in filename or '\\' in filename:
            return None
            
        s = self.get(session_id)
        if not s:
            return None
        
        for f in s.get("files", []):
            if f.get("filename") == filename:
                return f.get("path")
        return None
    
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
            "grouped_data": {},
            "building_height": None,
            "files": [],
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
            
            self._update_progress(session_id, 80, "Проверка результатов...")
            
            excel_for_smetchik = os.path.join(session_dir, 'ДЛЯ_СМЕТЧИКА_исправленный.xlsx')
            excel_all_data = os.path.join(session_dir, 'IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx')
            
            if not os.path.exists(excel_for_smetchik):
                excel_files = [f for f in os.listdir(session_dir) if f.endswith(('.xlsx', '.xls'))]
                if excel_files:
                    excel_for_smetchik = os.path.join(session_dir, excel_files[0])
                else:
                    raise RuntimeError("Не удалось найти созданный Excel файл")
            
            excel_filename = f"ДЛЯ_СМЕТЧИКА_{session_id[:8]}.xlsx"
            excel_path = os.path.join(session_dir, excel_filename)
            
            if os.path.exists(excel_for_smetchik):
                if excel_for_smetchik != excel_path:
                    shutil.copy2(excel_for_smetchik, excel_path)
            else:
                if os.path.exists(excel_all_data):
                    shutil.copy2(excel_all_data, excel_path)
            
            if not os.path.exists(excel_path):
                raise RuntimeError("Не удалось сохранить Excel файл")
            
            file_size = os.path.getsize(excel_path)
            
            additional_files = []
            
            if os.path.exists(excel_for_smetchik):
                additional_files.append({
                    "path": excel_for_smetchik,
                    "filename": os.path.basename(excel_for_smetchik),
                    "size": os.path.getsize(excel_for_smetchik)
                })
            
            if os.path.exists(excel_all_data) and excel_all_data != excel_for_smetchik:
                additional_files.append({
                    "path": excel_all_data,
                    "filename": os.path.basename(excel_all_data),
                    "size": os.path.getsize(excel_all_data)
                })

            try:
                glb_filename = _make_glb_file(ifc_path, session_dir)
                if os.path.exists(glb_filename):
                    additional_files.append({
                        "path": glb_filename,
                        "filename": os.path.basename(glb_filename),
                        "size": os.path.getsize(glb_filename)
                    })
            except Exception as e:
                logger.warning(f'Не удалось создать файл 3D модели: {e}')
            
            with self._state_lock:
                if session_id in self._sessions:
                    self._sessions[session_id]["excel_file_name"] = excel_filename
                    self._sessions[session_id]["excel_file_path"] = excel_path
                    self._sessions[session_id]["status"] = "ifc_processed"
                    self._sessions[session_id]["progress"] = 100
                    self._sessions[session_id]["progress_message"] = "Обработка завершена. Выберите строки и типы конструкций."
                    
                    self._sessions[session_id]["files"].append({
                        "path": excel_path,
                        "filename": excel_filename,
                        "size": file_size
                    })
                    
                    self._sessions[session_id]["files"].extend(additional_files)
                    
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
            "grouped_data": {},
            "building_height": None,
            "files": [],
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

        def check_and_merge_sheets(excel_for_smetchik, excel_all_data):
            """
            Проверяет наличие листов формата Данные_0, Данные_1 и т.д.
            Если такие есть - объединяет их в общий файл и возвращает новые пути
            """
            
            def get_numbered_sheets(filepath):
                """Получает список листов формата Данные_ЧИСЛО"""
                wb = load_workbook(filepath, read_only=True)
                sheet_names = wb.sheetnames
                wb.close()
                
                # Ищем листы формата Данные_0, Данные_1 и т.д.
                pattern = re.compile(r'^Данные_\d+$')
                return [name for name in sheet_names if pattern.match(name)]
            
            def merge_sheets_to_file(source_filepath, output_filename):
                """Объединяет все листы Данные_* в один файл"""
                numbered_sheets = get_numbered_sheets(source_filepath)
                
                if not numbered_sheets:
                    print(f"В файле {source_filepath} нет листов формата Данные_ЧИСЛО")
                    return source_filepath
                
                # Читаем и объединяем все листы Данные_*
                all_data = []
                for sheet_name in numbered_sheets:
                    df = pd.read_excel(source_filepath, sheet_name=sheet_name)
                    df['Источник_лист'] = sheet_name  # Добавляем информацию об источнике
                    all_data.append(df)
                
                # Объединяем все DataFrame
                merged_df = pd.concat(all_data, ignore_index=True)
                
                # Сохраняем в новый файл
                output_path = os.path.join(os.path.dirname(source_filepath), output_filename)
                merged_df.to_excel(output_path, sheet_name='Данные', index=False)
                
                print(f"Создан объединенный файл: {output_path}")
                print(f"Объединено листов: {len(numbered_sheets)}")
                print(f"Общее количество строк: {len(merged_df)}")
                
                return output_path
            
            # Проверяем и обрабатываем файл для сметчика
            print("=" * 50)
            print("Обработка файла ДЛЯ_СМЕТЧИКА:")
            new_for_smetchik = merge_sheets_to_file(
                excel_for_smetchik, 
                'ДЛЯ_СМЕТЧИКА_объединенный.xlsx'
            )
            
            # Проверяем и обрабатываем файл со всеми данными
            print("=" * 50)
            print("Обработка файла IFC_ВСЕ_ДАННЫЕ:")
            new_all_data = merge_sheets_to_file(
                excel_all_data, 
                'IFC_ВСЕ_ДАННЫЕ_объединенный.xlsx'
            )
            
            return new_for_smetchik, new_all_data

        try:
            self._update_progress(session_id, 5, "Извлечение элементов из чертежа...")
            
            if not os.path.exists(pdf_path):
                raise FileNotFoundError(f"PDF файл не найден: {pdf_path}")
            
            self._update_progress(session_id, 10, "Извлечение элементов из чертежа...")
            
            session_dir = os.path.join(self.output_folder, session_id)
            
            # Обработка PDF через ai-blueprint-to-ifc пайплайн с обновлением прогресса
            # progress_callback внутри будет обновлять прогресс от 25% до 85%
            self._update_progress(session_id, 20, "Извлечение элементов из чертежа...")
            result = self._process_pdf_with_progress(session_id, pdf_path, session_dir)
            
            self._update_progress(session_id, 90, "Извлечение элементов из чертежа...")
            
            excel_for_smetchik = result["excel_smetchik_path"]
            excel_all_data = result["excel_all_data_path"]
            painted_image_path = result.get("painted_image_path")
            materials_md_path = result.get("materials_md_path")

            # Перезаписываем переменные на новые пути в случае нескольких листов с данными
            excel_for_smetchik, excel_all_data = check_and_merge_sheets(
                excel_for_smetchik, 
                excel_all_data
            )
            
            if not os.path.exists(excel_for_smetchik):
                raise RuntimeError("Не удалось найти созданный Excel файл")
            
            excel_filename = f"ДЛЯ_СМЕТЧИКА_{session_id[:8]}.xlsx"
            excel_path = os.path.join(session_dir, excel_filename)
            
            # Копируем файл для работы интерфейса
            shutil.copy2(excel_for_smetchik, excel_path)
            
            file_size = os.path.getsize(excel_path)
            
            additional_files = []
            
            if os.path.exists(excel_for_smetchik):
                additional_files.append({
                    "path": excel_for_smetchik,
                    "filename": os.path.basename(excel_for_smetchik),
                    "size": os.path.getsize(excel_for_smetchik)
                })
            
            if os.path.exists(excel_all_data) and excel_all_data != excel_for_smetchik:
                additional_files.append({
                    "path": excel_all_data,
                    "filename": os.path.basename(excel_all_data),
                    "size": os.path.getsize(excel_all_data)
                })
            
            # Чертёж с отмеченными элементами
            if painted_image_path and os.path.exists(painted_image_path):
                additional_files.append({
                    "path": painted_image_path,
                    "filename": os.path.basename(painted_image_path),
                    "size": os.path.getsize(painted_image_path)
                })
            
            # Условные обозначения (markdown)
            if materials_md_path and os.path.exists(materials_md_path):
                additional_files.append({
                    "path": materials_md_path,
                    "filename": os.path.basename(materials_md_path),
                    "size": os.path.getsize(materials_md_path)
                })
            
            with self._state_lock:
                if session_id in self._sessions:
                    self._sessions[session_id]["excel_file_name"] = excel_filename
                    self._sessions[session_id]["excel_file_path"] = excel_path
                    self._sessions[session_id]["status"] = "ifc_processed"
                    self._sessions[session_id]["progress"] = 100
                    self._sessions[session_id]["progress_message"] = "Обработка завершена. Выберите строки и типы конструкций."
                    
                    self._sessions[session_id]["files"].append({
                        "path": excel_path,
                        "filename": excel_filename,
                        "size": file_size
                    })
                    
                    self._sessions[session_id]["files"].extend(additional_files)
                    
                    self._save()
                    
        except Exception as e:
            import traceback
            error_msg = f"{type(e).__name__}: {str(e)}"
            logger.error(f"Ошибка обработки PDF для сессии {session_id}:\n{traceback.format_exc()}")
            self._update(session_id, status="error", error=error_msg)
    
    def _process_pdf_with_progress(self, session_id: str, pdf_path: str, session_dir: str) -> Dict[str, str]:
        """
        Обработка PDF с пошаговым обновлением прогресса.
        """
        # Троттлинг: сохраняем не чаще раза в N секунд
        last_update_time = [0]  # список для мутабельности внутри замыкания
        min_interval = 2.0  # минимальный интервал между сохранениями (секунды)
        
        def progress_callback(stage_name: str, progress_percent: int):
            """Callback с троттлингом для обновления прогресса"""
            import time
            current_time = time.time()
            
            # Пропускаем обновления, которые слишком частые
            if current_time - last_update_time[0] < min_interval and progress_percent < 100:
                return
            
            last_update_time[0] = current_time
            
            # Обновляем прогресс
            self._update_progress(session_id, progress_percent, stage_name)
        
        # Запускаем обработку PDF с throttled callback
        result = process_pdf(pdf_path, output_folder=session_dir, progress_callback=progress_callback)
        
        # Финальное обновление прогресса
        self._update_progress(session_id, 90, "Проверка результатов...")
        
        return result
    
    # ========== Этап 1: Выбор строк и назначение типов конструкций ==========
    
    def select_rows(self, session_id: str, row_indices: List[int], 
                    all_rows: bool = False, row_types: Dict[int, str] = None,
                    row_materials: Dict[int, str] = None,
                    building_height: float = None, grouped_data: Dict[str, Any] = None) -> Dict[str, Any]:
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
            excel_path = s["excel_file_path"]
            if not excel_path or not os.path.exists(excel_path):
                raise RuntimeError("Excel файл не найден")
            
            try:
                df = pd.read_excel(excel_path)
                row_indices = list(range(len(df)))
            except Exception as e:
                raise RuntimeError(f"Ошибка чтения Excel файла: {str(e)}")
        
        # Сохраняем типы конструкций и материалы
        construction_types = row_types or {}
        construction_materials = row_materials or {}
        
        self._update(
            session_id,
            selected_rows=row_indices,
            construction_types=construction_types,
            construction_materials=construction_materials,
            building_height=building_height,
            grouped_data=grouped_data or {},
            status="processing",
            progress=0,
            progress_message=f"Выбрано {len(row_indices)} строк. Запуск этапов обработки..."
        )
        
        # Запускаем фоновую обработку этапов 1-6
        thread = threading.Thread(
            target=self._run_processing_pipeline,
            args=(session_id, row_indices, construction_types, construction_materials, 
                building_height),
            daemon=True,
            name=f"Pipeline-{session_id[:8]}"
        )
        thread.start()
        
        return {
            "session_id": session_id,
            "status": "processing",
            "selected_rows": len(row_indices),
            "message": f"Выбрано {len(row_indices)} строк, начата обработка"
        }


    def _run_processing_pipeline(self, session_id: str, row_indices: List[int], 
                                construction_types: Dict[int, str],
                                construction_materials: Dict[int, str] = None,
                                building_height: float = None) -> None:
        """Запуск полного пайплайна обработки: группировка + этапы 1-4"""
        try:
            s = self.get(session_id)
            if not s:
                return
            
            session_dir = os.path.join(self.output_folder, session_id)
            excel_path = s["excel_file_path"]
            
            if not excel_path or not os.path.exists(excel_path):
                raise RuntimeError("Excel файл не найден")
            
            self._update_progress(session_id, 3, "Подготовка данных...")
            
            # ===== Применяем выбранные пользователем материалы к ОРИГИНАЛЬНОМУ Excel =====
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
                    
                    # Сохраняем материалы в JSON для отладки
                    materials_file = os.path.join(session_dir, 'materials.json')
                    with open(materials_file, 'w', encoding='utf-8') as f:
                        json.dump(construction_materials, f, ensure_ascii=False, indent=2)
                        
                except Exception as e:
                    logger.error(f"Ошибка при применении материалов к Excel: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            
            # ===== НОВАЯ ЛОГИКА: фильтрация + группировка через group_excel =====
            
            # Шаг 1: Фильтруем исходный Excel — оставляем только выбранные строки
            self._update_progress(session_id, 5, "Фильтрация выбранных элементов...")
            
            df_original = pd.read_excel(excel_path, sheet_name='Данные')
            
            # Убираем дубликаты в индексах и сортируем
            unique_indices = sorted(set(row_indices))
            
            # Отбираем строки по индексам
            df_filtered = df_original.iloc[unique_indices].reset_index(drop=True)
            
            # Сохраняем отфильтрованный файл
            filtered_path = os.path.join(session_dir, 'filtered_elements.xlsx')
            with pd.ExcelWriter(filtered_path, engine='openpyxl') as writer:
                df_filtered.to_excel(writer, sheet_name='Данные', index=False)
            
            logger.info(f"Отфильтровано {len(df_filtered)} элементов из {len(df_original)}")
            
            # Шаг 2: Запускаем новую группировку из group_excel.py
            self._update_progress(session_id, 10, "Группировка элементов...")
            
            group_result = process_ifc_excel(filtered_path, session_dir)
            all_project_tree = process_ifc_excel(os.path.join(session_dir, 'IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx'), session_dir)
            
            # Переименовываем результат в Дерево_проекта.xlsx
            tree_excel_src = group_result['excel']
            tree_excel_dst = os.path.join(session_dir, 'Дерево_проекта_выбранные_элементы.xlsx')

            whole_tree = all_project_tree['excel']
            whole_tree_dst = os.path.join(session_dir, 'Дерево_проекта.xlsx')
            if os.path.exists(tree_excel_src) and tree_excel_src != tree_excel_dst:
                if os.path.exists(tree_excel_dst):
                    os.remove(tree_excel_dst)
                os.rename(tree_excel_src, tree_excel_dst)
            
            if os.path.exists(whole_tree) and whole_tree != whole_tree_dst:
                if os.path.exists(whole_tree_dst):
                    os.remove(whole_tree_dst)
                os.rename(whole_tree, whole_tree_dst)
            
            # Загружаем JSON с группами
            json_path = group_result['json']
            with open(json_path, 'r', encoding='utf-8') as f:
                groups = json.load(f)
            
            # Шаг 3: Извлекаем группы последнего уровня (листья дерева)
            self._update_progress(session_id, 15, "Формирование групп для сметчика...")
            
            def collect_leaf_groups(groups_list, result=None):
                """Рекурсивно собирает листовые группы (без детей)"""
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
            
            # Шаг 4: Создаём ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx из leaf_groups
            smetchik_rows = []
            
            for group in leaf_groups:
                first_element = dict(group.get('first_element', {}))
                
                # Копируем все поля первого элемента
                row_data = first_element.copy()
                
                # Добавляем групповые поля с постфиксом _grouped
                row_data['Объём_NetVolume_м3_grouped'] = group.get('total_volume', 0)
                row_data['Количество_в_группе_grouped'] = group.get('count', 1)
                
                # Добавляем суммарные площади
                for area_name, area_value in group.get('total_areas', {}).items():
                    # Формируем имя колонки с _grouped
                    if area_name.endswith('_grouped'):
                        row_data[area_name] = area_value
                    else:
                        row_data[f'{area_name}_grouped'] = area_value
                
                # Добавляем название группы и уровень
                row_data['Название_группы'] = group.get('name', '')
                row_data['Уровень_группы'] = group.get('level', 0)
                row_data['Индексы_элементов'] = ', '.join(str(i + 1) for i in group.get('indices', []))
                
                smetchik_rows.append(row_data)
            
            # Создаём DataFrame и сохраняем
            df_smetchik = pd.DataFrame(smetchik_rows)
            smetchik_path = os.path.join(session_dir, 'ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx')
            
            # Переставляем колонки: сначала основные, потом _grouped
            grouped_cols = [c for c in df_smetchik.columns if c.endswith('_grouped')]
            info_cols = ['Название_группы', 'Уровень_группы', 'Индексы_элементов']
            other_cols = [c for c in df_smetchik.columns if c not in grouped_cols and c not in info_cols]
            df_smetchik = df_smetchik[other_cols + grouped_cols + info_cols]
            
            with pd.ExcelWriter(smetchik_path, engine='openpyxl') as writer:
                df_smetchik.to_excel(writer, sheet_name='Данные', index=False)
            
            logger.info(f"Создан файл для сметчика: {len(df_smetchik)} строк (групп)")
            
            # Шаг 4.5: Определяем часть здания для каждой группы
            new_construction_types = {}
            
            for i, group in enumerate(leaf_groups):
                indices = group.get('indices', [])
                
                # Собираем части здания всех элементов в группе
                parts_in_group = []
                for idx in indices:
                    part = construction_types.get(str(idx), construction_types.get(idx, None))
                    if part:
                        parts_in_group.append(part)
                
                # Определяем часть здания группы (большинством)
                if parts_in_group:
                    part_counts = Counter(parts_in_group)
                    most_common_part = part_counts.most_common(1)[0][0]
                    new_construction_types[str(i)] = most_common_part
                else:
                    # Если не удалось определить — Надземная по умолчанию
                    new_construction_types[str(i)] = 'Надземная'
            
            # Сохраняем новый building_parts.json
            parts_file = os.path.join(session_dir, 'building_parts.json')
            with open(parts_file, 'w', encoding='utf-8') as f:
                json.dump(new_construction_types, f, ensure_ascii=False, indent=2)
            

            # Шаг 5: Заменяем excel_path и row_indices для дальнейшей обработки
            excel_path = smetchik_path
            row_indices = list(range(len(df_smetchik)))
            
            # ===== КОНЕЦ НОВОЙ ЛОГИКИ =====
            
            # Далее — стандартный пайплайн
            self._update_progress(session_id, 20, "Этап 1: Анализ элементов через LLM...")
            
            first_step(
                prompt_manager=self.prompt_manager,
                file=excel_path,
                rows=[i+1 for i in row_indices],
                output_folder=session_dir
            )
            
            self._update_progress(session_id, 40, "Этап 2: Фильтрация по части здания...")
            
            second_step(input_folder=session_dir)
            
            self._update_progress(session_id, 60, "Этап 3: Фильтрация по высоте здания...")
            third_step(input_folder=session_dir, building_height=building_height)
            
            self._update_progress(session_id, 90, "Этап 4: Формирование финального перечня...")
            fourth_step(input_folder=session_dir)
            
            self._update_progress(session_id, 95, "Сохранение результатов...")
            
            # Собираем финальные файлы
            final_files = []
            for f in os.listdir(session_dir):
                fpath = os.path.join(session_dir, f)
                if os.path.isfile(fpath):
                    # Пропускаем служебные файлы
                    if f in ['filtered_elements.xlsx', 'building_parts.json', 'materials.json']:
                        continue
                    # Пропускаем промежуточный JSON группировки
                    if f.endswith('_grouped.json') and 'filtered_elements' in f:
                        continue
                    # Пропускаем временные файлы LLM
                    if f.startswith('Нормализованные_данные_элемента_') or f.endswith('json') or f.endswith('ifc'):
                        continue
                    # Пропускаем промежуточные файлы этапов
                    if f.startswith('Промежуточные_работы_'):
                        continue
                    if f.startswith('height') or f.startswith('Финальный') or f.startswith('Подобранные') or f.startswith('Все_найденные'):
                        continue
                    
                    final_files.append({
                        "path": fpath,
                        "filename": f,
                        "size": os.path.getsize(fpath)
                    })
            
            final_files.sort(key=lambda x: x['filename'])
            
            # Добавляем файлы чертежа (для PDF-сессий)
            for f in os.listdir(session_dir):
                if f.startswith("blueprint_painted") and f.endswith(".png"):
                    fpath = os.path.join(session_dir, f)
                    if not any(ff['path'] == fpath for ff in final_files):
                        final_files.append({
                            "path": fpath,
                            "filename": f,
                            "size": os.path.getsize(fpath)
                        })
                elif f == "materials_colors.md":
                    fpath = os.path.join(session_dir, f)
                    if not any(ff['path'] == fpath for ff in final_files):
                        final_files.append({
                            "path": fpath,
                            "filename": f,
                            "size": os.path.getsize(fpath)
                        })
            
            with self._state_lock:
                if session_id in self._sessions:
                    self._sessions[session_id]["files"] = final_files
                    self._sessions[session_id]["status"] = "completed"
                    self._sessions[session_id]["has_results"] = True
                    self._sessions[session_id]["progress"] = 100
                    self._sessions[session_id]["progress_message"] = "Обработка завершена"
                    self._save()
            
        except Exception as e:
            import traceback
            error_msg = f"{type(e).__name__}: {str(e)}"
            logger.error(f"Ошибка пайплайна для сессии {session_id}:\n{traceback.format_exc()}")
            self._update(session_id, status="error", error=error_msg)
    
    # ========== Этап 3: Фильтрация по высоте (запускает этапы 3-4 если ещё не запущены) ==========
    
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
        
        # Если обработка уже запущена, просто обновляем высоту
        if s["status"] == "processing":
            self._update(session_id, building_height=building_height)
            return {
                "session_id": session_id,
                "status": "processing",
                "building_height": building_height,
                "message": f"Высота обновлена: {building_height}м"
            }
        
        self._update(
            session_id,
            building_height=building_height,
            status="processing",
            progress=75,
            progress_message=f"Запуск фильтрации по высоте: {building_height}м..."
        )
        
        # Если пайплайн ещё не запущен, запускаем этапы 3-4
        thread = threading.Thread(
            target=self._run_height_filtering,
            args=(session_id, building_height),
            daemon=True,
            name=f"Height-Filter-{session_id[:8]}"
        )
        thread.start()
        
        return {
            "session_id": session_id,
            "status": "processing",
            "building_height": building_height,
            "message": f"Высота: {building_height}м, начата обработка"
        }
    
    def _run_height_filtering(self, session_id: str, building_height: float) -> None:
        try:
            session_dir = os.path.join(self.output_folder, session_id)
            
            self._update_progress(session_id, 60, "Фильтрация по высоте...")
            third_step(input_folder=session_dir, building_height=building_height)
            
            self._update_progress(session_id, 90, "Формирование финального перечня...")
            fourth_step(input_folder=session_dir)
            
            self._update_progress(session_id, 95, "Сохранение результатов...")
            
            all_files = []
            for f in os.listdir(session_dir):
                fpath = os.path.join(session_dir, f)
                if os.path.isfile(fpath):
                    all_files.append({
                        "path": fpath,
                        "filename": f,
                        "size": os.path.getsize(fpath)
                    })
            
            with self._state_lock:
                if session_id in self._sessions:
                    self._sessions[session_id]["files"] = all_files
                    self._sessions[session_id]["status"] = "completed"
                    self._sessions[session_id]["has_results"] = True
                    self._sessions[session_id]["progress"] = 100
                    self._sessions[session_id]["progress_message"] = "Обработка завершена"
                    self._save()
                    
        except Exception as e:
            import traceback
            error_msg = f"{type(e).__name__}: {str(e)}"
            logger.error(f"Ошибка фильтрации по высоте для сессии {session_id}:\n{traceback.format_exc()}")
            self._update(session_id, status="error", error=error_msg)