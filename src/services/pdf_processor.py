"""
Обёртка для запуска пайплайна обработки PDF-чертежей
из модуля ai-blueprint-to-ifc.

Запускает процессор, получает список конструктивных элементов (стены и т.д.)
и формирует Excel-файлы в том же формате, что и zero_step для IFC.
"""

import os
import sys
import shutil
import time
import threading
from pathlib import Path
from typing import Any, Dict

from src.core.logger import setup_logger

logger = setup_logger(__name__)

# Абсолютный путь к папке ai-blueprint-to-ifc
_BLUEPRINT_DIR = Path(__file__).resolve().parent.parent.parent / "ai-blueprint-to-ifc"

_PDF_PROCESS_LOCK = threading.Lock()


def process_pdf(
    pdf_path: str,
    output_folder: str,
    progress_callback=None,
    reference_only: bool = False,
) -> Dict[str, Any]:
    with _PDF_PROCESS_LOCK:
        return _process_pdf_unlocked(
            pdf_path,
            output_folder,
            progress_callback=progress_callback,
            reference_only=reference_only,
        )


def _process_pdf_unlocked(
    pdf_path: str,
    output_folder: str,
    progress_callback=None,
    reference_only: bool = False,
) -> Dict[str, Any]:
    """
    Обрабатывает PDF-чертёж и создаёт Excel-файлы с конструктивными элементами.

    Аргументы:
        pdf_path: абсолютный путь к PDF-файлу (строка или Path).
        output_folder: папка для сохранения результатов.
        progress_callback: опциональная функция для обновления прогресса.
            Вызывается с аргументами (stage_name: str, progress_percent: int)
        reference_only: завершить обработку сразу после формирования двух
            JSON-справочников, не создавая дополнительные Excel/PNG/MD-файлы.

    Возвращает:
        Словарь с путями к созданным файлам. В полном режиме:
        - excel_all_data_path — IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx
        - excel_smetchik_path — ДЛЯ_СМЕТЧИКА_исправленный.xlsx
        В reference_only-режиме:
        - elements_json_path — ifc_elements_output.json
        - grouped_json_path — ifc_raw_elements_grouped.json
    """
    # Конвертируем в Path если строка, затем обратно в строку для консистентности
    pdf_path_obj = Path(pdf_path) if isinstance(pdf_path, str) else pdf_path
    pdf_path_str = str(pdf_path_obj.resolve())
    output_folder_str = str(Path(output_folder).resolve())

    # Сохраняем текущее состояние
    original_cwd = os.getcwd()
    original_sys_path = sys.path[:]

    # Устанавливаем переменные окружения для config.py ДО импорта
    blueprint_abs = str(_BLUEPRINT_DIR)
    os.environ.setdefault("LOG_DIR", os.path.join(blueprint_abs, "logs"))
    os.environ.setdefault("RECORDS_DIR", os.path.join(blueprint_abs, "logs", "characteristics_records"))
    os.environ.setdefault("LLM_REQUESTS_RECORDS_DIR", os.path.join(blueprint_abs, "logs", "llm_requests_records"))
    os.environ.setdefault("PROMPTS_DIR", os.path.join(blueprint_abs, "prompts"))
    os.environ.setdefault("PAGES_DIR", os.path.join(blueprint_abs, "pages"))
    os.environ.setdefault("MODELS_DIR", os.path.join(blueprint_abs, "models"))

    # Добавляем папку в sys.path для импорта локальных модулей
    if blueprint_abs not in sys.path:
        sys.path.insert(0, blueprint_abs)

    try:
        # Меняем рабочую директорию — скрипты используют относительные пути
        os.chdir(blueprint_abs)

        # Импортируем модули (кэшируются в sys.modules после первого импорта)
        from processor import Processor
        from result_former import calculate_volumes, form_result_df
        import pandas as pd

        logger.info(f"Запуск обработки PDF: {pdf_path_str}")

        # Этап 1: Извлечение контуров стен (быстрый этап - 5%)
        if progress_callback:
            progress_callback("Извлечение контуров стен", 5)

        # Создаём процессор
        processor = Processor(Path(pdf_path_str).resolve())
        
        # Запускаем процесс обработки в отдельном потоке для эмуляции прогресса
        result_container = {"result": None, "error": None}
        
        def run_process():
            try:
                result_container["result"] = processor.process()
            except Exception as e:
                result_container["error"] = e
        
        process_thread = threading.Thread(target=run_process, daemon=True)
        process_thread.start()
        
        # Пока идёт обработка, плавно увеличиваем прогресс (анализ штриховки - самый долгий этап ~90% полоски)
        # Растягиваем от 5% до 95% с постепенным замедлением (рассчитано на 3-6 минут)
        progress_stages = [
            (10, 10),    # 10% через 10 сек
            (20, 30),    # 20% через 30 сек
            (35, 60),    # 35% через 1 мин
            (50, 120),   # 50% через 2 мин
            (65, 180),   # 65% через 3 мин
            (80, 240),   # 80% через 4 мин
            (90, 300),   # 90% через 5 мин
            (95, 360),   # 95% через 6 мин
        ]
        
        elapsed = 0
        last_reported_progress = 5
        
        while process_thread.is_alive():
            time.sleep(1)
            elapsed += 1
            
            # Обновляем прогресс на основе elapsed времени
            if progress_callback:
                # Находим подходящий прогресс для текущего elapsed времени
                reported = False
                for target_progress, min_elapsed in progress_stages:
                    if elapsed >= min_elapsed and target_progress > last_reported_progress:
                        progress_callback("Анализ штриховки", target_progress)
                        last_reported_progress = target_progress
                        reported = True
                        break
                
                # Если прошло больше 360 секунд (6 мин), продолжаем очень медленно до 95%
                if elapsed > 360 and last_reported_progress < 95:
                    # Увеличиваем на 1% каждые 30 секунд после 6 минут
                    extra_progress = min(95, 95 + (elapsed - 360) // 30)
                    if extra_progress > last_reported_progress:
                        progress_callback("Анализ штриховки", extra_progress)
                        last_reported_progress = extra_progress
        
        # Ждём завершения потока
        process_thread.join()
        
        # Проверяем на ошибки
        if result_container["error"]:
            raise result_container["error"]
        
        result = result_container["result"]

        # processor.process() возвращает {"drawings": [{"painted_image", "materials_colors_md", "result"}, ...],
        #                                     "full_drawing": {"painted_image", "materials_colors_md"}}
        drawings = result.get("drawings", []) if isinstance(result, dict) else []
        full_drawing = result.get("full_drawing") if isinstance(result, dict) else None

        # Объединяем результаты всех чертежей в один
        merged = {}
        painted_images = []
        materials_colors_mds = []
        for drawing in drawings:
            dr = drawing.get("result", {})
            for key, value in dr.items():
                if key in merged and isinstance(merged[key], list) and isinstance(value, list):
                    merged[key].extend(value)
                else:
                    merged[key] = value
            if drawing.get("painted_image") is not None:
                painted_images.append(drawing["painted_image"])
            if drawing.get("materials_colors_md"):
                materials_colors_mds.append(drawing["materials_colors_md"])

        result = merged

        # Сводный чертёж и условные обозначения из full_drawing имеют приоритет над склейкой из drawings
        if full_drawing:
            if full_drawing.get("painted_image") is not None:
                painted_images = [full_drawing["painted_image"]]
            if full_drawing.get("materials_colors_md"):
                materials_colors_mds = [full_drawing["materials_colors_md"]]

        walls_count = len(result.get("walls", []))
        logger.info(f"PDF обработан. Найдено элементов: стен={walls_count}")

        # Этап 2: Анализ штриховки завершён
        if progress_callback:
            # Убеждаемся, что прогресс хотя бы 95%
            if last_reported_progress < 95:
                progress_callback("Анализ штриховки завершён", 95)
            else:
                progress_callback("Анализ штриховки завершён", last_reported_progress)

        # Формируем DataFrame в том же формате, что и zero_step
        calculated = calculate_volumes(result)
        df_all = form_result_df(calculated)

        # Этап 3: Формирование результатов (быстрый этап - последние 5%)
        if progress_callback:
            progress_callback("Формирование результатов", 98)

        # ========== АГРЕГИРОВАННЫЕ КОЛОНКИ (как в zero_step для IFC) ==========
        # Добавляем ДО build_reference_from_pdf, чтобы группировка видела те же колонки
        # Ширина, мм — из Длина_Width_мм (толщина стен / ширина колонн)
        if 'Длина_Width_мм' in df_all.columns:
            df_all['Ширина, мм'] = df_all['Длина_Width_мм']
        # Длина, мм — из Глубина_выдавливания_мм (высота/длина выдавливания)
        if 'Глубина_выдавливания_мм' in df_all.columns:
            df_all['Длина, мм'] = df_all['Глубина_выдавливания_мм']
        # Высота, мм — из Высота_сечения_мм
        if 'Высота_сечения_мм' in df_all.columns:
            df_all['Высота, мм'] = df_all['Высота_сечения_мм']
        # Периметр, м — из Периметр_мм / 1000
        if 'Периметр_мм' in df_all.columns:
            df_all['Периметр, м'] = pd.to_numeric(
                df_all['Периметр_мм'].replace('-', pd.NA), errors='coerce'
            ) / 1000
        # Площадь, м2 — из Площадь_GrossArea_м2
        if 'Площадь_GrossArea_м2' in df_all.columns:
            df_all['Площадь, м2'] = df_all['Площадь_GrossArea_м2']
        # Объём, м3 — из Объём_NetVolume_м3
        if 'Объём_NetVolume_м3' in df_all.columns:
            df_all['Объём, м3'] = df_all['Объём_NetVolume_м3']

        # Заполняем NaN в агрегированных колонках
        for col in ['Ширина, мм', 'Длина, мм', 'Высота, мм', 'Периметр, м', 'Площадь, м2', 'Объём, м3']:
            if col in df_all.columns:
                df_all[col] = df_all[col].fillna('-')

        # Колонки этажности (в PDF недоступны — прочерк)
        for col in ['Этаж', 'Тип_этажа', 'Уровень_этажа_мм']:
            if col not in df_all.columns:
                df_all[col] = '-'

        # Формируем ifc_elements_output.json и ifc_raw_elements_grouped.json
        # (аналогично пайплайну IFC из ifc_reference_builder.py)
        try:
            from src.services.ifc_reference_builder import build_reference_from_pdf
            build_reference_from_pdf(df_all, output_folder_str)
        except Exception as e:
            if reference_only:
                raise
            logger.warning(f"Не удалось сформировать JSON-файлы справочника для PDF: {e}", exc_info=True)

        if reference_only:
            return {
                "elements_json_path": os.path.join(
                    output_folder_str, "ifc_elements_output.json"
                ),
                "grouped_json_path": os.path.join(
                    output_folder_str, "ifc_raw_elements_grouped.json"
                ),
            }

        # Заполняем пропуски
        df_all = df_all.fillna("-")

        # Добавляем колонку № п/п если её нет
        if "№ п/п" not in df_all.columns:
            df_all.insert(0, "№ п/п", range(1, len(df_all) + 1))

        # Добавляем служебные колонки как в zero_step
        if "Примечание_сметчика" not in df_all.columns:
            df_all["Примечание_сметчика"] = ""
        if "Стоимость_за_ед_руб" not in df_all.columns:
            df_all["Стоимость_за_ед_руб"] = ""
        if "Общая_стоимость_руб" not in df_all.columns:
            df_all["Общая_стоимость_руб"] = ""

        # --- Сохраняем IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx ---
        excel_all_data_path = os.path.join(output_folder_str, "IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx")
        df_all.to_excel(excel_all_data_path, index=False, engine="openpyxl")
        logger.info(f"Сохранён файл всех данных: {excel_all_data_path}")

        # --- Формируем ДЛЯ_СМЕТЧИКА_исправленный.xlsx (как в zero_step для IFC) ---
        smetchik_cols = [
            'Тип (RU)', 'Тип элемента', 'Имя', 'GlobalId', 'Материал',
            'Этаж', 'Тип_этажа', 'Уровень_этажа_мм',
        ]

        # Оригинальные геометрические колонки (с _мм)
        for col in df_all.columns:
            if any(term in col for term in ['Длина', 'Ширина', 'Высота', 'Глубина']) and '_мм' in col:
                if col not in smetchik_cols:
                    smetchik_cols.append(col)

        # Объёмы
        for col in df_all.columns:
            if 'Объём' in col and ('_м3' in col or '_литры' in col):
                if col not in smetchik_cols:
                    smetchik_cols.append(col)

        # Площади (Gross)
        for col in df_all.columns:
            if 'Площадь' in col and 'Gross' in col and '_м2' in col:
                if col not in smetchik_cols:
                    smetchik_cols.append(col)

        # Агрегированные колонки (как в zero_step)
        aggregated_cols = ['Длина, мм', 'Ширина, мм', 'Высота, мм', 'Периметр, м', 'Площадь, м2', 'Объём, м3']
        for col in aggregated_cols:
            if col in df_all.columns and col not in smetchik_cols:
                smetchik_cols.append(col)

        # Только существующие колонки
        existing_cols = [col for col in smetchik_cols if col in df_all.columns]
        df_smetchik = df_all[existing_cols].copy()

        # Добавляем № п/п в начало
        if "№ п/п" not in df_smetchik.columns:
            df_smetchik.insert(0, "№ п/п", range(1, len(df_smetchik) + 1))

        # Служебные колонки
        if "Примечание_сметчика" not in df_smetchik.columns:
            df_smetchik["Примечание_сметчика"] = ""
        if "Стоимость_за_ед_руб" not in df_smetchik.columns:
            df_smetchik["Стоимость_за_ед_руб"] = ""
        if "Общая_стоимость_руб" not in df_smetchik.columns:
            df_smetchik["Общая_стоимость_руб"] = ""

        # Сводка по типам (как в zero_step)
        summary_data = []
        volume_col = 'Объём, м3' if 'Объём, м3' in df_all.columns else None

        if "Тип (RU)" in df_all.columns and "Тип элемента" in df_all.columns:
            group_cols = ["Тип (RU)", "Тип элемента"]
            if "Материал" in df_all.columns:
                group_cols.append("Материал")

            grouped = df_all.replace("-", pd.NA).groupby(group_cols)
            for group_key, group in grouped:
                if not isinstance(group_key, tuple):
                    group_key = (group_key,)
                count = len(group)
                total_volume = 0
                if volume_col and volume_col in df_all.columns:
                    vol_series = pd.to_numeric(group[volume_col], errors="coerce").fillna(0)
                    total_volume = vol_series.sum()

                row_data = {}
                for i, col_name in enumerate(group_cols):
                    row_data[col_name] = group_key[i]
                row_data["Количество, шт"] = count
                row_data["Объем, м³"] = round(total_volume, 3) if total_volume > 0 else "-"
                summary_data.append(row_data)

        excel_smetchik_path = os.path.join(output_folder_str, "ДЛЯ_СМЕТЧИКА_исправленный.xlsx")

        with pd.ExcelWriter(excel_smetchik_path, engine="openpyxl") as writer:
            df_smetchik.to_excel(writer, sheet_name="Данные", index=False)
            if summary_data:
                df_summary = pd.DataFrame(summary_data)
                df_summary.to_excel(writer, sheet_name="Сводка_по_типам", index=False)

        logger.info(f"Сохранён файл для сметчика: {excel_smetchik_path}")

        # --- Сохраняем изображение чертежа с отмеченными элементами ---
        painted_image_path = None
        if painted_images:
            # Если несколько чертежей — сохраняем каждый отдельно
            if len(painted_images) == 1:
                painted_image_path = os.path.join(output_folder_str, "blueprint_painted.png")
                painted_images[0].save(painted_image_path)
            else:
                for idx, img in enumerate(painted_images):
                    path = os.path.join(output_folder_str, f"blueprint_painted_{idx}.png")
                    img.save(path)
                painted_image_path = os.path.join(output_folder_str, "blueprint_painted_0.png")
            logger.info(f"Сохранён чертёж с отметками: {painted_image_path}")

        # --- Сохраняем условные обозначения (markdown) ---
        materials_md_path = None
        materials_md_content = ""
        if materials_colors_mds:
            materials_md_content = "\n\n".join(materials_colors_mds)
            materials_md_path = os.path.join(output_folder_str, "materials_colors.md")
            with open(materials_md_path, "w", encoding="utf-8") as f:
                f.write(materials_md_content)
            logger.info(f"Сохранены условные обозначения: {materials_md_path}")

        return {
            "excel_all_data_path": excel_all_data_path,
            "excel_smetchik_path": excel_smetchik_path,
            "painted_image_path": painted_image_path,
            "materials_md_path": materials_md_path,
        }

    except Exception as e:
        logger.error(f"Ошибка обработки PDF: {e}", exc_info=True)
        raise
    finally:
        # Восстанавливаем рабочую директорию
        os.chdir(original_cwd)
        # Восстанавливаем sys.path
        sys.path[:] = original_sys_path
