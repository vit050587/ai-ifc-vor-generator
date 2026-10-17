# ai-ifc-vor-generator

Сервис автоматической генерации **видов работ (ВОР)** для смет из BIM-моделей.
Принимает на вход **IFC-файлы** и **PDF-чертежи**, извлекает строительные элементы,
группирует их и формирует финальный **перечень работ** (Excel) со стоимостью.

Поддерживает два режима обработки (режимы переименованы; оба обрабатывают одни и те же
файлы — IFC и PDF-чертежи, различается только пайплайн подбора работ):

- **CS / ЦС** — **цифровой сборник** (ранее KR — «конструктивные решения»; ж/б конструкции:
  стены, колонны, перекрытия, балки и т.д.). Подбор работ — через внешний API цифрового
  справочника ТСН;
- **AI / ИИ** — **искусственный интеллект** (ранее AR — «архитектурные решения»; окна,
  двери, покрытия, кровля, отделка и т.д.). Подбор таблиц работ — детерминированный
  алгоритм, финальный выбор работ и разбор ПОС/ПЗ — локальная LLM.

Легаси-коды `KR`/`AR` по-прежнему принимаются API и нормализуются к `CS`/`AI`
(`src/core/modes.py`); внутренние коды пайплайн-модулей и имена артефактов
(`final_result_KR.json`, `final_result_AR.json` и др.) не изменились.

Сервис реализован на **Flask + gunicorn**, работает в **Docker Compose** вместе с **Ollama**
(локальная модель LLM) на базе графического ускорителя NVIDIA. Вся тяжёлая обработка
выполняется **в фоне** (в отдельных потоках), API — асинхронное: после загрузки файла
сервис сразу возвращает `sessionId`, а пользователь опрашивает статус до завершения.

---

## Функциональность (что делает сервис)

- **Загрузка IFC (.ifc) или PDF-чертежа (.pdf)** на выбор, с указанием режима обработки `CS` / `AI`
  (легаси `KR` / `AR` также принимаются).
- **Извлечение элементов** из IFC (через `ifcopenshell`): стены, колонны, плиты, балки, лестницы,
  фундаменты, сваи, а в режиме ИИ — окна, двери, кровля, покрытия, перила, мебель и др.
- **Извлечение данных из PDF-чертежей** (машинное зрение): обнаружение стен (YOLO),
  анализ штриховок и расшифровка материалов (DINO + VLM), легенды и масштабы.
- **Нормализация** характеристик элемента: размеры, геометрия (в т.ч. вычисление по bbox,
  когда в IFC нет QTO), материал, части здания, этажность.
- **Группировка** элементов в иерархию для сметчика (по частям здания, разделам, материалам,
  кодам МССК).
- **Подбор работ**:
  - в режиме **ЦС (CS)** — через **внешний API цифрового справочника ТСН** (по группам элементов);
  - в режиме **ИИ (AI)** — детерминированный подбор таблиц + локальная LLM-модель
    (этапы фильтрации, ранжирования и финального выбора работ, разбор ПОС/ПЗ).
- Расчёт **объёмов работ** (по группам/материалам), корректировка по нормам расхода
  (`koefs.xlsx`) и **стоимости** (по `price_cost.xlsx`).
- **Формирование Excel-перечней**: `ДЛЯ_СМЕТЧИКА*`, `ОБЩИЙ_Финальный_перечень_работ.xlsx`,
  `Дерево_проекта*` и др.
- **JSON-справочники** элементов и групп для внешних баз (`ifc_elements_output.json`,
  `ifc_raw_elements_grouped.json`).
- **Просмотр и скачивание** всех промежуточных и финальных файлов, выгрузка архивом.
- **3D-модель (GLB)** из IFC — формируется по запросу (`/api/session/<id>/3d_model`).
- **Множественные запуски** (runs) в рамках одной сессии — можно повторно обрабатывать
  выбранные строки, переключаться между запусками и их результатами.
- **Сравнение ведомостей объёмов работ** (comparison-сессии): загрузка нескольких моделей
  или чертежей (ЦС/ИИ) и ВОР заказчика (PDF/XLSX/XLS/XML/ZIP/GSFX/GGE), разбор ВОР заказчика,
  сопоставление работ и объёмов сформированного ВОР и ВОР заказчика (по шифру/семантике/
  конструкциям + LLM-валидация), отчёт в JSON/Excel.
- **Авторизация** (flask-login) + защита API.

---

## Структура проекта

