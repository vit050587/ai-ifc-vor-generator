# ifc_json_builder.py
"""
Сборщик JSON для IFC-данных
Поддерживает два режима:
- АР (Архитектурные решения) - использует filtered_elements_grouped_AR.json
- КР (Конструктивные решения) - использует selected_elements_grouped.json
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union
import pandas as pd
from difflib import SequenceMatcher

from src.services.api_works_lookup import _is_curing_work


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ОБЩИЕ)
# ============================================================

def safe_float(value, default=0.0) -> float:
    """Безопасное преобразование в float."""
    if value is None or pd.isna(value) or value == "-" or value == "":
        return default
    if isinstance(value, bool):
        return float(value)
    try:
        if isinstance(value, str):
            value = value.replace(",", ".").replace(" ", "")
        return float(value)
    except (ValueError, TypeError):
        return default


def clean_string(s: str) -> str:
    """Очистка строки для сравнения."""
    if not s or pd.isna(s):
        return ""
    s = str(s).strip().lower()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'[^\w\s\-\.\(\)]', '', s)
    return s


def similar_ratio(a: str, b: str) -> float:
    """Коэффициент схожести двух строк."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, clean_string(a), clean_string(b)).ratio()


def load_json_file(file_path: Path) -> Optional[Dict]:
    """Загрузка JSON из файла."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"  Ошибка загрузки {file_path.name}: {e}")
        return None


def load_excel_file(file_path: Path) -> Optional[pd.DataFrame]:
    """Загрузка Excel-файла."""
    try:
        return pd.read_excel(file_path, dtype=str)
    except Exception as e:
        print(f"  Ошибка загрузки Excel {file_path.name}: {e}")
        return None


def find_file_by_pattern(folder_path: Path, patterns: List[str]) -> Optional[Path]:
    """Поиск одного файла по паттернам."""
    for pattern in patterns:
        matches = list(folder_path.glob(pattern))
        if matches:
            return matches[0]
    return None


def clean_element_name(name: str) -> str:
    """
    Очищает имя элемента от ID и суффиксов в конце.
    """
    if not name:
        return ""
    
    result = str(name)
    
    # Удаляем :цифры в конце (например :1606533)
    result = re.sub(r':\d+$', '', result)
    
    # Удаляем GUID в конце (если есть)
    guid_pattern = r'[A-Za-z0-9$]{20,}'
    result = re.sub(r'\s*' + guid_pattern + r'\s*$', '', result)
    
    # Удаляем GUID в середине (если есть)
    result = re.sub(r'\s*' + guid_pattern + r'\s*', ' ', result)
    
    # Удаляем лишние пробелы
    result = re.sub(r'\s+', ' ', result).strip()
    
    return result


def extract_guid_from_string(text: str) -> Optional[str]:
    """Извлечение GUID из строки."""
    if not text or pd.isna(text):
        return None
    pattern = r'[A-Za-z0-9$]{20,}'
    match = re.search(pattern, str(text))
    if match:
        return match.group(0)
    return None


def extract_leaf_groups(group: Union[Dict, List]) -> List[Dict]:
    """Извлекает все конечные (листовые) группы из иерархической структуры."""
    leaf_groups = []
    
    def process_node(node: Dict, parent_chain: str = ""):
        if not isinstance(node, dict):
            return
        
        name = node.get("name", "")
        full_name = f"{parent_chain} > {name}" if parent_chain else name
        
        children = node.get("children", [])
        
        if not children:
            node_copy = node.copy()
            node_copy["full_name"] = full_name
            leaf_groups.append(node_copy)
        else:
            for child in children:
                process_node(child, full_name)
    
    if isinstance(group, list):
        for g in group:
            process_node(g)
    else:
        process_node(group)
    
    return leaf_groups


def collect_guids_from_indices(indices: List[int], all_elements_df: pd.DataFrame) -> List[str]:
    """Сбор всех GUIDs по индексам из DataFrame."""
    guids = []
    
    if all_elements_df is None or not indices:
        return guids
    
    guid_col = None
    for col in all_elements_df.columns:
        if "GlobalId" in str(col):
            guid_col = col
            break
    
    if not guid_col:
        return guids
    
    for idx in indices:
        if idx < len(all_elements_df):
            guid = all_elements_df.iloc[idx].get(guid_col, "")
            if guid and not pd.isna(guid) and str(guid) not in guids:
                guids.append(str(guid))
    
    return guids


# ============================================================
# КЛАСС ДЛЯ АР (АРХИТЕКТУРНЫЕ РЕШЕНИЯ)
# ============================================================

class ARBuilder:
    """Сборщик JSON для АР."""
    
    def __init__(self, folder_path: str):
        self.folder = Path(folder_path)
        self.data = {
            "groups": [],
            "elements_df": None,
            "excel_works": {},
        }
        
    def load_all_files(self) -> bool:
        """Загрузка всех необходимых файлов."""
        print(f"\nЗагрузка файлов из: {self.folder}")
        
        # 1. filtered_elements_grouped_AR.json
        groups_file = find_file_by_pattern(
            self.folder, 
            ["filtered_elements_grouped_AR.json", "filtered_elements_grouped.json"]
        )
        if not groups_file:
            print("  ОШИБКА: не найден filtered_elements_grouped_AR.json")
            return False
        
        groups_data = load_json_file(groups_file)
        if not groups_data:
            return False
        
        self.data["groups"] = groups_data if isinstance(groups_data, list) else []
        print(f"  Загружено групп: {len(self.data['groups'])}")
        
        # 2. filtered_elements.xlsx
        excel_file = find_file_by_pattern(self.folder, ["filtered_elements*.xlsx"])
        if not excel_file:
            print("  ОШИБКА: не найден filtered_elements.xlsx")
            return False
        
        self.data["elements_df"] = load_excel_file(excel_file)
        if self.data["elements_df"] is None:
            return False
        
        print(f"  Загружено элементов: {len(self.data['elements_df'])}")
        
        # 3. Общий Excel с работами
        works_excel = find_file_by_pattern(
            self.folder, 
            ["ОБЩИЙ_Финальный_перечень*.xlsx", "*финальный*перечень*.xlsx", "Финальный_перечень*.xlsx"]
        )
        if works_excel:
            df = load_excel_file(works_excel)
            if df is not None:
                self.data["excel_works"] = self._parse_excel_works(df)
                print(f"  Загружено групп из Excel: {len(self.data['excel_works'])}")
        else:
            print("  Предупреждение: не найден общий Excel с работами")
        
        return True
    
    def _parse_excel_works(self, df: pd.DataFrame) -> Dict[str, List[Dict]]:
        """Парсинг общего Excel с работами."""
        result = {}
        
        cols = df.columns.tolist()
        param_col = None
        code_col = None
        name_col = None
        unit_col = None
        volume_col = None
        
        for col in cols:
            col_str = str(col).strip()
            if "Параметризация" in col_str:
                param_col = col
            elif "Шифр" in col_str or "Код" in col_str:
                code_col = col
            elif "Наименование" in col_str and "расценки" in col_str.lower():
                name_col = col
            elif "Ед" in col_str and "изм" in col_str:
                unit_col = col
            elif "Объём" in col_str or "Объем" in col_str:
                volume_col = col
        
        if not name_col:
            # Ищем любую колонку с "Наименование"
            for col in cols:
                if "Наименование" in str(col):
                    name_col = col
                    break
        
        if not name_col:
            print("    Ошибка: не найдена колонка Наименование расценки/ресурса")
            return result
        
        current_element_name = None
        current_works = []
        current_param_lines = []
        last_code = None
        
        ifc_pattern = re.compile(r'Ifc(Wall|Door|Window|Slab|Beam|Column|Stair|Roof|Covering|Footing|Pile|Railing|CurtainWall|Plate|Member|BuildingElementProxy)')
        
        for idx, row in df.iterrows():
            first_col_val = str(row.get(cols[0], "")).strip() if cols else ""
            
            # Если в первой колонке есть IfcXXX - это заголовок новой группы
            if first_col_val and ifc_pattern.search(first_col_val):
                if current_element_name and current_works:
                    result[current_element_name] = current_works
                
                element_name = first_col_val
                for part in first_col_val.split():
                    if ifc_pattern.search(part):
                        element_name = element_name.replace(part, "").strip()
                        break
                
                guid = extract_guid_from_string(element_name)
                if guid:
                    element_name = element_name.replace(guid, "").strip()
                
                parts = element_name.split()
                if parts and ("этаж" in parts[-1].lower() or "подземный" in parts[-1].lower() or 
                            "цокольный" in parts[-1].lower() or "надземный" in parts[-1].lower()):
                    element_name = " ".join(parts[:-1])
                
                element_name = clean_element_name(element_name)
                
                current_element_name = element_name
                current_works = []
                current_param_lines = []
                last_code = None
                
                continue
            
            name_val = str(row.get(name_col, "")).strip() if name_col else ""
            
            if not name_val or name_val == "nan":
                continue
            
            param_val = str(row.get(param_col, "")).strip() if param_col else ""
            code_val = str(row.get(code_col, "")).strip() if code_col else ""
            unit_val = str(row.get(unit_col, "")).strip() if unit_col else ""
            volume_val = safe_float(row.get(volume_col)) if volume_col else 0.0
            
            if param_val and param_val != "nan" and param_val != "-":
                current_param_lines.append(param_val)
            
            if code_val == "-" or code_val == "nan" or not code_val:
                if current_element_name:
                    work = {
                        "code": "",
                        "name": name_val,
                        "unit": unit_val,
                        "volume": volume_val,
                        "parameterization": "\n".join(current_param_lines) if current_param_lines else "",
                        "position_type": "material",
                        "parent_code": last_code
                    }
                    current_works.append(work)
                    current_param_lines = []
                continue
            
            if current_element_name and code_val:
                work = {
                    "code": code_val,
                    "name": name_val,
                    "unit": unit_val,
                    "volume": volume_val,
                    "parameterization": "\n".join(current_param_lines) if current_param_lines else "",
                    "position_type": "work" if not code_val.startswith("1.") else "material"
                }
                current_works.append(work)
                last_code = code_val
                current_param_lines = []
        
        if current_element_name and current_works:
            result[current_element_name] = current_works
        
        return result
    
    def _get_element_row(self, index: int) -> Optional[pd.Series]:
        """Получение строки из filtered_elements.xlsx по индексу."""
        df = self.data["elements_df"]
        if df is None or index >= len(df):
            return None
        return df.iloc[index]
    
    def _collect_guids(self, indices: List[int]) -> List[str]:
        """Сбор всех GUIDs по индексам."""
        guids = []
        df = self.data["elements_df"]
        
        if df is None:
            return guids
        
        guid_col = None
        for col in df.columns:
            if "GlobalId" in str(col) or "globalid" in str(col).lower():
                guid_col = col
                break
        
        if not guid_col:
            return guids
        
        for idx in indices:
            if idx < len(df):
                row = df.iloc[idx]
                guid = row.get(guid_col, "")
                if guid and not pd.isna(guid) and str(guid).strip():
                    guid_str = str(guid).strip()
                    if guid_str not in guids:
                        guids.append(guid_str)
        
        return guids
    
    def _extract_properties(self, first_row: pd.Series) -> Dict:
        """Извлечение свойств из filtered_elements.xlsx."""
        properties = {}
        
        length = first_row.get("Длина, мм", "")
        if length and not pd.isna(length) and length != "-":
            properties["lengthMm"] = safe_float(length)
        
        width = first_row.get("Ширина, мм", "")
        if width and not pd.isna(width) and width != "-":
            properties["widthMm"] = safe_float(width)
        
        height = first_row.get("Высота, мм", "")
        if height and not pd.isna(height) and height != "-":
            properties["heightMm"] = safe_float(height)
        
        area = first_row.get("Площадь, м2", "")
        if area and not pd.isna(area) and area != "-":
            properties["areaM2"] = safe_float(area)
        
        volume = first_row.get("Объём, м3", "")
        if volume and not pd.isna(volume) and volume != "-":
            properties["volumeM3"] = safe_float(volume)
        
        material = first_row.get("Материал", "")
        if material and not pd.isna(material) and material != "-":
            properties["material"] = str(material)
        
        material_layer = first_row.get("Свойство::IfcMaterialLayer::Name", "")
        if material_layer and not pd.isna(material_layer) and material_layer != "-":
            properties["materialLayer"] = str(material_layer)
        
        return properties
    
    def build(self, source_file: str, discipline: str = "АР", output_filename: str = "final_result_AR.json") -> Dict:
        """Основной метод сборки."""
        
        if not self.load_all_files():
            return {}
        
        element_groups = []
        
        # Извлекаем все конечные (листовые) группы
        leaf_groups = extract_leaf_groups(self.data["groups"])
        
        print(f"\nВсего листовых групп: {len(leaf_groups)}")
        
        for group_idx, group in enumerate(leaf_groups, 1):
            indices = group.get("indices", [])
            if not indices:
                continue
            
            first_idx = indices[0]
            first_row = self._get_element_row(first_idx)
            if first_row is None:
                continue
            
            element_name = first_row.get("Имя", "")
            element_name_clean = clean_element_name(element_name)
            
            # Используем full_name из extract_leaf_groups для контекста
            full_name = group.get("full_name", element_name_clean)
            
            print(f"\n  Обработка группы {group_idx}: {element_name_clean}")
            print(f"    Полный путь: {full_name}")
            print(f"    Элементов: {len(indices)}")
            
            # Ищем работы в Excel по имени элемента
            works_from_excel = []
            if element_name_clean in self.data["excel_works"]:
                works_from_excel = self.data["excel_works"][element_name_clean]
                print(f"    Найдены работы: {len(works_from_excel)}")
            else:
                # Пробуем найти по полному имени
                if full_name in self.data["excel_works"]:
                    works_from_excel = self.data["excel_works"][full_name]
                    print(f"    Найдены работы по полному имени: {len(works_from_excel)}")
                else:
                    # Ищем частичное совпадение
                    for excel_name, works in self.data["excel_works"].items():
                        if element_name_clean in excel_name or excel_name in element_name_clean:
                            works_from_excel = works
                            print(f"    Найдены работы по частичному совпадению: {excel_name}")
                            break
                    
                    if not works_from_excel:
                        print(f"    РАБОТЫ НЕ НАЙДЕНЫ для: {element_name_clean}")
                        print(f"    Доступные ключи Excel: {list(self.data['excel_works'].keys())[:5]}")
            
            # Формируем positions
            positions = []
            for work in works_from_excel:
                if not work.get("code") or work["code"] == "":
                    continue
                
                position = {
                    "positionType": work.get("position_type", "work"),
                    "code": work.get("code", ""),
                    "name": work.get("name", ""),
                    "unit": work.get("unit", "") if work.get("unit") else None,
                    "quantity": work.get("volume", None) if work.get("volume", 0) > 0 else None,
                    "quantityStatus": "calculated" if work.get("volume", 0) > 0 else "missing_data",
                    "selectionParameters": {}
                }
                
                if work.get("parameterization"):
                    position["selectionParameters"]["parameterization"] = work["parameterization"]
                
                positions.append(position)
            
            # Собираем GUIDs
            guids = self._collect_guids(indices)
            
            # Собираем свойства
            properties = self._extract_properties(first_row)
            
            # Определяем category из иерархии группы
            # full_name выглядит как: "Перегородка > Многослойные > Базовая стена:KRST_Вн_Зашивка_ГКЛВО25+УТ50 (75)"
            # Category = "Перегородка" (первый элемент пути)
            # Или используем Тип (RU) из first_row
            category = first_row.get("Тип (RU)", "")
            
            # Если есть full_name, берем первый элемент как category
            if full_name:
                path_parts = full_name.split(" > ")
                if path_parts:
                    # Первый элемент пути - это category (например, "Перегородка")
                    category = path_parts[0]
            
            # typeName - это конкретное имя элемента (последний элемент пути)
            type_name = element_name_clean
            
            # Формируем element data
            element_data = {
                "ifcClass": first_row.get("Тип элемента", ""),  # IfcWall
                "category": category,  # Перегородка
                "typeName": type_name,  # Базовая стена:KRST_Вн_Зашивка_ГКЛВО25+УТ50 (75)
                "level": first_row.get("Этаж", None),
                "levelType": first_row.get("Тип_этажа", None),
                "count": len(indices),
                "guids": guids
            }
            
            # Добавляем информацию о родительской группе (материал)
            if full_name:
                path_parts = full_name.split(" > ")
                if len(path_parts) > 2:
                    # Второй элемент - это материал/подкатегория
                    element_data["materialGroup"] = path_parts[1]  # Например, "Многослойные" или "Камень бетонный"
            
            element_groups.append({
                "groupId": f"group-{group_idx:04d}",
                "element": element_data,
                "properties": properties,
                "positions": positions
            })
            
            print(f"    Category: {category}")
            print(f"    TypeName: {type_name}")
            print(f"    GUIDs: {len(guids)}, Positions: {len(positions)}")
        
        result = {
            "schemaVersion": "1.0",
            "sourceFile": source_file,
            "discipline": discipline,
            "elementGroups": element_groups
        }
        
        output_path = self.folder / output_filename
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        
        print(f"\nРезультат сохранён: {output_path}")
        print(f"Всего групп: {len(element_groups)}")
        
        return result


# ============================================================
# КЛАСС ДЛЯ КР (КОНСТРУКТИВНЫЕ РЕШЕНИЯ)
# ============================================================

class KRBuilder:
    """Сборщик JSON для КР."""
    
    def __init__(self, folder_path: str):
        self.folder = Path(folder_path)
        self.data = {
            "selected_groups": [],
            "filtered_groups_mssk": [],
            "elements_df": None,
            "api_works": None,
        }
        
    def load_all_files(self) -> bool:
        """Загрузка всех необходимых файлов."""
        print(f"\nЗагрузка файлов из: {self.folder}")
        
        # 1. selected_elements_grouped.json
        selected_file = find_file_by_pattern(self.folder, ["selected_elements_grouped*.json"])
        if not selected_file:
            print("  ОШИБКА: не найден selected_elements_grouped.json")
            return False
        
        selected_data = load_json_file(selected_file)
        if not selected_data:
            return False
        
        self.data["selected_groups"] = selected_data if isinstance(selected_data, list) else []
        print(f"  Загружено групп из selected: {len(self.data['selected_groups'])}")
        
        # 2. filtered_elements_grouped_mssk.json
        mssk_file = find_file_by_pattern(self.folder, ["filtered_elements_grouped_mssk*.json"])
        if mssk_file:
            mssk_data = load_json_file(mssk_file)
            if mssk_data:
                self.data["filtered_groups_mssk"] = extract_leaf_groups(mssk_data)
                print(f"  Загружено групп из filtered_mssk: {len(self.data['filtered_groups_mssk'])}")
        
        # 3. filtered_elements.xlsx
        excel_file = find_file_by_pattern(self.folder, ["filtered_elements*.xlsx"])
        if excel_file:
            self.data["elements_df"] = load_excel_file(excel_file)
            if self.data["elements_df"] is not None:
                print(f"  Загружено элементов из Excel: {len(self.data['elements_df'])}")
        
        # 4. api_works_response.json
        api_file = find_file_by_pattern(self.folder, ["api_works_response*.json", "api_response*.json"])
        if api_file:
            self.data["api_works"] = load_json_file(api_file)
            if self.data["api_works"] is not None:
                print(f"  Загружен api_works_response.json")
        
        return True
    
    def _extract_element_data(self, group: Dict) -> Dict:
        """Извлечение данных элемента из группы."""
        first_element = {}
        
        # 1. Извлекаем имя элемента из additionalCharacteristics
        for char in group.get("additionalCharacteristics", []):
            name = char.get("name", "")
            values = char.get("values", [])
            if values:
                value = values[0].get("strValue", "")
                if name == "Имя элемента":
                    first_element["Имя"] = value
                elif name == "Толщина":
                    first_element["Ширина, мм"] = value
                elif name == "Этаж":
                    first_element["Этаж"] = value
                elif name == "Тип этажа":
                    first_element["Тип_этажа"] = value
        
        # 2. Извлекаем данные из characteristics
        for char in group.get("characteristics", []):
            name = char.get("name", "")
            values = char.get("values", [])
            if values:
                value = values[0].get("strValue", "")
                if name == "Материал":
                    first_element["Материал"] = value
                elif name == "Толщина":
                    first_element["Ширина, мм"] = value
                elif name == "Расположение":
                    first_element["Расположение"] = value
        
        # 3. Ищем соответствие в filtered_groups_mssk (СТАРАЯ ЛОГИКА ДЛЯ GUIDs)
        element_name = first_element.get("Имя", "")
        element_name_clean = clean_element_name(element_name)
        matched_fg = None
        
        if self.data["filtered_groups_mssk"]:
            # Точное совпадение
            for fg in self.data["filtered_groups_mssk"]:
                fe = fg.get("first_element", {})
                if fe.get("Имя") == element_name:
                    matched_fg = fg
                    break
            
            # Совпадение по имени без ID
            if not matched_fg and element_name_clean:
                for fg in self.data["filtered_groups_mssk"]:
                    fe = fg.get("first_element", {})
                    fg_name_clean = clean_element_name(fe.get("Имя", ""))
                    if fg_name_clean == element_name_clean:
                        matched_fg = fg
                        break
            
            # Частичное совпадение
            if not matched_fg and element_name:
                for fg in self.data["filtered_groups_mssk"]:
                    fe = fg.get("first_element", {})
                    fg_name = fe.get("Имя", "")
                    if fg_name and element_name:
                        if element_name in fg_name or fg_name in element_name:
                            matched_fg = fg
                            break
        
        # 4. Если нашли соответствие в filtered_groups_mssk
        if matched_fg:
            fe = matched_fg.get("first_element", {})
            # Обновляем first_element данными из matched_fg
            for key, value in fe.items():
                if value and value != "-" and not pd.isna(value):
                    first_element[key] = value
            
            # СТАРАЯ ЛОГИКА СБОРА GUIDs
            indices = matched_fg.get("indices", [])
            if indices and self.data["elements_df"] is not None:
                all_guids = collect_guids_from_indices(indices, self.data["elements_df"])
                if all_guids:
                    first_element["AllGuids"] = all_guids
        
        # 5. ДОПОЛНИТЕЛЬНО: ищем данные в elements_df (для геометрии)
        # GUIDs НЕ трогаем - только дополняем first_element данными
        if self.data["elements_df"] is not None:
            df = self.data["elements_df"]
            
            # Ищем колонку "Имя" в Excel
            name_col = None
            for col in df.columns:
                if col == "Имя":
                    name_col = col
                    break
            
            if name_col and name_col in df.columns:
                # Пробуем найти по точному совпадению
                mask_exact = df[name_col].astype(str) == element_name
                
                # Пробуем найти по частичному совпадению
                mask_partial = df[name_col].astype(str).str.contains(
                    element_name_clean, na=False, case=False, regex=False
                )
                
                mask = mask_exact if mask_exact.any() else mask_partial
                
                if mask.any():
                    row = df[mask].iloc[0]
                    # Дополняем first_element данными из Excel
                    # НЕ трогаем AllGuids - они уже собраны правильно
                    for key, value in row.items():
                        if key == "AllGuids":
                            continue  # Пропускаем, GUIDs уже собраны
                        if value is not None and not pd.isna(value) and str(value) != "-":
                            first_element[key] = value
        
        # 6. GUIDs (СТАРАЯ ЛОГИКА)
        guids = first_element.get("AllGuids", [])
        if not guids and first_element.get("GlobalId"):
            guids.append(str(first_element["GlobalId"]))
        
        # 7. Определяем category и typeName
        category = group.get("buildingElementName", "")
        if not category:
            category = first_element.get("Тип (RU)", "")
        
        type_name = first_element.get("Имя", "")
        type_name_clean = clean_element_name(type_name)
        
        return {
            "ifc_class": first_element.get("Тип элемента", ""),
            "category": category,
            "type_name": type_name_clean,
            "level": first_element.get("Этаж", ""),
            "level_type": first_element.get("Тип_этажа", ""),
            "guids": guids,
            "first_element": first_element  # Теперь содержит ВСЕ данные
        }
    
    def _get_total_volume(self, group: Dict) -> float:
        """Получение общего объёма группы (суммарный)."""
        # 1. Сначала пробуем totalMeasure
        total_measure = group.get("totalMeasure", {})
        if total_measure:
            measure_type = total_measure.get("type", "")
            value = safe_float(total_measure.get("value", 0))
            
            # Если тип volume - это суммарный объём группы
            if measure_type == "volume" and value > 0:
                return value
        
        # 2. Затем total_volume (агрегированное поле из группировки)
        if "total_volume" in group and group["total_volume"]:
            value = safe_float(group["total_volume"])
            if value > 0:
                return value
        
        # 3. Если totalMeasure.type == "count", значит объёма в группе нет
        # В этом случае суммируем объёмы всех элементов из Excel
        if total_measure.get("type") == "count":
            count = safe_float(total_measure.get("value", 0))
            if count > 0 and self.data["elements_df"] is not None:
                # Суммируем объёмы по всем элементам группы
                # Но у нас нет индексов для этой группы в selected_elements_grouped.json
                # Поэтому используем first_element как приближение
                first_element = group.get("first_element", {})
                volume = first_element.get("Объём, м3", "")
                if volume and volume != "-" and not pd.isna(volume):
                    volume_val = safe_float(volume)
                    if volume_val > 0:
                        # Это объём ОДНОГО элемента, умножаем на count
                        return volume_val * count
        
        return 0.0
    
    def _get_total_area(self, group: Dict) -> float:
        """Получение общей площади группы (суммарная)."""
        # 1. Сначала пробуем totalAreas - это суммарные площади группы
        total_areas = group.get("totalAreas", {})
        if total_areas:
            # Приоритет: "Площадь, м2" (суммарная площадь группы)
            if "Площадь, м2" in total_areas:
                value = safe_float(total_areas["Площадь, м2"])
                if value > 0:
                    return value
            
            # Затем другие ключи площади
            for key in ["Площадь_GrossSideArea_м2", 
                       "QTO_Qto_WallBaseQuantities_Площадь_GrossSideArea_м2",
                       "QTO_Qto_WallBaseQuantities_Площадь_GROSS_м2"]:
                if key in total_areas:
                    value = safe_float(total_areas[key])
                    if value > 0:
                        return value
            
            # Затем первое доступное значение
            for key, val in total_areas.items():
                value = safe_float(val)
                if value > 0:
                    return value
        
        # 2. Если totalAreas пуст, но есть totalMeasure с area
        total_measure = group.get("totalMeasure", {})
        if total_measure.get("type") == "area":
            value = safe_float(total_measure.get("value", 0))
            if value > 0:
                return value
        
        return 0.0
    
    def _get_reinforcement(self, group: Dict) -> float:
        """Получение армирования из группы."""
        if "total_reinforcement" in group and group["total_reinforcement"]:
            return safe_float(group["total_reinforcement"])
        
        if "_reinforcementVolumeRatio" in group:
            return safe_float(group["_reinforcementVolumeRatio"])
        
        first_element = group.get("first_element", {})
        reinf = first_element.get("ReinforcementVolumeRatio", "")
        if reinf and reinf != "-":
            return safe_float(reinf)
        
        return 0.0
    
    def _build_properties(self, group: Dict) -> Dict:
        """Сбор свойств из группы."""
        properties = {}
        first_element = group.get("first_element", {})
        
        # Определяем тип элемента
        ifc_class = first_element.get("Тип элемента", "")
        element_type = first_element.get("Тип (RU)", "")
        
        # ===== ОБЩИЕ СВОЙСТВА ДЛЯ ВСЕХ =====
        
        # Материал
        material = first_element.get("Материал", "")
        if material and material != "-" and not pd.isna(material):
            properties["material"] = str(material)
        
        # Слой материала
        material_layer = first_element.get("Свойство::IfcMaterialLayer::Name", "")
        if material_layer and material_layer != "-" and not pd.isna(material_layer):
            properties["materialLayer"] = str(material_layer)
        
        # Класс бетона
        concrete_class = first_element.get("ExpCheck_MaterialConcrete_MGE_ConcreteGrade", "")
        if concrete_class and concrete_class != "-" and not pd.isna(concrete_class):
            properties["concreteClass"] = str(concrete_class)
        
        # Водонепроницаемость
        water_resist = first_element.get("ExpCheck_MaterialConcrete_MGE_WaterResist", "")
        if water_resist and water_resist != "-" and not pd.isna(water_resist):
            properties["waterResistance"] = str(water_resist)
        
        # Морозостойкость
        freeze_durability = first_element.get("ExpCheck_MaterialConcrete_MGE_FreezeDurability", "")
        if freeze_durability and freeze_durability != "-" and not pd.isna(freeze_durability):
            properties["freezeDurability"] = str(freeze_durability)
        
        # Армирование
        reinforcement = self._get_reinforcement(group)
        if reinforcement and reinforcement != "-":
            properties["reinforcementVolumeRatio"] = safe_float(reinforcement)
        
        # Расположение
        location = None
        for char in group.get("characteristics", []):
            if char.get("name") == "Расположение":
                values = char.get("values", [])
                if values:
                    location = values[0].get("strValue")
                break
        
        if location:
            properties["location"] = location
        
        # ===== ГЕОМЕТРИЧЕСКИЕ ПАРАМЕТРЫ (БЕЗ ПЛОЩАДЕЙ, ОБЪЁМОВ, DEPTH, HEIGHT) =====
        
        # Для стен (IfcWall)
        if ifc_class == "IfcWall" or "Стена" in element_type:
            # Толщина стены (ширина)
            thickness = first_element.get("Ширина, мм", "")
            if thickness and thickness != "-" and not pd.isna(thickness):
                properties["thicknessMm"] = safe_float(thickness)
        
        # Для колонн (IfcColumn)
        elif ifc_class == "IfcColumn" or "Колонн" in element_type:
            # Наименьшая сторона
            width = first_element.get("Ширина, мм", "")
            if width and width != "-" and not pd.isna(width):
                width_val = safe_float(width)
                properties["minSideMm"] = width_val
            
            # Периметр
            perimeter = first_element.get("Периметр, мм", "")
            if perimeter and perimeter != "-" and not pd.isna(perimeter):
                properties["perimeterMm"] = safe_float(perimeter)
            elif "minSideMm" in properties:
                # Если периметр не указан, предполагаем квадрат
                properties["perimeterMm"] = 4 * properties["minSideMm"]
        
        # Для балок (IfcBeam)
        elif ifc_class == "IfcBeam" or "Балк" in element_type:
            # Ширина сечения
            width = first_element.get("Ширина, мм", "")
            if width and width != "-" and not pd.isna(width):
                properties["widthMm"] = safe_float(width)
            
            # Периметр сечения
            perimeter = first_element.get("Периметр, мм", "")
            if perimeter and perimeter != "-" and not pd.isna(perimeter):
                properties["perimeterMm"] = safe_float(perimeter)
        
        # Для плит (IfcSlab)
        elif ifc_class == "IfcSlab" or "Плит" in element_type or "Перекрыт" in element_type:
            # Толщина плиты
            thickness = first_element.get("Ширина, мм", "")
            if thickness and thickness != "-" and not pd.isna(thickness):
                properties["thicknessMm"] = safe_float(thickness)
        
        # Для фундаментов (IfcFooting)
        elif ifc_class == "IfcFooting" or "Фундамент" in element_type:
            # Ширина
            width = first_element.get("Ширина, мм", "")
            if width and width != "-" and not pd.isna(width):
                properties["widthMm"] = safe_float(width)
        
        # Для остальных - общие параметры
        else:
            # Ширина
            width = first_element.get("Ширина, мм", "")
            if width and width != "-" and not pd.isna(width):
                properties["widthMm"] = safe_float(width)
        
        return properties
    
    def _build_positions_from_api(self, group: Dict) -> List[Dict]:
        """Формирование позиций из works в ответе API.

        Работы извлекаются из актуального формата ответа API ТСН:
        позиции (data[]) → workGroups[] → works[] (поддерживается и
        устаревший формат data[].works).

        Отбор позиций повторяет логику api_works_lookup:
        остаются позиции, у которых нет характеристик, отсутствующих
        в запросе группы (например, «Фундаментная плита под
        оборудование …» отсекается для обычной плиты). Если точных
        позиций нет — берутся все (fallback).

        Работы «Уход за бетоном …» добавляются всегда, если они есть
        в ответе API, — даже если они пришли в позициях, отсечённых
        отбором: уход за бетоном обязателен для любой монолитной
        конструкции и не зависит от лишних характеристик позиции,
        в которой пришёл. Дедупликация — по шифру расценки.
        """
        positions = []
        
        if not self.data["api_works"]:
            return positions
        
        # Получаем имя элемента из группы
        group_element_name = ""
        for char in group.get("additionalCharacteristics", []):
            if char.get("name") == "Имя элемента":
                values = char.get("values", [])
                if values:
                    group_element_name = values[0].get("strValue", "")
                break
        
        group_element_name_clean = clean_element_name(group_element_name)
        
        # Получаем значения из группы
        total_volume = self._get_total_volume(group)
        total_area = self._get_total_area(group)
        reinforcement = self._get_reinforcement(group)
        
        # Ищем соответствующий result в api_works
        results = self.data["api_works"].get("results", [])
        
        # Для отслеживания уже добавленных работ
        seen_codes = set()

        def _work_to_position(work: Dict) -> Dict:
            """Преобразование расценки API в позицию выходного JSON."""
            work_code = work.get("code", "")

            work_position = {
                "positionType": "work",
                "code": work_code,
                "name": work.get("name", ""),
                "unit": work.get("unitOfMeasure", ""),
                "quantity": None,
                "quantityStatus": "missing_data",
                "selectionParameters": {},
                "quantityCalculation": None
            }

            # Заполняем selectionParameters из characteristics
            for char in work.get("characteristics", []):
                work_position["selectionParameters"][char.get("name", "")] = char.get("value", "")

            # Рассчитываем quantity
            work_unit = work.get("unitOfMeasure", "").lower()

            if "м3" in work_unit or "m3" in work_unit or "m[3" in work_unit:
                if total_volume > 0:
                    work_position["quantity"] = round(total_volume, 2)
                    work_position["quantityStatus"] = "calculated"
                    work_position["quantityCalculation"] = {
                        "sourceProperty": "NetVolume",
                        "sourceValue": total_volume,
                        "sourceUnit": "м³",
                        "conversionFactor": 1.0,
                        "formula": f"{total_volume}"
                    }
            elif "м2" in work_unit or "m2" in work_unit:
                if total_area > 0:
                    work_position["quantity"] = round(total_area, 2)
                    work_position["quantityStatus"] = "calculated"
                    work_position["quantityCalculation"] = {
                        "sourceProperty": "GrossSideArea",
                        "sourceValue": total_area,
                        "sourceUnit": "м²",
                        "conversionFactor": 1.0,
                        "formula": f"{total_area}"
                    }
            elif "т" in work_unit or "t" in work_unit:
                if reinforcement > 0:
                    quantity_tons = reinforcement / 1000.0
                    work_position["quantity"] = round(quantity_tons, 3)
                    work_position["quantityStatus"] = "calculated"
                    work_position["quantityCalculation"] = {
                        "sourceProperty": "ReinforcementVolumeRatio",
                        "sourceValue": reinforcement,
                        "sourceUnit": "кг",
                        "conversionFactor": 0.001,
                        "formula": f"{reinforcement} / 1000"
                    }

            return work_position

        for result_item in results:
            # Получаем имя элемента из result
            result_element = result_item.get("element", {})
            result_element_name = ""
            for char in result_element.get("additionalCharacteristics", []):
                if char.get("name") == "Имя элемента":
                    values = char.get("values", [])
                    if values:
                        result_element_name = values[0].get("strValue", "")
                    break
            
            result_element_name_clean = clean_element_name(result_element_name)
            
            # СОПОСТАВЛЯЕМ по имени элемента
            if group_element_name_clean and result_element_name_clean:
                if group_element_name_clean != result_element_name_clean:
                    continue
            
            response_data = result_item.get("response", {}).get("data", [])
            if not isinstance(response_data, list):
                continue

            # Характеристики запроса группы — для отбора позиций
            requested_chars = {
                str(char.get("name", "")).strip().lower()
                for char in group.get("characteristics", []) or []
                if isinstance(char, dict)
            }

            def _select_positions(items: List[Dict]) -> List[Dict]:
                """Отбор позиций без лишних характеристик (⊆ запроса)."""
                if len(items) <= 1 or not requested_chars:
                    return items
                exact = []
                for pos in items:
                    pos_chars = [
                        str(char.get("name", "")).strip().lower()
                        for char in pos.get("characteristics", []) or []
                        if isinstance(char, dict)
                    ]
                    if all(name in requested_chars for name in pos_chars):
                        exact.append(pos)
                return exact if exact else items

            def _iter_position_works(item: Dict) -> List[Dict]:
                """Работы позиции: data[].works или data[].workGroups[].works."""
                works = []
                nested = item.get("works")
                if isinstance(nested, list):
                    works.extend(w for w in nested if isinstance(w, dict))
                for wg in item.get("workGroups") or []:
                    if isinstance(wg, dict):
                        wg_works = wg.get("works")
                        if isinstance(wg_works, list):
                            works.extend(w for w in wg_works if isinstance(w, dict))
                return works

            # Все работы ответа — включая позиции, отсечённые отбором
            # (из них гарантированно добавляется «Уход за бетоном»)
            all_works: List[Dict] = []
            for data_item in response_data:
                if isinstance(data_item, dict):
                    all_works.extend(_iter_position_works(data_item))

            selected_positions = _select_positions(
                [d for d in response_data if isinstance(d, dict)]
            )

            # 1. Работы отобранных позиций
            for data_item in selected_positions:
                for work in _iter_position_works(data_item):
                    work_code = work.get("code", "")

                    # Пропускаем дубликаты
                    if work_code in seen_codes:
                        continue
                    seen_codes.add(work_code)

                    positions.append(_work_to_position(work))

            # 2. Работы «Уход за бетоном» добавляются всегда, если они
            # есть в ответе API, — даже если они пришли в позициях,
            # отсечённых отбором (см. докстринг).
            for work in all_works:
                if not _is_curing_work(work):
                    continue
                work_code = work.get("code", "")
                if work_code in seen_codes:
                    continue
                seen_codes.add(work_code)
                positions.append(_work_to_position(work))
        
        return positions
    
    def build(self, source_file: str, discipline: str = "КР", 
              output_filename: str = "final_result_KR.json") -> Dict:
        """Основной метод сборки."""
        print("\n" + "="*60)
        print("КР-СБОРЩИК")
        print("="*60)
        
        if not self.load_all_files():
            print("ОШИБКА: не удалось загрузить файлы")
            return {}
        
        element_groups = []
        for idx, group in enumerate(self.data["selected_groups"], 1):
            element_data = self._extract_element_data(group)
            
            # ВАЖНО: добавляем first_element обратно в group
            group["first_element"] = element_data["first_element"]
            
            # Теперь _build_properties получит правильный first_element
            properties = self._build_properties(group)
            positions = self._build_positions_from_api(group)
            
            element_groups.append({
                "groupId": f"group-{idx:04d}",
                "element": {
                    "ifcClass": element_data["ifc_class"] if element_data["ifc_class"] else None,
                    "category": element_data["category"],
                    "typeName": element_data["type_name"],
                    "level": element_data["level"] if element_data["level"] else None,
                    "levelType": element_data["level_type"] if element_data["level_type"] else None,
                    "count": group.get("count", group.get("elementCount", 0)),
                    "guids": element_data["guids"]
                },
                "properties": properties,
                "positions": positions
            })
            print(f"  Группа {idx}: {len(element_data['guids'])} элементов, "
                  f"{len(positions)} позиций")
        
        result = {
            "schemaVersion": "1.0",
            "sourceFile": source_file,
            "discipline": discipline,
            "elementGroups": element_groups
        }
        
        output_path = self.folder / output_filename
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        
        print(f"\nРезультат сохранён: {output_path}")
        print(f"Всего групп: {len(element_groups)}")
        
        return result


# ============================================================
# УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ЗАПУСКА
# ============================================================

# ifc_json_builder.py - исправленная функция build_final_json

def build_final_json(
    input_folder: str,
    source_file: str,
    discipline: str,
    output_filename: Optional[str] = None
) -> Dict:
    """
    Универсальная функция сборки JSON.
    
    Args:
        input_folder: Путь к папке с файлами
        source_file: Имя исходного IFC-файла
        discipline: "АР"/"AR" или "КР"/"KR"
        output_filename: Имя выходного файла (опционально)
    
    Returns:
        Собранный JSON-объект
    """
    # Нормализуем дисциплину
    discipline_upper = discipline.upper()
    
    # Маппинг латинских обозначений на русские
    discipline_map = {
        "AR": "АР",
        "KR": "КР",
        "АР": "АР",
        "КР": "КР",
    }
    
    if discipline_upper not in discipline_map:
        raise ValueError(f"Неизвестная дисциплина: {discipline}. Допустимые: АР, КР, AR, KR")
    
    discipline_normalized = discipline_map[discipline_upper]
    
    if output_filename is None:
        output_filename = f"final_result_{discipline_upper}.json"
    
    if discipline_normalized == "АР":
        builder = ARBuilder(input_folder)
        return builder.build(source_file, discipline_normalized, output_filename)
    elif discipline_normalized == "КР":
        builder = KRBuilder(input_folder)
        return builder.build(source_file, discipline_normalized, output_filename)