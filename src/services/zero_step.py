# ВЫТАСКИВАЕМ ИЗ ФАЙЛА .ifc ВСЮ ИНФОРМАЦИЮ (исправленная версия с литрами и QTO)

import ifcopenshell
import pandas as pd
import os

from src.core.logger import setup_logger

logger = setup_logger("zero_step")

 
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

# Список специфических свойств для извлечения
SPECIFIC_PROPERTIES = [
    'Pset_ConcreteElementGeneral.ReinforcementVolumeRatio',
    'ExpCheck_MaterialConcrete.MGE_ConcreteGrade',
    'ExpCheck_MaterialConcrete.MGE_WaterResist',
    'ExpCheck_MaterialConcrete.MGE_FreezeDurability'
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
    """Извлекает все количественные характеристики элемента (QTO) - ДУБЛИРУЕТ в старом и новом формате"""
    quantities = {}
    try:
        if hasattr(element, 'IsDefinedBy'):
            for rel in element.IsDefinedBy:
                if rel.is_a('IfcRelDefinesByProperties'):
                    props = rel.RelatingPropertyDefinition
                    if props and props.is_a('IfcElementQuantity'):
                        # Получаем название QTO набора
                        qto_set_name = safe_get_attr(props, 'Name')
                        
                        if hasattr(props, 'Quantities'):
                            for qty in props.Quantities:
                                qty_name = safe_get_attr(qty, 'Name')
                                qty_type = qty.is_a()
                                
                                if qty_type == 'IfcQuantityLength':
                                    value = safe_get_attr(qty, 'LengthValue')
                                    if value and value != '-':
                                        # НОВЫЙ формат с QTO_
                                        quantities[f'QTO_{qto_set_name}_Длина_{qty_name}_мм'] = round(float(value), 2)
                                        # СТАРЫЙ формат без QTO_ (как было в работающей версии)
                                        quantities[f'Длина_{qty_name}_мм'] = round(float(value), 2)
                                
                                elif qty_type == 'IfcQuantityArea':
                                    value = safe_get_attr(qty, 'AreaValue')
                                    if value and value != '-':
                                        # НОВЫЙ формат с QTO_
                                        quantities[f'QTO_{qto_set_name}_Площадь_{qty_name}_м2'] = round(float(value), 3)
                                        # СТАРЫЙ формат без QTO_ (как было в работающей версии)
                                        quantities[f'Площадь_{qty_name}_м2'] = round(float(value), 3)
                                        
                                        # ЕСЛИ ЭТО GROSS - создаем дополнительные ключи для приоритетного поиска
                                        if 'gross' in qty_name.lower():
                                            # Специальный ключ для поиска Gross в geometry_mapping
                                            quantities[f'QTO_{qto_set_name}_Площадь_GROSS_м2'] = round(float(value), 3)
                                            # И в старом формате тоже
                                            quantities[f'Площадь_GROSS_м2'] = round(float(value), 3)
                                
                                elif qty_type == 'IfcQuantityVolume':
                                    value = safe_get_attr(qty, 'VolumeValue')
                                    if value and value != '-':
                                        if qty_name.lower() == 'netvolume':
                                            # НОВЫЙ формат
                                            quantities[f'QTO_{qto_set_name}_Объём_{qty_name}_м3'] = round(float(value), 3)
                                            # СТАРЫЙ формат
                                            quantities[f'Объём_{qty_name}_м3'] = round(float(value), 3)
                                        else:
                                            # НОВЫЙ формат
                                            quantities[f'QTO_{qto_set_name}_Объём_{qty_name}_литры'] = round(float(value), 2)
                                            # СТАРЫЙ формат
                                            quantities[f'Объём_{qty_name}_литры'] = round(float(value), 2)
                                
                                elif qty_type == 'IfcQuantityCount':
                                    value = safe_get_attr(qty, 'CountValue')
                                    if value and value != '-':
                                        # НОВЫЙ формат
                                        quantities[f'QTO_{qto_set_name}_Количество_{qty_name}'] = value
                                        # СТАРЫЙ формат
                                        quantities[f'Количество_{qty_name}'] = value
                                
                                elif qty_type == 'IfcQuantityWeight':
                                    value = safe_get_attr(qty, 'WeightValue')
                                    if value and value != '-':
                                        # НОВЫЙ формат
                                        quantities[f'QTO_{qto_set_name}_Вес_{qty_name}_кг'] = round(float(value), 2)
                                        # СТАРЫЙ формат
                                        quantities[f'Вес_{qty_name}_кг'] = round(float(value), 2)
    except Exception as e:
        logger.error(f"Ошибка при получении QTO параметров: {e}")
    return quantities


def get_geometry_from_representation(element):
    """Извлекает геометрические параметры из представления элемента"""
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
                                            geometry['Длина_мм'] = round(float(swept.XDim), 2)
                                        if hasattr(swept, 'YDim'):
                                            geometry['Толщина_мм'] = round(float(swept.YDim), 2)
    except Exception as e:
        logger.error(f"Ошибка при анализе геометрии: {e}")
    return geometry


def get_placement_info(element):
    """Извлекает информацию о размещении элемента"""
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
    """Извлекает все свойства элемента из Property Sets"""
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


def get_specific_properties(element):
    """
    Извлекает конкретные свойства по заданному списку SPECIFIC_PROPERTIES
    Формат: 'PsetName.PropertyName'
    """
    properties = {}
    
    # Инициализируем все целевые свойства значением '-'
    for prop_path in SPECIFIC_PROPERTIES:
        col_name = prop_path.replace('.', '_')
        properties[col_name] = '-'
    
    try:
        if hasattr(element, 'IsDefinedBy'):
            for rel in element.IsDefinedBy:
                if rel.is_a('IfcRelDefinesByProperties'):
                    props = rel.RelatingPropertyDefinition
                    if props:
                        pset_name = safe_get_attr(props, 'Name')
                        
                        # Проверяем Property Sets
                        if props.is_a('IfcPropertySet'):
                            if hasattr(props, 'HasProperties'):
                                for prop in props.HasProperties:
                                    prop_name = safe_get_attr(prop, 'Name')
                                    full_name = f"{pset_name}.{prop_name}"
                                    
                                    # Проверяем, нужно ли нам это свойство
                                    if full_name in SPECIFIC_PROPERTIES:
                                        col_name = full_name.replace('.', '_')
                                        if prop.is_a('IfcPropertySingleValue') and prop.NominalValue:
                                            if hasattr(prop.NominalValue, 'wrappedValue'):
                                                properties[col_name] = prop.NominalValue.wrappedValue
                                            else:
                                                properties[col_name] = str(prop.NominalValue)
                                        elif prop.is_a('IfcPropertyEnumeratedValue'):
                                            if prop.EnumerationValues:
                                                properties[col_name] = str(prop.EnumerationValues[0].wrappedValue)
                                        break
    except Exception as e:
        logger.error(f"Ошибка при извлечении специфических свойств: {e}")
    
    return properties


def get_element_info(element):
    """Собирает всю информацию об элементе"""
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
    
    # Геометрия
    info.update(get_geometry_from_representation(element))
    
    # Размещение
    info.update(get_placement_info(element))
    
    # QTO характеристики (количественные)
    info.update(get_all_quantities(element))
    
    # Все свойства
    info.update(get_all_properties(element))
    
    # СПЕЦИФИЧЕСКИЕ СВОЙСТВА ДЛЯ СМЕТЧИКА
    info.update(get_specific_properties(element))
    
    return info


def analyze_qto_properties(ifc_file_path):
    """
    Анализирует все QTO свойства в IFC файле (для отладки)
    """
    model = ifcopenshell.open(ifc_file_path)
    
    print("=" * 80)
    print("АНАЛИЗ QTO (Quantity Take-Off) СВОЙСТВ В IFC ФАЙЛЕ")
    print("=" * 80)
    
    # Словарь для сбора всех уникальных QTO свойств
    all_qto_properties = {}
    
    # Проходим по всем элементам
    for element in model:
        if hasattr(element, 'IsDefinedBy'):
            for rel in element.IsDefinedBy:
                if rel.is_a('IfcRelDefinesByProperties'):
                    props = rel.RelatingPropertyDefinition
                    
                    # Проверяем, является ли это QTO
                    if props and props.is_a('IfcElementQuantity'):
                        qto_name = props.Name if hasattr(props, 'Name') else "Без имени"
                        element_type = element.is_a()
                        
                        if qto_name not in all_qto_properties:
                            all_qto_properties[qto_name] = {
                                'count': 0,
                                'element_types': set(),
                                'quantities': {}
                            }
                        
                        all_qto_properties[qto_name]['count'] += 1
                        all_qto_properties[qto_name]['element_types'].add(element_type)
                        
                        # Анализируем количества внутри QTO
                        if hasattr(props, 'Quantities'):
                            for qty in props.Quantities:
                                qty_name = qty.Name
                                qty_type = qty.is_a()
                                
                                if qty_name not in all_qto_properties[qto_name]['quantities']:
                                    all_qto_properties[qto_name]['quantities'][qty_name] = {
                                        'type': qty_type,
                                        'count': 0
                                    }
                                
                                all_qto_properties[qto_name]['quantities'][qty_name]['count'] += 1
    
    # Выводим результаты
    for qto_name, qto_data in all_qto_properties.items():
        print(f"\n📊 QTO Set: {qto_name}")
        print(f"   Количество использований: {qto_data['count']}")
        print(f"   Типы элементов: {', '.join(sorted(qto_data['element_types']))}")
        print(f"   Свойства:")
        
        for qty_name, qty_data in qto_data['quantities'].items():
            print(f"     • {qty_name} ({qty_data['type']}) - {qty_data['count']} использований")
    
    return all_qto_properties


def zero_step(ifc_file, output_folder=None):
    """Основная функция обработки IFC файла"""
    logger.info(f"Начата обработка файла {ifc_file}")

    model = ifcopenshell.open(ifc_file)

    logger.info("Обработка ifc с анализом этажей")
    
    # Анализ QTO свойств (для отладки)
    analyze_qto_properties(ifc_file)

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
    
    # Разделяем колонки на QTO и обычные
    qto_cols = [col for col in df.columns if col.startswith('QTO_')]
    regular_other_cols = [col for col in df.columns if col not in base_cols + storey_cols + qto_cols]
    
    # Сортируем: базовые, этажи, QTO, обычные свойства
    df = df[base_cols + storey_cols + qto_cols + regular_other_cols]

    # Проверка и переименование столбцов для совместимости
    if 'Длина_Width_мм' not in df.columns and 'Толщина_мм' in df.columns:
        df['Длина_Width_мм'] = df['Толщина_мм']
        logger.info("Столбец 'Толщина_мм' скопирован в 'Длина_Width_мм'")
    
    # ============================================================================
    # ОПРЕДЕЛЯЕМ СЛОВАРЬ СООТВЕТСТВИЯ ЭЛЕМЕНТОВ И ГЕОМЕТРИЧЕСКИХ ПАРАМЕТРОВ
    # ============================================================================
    
    geometry_mapping = {
        'Стены': {
            'ДЛИНА': [
                'Длина_Length_мм',
                'QTO_Qto_WallBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_WallBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Длина_Width_мм',
                'QTO_Qto_WallBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_WallBaseQuantities_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Длина_Height_мм',
                'QTO_Qto_WallBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_WallBaseQuantities_Height',
                'Высота_мм', 'Height_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_WallBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_WallBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_WallBaseQuantities_GROSS',
                'Площадь_GrossSideArea_м2',
                'QTO_Qto_WallBaseQuantities_Площадь_GrossSideArea_м2',
                'Свойство_Qto_WallBaseQuantities_GrossSideArea',
                'Площадь_GrossFootprintArea_м2',
                'QTO_Qto_WallBaseQuantities_Площадь_GrossFootprintArea_м2',
                'Свойство_Qto_WallBaseQuantities_GrossFootprintArea',
                'Площадь_GrossArea_м2',
                'QTO_Qto_WallBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_WallBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_WallBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_WallBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_WallBaseQuantities_NetVolume',
                'Свойство_Qto_WallBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_RusSet_WallLabel_RUS_ReinforcementVolumeRatio',
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Свойство_ExpCheck_WallReinforcement_MGE_ReinforceStrengthClass',
                'ReinforcementVolumeRatio'
            ],
        },
        'Перекрытия': {
            'ДЛИНА': [
                'Длина_Length_мм',
                'QTO_Qto_SlabBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_SlabBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Длина_Width_мм',
                'QTO_Qto_SlabBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_SlabBaseQuantities_Width',
                'Свойство_RusSet_SlabBaseQuantities_RUS_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Длина_Height_мм',
                'QTO_Qto_SlabBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_SlabBaseQuantities_NominalThickness',
                'Свойство_Pset_PrecastSlab_NominalThickness',
                'Высота_мм', 'Height_мм', 'Глубина_выдавливания_мм'
            ],
            'ПЕРИМЕТР': [
                'Длина_Perimeter_мм',
                'QTO_Qto_SlabBaseQuantities_Длина_Perimeter_мм',
                'Свойство_Qto_SlabBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_SlabBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_SlabBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_SlabBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_SlabBaseQuantities_GrossArea',
                'Площадь_GrossSlabArea_м2',
                'QTO_Qto_SlabBaseQuantities_Площадь_GrossSlabArea_м2',
                'Свойство_Qto_SlabBaseQuantities_GrossSlabArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_SlabBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_SlabBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_SlabBaseQuantities_NetVolume',
                'Свойство_Qto_SlabBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_RusSet_SlabLabel_RUS_ReinforcementVolumeRatio',
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Свойство_ExpCheck_SlabReinforcement_MGE_ReinforceStrengthClass',
                'ReinforcementVolumeRatio'
            ],
        },
        'Колонны': {
            'ДЛИНА': [
                'Длина_Length_мм',
                'QTO_Qto_ColumnBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_ColumnBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Длина_Width_мм',
                'QTO_Qto_ColumnBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_ColumnBaseQuantities_Width',
                'Свойство_RusSet_ColumnBaseQuantities_RUS_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Длина_Height_мм',
                'QTO_Qto_ColumnBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_ColumnBaseQuantities_Height',
                'Свойство_RusSet_ColumnBaseQuantities_RUS_Height',
                'Высота_мм', 'Height_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_ColumnBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_ColumnBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_ColumnBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_ColumnBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_ColumnBaseQuantities_GrossArea',
                'Площадь_GrossSurfaceArea_м2',
                'QTO_Qto_ColumnBaseQuantities_Площадь_GrossSurfaceArea_м2',
                'Свойство_Qto_ColumnBaseQuantities_GrossSurfaceArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_ColumnBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_ColumnBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_ColumnBaseQuantities_NetVolume',
                'Свойство_Qto_ColumnBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_RusSet_ColumnLabel_RUS_ReinforcementVolumeRatio',
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Свойство_ExpCheck_ColumnReinforcement_MGE_ReinforceStrengthClass',
                'ReinforcementVolumeRatio'
            ],
        },
        'Балки': {
            'ДЛИНА': [
                'Длина_Length_мм',
                'QTO_Qto_BeamBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_BeamBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Длина_Width_мм',
                'QTO_Qto_BeamBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_BeamBaseQuantities_Width',
                'Свойство_RusSet_BeamBaseQuantities_RUS_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Длина_Height_мм',
                'QTO_Qto_BeamBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_BeamBaseQuantities_Height',
                'Свойство_RusSet_BeamBaseQuantities_RUS_Height',
                'Высота_мм', 'Height_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_BeamBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_BeamBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_BeamBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_BeamBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_BeamBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_BeamBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_BeamBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_BeamBaseQuantities_NetVolume',
                'Свойство_Qto_BeamBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_RusSet_BeamLabel_RUS_ReinforcementVolumeRatio',
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Свойство_ExpCheck_BeamReinforcement_MGE_ReinforceStrengthClass',
                'ReinforcementVolumeRatio'
            ],
        },
        'Лестницы': {
            'ДЛИНА': [
                'Длина_Length_мм',
                'QTO_Qto_StairBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_StairBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Длина_Width_мм',
                'QTO_Qto_StairBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_StairBaseQuantities_Width',
                'Свойство_RusSet_StairBaseQuantities_RUS_Width',
                'Свойство_RusSet_StairFlightBaseQuantities_RUS_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Длина_Height_мм',
                'QTO_Qto_StairBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_StairBaseQuantities_Height',
                'Свойство_RusSet_StairBaseQuantities_RUS_Height',
                'Высота_мм', 'Height_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_StairBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_StairBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_StairBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_StairBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_StairBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_StairBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_StairBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_StairBaseQuantities_NetVolume',
                'Свойство_Qto_StairBaseQuantities_GrossVolume',
                'Свойство_Qto_StairFlightBaseQuantities_NetVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_RusSet_StairLabel_RUS_ReinforcementVolumeRatio',
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Свойство_ExpCheck_StairReinforcement_MGE_ReinforceStrengthClass',
                'ReinforcementVolumeRatio'
            ],
        },
        'Пандусы': {
            'ДЛИНА': [
                'Длина_Length_мм',
                'QTO_Qto_RampBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_RampBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Длина_Width_мм',
                'QTO_Qto_RampBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_RampBaseQuantities_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Длина_Height_мм',
                'QTO_Qto_RampBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_RampBaseQuantities_Height',
                'Высота_мм', 'Height_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_RampBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_RampBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_RampBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_RampBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_RampBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_RampBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_RampBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_RampBaseQuantities_NetVolume',
                'Свойство_Qto_RampBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_RusSet_RampLabel_RUS_ReinforcementVolumeRatio',
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'ReinforcementVolumeRatio'
            ],
        },
        'Прочие_элементы': {
            'ДЛИНА': ['Длина_мм', 'Length_мм'],
            'ШИРИНА': ['Толщина_мм', 'Width_мм', 'Длина_Width_мм'],
            'ВЫСОТА': ['Высота_мм', 'Height_мм', 'Глубина_выдавливания_мм'],
            'ПЕРИМЕТР': ['Perimeter_мм', 'Периметр_мм'],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_BaseQuantities_Площадь_GROSS_м2',
                'Свойство_BaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_BaseQuantities_Площадь_GrossArea_м2',
                'Свойство_BaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_BaseQuantities_Объём_NetVolume_м3',
                'QTO_BaseQuantities_Объём_GrossVolume_литры',
                'Свойство_BaseQuantities_NetVolume',
                'Свойство_BaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'ReinforcementVolumeRatio'
            ],
        },
        'Покрытие': {
            'ДЛИНА': ['Длина_мм', 'Length_мм'],
            'ШИРИНА': ['Толщина_мм', 'Width_мм', 'Длина_Width_мм'],
            'ВЫСОТА': ['Высота_мм', 'Height_мм'],
            'ПЕРИМЕТР': ['Perimeter_мм', 'Периметр_мм'],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_CoveringBaseQuantities_Площадь_GROSS_м2',
                'Свойство_CoveringBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_CoveringBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_CoveringBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_CoveringBaseQuantities_Объём_NetVolume_м3',
                'QTO_CoveringBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_CoveringBaseQuantities_NetVolume',
                'Свойство_CoveringBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'ReinforcementVolumeRatio'
            ],
        },
        'Свая': {
            'ДЛИНА': [
                'Длина_Length_мм',
                'QTO_Qto_PileBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_PileBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Длина_Width_мм',
                'QTO_Qto_PileBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_PileBaseQuantities_Width',
                'Свойство_RusSet_PileBaseQuantities_RUS_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Свойство_Qto_PileBaseQuantities_Height',
                'Высота_мм', 'Height_мм', 'Глубина_выдавливания_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_PileBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_PileBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_PileBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_PileBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_PileBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_PileBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_PileBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_PileBaseQuantities_NetVolume',
                'Свойство_Qto_PileBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'ReinforcementVolumeRatio'
            ],
        },
    }
    
    # ============================================================================
    # ФУНКЦИЯ ДЛЯ ИЗВЛЕЧЕНИЯ ЗНАЧЕНИЯ ПО ТИПУ ЭЛЕМЕНТА
    # ============================================================================
    
    def get_geometry_value(row, param_name, convert_to_m=False, convert_to_m3=False):
        """
        Извлекает геометрический параметр для конкретного элемента
        Для ПЛОЩАДИ - ищет ТОЛЬКО Gross
        Для ReinforcementVolumeRatio - возвращает как строку
        """
        element_type = row['Тип (RU)']
        
        # Получаем список возможных столбцов для этого типа элемента и параметра
        if element_type not in geometry_mapping:
            return '-'
        
        possible_columns = geometry_mapping[element_type].get(param_name, [])
        
        # Ищем первый существующий столбец с непустым значением
        for col in possible_columns:
            if col in df.columns:
                value = row[col]
                if value != '-' and pd.notna(value):
                    try:
                        # Для ReinforcementVolumeRatio возвращаем как есть
                        if param_name == 'ReinforcementVolumeRatio':
                            return str(value)
                        
                        num_value = float(value)
                        
                        # Конвертация единиц измерения
                        if convert_to_m:
                            # Конвертируем мм в м
                            if 'мм' in col or 'mm' in col.lower():
                                return round(num_value / 1000, 3)
                        elif convert_to_m3:
                            # Конвертируем литры в м3
                            if 'литры' in col.lower() or 'liters' in col.lower():
                                return round(num_value / 1000, 3)
                        
                        return round(num_value, 3)
                    except:
                        # Если не удалось преобразовать в число, возвращаем как строку
                        if param_name == 'ReinforcementVolumeRatio':
                            return str(value)
                        continue
        
        return '-'
    
    # ============================================================================
    # СОЗДАЕМ АГРЕГИРОВАННЫЕ СТОЛБЦЫ ГЕОМЕТРИИ И СВОЙСТВ
    # ============================================================================
    
    logger.info("Создание агрегированных столбцов с приоритетом Gross...")
    
    df['Длина, мм'] = df.apply(lambda row: get_geometry_value(row, 'ДЛИНА'), axis=1)
    df['Ширина, мм'] = df.apply(lambda row: get_geometry_value(row, 'ШИРИНА'), axis=1)
    df['Высота, мм'] = df.apply(lambda row: get_geometry_value(row, 'ВЫСОТА'), axis=1)
    df['Периметр, м'] = df.apply(lambda row: get_geometry_value(row, 'ПЕРИМЕТР', convert_to_m=True), axis=1)
    df['Площадь, м2'] = df.apply(lambda row: get_geometry_value(row, 'ПЛОЩАДЬ'), axis=1)
    df['Объём, м3'] = df.apply(lambda row: get_geometry_value(row, 'ОБЪЕМ', convert_to_m3=True), axis=1)
    df['ReinforcementVolumeRatio'] = df.apply(lambda row: get_geometry_value(row, 'ReinforcementVolumeRatio'), axis=1)
    
    # Удаляем технические столбцы
    cols_to_drop = ['Глубина_выдавливания_мм', 'Координата_X_мм', 'Координата_Y_мм', 'Координата_Z_мм']
    df = df.drop([col for col in cols_to_drop if col in df.columns], axis=1)

    if output_folder:
        output_filename = os.path.join(output_folder, 'IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx')
    else:
        output_filename = 'IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx'

    df.to_excel(output_filename, index=False)
    
    # ============================================================================
    # СОЗДАЕМ СОКРАЩЕННУЮ ТАБЛИЦУ ДЛЯ СМЕТЧИКА
    # ============================================================================
    
    # Создаем сокращенный DataFrame
    df_short = pd.DataFrame()
    df_short['№ п/п'] = range(1, len(df) + 1)
    
    # Базовые столбцы
    df_short['Тип (RU)'] = df['Тип (RU)']
    df_short['Тип элемента'] = df['Тип элемента']
    df_short['Имя'] = df['Имя']
    df_short['GlobalId'] = df['GlobalId']
    df_short['Материал'] = df['Материал']
    df_short['Этаж'] = df['Этаж']
    df_short['Тип этажа'] = df['Тип_этажа']
    
    # Добавляем агрегированные геометрические столбцы
    df_short['Длина, мм'] = df['Длина, мм']
    df_short['Толщина, мм'] = df['Ширина, мм']
    df_short['Высота, мм'] = df['Высота, мм']
    df_short['Периметр, м'] = df['Периметр, м']
    df_short['Площадь (Gross), м2'] = df['Площадь, м2']  
    df_short['Объем (Net), м3'] = df['Объём, м3']
    df_short['ReinforcementVolumeRatio'] = df['ReinforcementVolumeRatio']
    
    # Заменяем NaN на '-'
    df_short = df_short.fillna('-')
    
    # Сохраняем сокращенную таблицу
    if output_folder:
        short_output_file = os.path.join(output_folder, 'ДЛЯ_СМЕТЧИКА_сокращенный.xlsx')
    else:
        short_output_file = 'ДЛЯ_СМЕТЧИКА_сокращенный.xlsx'
    
    df_short.to_excel(short_output_file, index=False)
    
    # ============================================================================
    # СОЗДАЕМ ПОЛНУЮ ТАБЛИЦУ ДЛЯ СМЕТЧИКА
    # ============================================================================
    
    smetchik_cols = ['Тип (RU)', 'Тип элемента', 'Имя', 'GlobalId', 'Материал', 'Этаж', 'Тип_этажа', 'Уровень_этажа_мм']

    # Приоритетно добавляем QTO колонки (только Gross для площадей)
    for col in df.columns:
        if col.startswith('QTO_'):
            # Для площадей - добавляем только Gross
            if 'Площадь' in col:
                if 'Gross' in col or 'GROSS' in col:
                    smetchik_cols.append(col)
            else:
                smetchik_cols.append(col)
    
    # Добавляем обычные геометрические параметры
    for col in df.columns:
        if not col.startswith('QTO_') and not col.endswith('_агрег_мм') and not col.endswith('_агрег_м') and not col.endswith('_агрег_м2') and not col.endswith('_агрег_м3'):
            if any(term in col for term in ['Длина', 'Ширина', 'Высота', 'Глубина']) and '_мм' in col:
                smetchik_cols.append(col)
            elif 'Объём' in col and ('_м3' in col or '_литры' in col):
                smetchik_cols.append(col)
            elif 'Площадь' in col and 'Gross' in col and '_м2' in col:
                smetchik_cols.append(col)

    # ДОБАВЛЯЕМ СПЕЦИФИЧЕСКИЕ СВОЙСТВА ИЗ СПИСКА
    specific_col_names = [prop.replace('.', '_') for prop in SPECIFIC_PROPERTIES]
    for col in specific_col_names:
        if col in df.columns:
            smetchik_cols.append(col)
    
    # Добавляем агрегированные столбцы
    aggregated_cols = ['Длина, мм', 'Ширина, мм', 'Высота, мм', 'Периметр, м', 'Площадь, м2', 'Объём, м3', 'ReinforcementVolumeRatio']
    for col in aggregated_cols:
        if col in df.columns and col not in smetchik_cols:
            smetchik_cols.append(col)

    existing_cols = [col for col in smetchik_cols if col in df.columns]

    df_smetchik = df[existing_cols].copy()
    df_smetchik = df_smetchik.fillna('-')

    df_smetchik.insert(0, '№ п/п', range(1, len(df_smetchik) + 1))
    df_smetchik['Примечание_сметчика'] = ''
    df_smetchik['Стоимость_за_ед_руб'] = ''
    df_smetchik['Общая_стоимость_руб'] = ''

    # Поиск колонки с объемом для сводки (приоритет агрегированный)
    volume_col = 'Объём, м3'
    if volume_col not in df.columns:
        volume_col = None
        for col in df.columns:
            if col.startswith('QTO_') and 'Объём_NetVolume_м3' in col:
                volume_col = col
                break
        if not volume_col:
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
        
        # Также считаем общую Gross площадь
        total_gross_area = 0
        if 'Площадь, м2' in df.columns:
            area_series = pd.to_numeric(group['Площадь, м2'], errors='coerce').fillna(0)
            total_gross_area = area_series.sum()
        
        summary_data.append({
            'Тип (RU)': type_ru,
            'Тип элемента': type_elem,
            'Материал': material if material != '-' else 'Не указан',
            'Количество, шт': count,
            'Объем, м³': round(total_volume, 3) if total_volume > 0 else '-',
            'Площадь Gross, м²': round(total_gross_area, 3) if total_gross_area > 0 else '-',
        })

    df_summary = pd.DataFrame(summary_data)

    total_count = df_summary['Количество, шт'].sum()
    total_volume = 0
    total_gross_area = 0
    for _, row in df_summary.iterrows():
        if row['Объем, м³'] != '-':
            total_volume += row['Объем, м³']
        if row['Площадь Gross, м²'] != '-':
            total_gross_area += row['Площадь Gross, м²']

    total_row = pd.DataFrame([{
        'Тип (RU)': 'ВСЕГО',
        'Тип элемента': '',
        'Материал': '',
        'Количество, шт': total_count,
        'Объем, м³': round(total_volume, 3),
        'Площадь Gross, м²': round(total_gross_area, 3),
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
    logger.info(f"Сокращенный файл сохранен в {short_output_file}")
    logger.info("===ПРЕДВАРИТЕЛЬНЫЙ ЭТАП ЗАВЕРШЕН===")