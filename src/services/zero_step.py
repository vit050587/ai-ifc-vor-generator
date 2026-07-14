# ВЫТАСКИВАЕМ ИЗ ФАЙЛА .ifc ВСЮ ИНФОРМАЦИЮ (исправленная версия с литрами)

import ifcopenshell
import pandas as pd
import os

from src.core.logger import setup_logger

logger = setup_logger("ifc_step")

 
element_types = [
    ('IfcWall', 'Стены'),
    ('IfcWallStandardCase', 'Стены'),
    ('IfcSlab', 'Перекрытия'),
    ('IfcColumn', 'Колонны'),
    ('IfcBeam', 'Балки'),
    ('IfcStair', 'Лестницы'),
    ('IfcStairFlight', 'Лестницы'),
    ('IfcRamp', 'Пандусы'),
    ('IfcBuildingElementProxy', 'Прочие_элементы'),
    ('ifcCovering', 'Покрытие'),
    ('ifcPile', 'Свая')
]


def safe_get_attr(obj, attr, default='-'):
    try:
        val = getattr(obj, attr, default)
        if val is None:
            return default
        if hasattr(val, 'wrappedValue'):
            return val.wrappedValue
        return val
    except Exception as e:
        logger.error(f"Ошибка при обработке: {e}")
        return default


def classify_storey_type(storey_name, elevation_mm):
    """Классифицирует тип этажа по имени и высоте"""
    storey_name_lower = str(storey_name).lower()
    
    
    if any(word in storey_name_lower for word in ['подвал', 'basement', '-1', 'подзем']):
        return 'Подземный'
    elif any(word in storey_name_lower for word in ['цоколь', 'ground', '0 этаж', 'нулевой']):
        return 'Цокольный'
    elif any(word in storey_name_lower for word in ['техническ', 'technical']):
        return 'Технический'
    elif any(word in storey_name_lower for word in ['мансард', 'attic']):
        return 'Мансардный'
    elif any(word in storey_name_lower for word in ['крыш', 'roof', 'кровл']):
        return 'Кровля'

    if elevation_mm != '-':
        try:
            elev_m = float(elevation_mm) / 1000
            if elev_m < 0:
                return 'Подземный'
            elif elev_m < 0.5:
                return 'Цокольный'
            else:
                return 'Надземный'
        except Exception as e:
            print(f'Ошибка: {e}')

    return 'Не определен'


def get_element_storey(element):
    """Извлекает информацию об этаже, на котором находится элемент"""
    storey_info = {
        'Этаж': '-',
        'Уровень_этажа_мм': '-',
        'Тип_этажа': '-'
    }
    
    try:
        if hasattr(element, 'ContainedInStructure'):
            for rel in element.ContainedInStructure:
                if rel.is_a('IfcRelContainedInSpatialStructure'):
                    container = rel.RelatingStructure
                    if container and container.is_a('IfcBuildingStorey'):
                        storey_info['Этаж'] = safe_get_attr(container, 'Name')
                        
                        if hasattr(container, 'Elevation'):
                            elevation = safe_get_attr(container, 'Elevation')
                            if elevation != '-':
                                
                                elev_val = float(elevation)
                                if abs(elev_val) > 100:  
                                    elev_val = elev_val / 1000
                                storey_info['Уровень_этажа_мм'] = round(elev_val * 1000, 2)
                        
                        storey_info['Тип_этажа'] = classify_storey_type(
                            storey_info['Этаж'], 
                            storey_info['Уровень_этажа_мм']
                        )
                        break
    except Exception as e:
        print(f'Error: {e}')
    
    return storey_info

def get_all_quantities(element):
     
    quantities = {}
    try:
        if hasattr(element, 'IsDefinedBy'):
            for rel in element.IsDefinedBy:
                if rel.is_a('IfcRelDefinesByProperties'):
                    props = rel.RelatingPropertyDefinition
                    if props and props.is_a('IfcElementQuantity'):
                        if hasattr(props, 'Quantities'):
                            for qty in props.Quantities:
                                qty_name = safe_get_attr(qty, 'Name')
                                qty_type = qty.is_a()
                                
                                if qty_type == 'IfcQuantityLength':
                                    value = safe_get_attr(qty, 'LengthValue')
                                    if value and value != '-':
                                        quantities[f'Длина_{qty_name}_мм'] = round(float(value), 2)
                                
                                elif qty_type == 'IfcQuantityArea':
                                    value = safe_get_attr(qty, 'AreaValue')
                                    if value and value != '-':
                                        quantities[f'Площадь_{qty_name}_м2'] = round(float(value), 3)
                                
                                elif qty_type == 'IfcQuantityVolume':
                                    value = safe_get_attr(qty, 'VolumeValue')
                                    if value and value != '-':
                                        if qty_name.lower() == 'netvolume':
                                            quantities[f'Объём_{qty_name}_м3'] = round(float(value), 3)
                                        else:
                                            quantities[f'Объём_{qty_name}_литры'] = round(float(value), 2)
                                
                                elif qty_type == 'IfcQuantityCount':
                                    value = safe_get_attr(qty, 'CountValue')
                                    if value and value != '-':
                                        quantities[f'Количество_{qty_name}'] = value
                                
                                elif qty_type == 'IfcQuantityWeight':
                                    value = safe_get_attr(qty, 'WeightValue')
                                    if value and value != '-':
                                        quantities[f'Вес_{qty_name}_кг'] = round(float(value), 2)
    except Exception as e:
        logger.error(f"Ошибка при получении параметров: {e}")
    return quantities