```
ai-ifc-vor-generator/
├── Dockerfile                 # Образ сервиса (CUDA 12.9, Python 3.11)
├── docker-compose.yml         # Связка ollama (GPU) + web-сервис
├── nginx.conf                 # Проксирование /ifc-vor/ → 127.0.0.1:6001
├── Makefile                   # Утилиты: make up / down / restart / logs / clean
├── requirements.txt           # Python-зависимости
├── .env                       # Переменные окружения (модели, API, Keycloak)
│
├── src/                       # Основной сервис (Flask)
│   ├── __init__.py            # create_app(), регистрация blueprint, Swagger
│   ├── wsgi.py                # Точка входа gunicorn
│   ├── routes.py              # Все HTTP-эндпоинты (REST API)
│   ├── schemas.py             # Pydantic-схемы запросов/ответов
│   ├── templates/
│   │   └── index.html         # Веб-интерфейс (SPA на JS)
│   ├── core/
│   │   ├── config.py          # Конфигурация из env (пути, модели, API)
│   │   ├── logger.py          # Настройка логирования
│   │   ├── prompt_manager.py  # Загрузка промптов из prompts/
│   │   └── keycloak.py        # Провайдер Bearer-токена Keycloak (client_credentials)
│   └── services/              # Бизнес-логика и пайплайн
│       ├── session_manager.py # Управление сессиями, запусками, фоновыми потоками
│       ├── zero_step.py       # Извлечение элементов из IFC (КР/АР), нормализация
│       ├── ifc_raw_dump.py    # «Сырой» дамп свойств/QTO/материалов + расчёт по bbox
│       ├── ifc_reference_builder.py # Построение JSON-справочников из IFC/PDF (КР)
│       ├── selection_template_builder.py # Шаблон параметров подбора по элементам (АР)
│       ├── works_table_selector.py  # АР: детерминированный подбор таблиц работ (8 шагов)
│       ├── works_fetcher.py   # АР: работы таблиц из цифрового сборника (larix)
│       ├── works_final_selector.py  # АР: финальный подбор работ через LLM (WORKS_SELECTOR=llm)
│       ├── perechen_kb.py     # АР: база знаний перечня сметчиков (perechen_kr.xlsx)
│       ├── perechen_pipeline.py     # АР: подбор по перечню (WORKS_SELECTOR=perechen)
│       ├── local_works.py / local_resources.py # АР: локальный справочник расценок/ресурсов
│       ├── tsn_coeffs.py      # АР: поправочные коэффициенты сборников ТСН (data/tsn)
│       ├── pd_full.py         # АР: полный разбор ПОС/ПЗ (≈40 параметров)
│       ├── ifc_features.py    # АР: признаки геометрии IFC (криволинейность, наклон и др.)
│       ├── doc_facts.py       # АР: факты из ВОР/ПД (класс бетона/арматуры, объёмы, сверка)
│       ├── ifc_json_builder.py      # Сборка финального JSON (ARBuilder/KRBuilder)
│       ├── position_links.py  # Ссылки на позиции цифрового сборника (КР)
│       ├── vor_comparison.py  # АР: сравнение ВОР заказчика и сформированного перечня
│       ├── works_cost.py      # Расчёт/форматирование стоимости перечня (КР)
│       ├── group_excel.py     # Группировка элементов (КР и АР), правила группировки
│       ├── api_works_lookup.py# Подбор работ через API справочника ТСН (КР)
│       ├── materials_lookup.py# Карта МССК-кодов материалов (АР)
│       ├── mssk_lookup.py     # Карта МССК-кодов элементов
│       ├── pd_parser.py       # Разбор PDF ПОС/ПЗ через LLM (АР)
│       ├── doc_pdf_parser.py  # Разбор PDF ВОР/ПД через LLM (АР)
│       ├── pdf_processor.py   # Обёртка пайплайна обработки PDF
│       ├── kb_inspect.py / kb_model.py / kb_etalon.py # Вспомогательные инструменты базы знаний
│       ├── comparison_manager.py   # Сравнение ВОР: оркестратор comparison-сессий
│       ├── comparison_documents.py # Сравнение ВОР: константы и уточнение материалов
│       ├── comparison_vor.py       # Сравнение ВОР: компактный отчёт сравнения
│       ├── vor_parser.py           # Сравнение ВОР: локальный парсер ВОР заказчика (PDF/XLSX)
│       ├── works_comparison/  # Пакет сравнения позиций (LLM-валидация) — см. ниже
│       └── serializer.py      # Экспорт IFC → GLB (3D модель)
│
│   # src/services/works_comparison/ — сравнение позиций смет заказчика и IFC
│   #   (подключён к сервису через comparison_manager, режим «Сравнение ВОР»):
│   #   processor.py, positions_extractor.py, ifc_positions_extractor.py,
│   #   positions_processor.py, ifc_positions_processor.py, compare_positions_groups.py,
│   #   semantic_matching.py, construction_matching.py, work_group_validator.py,
│   #   validation_result_former.py, ollama_service.py, units.py,
│   #   normalization_functions.py, utils.py, config.py
│
├── ai-blueprint-to-ifc/       # Пайплайн машинного зрения для PDF-чертежей
│   ├── config.py              # Настройки (модели, параметры, пороги)
│   ├── processor.py           # Оркестратор обработки чертежа
│   ├── pdf_prcoessor.py       # Конвертация PDF → изображения, тайлы
│   ├── walls_processor.py     # Детекция стен (YOLO OBB)
│   ├── hatching_processor.py  # Анализ штриховок и расшифровка материалов
│   ├── dino_service.py        # DINO-модель (сравнение символов легенды)
│   ├── yolo_service.py        # Обёртка над YOLO/ultralytics
│   ├── transformer_service.py # VLM (GreenMap qwen3-vl) для извлечения данных
│   ├── ollama_service.py      # Обёртка над Ollama (VLM Qwen3-VL)
│   ├── layout_processor.py    # Детекция layout (чертёж/легенда)
│   ├── legend_layout_processor.py # Разбор строк легенды
│   ├── drawing_statistics_analyzer.py # Статистика уверенности обработки
│   ├── result_former.py       # Формирование DataFrame результатов
│   ├── rectangle_utils.py     # Работа с OBB-прямоугольниками
│   ├── draw_geometry.py       # Отрисовка размеченных чертежей
│   ├── dino_train_creator.py  # Подготовка данных для обучения DINO
│   ├── models/                # Веса ML-моделей
│   │   ├── yolo_walls_obb.pt
│   │   ├── yolo_layout.pt
│   │   ├── yolo_legend_layout.pt
│   │   └── dino_hatching.pt
│   └── prompts/               # Промпты VLM для чертежей
│       ├── get_scale.txt
│       └── get_text_from_image.txt
│
├── prompts/                   # Промпты LLM (validate_material.txt, validate_work.txt —
│                              #   пакет works_comparison; element_analyze.txt — legacy)
├── data/                      # Справочники и входные данные (только чтение)
│   ├── perechen_kr.xlsx, perechen_kr_1.xlsx  # Перечни работ (КР; perechen_kr — база знаний АР)
│   ├── koefs.xlsx                            # Нормы расхода (корректировка объёма, КР и АР)
│   ├── price_cost.xlsx                       # Стоимость расценок (КР; локальный справочник АР)
│   ├── ifc_to_collections.json               # Правила IfcClass → сборники (АР)
│   ├── msck_elements_compact.json            # Дерево МССК-элементов (АР)
│   ├── tree_work_compact.json                # Дерево сборников ГЭСН/ТСН-2001 (АР)
│   ├── works_classification.json             # Классификация сборников + 10 констант (АР)
│   ├── params_registry.json                  # Реестр параметров ПОС/ПЗ (АР)
│   ├── selection_parameters.json / selection_parameters_mapping.json # Шаблон и карта параметров подбора (АР)
│   ├── tsn/                                  # Разобранные сборники ТСН (поправки, АР)
│   ├── comparison_vor_template.xlsx          # Шаблон иерархии ВОР (сравнение ВОР)
│   ├── elements_mssk.xlsx / elements_mssk_nested.json  # Справочник МССК элементов
│   ├── materials_mssk.xlsx / materials_mssk_nested.json # Справочник МССК материалов
│   └── *.json                                # Кеши LLM-решений (kb_cache, tsn_decisions_cache,
│                                             #   pd_full_cache_v2, doc_facts_cache) и кеши расценок
│                                             #   (local_works, local_resources, local_norms)
│
├── uploads/                   # Загруженные файлы (по сессиям)
└── outputs/                   # Результаты обработки
    ├── sessions.json          # «База» всех сессий
    └── <session_id>/          # Директория одной сессии
        ├── original/          # Исходные файлы (IFC/PDF, исходные Excel, GLB)
        ├── run_<NNN>/         # Результаты запуска NNN
        └── ...справочные и промежуточные JSON/XLSX...
```

