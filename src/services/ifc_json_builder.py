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
        
        # 1. Извлекаем данные из additionalCharacteristics
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
        
        # 3. Ищем соответствие в filtered_groups_mssk для получения ВСЕХ данных
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
        
        # 4. Если нашли соответствие, берем ВСЕ данные из matched_fg
        if matched_fg:
            fe = matched_fg.get("first_element", {})
            # Обновляем first_element всеми данными из matched_fg
            for key, value in fe.items():
                if value and value != "-" and not pd.isna(value):
                    first_element[key] = value
            
            # Получаем индексы для сбора GUIDs
            indices = matched_fg.get("indices", [])
            if indices and self.data["elements_df"] is not None:
                all_guids = collect_guids_from_indices(indices, self.data["elements_df"])
                if all_guids:
                    first_element["AllGuids"] = all_guids
        
        # 5. Если matched_fg не найден, ищем в elements_df напрямую по имени
        if not matched_fg and self.data["elements_df"] is not None:
            df = self.data["elements_df"]
            if "Имя" in df.columns:
                # Ищем строку с таким же именем
                mask = df["Имя"].astype(str).str.contains(element_name_clean, na=False, case=False)
                if mask.any():
                    row = df[mask].iloc[0]
                    for key, value in row.items():
                        if value and not pd.isna(value) and value != "-":
                            first_element[key] = value
                    
                    # Собираем GUIDs
                    guid_col = None
                    for col in df.columns:
                        if "GlobalId" in str(col):
                            guid_col = col
                            break
                    
                    if guid_col:
                        guids = df[mask][guid_col].astype(str).tolist()
                        first_element["AllGuids"] = guids
        
        # 6. GUIDs
        guids = first_element.get("AllGuids", [])
        if not guids and first_element.get("GlobalId"):
            guids.append(str(first_element["GlobalId"]))
        
        # 7. Добавляем общие данные из группы
        first_element["_total_volume"] = self._get_total_volume(group)
        first_element["_total_area"] = self._get_total_area(group)
        
        # 8. Определяем category и typeName
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
        """Получение общего объёма из группы."""
        if "total_volume" in group and group["total_volume"]:
            return safe_float(group["total_volume"])
        
        total_measure = group.get("totalMeasure", {})
        if total_measure.get("value"):
            return safe_float(total_measure["value"])
        
        first_element = group.get("first_element", {})
        for key in ["Объём, м3", "QTO_Qto_WallBaseQuantities_Объём_NetVolume_м3", "QTO_bbox::Объём_м3"]:
            if key in first_element and first_element[key] != "-":
                return safe_float(first_element[key])
        
        return 0.0
    
    def _get_total_area(self, group: Dict) -> float:
        """Получение общей площади из группы."""
        total_areas = group.get("totalAreas", {})
        if total_areas:
            for key in ["Площадь, м2", "QTO_Qto_WallBaseQuantities_Площадь_GrossSideArea_м2",
                       "QTO_Qto_WallBaseQuantities_Площадь_GROSS_м2", "Площадь_GrossSideArea_м2"]:
                if key in total_areas and total_areas[key]:
                    return safe_float(total_areas[key])
        
        first_element = group.get("first_element", {})
        for key in ["Площадь, м2", "QTO_Qto_WallBaseQuantities_Площадь_GrossSideArea_м2"]:
            if key in first_element and first_element[key] != "-":
                return safe_float(first_element[key])
        
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
        
        # Получаем first_element из разных источников
        first_element = dict(group.get("first_element", {}))
        
        # Функция для получения значения из характеристик
        def get_char_value(char_list, name):
            """Ищет значение характеристики по имени."""
            if not char_list:
                return None
            for char in char_list:
                if char.get("name") == name:
                    values = char.get("values", [])
                    if values:
                        return values[0].get("strValue", "")
            return None
        
        # Дополняем first_element данными из additionalCharacteristics
        additional_chars = group.get("additionalCharacteristics", [])
        for char in additional_chars:
            name = char.get("name", "")
            values = char.get("values", [])
            if values and name:
                value = values[0].get("strValue", "")
                if value and value != "-":
                    first_element[name] = value
        
        # Дополняем first_element данными из characteristics
        characteristics = group.get("characteristics", [])
        for char in characteristics:
            name = char.get("name", "")
            values = char.get("values", [])
            if values and name:
                value = values[0].get("strValue", "")
                if value and value != "-":
                    first_element[name] = value
        
        # ТЕПЕРЬ first_element содержит все данные
        
        # Материал
        material = first_element.get("Материал", "")
        if material and material != "-":
            properties["material"] = material
        
        # Слой материала
        material_layer = first_element.get("Свойство::IfcMaterialLayer::Name", "")
        if material_layer and material_layer != "-":
            properties["materialLayer"] = material_layer
        
        # Класс бетона
        concrete_class = first_element.get("ExpCheck_MaterialConcrete_MGE_ConcreteGrade", "")
        if concrete_class and concrete_class != "-":
            properties["concreteClass"] = concrete_class
        
        # Водонепроницаемость
        water_resist = first_element.get("ExpCheck_MaterialConcrete_MGE_WaterResist", "")
        if water_resist and water_resist != "-":
            properties["waterResistance"] = water_resist
        
        # Морозостойкость
        freeze_durability = first_element.get("ExpCheck_MaterialConcrete_MGE_FreezeDurability", "")
        if freeze_durability and freeze_durability != "-":
            properties["freezeDurability"] = freeze_durability
        
        # ===== ГЕОМЕТРИЧЕСКИЕ ПАРАМЕТРЫ =====
        
        # Длина (мм)
        length = first_element.get("Длина, мм", first_element.get("QTO_bbox::Длина_мм", ""))
        if length and length != "-" and not pd.isna(length):
            properties["lengthMm"] = safe_float(length)
        
        # Ширина (мм)
        width = first_element.get("Ширина, мм", first_element.get("QTO_bbox::Ширина_мм", ""))
        if width and width != "-" and not pd.isna(width):
            properties["widthMm"] = safe_float(width)
        
        # Высота (мм)
        height = first_element.get("Высота, мм", first_element.get("QTO_bbox::Высота_мм", ""))
        if height and height != "-" and not pd.isna(height):
            properties["heightMm"] = safe_float(height)
        
        # Периметр (мм)
        perimeter = first_element.get("Периметр, мм", "")
        if perimeter and perimeter != "-" and not pd.isna(perimeter):
            properties["perimeterMm"] = safe_float(perimeter)
        
        # Площадь (м2)
        area = first_element.get("Площадь, м2", "")
        if area and area != "-" and not pd.isna(area):
            properties["areaM2"] = safe_float(area)
        
        # Объём (м3)
        volume = first_element.get("Объём, м3", first_element.get("QTO_bbox::Объём_м3", ""))
        if volume and volume != "-" and not pd.isna(volume):
            properties["volumeM3"] = safe_float(volume)
        
        # Армирование
        reinforcement = self._get_reinforcement(group)
        if reinforcement:
            properties["reinforcementVolumeRatio"] = safe_float(reinforcement)
        
        # Расположение
        location = get_char_value(characteristics, "Расположение")
        if location:
            properties["location"] = location
        
        # Уровень этажа (мм)
        level_mm = first_element.get("Уровень_этажа_мм", "")
        if level_mm and level_mm != "-" and not pd.isna(level_mm):
            properties["levelMm"] = safe_float(level_mm)
        
        return properties
    
    def _build_positions_from_api(self, group: Dict) -> List[Dict]:
        """Формирование позиций из ответа API."""
        positions = []
        
        if not self.data["api_works"]:
            return positions
        
        total_volume = self._get_total_volume(group)
        total_area = self._get_total_area(group)
        reinforcement = self._get_reinforcement(group)
        
        results = self.data["api_works"].get("results", [])
        for result_item in results:
            response_data = result_item.get("response", {}).get("data", [])
            
            for data_item in response_data:
                main_position = {
                    "positionType": "work",
                    "code": data_item.get("code", ""),
                    "name": data_item.get("fullName", data_item.get("name", "")),
                    "unit": data_item.get("unitOfMeasure", ""),
                    "quantity": total_volume if total_volume else None,
                    "quantityStatus": "calculated" if total_volume else "missing_data",
                    "selectionParameters": {},
                    "quantityCalculation": None
                }
                
                for char in data_item.get("characteristics", []):
                    main_position["selectionParameters"][char.get("name", "")] = char.get("value", "")
                
                if total_volume:
                    main_position["quantityCalculation"] = {
                        "sourceProperty": "NetVolume",
                        "sourceValue": total_volume,
                        "sourceUnit": "м³",
                        "conversionFactor": 1.0,
                        "formula": f"{total_volume}"
                    }
                
                positions.append(main_position)
                
                for work in data_item.get("works", []):
                    work_position = {
                        "positionType": "work",
                        "code": work.get("code", ""),
                        "name": work.get("name", ""),
                        "unit": work.get("unitOfMeasure", ""),
                        "quantity": None,
                        "quantityStatus": "missing_data",
                        "selectionParameters": {},
                        "quantityCalculation": None
                    }
                    
                    for char in work.get("characteristics", []):
                        work_position["selectionParameters"][char.get("name", "")] = char.get("value", "")
                    
                    unit = work.get("unitOfMeasure", "").lower()
                    
                    if "м2" in unit and total_area:
                        work_position["quantity"] = total_area
                        work_position["quantityStatus"] = "calculated"
                        work_position["quantityCalculation"] = {
                            "sourceProperty": "GrossSideArea",
                            "sourceValue": total_area,
                            "sourceUnit": "м²",
                            "conversionFactor": 1.0,
                            "formula": f"{total_area}"
                        }
                    elif "м3" in unit and total_volume:
                        work_position["quantity"] = total_volume
                        work_position["quantityStatus"] = "calculated"
                        work_position["quantityCalculation"] = {
                            "sourceProperty": "NetVolume",
                            "sourceValue": total_volume,
                            "sourceUnit": "м³",
                            "conversionFactor": 1.0,
                            "formula": f"{total_volume}"
                        }
                    elif "т" in unit and reinforcement:
                        work_position["quantity"] = reinforcement / 1000.0
                        work_position["quantityStatus"] = "calculated"
                        work_position["quantityCalculation"] = {
                            "sourceProperty": "ReinforcementVolumeRatio",
                            "sourceValue": reinforcement,
                            "sourceUnit": "кг/м³",
                            "conversionFactor": 0.001,
                            "formula": f"{reinforcement} / 1000"
                        }
                    
                    positions.append(work_position)
        
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