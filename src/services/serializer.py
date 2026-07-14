# Часть 1: Экспорт IFC в GLB с помощью IfcOpenShell
import multiprocessing
import ifcopenshell
import ifcopenshell.geom
import os


def _make_glb_file(ifc_file_orig, output_folder):
    # 1. Открываем IFC-файл
    ifc_file = ifcopenshell.open(ifc_file_orig)

    # 2. Настройки экспорта
    settings = ifcopenshell.geom.settings()
    #settings.set("dimensionality", ifcopenshell.ifcopenshell_wrapper.CURVES_SURFACES_AND_SOLIDS)

    # Включаем генерацию материалов (если в IFC есть цвета, они превратятся в PBR-материалы)
    #settings.set("apply-default-materials", True) 


    # 3. Настройки сериализатора (GLB)
    serialiser_settings = ifcopenshell.geom.serializer_settings()
    serialiser_settings.set("use-element-guids", True)

    # Settings for obj
    settings.set("dimensionality", ifcopenshell.ifcopenshell_wrapper.CURVES_SURFACES_AND_SOLIDS)
    settings.set("apply-default-materials", True)
    settings.set("use-world-coords", True)

    # 4. Создаем сериализатор
    output_filename = os.path.join(output_folder, "3Dmodel.glb") if output_folder else "3D модель.glb"
    serialiser = ifcopenshell.geom.serializers.gltf(output_filename, settings, serialiser_settings)

    # Подготавливаем экспортер
    serialiser.setFile(ifc_file)
    serialiser.setUnitNameAndMagnitude("METER", 1.0)
    serialiser.writeHeader()

    # 5. Начинаем проход по элементам
    iterator = ifcopenshell.geom.iterator(settings, ifc_file, multiprocessing.cpu_count())

    if iterator.initialize():
        while True:
            serialiser.write(iterator.get())
            if not iterator.next():
                break
    serialiser.finalize()

    print(f"Файл {output_filename} успешно создан!")

    return output_filename