---

## Используемые модели ИИ

| Модель | Фреймворк | Роль |
|--------|-----------|------|
| `yolo_walls_obb.pt` | Ultralytics YOLO (OBB) | Детекция стен на чертеже PDF |
| `yolo_layout.pt` | Ultralytics YOLO | Детекция layout-областей (чертёж/легенда) |
| `yolo_legend_layout.pt` | Ultralytics YOLO | Детекция таблиц легенды |
| `dino_hatching.pt` | DINO (TorchScript) | Анализ штриховок, сравнение символов легенды |
| `hf.co/unsloth/Qwen3-VL-8B-Instruct-GGUF` (`OLLAMA_MODEL_NAME`) | Ollama / VLM | Обработка чертежей PDF: распознавание материалов/штриховок по изображению |
| `yandex/YandexGPT-5-Lite-8B-instruct-GGUF` (`NORMS_LLM_MODEL` / `model_ollama`, `LLM_MODEL`, `KB_LLM_MODEL`) | Ollama | Режим ИИ (АР): разбор ПОС/ПЗ/ВОР/ПД (`pd_parser`, `doc_pdf_parser`, `pd_full`), подбор работ по перечню (`perechen_kb`/`perechen_pipeline`), поправки ТСН (`tsn_coeffs`) |
| `OLLAMA_MODEL` (`works_comparison`) | Ollama | Сравнение ВОР: семантическое сопоставление позиций и LLM-валидация подобранных работ |
| `GreenMap/qwen3-vl-4b-ru-blueprint-extractor` (`TG_MODEL_DIR`) | transformers (HF) | Извлечение данных с чертежа/легенды (VLM через `transformer_service`) |
| **ifcopenshell** | C++/Python | Парсинг и геометрия IFC, экспорт в GLB (не ML) |