def get_geometry_from_representation(element):
     
    geometry = {}
    try:
        if hasattr(element, 'Representation') and element.Representation:
            if hasattr(element.Representation, 'Representations'):
                for rep in element.Representation.Representations:
                    if hasattr(rep, 'Items'):
                        for item in rep.Items:
                            if item.is_a('IfcExtrudedAreaSolid'):
                                if hasattr(item, 'Depth'):
                                    geometry['Глубина_выдавливания_мм'] = round(float(item.Depth), 2)
                                
                                if hasattr(item, 'SweptArea') and item.SweptArea:
                                    swept = item.SweptArea
                                    if swept.is_a('IfcRectangleProfileDef'):
                                        if hasattr(swept, 'XDim'):
                                            geometry['Ширина_сечения_мм'] = round(float(swept.XDim), 2)
                                        if hasattr(swept, 'YDim'):
                                            geometry['Высота_сечения_мм'] = round(float(swept.YDim), 2)
    except Exception as e:
        logger.error(f"Ошибка при анализе геометрии: {e}")
    return geometry

def get_placement_info(element):
 
    placement = {}
    try:
        if hasattr(element, 'ObjectPlacement'):
            placement_obj = element.ObjectPlacement
            if placement_obj and placement_obj.is_a('IfcLocalPlacement'):
                if hasattr(placement_obj, 'RelativePlacement'):
                    rel_place = placement_obj.RelativePlacement
                    if rel_place and hasattr(rel_place, 'Location'):
                        loc = rel_place.Location
                        if hasattr(loc, 'Coordinates'):
                            coords = loc.Coordinates
                            if len(coords) >= 3:
                                placement['Координата_X_мм'] = round(float(coords[0]), 2)
                                placement['Координата_Y_мм'] = round(float(coords[1]), 2)
                                placement['Координата_Z_мм'] = round(float(coords[2]), 2)
    except:
        pass
    return placement

def get_all_properties(element):
    
    properties = {}
    try:
        if hasattr(element, 'IsDefinedBy'):
            for rel in element.IsDefinedBy:
                if rel.is_a('IfcRelDefinesByProperties'):
                    props = rel.RelatingPropertyDefinition
                    if props:
                        pset_name = safe_get_attr(props, 'Name')
                        if props.is_a('IfcPropertySet'):
                            if hasattr(props, 'HasProperties'):
                                for prop in props.HasProperties:
                                    prop_name = safe_get_attr(prop, 'Name')
                                    val = prop.NominalValue
                                    if val:
                                        if hasattr(val, 'wrappedValue'):
                                            value = val.wrappedValue
                                        else:
                                            value = str(val)
                                        key = f"Свойство_{pset_name}_{prop_name}" if pset_name != '-' else f"Свойство_{prop_name}"
                                        properties[key] = value
    except:
        pass
    return properties

def get_element_info(element):
     
    info = {
        'GlobalId': safe_get_attr(element, 'GlobalId'),
        'Имя': safe_get_attr(element, 'Name'),
        'Тип элемента': element.is_a(),
        'Тег': safe_get_attr(element, 'Tag'),
    }
    
    # Информация об этаже
    info.update(get_element_storey(element))
    
    # Материал
    material_found = False
    try:
        if hasattr(element, 'HasAssociations'):
            for rel in element.HasAssociations:
                if rel.is_a('IfcRelAssociatesMaterial'):
                    material = rel.RelatingMaterial
                    if material.is_a('IfcMaterial'):
                        info['Материал'] = safe_get_attr(material, 'Name')
                        material_found = True
                    elif material.is_a('IfcMaterialLayerSetUsage'):
                        if material.ForLayerSet and material.ForLayerSet.MaterialLayers:
                            layers = []
                            for layer in material.ForLayerSet.MaterialLayers:
                                if layer.Material:
                                    layers.append(safe_get_attr(layer.Material, 'Name'))
                            info['Материал'] = ', '.join(layers) if layers else '-'
                            material_found = True
    except:
        pass
    
    if not material_found:
        info['Материал'] = '-'
    
     
    info.update(get_geometry_from_representation(element))
    
     
    info.update(get_placement_info(element))
    
     
    info.update(get_all_quantities(element))
    
     
    info.update(get_all_properties(element))
    
    return info

 

