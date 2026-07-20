import json
import os
import shutil
import threading
import uuid
import re
from datetime import datetime
from typing import Dict, List, Optional, Any
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
                glb_filename =_make_glb_file(ifc_path, session_dir)
            except Exception as e:
                logger.warning(f'Не удалось создать файл 3D модели: {e}')

            if os.path.exists(glb_filename):
                additional_files.append({
                    "path": glb_filename,
                    "filename": os.path.basename(excel_all_data),
                    "size": os.path.getsize(excel_all_data)
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

            
            #Перезаписываем переменные на новые пути в случае нескольких листов с данными
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
        grouped_data = grouped_data or {}
        
        self._update(
            session_id,
            selected_rows=row_indices,
            construction_types=construction_types,
            construction_materials=construction_materials,
            building_height=building_height,
            grouped_data=grouped_data,
            status="processing",
            progress=0,
            progress_message=f"Выбрано {len(row_indices)} строк. Запуск этапов обработки..."
        )
        
        # Запускаем фоновую обработку этапов 1-6
        thread = threading.Thread(
            target=self._run_processing_pipeline,
            args=(session_id, row_indices, construction_types, construction_materials, 
                building_height, grouped_data),
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
                                building_height: float = None,
                                grouped_data: Dict[str, Any] = None) -> None:
        """Запуск полного пайплайна обработки: этапы 1-4"""
        try:
            s = self.get(session_id)
            if not s:
                return
            
            session_dir = os.path.join(self.output_folder, session_id)
            excel_path = s["excel_file_path"]
            
            if not excel_path or not os.path.exists(excel_path):
                raise RuntimeError("Excel файл не найден")
            
            self._update_progress(session_id, 3, "Подготовка данных...")
            
            # Применяем выбранные пользователем материалы к ОРИГИНАЛЬНОМУ Excel
            if construction_materials:
                try:
                    from openpyxl import load_workbook
                    
                    logger.info(f"Применяем материалы к файлу: {excel_path}")
                    logger.info(f"Материалы для обновления: {construction_materials}")
                    
                    # Открываем книгу
                    wb = load_workbook(excel_path)
                    ws = wb['Данные']
                    
                    # Находим колонку "Материал"
                    material_col = None
                    for col_idx, cell in enumerate(ws[1], 1):
                        if cell.value == 'Материал':
                            material_col = col_idx
                            break
                    
                    if material_col:
                        updated_count = 0
                        # Начинаем со 2-й строки (после заголовков)
                        for row_idx in range(2, ws.max_row + 1):
                            data_idx = row_idx - 2  # 0-based индекс
                            
                            if str(data_idx) in construction_materials:
                                material = construction_materials[str(data_idx)]
                                if material and material != '-':
                                    old_value = ws.cell(row=row_idx, column=material_col).value
                                    ws.cell(row=row_idx, column=material_col).value = material
                                    updated_count += 1
                        
                        if updated_count > 0:
                            # Сохраняем книгу со всеми листами
                            wb.save(excel_path)
                            logger.info(f"✅ Excel обновлён: {updated_count} материалов изменено в файле {excel_path}")
                        else:
                            logger.info("Нет материалов для обновления (все значения уже установлены или равны '-')")
                    else:
                        logger.warning("Колонка 'Материал' не найдена на листе 'Данные'")
                    
                    wb.close()
                    
                    # Сохраняем материалы в JSON для отладки
                    materials_file = os.path.join(session_dir, 'materials.json')
                    with open(materials_file, 'w', encoding='utf-8') as f:
                        json.dump(construction_materials, f, ensure_ascii=False, indent=2)
                        
                except Exception as e:
                    logger.error(f"Ошибка при применении материалов к Excel: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            
            # Если есть сгруппированные данные — создаём новый Excel для пайплайна
            if grouped_data:
                # Читаем УЖЕ ОБНОВЛЕННЫЙ Excel
                df_full = pd.read_excel(excel_path, sheet_name='Данные')
                df_full = df_full.replace('-', pd.NA)
                            
                # Очищаем "Имя" от ID после последнего ":"
                def clean_name(name):
                    if pd.isna(name):
                        return name
                    name = str(name)
                    last_colon = name.rfind(':')
                    if last_colon != -1:
                        after = name[last_colon+1:].strip()
                        if after.isdigit():
                            return name[:last_colon].strip()
                    return name

                df_full['Имя_очищенное'] = df_full['Имя'].apply(clean_name)
                
                # Часть здания берём из construction_types
                if 'Часть здания' not in df_full.columns:
                    df_full['Часть здания'] = 'Надземная'
                
                for idx_str, part in construction_types.items():
                    idx = int(idx_str)
                    if idx < len(df_full):
                        df_full.at[idx, 'Часть здания'] = part
                
                # Группируем
                group_cols_base = ['Часть здания', 'Материал', 'Имя_очищенное']
                columns_df = df_full.columns
                group_cols = [col for col in group_cols_base if col in columns_df]
                
                # Колонки для суммирования
                sum_cols = [col for col in columns_df if 'объем' in col.lower() or 'площадь' in col.lower()]
                
                # Создаем agg_dict, исключая group_cols
                agg_dict = {col: 'sum' for col in sum_cols if col in df_full.columns}
                
                for col in df_full.columns:
                    if col not in agg_dict and col not in group_cols:
                        agg_dict[col] = 'first'
                
                # Группируем
                df_grouped = df_full.groupby(group_cols, dropna=False).agg(agg_dict).reset_index()

                counts = df_full.groupby(group_cols, dropna=False).size()
                counts.name = 'Количество элементов в группе'
                df_grouped = df_grouped.merge(counts, on=group_cols)

                # Список № п/п через точку с запятой
                def join_numbers(indices):
                    nums = df_full.iloc[indices]['№ п/п'].dropna().astype(int).astype(str).tolist()
                    return '; '.join(nums)

                numbers = df_full.groupby(group_cols, dropna=False).apply(
                    lambda g: join_numbers(g.index), include_groups=False
                )
                numbers.name = 'Элементы в группе'
                df_grouped = df_grouped.merge(numbers, on=group_cols)

                # Переименовываем Имя_очищенное обратно в Имя
                if 'Имя' in df_grouped.columns and 'Имя_очищенное' in group_cols:
                    df_grouped = df_grouped.drop(['Имя'], axis=1)
                if 'GlobalId' in df_grouped.columns:
                    df_grouped = df_grouped.drop(['GlobalId'], axis=1)
                
                df_grouped = df_grouped.rename(columns={'Имя_очищенное': 'Имя'})

                # Желаемый порядок колонок
                desired_order = [
                    'Часть здания', 'Материал', '№ п/п', 
                    'Количество элементов в группе', 'Элементы в группе', 'Тип (RU)', 
                    'Тип элемента', 'Имя', 'Этаж', 'Тип_этажа', 'Уровень_этажа_мм',
                    'Глубина_выдавливания_мм', 'Длина_Height_мм', 'Длина_Length_мм', 
                    'Длина_Perimeter_мм', 'Длина_Width_мм', 'Объём_GrossVolume_литры', 
                    'Объём_NetVolume_м3', 'Площадь_CrossSectionArea_м2', 
                    'Площадь_GrossArea_м2', 'Площадь_GrossSideArea_м2',
                    'Площадь_NetArea_м2', 'Площадь_NetSideArea_м2', 
                    'Площадь_OuterSurfaceArea_м2', 'Примечание_сметчика', 
                    'Стоимость_за_ед_руб', 'Общая_стоимость_руб'
                ]
                
                existing_cols = [col for col in desired_order if col in df_grouped.columns]
                other_cols = [col for col in df_grouped.columns if col not in desired_order]
                df_grouped = df_grouped[existing_cols + other_cols]
                
                if '№ п/п' in df_grouped.columns:
                    df_grouped['_sort'] = pd.to_numeric(df_grouped['№ п/п'], errors='coerce').fillna(0)
                    df_grouped = df_grouped.sort_values('_sort').drop(columns=['_sort'])

                # Сохраняем полный сгруппированный файл
                full_grouped_path = os.path.join(session_dir, 'Результаты группировки.xlsx')
                with pd.ExcelWriter(full_grouped_path, engine='openpyxl') as writer:
                    df_grouped.to_excel(writer, sheet_name='Данные', index=False)
                
                # Для пайплайна — только выбранные строки
                df_selected = df_full.iloc[row_indices].reset_index(drop=True)
                
                # Применяем суммированные значения из grouped_data
                for idx_str, group_info in grouped_data.items():
                    orig_idx = int(idx_str)
                    if orig_idx in row_indices:
                        pos = row_indices.index(orig_idx)
                        
                        # 1. Применяем суммированные значения с суффиксом _grouped
                        summed = group_info.get("summed", {})
                        for col, value in summed.items():
                            grouped_col = f"{col}_grouped"
                            if grouped_col not in df_selected.columns:
                                df_selected[grouped_col] = 0.0
                            try:
                                df_selected.at[pos, grouped_col] = float(value)
                            except (ValueError, TypeError):
                                df_selected.at[pos, grouped_col] = value
                        
                        
                        # 2. Добавляем количество элементов в группе
                        count = group_info.get("count", 1)
                        if 'Количество_в_группе' not in df_selected.columns:
                            df_selected['Количество_в_группе'] = 1
                        df_selected.at[pos, 'Количество_в_группе'] = count
                
                grouped_excel_path = os.path.join(session_dir, 'ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx')
                with pd.ExcelWriter(grouped_excel_path, engine='openpyxl') as writer:
                    df_selected.to_excel(writer, sheet_name='Данные', index=False)
                
                excel_path = grouped_excel_path
                row_indices = list(range(len(df_selected)))
            
            self._update_progress(session_id, 5, "Этап 1: Анализ элементов через LLM...")
            
            # Этап 1: first_step
            first_step(
                prompt_manager=self.prompt_manager,
                file=excel_path,
                rows=[i+1 for i in row_indices],
                output_folder=session_dir
            )
            
            self._update_progress(session_id, 20, "Этап 2: Фильтрация по части здания...")

            parts_file = os.path.join(session_dir, 'building_parts.json')
            with open(parts_file, 'w', encoding='utf-8') as f:
                json.dump(construction_types, f, ensure_ascii=False, indent=2)

            # Этап 2: second_step
            second_step(input_folder=session_dir)
            
            # Этап 3: third_step
            self._update_progress(session_id, 60, "Этап 3: Фильтрация по высоте здания...")
            third_step(input_folder=session_dir, building_height=building_height)
            
            # Этап 4: fourth_step
            self._update_progress(session_id, 90, "Этап 4: Формирование финального перечня...")
            fourth_step(input_folder=session_dir)
            
            self._update_progress(session_id, 95, "Сохранение результатов...")
            
            # Собираем финальные файлы результатов
            final_files = []
            for f in os.listdir(session_dir):
                # Пропускаем временные и служебные файлы
                if 'группир' in f or "ОБЩИЙ" in f or ("ДЛЯ_СМЕТЧИКА" in f and "испр"not in f) or '.glb' in f:
                    
                    fpath = os.path.join(session_dir, f)
                    if os.path.isfile(fpath):
                        final_files.append({
                            "path": fpath,
                            "filename": f,
                            "size": os.path.getsize(fpath)
                        })

            final_files.sort(key=lambda x: x['filename'])
            
            # Сохраняем файлы чертежа и условных обозначений (для PDF-сессий)
            blueprint_files = []
            for f in os.listdir(session_dir):
                if f.startswith("blueprint_painted") and f.endswith(".png"):
                    fpath = os.path.join(session_dir, f)
                    blueprint_files.append({
                        "path": fpath,
                        "filename": f,
                        "size": os.path.getsize(fpath)
                    })
                elif f == "materials_colors.md":
                    fpath = os.path.join(session_dir, f)
                    blueprint_files.append({
                        "path": fpath,
                        "filename": f,
                        "size": os.path.getsize(fpath)
                    })
            
            with self._state_lock:
                if session_id in self._sessions:
                    # Объединяем файлы результатов с файлами чертежа
                    self._sessions[session_id]["files"] = final_files + blueprint_files
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