Роль моделей по задачам:

- **PDF-чертежи** — весь конвейер в `ai-blueprint-to-ifc`: YOLO (стены/layout/легенда) →
  DINO + VLM (штриховки и материалы) → формирование Excel.
- **IFC-файлы** — `ifcopenshell`: атрибуты, Property Sets, QTO, геометрия → расчёт количеств.
- **Подбор работ (нормы)** — в режиме ИИ (АР) используется локальная LLM (YandexGPT),
  в режиме ЦС (КР) — внешний API справочника ТСН (LLM не используется).

---

## Изменения и дополнения функционала

Основные модули и изменения поведения по сравнению с базовой версией:

- **Подбор работ для ЦС (КР) через API цифрового справочника ТСН** (`api_works_lookup.py`).
  Ключевое изменение: в режиме **ЦС (КР)** работы больше не подбираются LLM (этапы 1–4 не выполняются).
  Вместо этого группы выбранных элементов конвертируются в формат `ifc_raw_elements_grouped.json`
  и отправляются **POST**-запросами на эндпоинт
  `digital-collection/building-elements/positions`; из ответа формируется
  `ОБЩИЙ_Финальный_перечень_работ.xlsx` с шифрами ТСН, объёмами и стоимостью.
- **Режим ИИ (АР, архитектурные решения)** — расширенный набор IFC-классов (окна, двери, кровля,
  покрытия, перила, мебель и др.) и группа правил группировки по кодам МССК и материалам.
- **Авторизация через Keycloak** (`keycloak.py`) — автоматическое получение/обновление
  Bearer-токена (`client_credentials`) для API справочника работ; статичный `WORKS_API_TOKEN`
  используется как fallback.
- **Расчёт количеств из геометрии (bbox/QTO)** (`ifc_raw_dump.py`) — когда в IFC нет
  наборов `IfcElementQuantity`, объём/длины/площади вычисляются по тесселированной геометрии
  элемента (меш/выдавливание), благодаря чему данные есть и для ИИ-элементов (АР).
- **Сырой дамп параметров** `IFC_исходные_параметры.xlsx/.json` выгружается в корень сессии
  и включается в результаты — полный исходный набор свойств каждого элемента.
- **Группировка по кодам МССК и материалам** для превью и группировки в режиме ИИ (АР)
  (справочники `elements_mssk_nested.json` и `materials_mssk_nested.json`).
- **JSON-справочники элементов и групп** (`ifc_elements_output.json`, `ifc_raw_elements_grouped.json`)
  теперь формируются автоматически сразу после извлечения IFC и доступны для скачивания.
- **Множественные запуски (runs)** внутри сессии: повторный подбор работ для другой выборки
  строк без повторной обработки исходного файла, переключение между запусками.
- **3D-модель по запросу**: GLB-файл больше не создаётся автоматически — формируется только
  при вызове `/api/session/<id>/3d_model` (отдельная кнопка в UI).
- **Объединение листов** PDF-обработки (`Данные_N` в один лист `Данные`), формирование
  изображения чертежа с разметкой и markdown-условных обозначений материалов.
- **Фильтрация по высоте** и расчёт высоты основного этажа (в ИИ (АР) важна высота типового этажа,
  а не общая высота здания).
- **Константы подбора работ (ИИ)**: эндпоинт `works_constants` отдаёт схему констант и значения,
  определённые из IFC и из файла ПОС; загрузка ПОС (`upload_pos`) разбирает PDF ПОС через LLM
  в `ПОС_глобальные_константы.json`; константы передаются в запуски вместе с выбором строк
  (`globalConstants`, `floorHeight` в `select_rows` / `new_run`).
- **Сборка финального JSON по запросу**: для каждого запуска доступна отдельная сборка
  (`build_final_json` / `final_json/status` / `final_json/result`) со статусами и ошибками
  (`finalJsonStatus` / `finalJsonError` в `RunInfo`).
- **Сравнение ведомостей объёмов работ** (comparison-сессии, `comparison_manager.py`):
  загрузка нескольких моделей/чертежей (ЦС/ИИ) и ВОР заказчика (PDF/XLSX/XLS/XML/ZIP/GSFX/GGE);
  по каждой модели формируется ВОР тем же конвейером, затем ВОР заказчика разбирается
  (PDF/XLSX — локальным парсером `vor_parser`, остальные — внешним сервисом) и сравнивается
  с сформированным ВОР пакетом `works_comparison/` (группировка по шифру+единице,
  семантическое сопоставление, LLM-валидация) → `result.json`/`result.xlsx`.