def zero_step(ifc_file, output_folder=None):
    
    logger.info(f"Начата обработка файла {ifc_file}")

    model = ifcopenshell.open(ifc_file)

    logger.info("Обрбаотка ifc с анализом этажей")

    storeys = {}
    for storey in model.by_type('IfcBuildingStorey'):
        name = safe_get_attr(storey, 'Name')
        elevation = safe_get_attr(storey, 'Elevation')
        if elevation != '-':
            elev_val = float(elevation)
            
            if abs(elev_val) > 100:  
                elev_val = elev_val / 1000
                print(f"   ⚠️ Обнаружены миллиметры! {float(elevation)} мм → {elev_val} м")
            elevation_mm = round(elev_val * 1000, 2)
            storey_type = classify_storey_type(name, elevation_mm)
            print(f"   • {name}: {elev_val} м ({elevation_mm} мм) - {storey_type}")
            storeys[name] = {'elevation': elev_val, 'type': storey_type}


    all_elevations = []
    ground_elevations = []   

    for storey in model.by_type('IfcBuildingStorey'):
        if hasattr(storey, 'Elevation') and storey.Elevation is not None:
            elev = float(storey.Elevation)
            
            
            if abs(elev) > 100:   
                elev = elev / 1000
            
            all_elevations.append(elev)
            
            
            name = safe_get_attr(storey, 'Name')
            elev_mm = round(elev * 1000, 2)
            storey_type = classify_storey_type(name, elev_mm)
            
            
            if storey_type in ['Цокольный', 'Надземный', 'Технический', 'Мансардный']:
                ground_elevations.append(elev)

    
    if ground_elevations:
        min_ground = min(ground_elevations)   
        max_ground = max(ground_elevations)   
        height_above_ground = max_ground - min_ground
        
        
        if all_elevations:
            total_height = max(all_elevations) - min(all_elevations)
            
            building_height_info = {
                'Высота_надземной_части_м': round(height_above_ground, 3),
                'Общая_высота_здания_м': round(total_height, 3),
                'Минимальная_отметка_надземной_части_м': round(min_ground, 3),
                'Максимальная_отметка_надземной_части_м': round(max_ground, 3)
            }
        else:
            height_above_ground = 0
            building_height_info = {
                'Высота_надземной_части_м': 0,
                'Общая_высота_здания_м': 0,
                'Минимальная_отметка_надземной_части_м': 0,
                'Максимальная_отметка_надземной_части_м': 0
            }
    else:
        
        height_above_ground = 0
        building_height_info = {
            'Высота_надземной_части_м': 0,
            'Общая_высота_здания_м': 0,
            'Минимальная_отметка_надземной_части_м': 0,
            'Максимальная_отметка_надземной_части_м': 0
        }

    elements = []

    for ifc_type, ru_name in element_types:
        elems = model.by_type(ifc_type)
        if len(elems) > 0:
            print(f"   {ifc_type} ({ru_name}): {len(elems)} шт")
            for elem in elems:
                elem_info = get_element_info(elem)
                elem_info['Тип (RU)'] = ru_name
                elements.append(elem_info)

    
    df = pd.DataFrame(elements)
    df = df.fillna('-')

    base_cols = ['Тип (RU)', 'Тип элемента', 'Имя', 'GlobalId', 'Материал']
    storey_cols = ['Этаж', 'Тип_этажа', 'Уровень_этажа_мм']
    other_cols = [col for col in df.columns if col not in base_cols + storey_cols]
    df = df[base_cols + storey_cols + other_cols]

    if output_folder:
        output_filename = os.path.join(output_folder, 'IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx')
    else:
        output_filename = 'IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx'

    df.to_excel(output_filename, index=False)
    
    # ИЗ ВСЕХ ПОЛУЧЕННЫХ ДАННЫХ ДЕЛАЕМ ВЫЖИМКУ ДЛЯ СМЕТЧИКА (исправленная версия с литрами)
    
    smetchik_cols = ['Тип (RU)', 'Тип элемента', 'Имя', 'GlobalId', 'Материал', 'Этаж', 'Тип_этажа', 'Уровень_этажа_мм']

    
    for col in df.columns:
        if 'Длина' in col and '_мм' in col:
            smetchik_cols.append(col)
        elif 'Ширина' in col and '_мм' in col:
            smetchik_cols.append(col)
        elif 'Высота' in col and '_мм' in col:
            smetchik_cols.append(col)
        elif 'Глубина' in col and '_мм' in col:
            smetchik_cols.append(col)

    
    for col in df.columns:
        if 'Объём' in col and ('_м3' in col or '_литры' in col):
            smetchik_cols.append(col)

    
    for col in df.columns:
        if 'Площадь' in col and '_м2' in col:
            smetchik_cols.append(col)

    
    existing_cols = [col for col in smetchik_cols if col in df.columns]

    df_smetchik = df[existing_cols].copy()
    df_smetchik = df_smetchik.fillna('-')

    
    df_smetchik.insert(0, '№ п/п', range(1, len(df_smetchik) + 1))
    df_smetchik['Примечание_сметчика'] = ''
    df_smetchik['Стоимость_за_ед_руб'] = ''
    df_smetchik['Общая_стоимость_руб'] = ''

    
    volume_col = None
    for col in df.columns:
        if 'Объём_NetVolume_м3' in col:
            volume_col = col
            break

    summary_data = []
    grouped = df.groupby(['Тип (RU)', 'Тип элемента', 'Материал'])

    for (type_ru, type_elem, material), group in grouped:
        count = len(group)
        
        total_volume = 0
        if volume_col and volume_col in df.columns:
            vol_series = pd.to_numeric(group[volume_col], errors='coerce').fillna(0)
            total_volume = vol_series.sum()
        
        summary_data.append({
            'Тип (RU)': type_ru,
            'Тип элемента': type_elem,
            'Материал': material if material != '-' else 'Не указан',
            'Количество, шт': count,
            'Объем, м³': round(total_volume, 3) if total_volume > 0 else '-',
        })

    df_summary = pd.DataFrame(summary_data)

    
    total_count = df_summary['Количество, шт'].sum()
    total_volume = 0
    for _, row in df_summary.iterrows():
        if row['Объем, м³'] != '-':
            total_volume += row['Объем, м³']

    total_row = pd.DataFrame([{
        'Тип (RU)': 'ВСЕГО',
        'Тип элемента': '',
        'Материал': '',
        'Количество, шт': total_count,
        'Объем, м³': round(total_volume, 3),
    }])
    df_summary = pd.concat([df_summary, total_row], ignore_index=True)

    if output_folder:
        output_file = os.path.join(output_folder, 'ДЛЯ_СМЕТЧИКА_исправленный.xlsx')
        height_file = os.path.join(output_folder, 'height.txt')
    else:
        output_file = 'ДЛЯ_СМЕТЧИКА_исправленный.xlsx'
        height_file = 'height.txt'

    logger.info("Обработка файла завершена")

    with open(height_file, 'w', encoding='utf-8') as file:
        file.write(str(building_height_info['Высота_надземной_части_м']))

    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
        df_smetchik.to_excel(writer, sheet_name='Данные', index=False)
        df_summary.to_excel(writer, sheet_name='Сводка_по_типам', index=False)
        
        
        df_height = pd.DataFrame([{
            'Параметр': 'Высота надземной части',
            'Значение_м': building_height_info['Высота_надземной_части_м'],
            'Значение_мм': building_height_info['Высота_надземной_части_м'] * 1000
        }, {
            'Параметр': 'Общая высота здания',
            'Значение_м': building_height_info['Общая_высота_здания_м'],
            'Значение_мм': building_height_info['Общая_высота_здания_м'] * 1000
        }, {
            'Параметр': 'Минимальная отметка надземной части',
            'Значение_м': building_height_info['Минимальная_отметка_надземной_части_м'],
            'Значение_мм': building_height_info['Минимальная_отметка_надземной_части_м'] * 1000
        }, {
            'Параметр': 'Максимальная отметка надземной части',
            'Значение_м': building_height_info['Максимальная_отметка_надземной_части_м'],
            'Значение_мм': building_height_info['Максимальная_отметка_надземной_части_м'] * 1000
        }])
        
        df_height.to_excel(writer, sheet_name='Высота_здания', index=False)

    logger.info(f"Файл сохранен в {output_file}")
    logger.info("===ПРЕДВАРИТЕЛЬНЫЙ ЭТАП ЗАВЕРШЕН===")

 