- **Разбор ВОР/ПД и подбор по перечню сметчиков** (режим ИИ): `doc_pdf_parser` (ВОР/ПД),
  `pd_full` (≈40 параметров ПОС/ПЗ), `perechen_kb`/`perechen_pipeline` (подбор по
  `perechen_kr.xlsx` + локальный справочник `price_cost.xlsx`), `tsn_coeffs`
  (поправочные коэффициенты сборников ТСН), `doc_facts` (факты ВОР/ПД), `vor_comparison`
  (сравнение ВОР заказчика и сформированного перечня).

---

## Данные и справочники

| Файл | Назначение |
|------|-----------|
| `data/perechen_kr.xlsx`, `perechen_kr_1.xlsx` | Перечень доступных расценок/работ для режима КР |
| `data/koefs.xlsx` | Нормы расхода/коэффициенты для корректировки объёмов работ (КР) |
| `data/price_cost.xlsx` | Стоимость расценок (Шифр → Текущие прямые затраты) (КР) |
| `data/ifc_to_collections.json`, `msck_elements_compact.json`, `tree_work_compact.json`, `works_classification.json`, `params_registry.json` | Справочники детерминированного подбора работ (АР) |
| `data/elements_mssk.xlsx`, `elements_mssk_nested.json` | МССК-справочник строительных элементов (иерархия категория→назначение→класс→подкласс) |
| `data/materials_mssk.xlsx`, `materials_mssk_nested.json` | МССК-справочник материалов (для группировки АР элементов по слоям) |
| `data/selection_parameters.json`, `selection_parameters_mapping.json` | Шаблон параметров подбора и карта соответствия (сырой дамп IFC → параметры) — режим АР |
| `data/tsn/*.json` | Разобранные сборники ТСН (поправочные коэффициенты, состав норм) — режим АР |
| `data/comparison_vor_template.xlsx` | Шаблон стандартной иерархии ВОР для отчёта сравнения |
| `data/*.json` (кеши) | `kb_cache.json`, `tsn_decisions_cache.json`, `pd_full_cache_v2.json`, `doc_facts_cache.json`, `local_works.json`, `local_resources.json`, `local_norms.json` — кеши LLM-решений и локальных расценок |

Новые/изменённые файлы-результаты в `outputs/<session_id>/`:

- `IFC_исходные_параметры.xlsx` / `.json` — сырой дамп всех свойств элементов IFC;
- `ifc_elements_output.json` — справочник по каждому элементу (characteristics / additionalCharacteristics);
- `ifc_raw_elements_grouped.json` — справочник групп элементов (для API подбора работ);
- `ДЛЯ_СМЕТЧИКА_исправленный.xlsx` — основная таблица элементов для выбора строк;
- `Дерево_проекта*.xlsx` — иерархическая группировка;
- `ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx` — группы с объёмами;
- `ОБЩИЙ_Финальный_перечень_работ.xlsx` — итоговый перечень работ с объёмом и стоимостью;
- `3Dmodel.glb` — 3D-модель (по запросу);
- `blueprint_painted.png`, `materials_colors.md` — размеченный чертёж и условные обозначения (PDF);
- `api_works_response.json` — сырые ответы API подбора работ (КР).

Файлы режима ИИ (АР): `Подобранные_таблицы_работ.json`, `Подобранные_работы.json`,
`Финальный_перечень_работ.xlsx` (+ `.json`), `analytic.json`, `period.json`,
`ПОС_глобальные_константы.json`, `ПЗ_глобальные_константы.json`,
`ВОР_ведомость_объемов_работ.json/.xlsx`, `ПД_характеристики_элементов.json/.xlsx`,
`pd_full.json`, `Параметры_подбора_элементов.json`, `Сравнение_ВОР_и_перечня.xlsx/.json`.

Файлы сравнения ведомостей (каталог `outputs/<comparison_id>/`): `models.json`,
`estimates.json`, `model_analytics.json`, `result.json`, `result.xlsx`,
`semantic_match_diagnostics.jsonl`.

---

## База данных

Сервис **не использует классическую СУБД**. Хранилища:

- **Состояние сессий** — JSON-файл `outputs/sessions.json` (потокобезопасно, с атомарной записью,
  резервным копированием при повреждении). Хранит метаданные сессий, запусков, списки файлов,
  прогресс, а также **сравнения** (записи с `session_type='comparison'`).
- **Результаты и файлы сессий** — файловая система: `outputs/<session_id>/` (+ `original/`,
  `run_<NNN>/`), загрузки — `uploads/<session_id>/`. Сравнения хранятся в
  `outputs/<comparison_id>/` (`models.json`, `estimates.json`, `result.json`, `result.xlsx`).
- **Справочные данные** — статические Excel/JSON-файлы в `data/` (перечни, коэффициенты, цены,
  МССК).
- **Внешние системы (по сети)**:
  - **Ollama** — локальная LLM-инференс;
  - **API цифрового справочника ТСН** `normativ.mgexp.org/digital-collection/...` — подбор работ в ЦС (КР);
  - **Keycloak** `normativ-idm.mgexp.org/...` — выдача/обновление Bearer-токена.

---

## API

Базовый префикс: `/ifc-vor`. Swagger UI: `/ifc-vor/docs`, спецификация: `/ifc-vor/apispec.json`.

Все вызовы API (кроме `/login`, `/logout`, `/docs`) **требуют авторизации**:
по cookie сессии (фронтенд) или возвращают `401` для `/ifc-vor/api/*`.

Обработка **асинхронная**: `upload_ifc` / `reference` сразу возвращают `sessionId`
и `status`, далее статус опрашивается через `GET /api/session/<id>/status`.

### Авторизация и здоровье
| Метод | Путь | Описание |
|-------|------|----------|
| GET/POST | `/ifc-vor/login` | Вход (HTML-форма) |
| GET | `/ifc-vor/logout` | Выход |
| GET | `/ifc-vor/api/health` | `{"status":"ok","timestamp":...}` |

### Загрузка и справочники
| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/ifc-vor/api/upload_ifc` | Загрузка IFC/PDF + `processingType` (CS/AI; легаси KR/AR), async. Ответ: `sessionId, status, sourceType, processingType, message` |
| POST | `/ifc-vor/api/reference` | Только построение JSON-справочников из IFC/PDF (202). Возвращает `Location` и `Retry-After` |
| GET | `/ifc-vor/api/session/<id>/reference` | Результат построения справочников (200 готово / 202 в процессе / 422 ошибка) |

### Сессии
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/ifc-vor/api/sessions` | Список всех сессий (+`total`) |
| GET | `/ifc-vor/api/session/<id>` | Полные данные сессии |
| GET | `/ifc-vor/api/session/<id>/status` | Краткий статус для поллинга (`status`, `realStatus`, `progress`, `progressMessage`, `hasResults`, `error`) |
| POST | `/ifc-vor/api/session/<id>/restore` | Данные для восстановления интерфейса |
| DELETE | `/ifc-vor/api/session/<id>` | Удаление сессии и файлов |

### Обработка (выбор строк и запуски)
| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/ifc-vor/api/session/<id>/select_rows` | Выбор строк (`rowIndices`, `allRows`, `rowTypes`, `rowMaterials`, `buildingHeight`, `processingType`; ИИ — также `floorHeight` и `globalConstants`) и запуск обработки |
| POST | `/ifc-vor/api/session/<id>/new_run` | Новый запуск обработки (без повтора извлечения из файла; ИИ — `floorHeight`, `globalConstants`) |
| GET | `/ifc-vor/api/session/<id>/runs` | Список запусков сессии |
| POST | `/ifc-vor/api/session/<id>/switch_run/<run_id>` | Переключение на другой запуск |
| POST | `/ifc-vor/api/session/<id>/filter_height` | Обновить высоту здания |

Статусы сессии: `ifc_processing` / `pdf_processing` / `selecting_rows` / `processing` /
`completed` / `error` (маппинг в краткий `status`: `processing`, `selecting_rows`, ...).

### Константы подбора работ и документы (режим ИИ)
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/ifc-vor/api/session/<id>/works_constants` | Схема констант подбора работ (`works_classification.json`) + значения из IFC/ПОС/ПЗ (`constants`, `detected`, `posDetected`, `pzDetected`, `vorStatus`, `pdStatus`, `vorCount`, `pdCount`) |
| POST | `/ifc-vor/api/session/<id>/upload_pos` | Загрузка PDF ПОС: фоновый разбор через LLM → `ПОС_глобальные_константы.json` |
| POST | `/ifc-vor/api/session/<id>/upload_pz` | Загрузка PDF пояснительной записки → `ПЗ_глобальные_константы.json` |
| POST | `/ifc-vor/api/session/<id>/upload_vor` | Загрузка PDF ВОР заказчика → `ВОР_ведомость_объемов_работ.json/.xlsx` |
| POST | `/ifc-vor/api/session/<id>/upload_pd` | Загрузка PDF проектной документации → `ПД_характеристики_элементов.json/.xlsx` |
| GET | `/ifc-vor/api/session/<id>/pd_full` | Полный разбор ПОС/ПЗ (≈40 параметров, ленивый фоновый запуск) → `pd_full.json` |
| GET | `/ifc-vor/api/session/<id>/doc_facts` | Тезисная сводка фактов ВОР/ПД (`doc_facts.facts_items`) |
| GET | `/ifc-vor/api/session/<id>/analytic` | Аналитическая справка по расчёту (`analytic.json`) |
| GET | `/ifc-vor/api/session/<id>/compare_vor` | Сравнение ВОР заказчика и сформированного перечня (`Сравнение_ВОР_и_перечня.xlsx/.json`; `?rebuild=1`) |
| GET | `/ifc-vor/api/session/<id>/tsn_mode` / `/pd_mode` | Переключатели «поправки ТСН: вкл/выкл» и «с учётом ПОС/ПЗ» (`?set=on|off`) |
| GET | `/ifc-vor/api/tsn/docs` | Список загруженных сборников ТСН |
| GET | `/ifc-vor/api/session/<id>/position_links` | Ссылки на позиции цифрового сборника по группам элементов (`position_links.json`, только ЦС) |

### Финальный JSON
| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/ifc-vor/api/session/<id>/run/<run_number>/build_final_json` | Запустить сборку финального JSON для запуска (`final_result_AR.json` / `final_result_KR.json`) |
| GET | `/ifc-vor/api/session/<id>/run/<run_number>/final_json/status` | Статус сборки финального JSON |
| GET | `/ifc-vor/api/session/<id>/run/<run_number>/final_json/result` | Результат сборки финального JSON |

### Файлы и просмотр
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/ifc-vor/api/session/<id>/preview` | Предпросмотр Excel (заголовки/строки) + карты МССК/материалов |
| GET | `/ifc-vor/api/session/<id>/preview_result/<filename>` | Предпросмотр произвольного xlsx/csv/json |
| GET | `/ifc-vor/api/session/<id>/blueprint_image` | PNG размеченного чертежа (для PDF) |
| GET | `/ifc-vor/api/session/<id>/materials_md` | Markdown условных обозначений материалов (для PDF) |
| GET | `/ifc-vor/api/session/<id>/download/<filename>` | Скачать файл результата |
| GET | `/ifc-vor/api/session/<id>/download_all` | Скачать все файлы сессии ZIP-архивом |
| POST | `/ifc-vor/api/session/<id>/3d_model` | Создать 3D-модель (GLB) по запросу (только IFC) |
| GET | `/ifc-vor/api/session/<id>/3d_model/status` | Статус генерации 3D-модели (`none/creating/ready/error`) |

### Сравнение ведомостей объёмов работ
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/ifc-vor/api/comparisons` | Список сохранённых сравнений (новые сверху) |
| POST | `/ifc-vor/api/comparisons` | Создать сравнение (multipart: несколько `files` — модели/чертежи одного формата, `processing_type` CS/AI; в ИИ — `pos`/`pz`/`vor`/`pd`), 202 |
| GET | `/ifc-vor/api/comparisons/<id>` | Состояние сравнения (статус, стадии, документы, параметры, предупреждения) |
| POST | `/ifc-vor/api/comparisons/<id>/estimates` | Загрузить ВОР заказчика (PDF/XLSX/XLS/XML/ZIP/GSFX/GGE) и запустить сравнение, 202 |
| POST | `/ifc-vor/api/comparisons/<id>/parameters` | Подтвердить параметры расчёта (ИИ): `overrides`, `use_documents`, `material_codes`, `use_tsn`, 202 |
| POST | `/ifc-vor/api/comparisons/<id>/saved-vor` | Переиспользовать уже загруженный PDF ВОР заказчика, 202 |
| POST | `/ifc-vor/api/comparisons/<id>/retry` | Повторить разбор/сравнение (`sourceIndex`, `quantityColumn`), 202 |
| GET | `/ifc-vor/api/comparisons/<id>/files/<filename>` | Скачать артефакт (`result.json`, `result.xlsx`, `models.json`, `estimates.json`, `semantic_match_diagnostics.jsonl`); `?download=1` |

Статусы сравнения: `models_processing` / `parameters_required` (ИИ) / `models_ready` /
`comparing` / `input_required` / `completed` / `error`.

### Форматы ответа
Все ответы — JSON в **camelCase** (Pydantic `CamelModel`). Ошибки — `{"detail": "..."}`
(коды 400/401/404/409/413/422/500). Загрузка файла — `multipart/form-data` (`file`, `processingType`);
выбор строк/запуски — JSON-тело.

---

## Пайплайн сервиса (со стороны пользователя)

### 1. Вход
Пользователь открывает веб-интерфейс (`/ifc-vor/`), проходит авторизацию.

### 2. Загрузка исходных данных — **две вариации**
Пользователь выбирает **режим обработки** (ЦС или ИИ) и загружает файл:

- **Вариант A — IFC-файл.** `SessionManager.process_ifc` сохраняет файл и запускает фоновую
  обработку: `zero_step` (+ `ifc_raw_dump` и `ifc_reference_builder`) извлекают элементы,
  формируют `ДЛЯ_СМЕТЧИКА_исправленный.xlsx` и JSON-справочники.
- **Вариант Б — PDF-чертёж.** `pdf_processor` запускает конвейер машинного зрения
  (`ai-blueprint-to-ifc`): детекция стен/layout/легенды (YOLO), анализ штриховок (DINO + VLM),
  формирование таких же Excel-таблиц, а также размеченных изображений и условных обозначений.

### 3. Просмотр и выбор элементов
После первой обработки сессия переходит в статус `selecting_rows`. Пользователь видит таблицу
`ДЛЯ_СМЕТЧИКА*` (превью), может указать для строк **часть здания** (Надземная/Подземная/Цоколь),
**материал**, задать **высоту здания** (или она берётся из IFC/расчёта) и выбрать строки.

### 4. Генерация перечня работ — **главная вариация пайплайна**

- **Режим ЦС (внешний справочник ТСН):**
  1. Выбранные строки фильтруются и **группируются** (`process_ifc_excel`).
  2. Листовые группы конвертируются в формат `ifc_raw_elements_grouped.json`
     (`buildingElementName`, `totalMeasure`, `characteristics`, `additionalCharacteristics`).
  3. Каждая группа отправляется **POST**-запросом в API цифрового справочника ТСН.
  4. Из ответов формируется `ОБЩИЙ_Финальный_перечень_работ.xlsx`
     (Шифр ТСН, наименование, ед. изм., объём; объём корректируется по `koefs.xlsx`,
     стоимость — по `price_cost.xlsx`).

- **Режим ИИ (локальный детерминированный подбор, `works_table_selector`):**
  1. Применение материалов пользователя, фильтрация выбранных строк.
  2. Группировка (МССК → Материал → Наименование) → деревья проекта и группы для сметчика.
  3. Детерминированный подбор таблиц работ (8 шагов по `data/algorithm.md`,
     справочники `ifc_to_collections.json`, `msck_elements_compact.json`,
     `tree_work_compact.json`, `works_classification.json`) → `Подобранные_таблицы_работ.json`.
  4. Сборка `final_result_AR.json` (`ifc_json_builder`).

В обоих режимах:
- дополнительно формируются `Дерево_проекта*.xlsx`, справочные JSON и сырой дамп IFC;
- в каждый **запуск** (`run_<NNN>`) сохраняются все промежуточные и финальные файлы.

### 5. Результаты
Пользователь просматривает статус, доступные файлы, может **скачать** их по отдельности или
архивом, а для IFC — запросить **3D-модель (GLB)**. При необходимости — запустить **новый запуск**
с другой выборкой строк или **переключиться** между запусками.

### 6. Сравнение ведомостей объёмов работ (отдельная вкладка)
1. Пользователь выбирает режим (ЦС/ИИ) и загружает **несколько моделей/чертежей** одного
   формата (в ИИ — дополнительно ПОС/ПЗ/ВОР/ПД). По каждой модели формируется ВОР тем же
   конвейером (в ИИ — с проверкой параметров расчёта).
2. После готовности ВОР загружается **ВОР заказчика** (PDF/XLSX/XLS/XML/ZIP/GSFX/GGE).
   PDF/XLSX разбираются локальным парсером, остальные — внешним сервисом.
3. Сервис сопоставляет работы и объёмы сформированного ВОР и ВОР заказчика
   (по шифру+единице, при отсутствии шифров — семантически и по конструкциям, с LLM-валидацией)
   и формирует отчёт `result.json` / `result.xlsx` (иерархия часть здания → раздел → подраздел
   → группа/работа; расхождения объёмов, наличие в одном из документов, оценка ИИ).
4. Результат отображается в интерфейсе (виды «Сравнение ВОР», «Объёмы», «Позиции по вердикту ИИ»,
   «Статистика по группам») и доступен для скачивания.

---

## Запуск и развертывание

Требования: Docker + Docker Compose, GPU NVIDIA (CUDA). Порт наружу — `6001` (внутри `6000`),
Ollama — `11450` (внутри `11434`).

```bash
make up        # собрать и запустить (docker-compose up -d --build + логи)
make down      # остановить
make restart   # перезапуск
make logs      # логи
make clean     # остановить и удалить volumes
```

Настройка — через `.env` (в `docker-compose.yml`, сервисы `ollama` и `web`):

- `OLLAMA_BASE_URL` — адрес Ollama;
- `OLLAMA_MODEL_NAME` — VLM для обработки чертежей (Qwen3-VL-8B);
- `NORMS_LLM_MODEL` — LLM для этапов ИИ (АР) (YandexGPT-5-Lite-8B);
- `DOCUMENTS_PATH`, `KOEFS_PATH`, `PRICE_COST_PATH` — справочники;
- `WORKS_API_URL`, `WORKS_API_TOKEN` — API справочника ТСН;
- `KEYCLOAK_TOKEN_URL`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET` — авто-токен Keycloak;
- `UPLOAD_FOLDER`, `OUTPUT_FOLDER`, `SESSIONS_FILE` — каталоги данных;
- `MAX_UPLOAD_MB` — лимит загрузки.

Проксирование Nginx — по пути `/ifc-vor/` на `http://127.0.0.1:6001` (`nginx.conf`).

---

## Технологический стек

Flask 3, gunicorn, Pydantic, flasgger (Swagger), ifcopenshell, pandas, openpyxl,
langchain/langgraph, ollama, ultralytics (YOLO), supervision, transformers, torch,
pymorphy3, fuzzywuzzy/fuzzysearch, requests, httpx, Flask-Login, Keycloak.
