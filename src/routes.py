# routes.py
import io
import os
import zipfile
import json
import pandas as pd
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user
from flask import render_template_string
from flask import (
    Blueprint, current_app, request,
    send_file, render_template,
    jsonify, redirect, Response
)
from src.services.session_manager import (
    SessionManager,
    POS_CONSTANTS_FILENAME,
    PZ_CONSTANTS_FILENAME,
    VOR_RESULT_JSON,
    PD_RESULT_JSON,
)
from src.services.mssk_lookup import get_mssk_code_map
from src.core.logger import setup_logger
from src.core.config import load_config
from src.core.modes import (
    to_pipeline_type, to_public_type,
    public_runs, public_session, mode_name,
)
from src.schemas import (
    ErrorResponse, SessionFull, SessionListResponse,
    UploadResponse, StatusResponse, DeleteResponse,
    PreviewResponse, RestoreResponse, HealthResponse,
    SelectRowsResponse, FilterHeightResponse, NewRunResponse,
    RunSwitchResponse, RunsListResponse, ReferenceAcceptedResponse,
    ReferenceBuildResponse, PositionLinksResponse,
    FinalJsonBuildResponse, FinalJsonStatusResponse, FinalJsonResultResponse,
    WorksConstantsResponse, PosUploadResponse,
)

logger = setup_logger(__name__)

bp = Blueprint("main", __name__, url_prefix="/ifc-vor")

# ---------- Авторизация ----------

USERS = {
    "admin": {
        'password': "admin54321",
        'role': "expert"
    },
    "test": {
        'password': "test",
        'role': "base"
    },
    "test1": {
        'password': "test1",
        'role': "base"
    }
}


class User(UserMixin):
    def __init__(self, username, role):
        self.id = username
        self.username = username
        self.role = role


login_manager = LoginManager()


@login_manager.user_loader
def load_user(username):
    if username in USERS:
        return User(username, USERS[username]['role'])
    return None


def init_login_manager():
    from flask import current_app
    if not hasattr(current_app, 'login_manager'):
        login_manager.init_app(current_app)


@bp.route('/login', methods=['GET', 'POST'])
def login():
    init_login_manager()
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username in USERS and USERS[username]['password'] == password:
            login_user(User(username, USERS[username]['role']))
            next_url = request.args.get('next', '/ifc-vor/')
            return f'<meta http-equiv="refresh" content="0; url={next_url}">'
        return '<h2>Неверный логин или пароль</h2><a href="/ifc-vor/login">Попробовать снова</a>'

    return render_template_string('''
    <html><body style="font-family: Arial; max-width: 400px; margin: 50px auto;">
    <h2>Вход в систему</h2>
    <form method="post">
        <input name="username" placeholder="Логин" required
               style="width: 100%; padding: 8px; margin: 5px 0;"><br>
        <input type="password" name="password" placeholder="Пароль" required
               style="width: 100%; padding: 8px; margin: 5px 0;"><br>
        <button type="submit" style="padding: 10px 20px; background: #007bff; color: white;
                border: none; cursor: pointer; width: 100%;">Войти</button>
    </form>
    </body></html>
    ''')


@bp.route('/logout')
def logout():
    logout_user()
    return '<h2>Вы вышли</h2><a href="/ifc-vor/login">Войти снова</a>'


@bp.before_request
def protect():
    if request.endpoint in ('main.login', 'main.logout', 'flasgger.apispec', 'flasgger.static'):
        return None

    init_login_manager()

    if not current_user.is_authenticated:
        if request.path.startswith('/ifc-vor/api/'):
            return jsonify({"detail": "Требуется авторизация"}), 401
        return redirect('/ifc-vor/login?next=' + request.url)


# ---------- Менеджер сессий ----------

_manager: SessionManager | None = None


def _get_manager() -> SessionManager:
    global _manager
    if _manager is None:
        cfg = load_config()
        _manager = SessionManager(
            upload_folder=current_app.config["UPLOAD_FOLDER"],
            output_folder=current_app.config["OUTPUT_FOLDER"],
            sessions_file=os.path.abspath(current_app.config["SESSIONS_FILE"]),
            perechen_xlsx=cfg.DOCUMENTS_PATH,
            koefs_xlsx=cfg.KOEFS_PATH
        )
        logger.info("SessionManager инициализирован")
    return _manager


def _ok(schema_instance, status: int = 200) -> Response:
    return Response(
        schema_instance.model_dump_json(exclude_none=False, by_alias=True),
        status=status,
        mimetype="application/json",
    )


def _err(schema_instance, status: int) -> Response:
    return Response(
        schema_instance.model_dump_json(by_alias=True),
        status=status,
        mimetype="application/json",
    )


# ------- HTML -------

@bp.route("/", methods=["GET"])
def index():
    return render_template("index.html")


# ------- API -------

@bp.route("/api/health", methods=["GET"])
def health_check():
    """
    Проверка работоспособности сервиса.
    ---
    tags:
      - health
    responses:
      200:
        description: Сервис работает
        schema:
          type: object
          properties:
            status:
              type: string
              example: ok
            timestamp:
              type: string
              example: "2026-06-17T12:00:00"
    """
    return _ok(HealthResponse(
        status="ok",
        timestamp=pd.Timestamp.now().isoformat()
    ))


@bp.route("/api/upload_ifc", methods=["POST"])
def upload_ifc():
    """
    Загрузка файла (IFC или PDF) и запуск фоновой обработки.
    ---
    tags:
      - upload
    consumes:
      - multipart/form-data
    parameters:
      - name: file
        in: formData
        type: file
        required: true
        description: IFC-файл (.ifc) или PDF-чертёж (.pdf), максимум 500 МБ
      - name: processingType
        in: formData
        type: string
        required: false
        default: "CS"
        description: Режим обработки — CS (ЦС, цифровой сборник; подбор через API ТСН)
          или AI (ИИ, искусственный интеллект; подбор через LLM). Легаси-коды
          KR/AR также принимаются и эквивалентны CS/AI
      - name: posFile
        in: formData
        type: file
        required: false
        description: Дополнительный PDF-файл ПОС (только для режима AI/ИИ) — из него
          извлекаются глобальные константы подбора работ
      - name: pzFile
        in: formData
        type: file
        required: false
        description: Дополнительный PDF-файл пояснительной записки (только для
          режима AI/ИИ) — разбирается тем же пайплайном, что и ПОС; из него
          извлекаются глобальные константы, не найденные в IFC и ПОС
      - name: vorFile
        in: formData
        type: file
        required: false
        description: Дополнительный PDF-файл ВОР (ведомость объёмов работ, только
          для режима AI/ИИ) — из него извлекаются группы элементов, объёмы работ
          и единицы измерения
      - name: pdFile
        in: formData
        type: file
        required: false
        description: Дополнительный PDF-файл ПД (проектная документация, только
          для режима AI/ИИ) — разбирается только текстовая часть; извлекаются
          характеристики элементов (класс бетона, прочность, пластичность,
          морозостойкость, водонепроницаемость)
      - name: autoLegend
        in: formData
        type: string
        required: false
        default: "true"
        description: >
          PDF: автоматически определять легенду чертежа. "true" (по умолчанию) —
          легенда определяется пайплайном как раньше; "false" — используется
          ручная легенда из параметра legend
      - name: legend
        in: formData
        type: string
        required: false
        description: >
          PDF, если autoLegend=false: JSON вида
          {"items":[{"label":"...", "image":"data:image/png;base64,...",
          "type":"image|pdf", "filename":"..."}]}. Поле image — data URL
          (base64) картинки или PDF-файла легенды
    responses:
      200:
        description: Файл принят, сессия создана, обработка запущена
        schema:
          type: object
          properties:
            sessionId:
              type: string
              example: "3fa85f64-5717-4562-b3fc-2c963f66afa6"
            status:
              type: string
              example: ifc_processing
            sourceType:
              type: string
              example: "ifc"
            message:
              type: string
              example: "IFC файл принят, начата обработка"
      400:
        description: Ошибка валидации
        schema:
          type: object
          properties:
            detail:
              type: string
      500:
        description: Внутренняя ошибка сервера
        schema:
          type: object
          properties:
            detail:
              type: string
    """
    if "file" not in request.files:
        return _err(ErrorResponse(detail="Файл не передан"), 400)

    f = request.files["file"]
    if not f.filename:
        return _err(ErrorResponse(detail="Пустое имя файла"), 400)

    filename_lower = f.filename.lower()
    is_ifc = filename_lower.endswith(".ifc")
    is_pdf = filename_lower.endswith(".pdf")

    if not is_ifc and not is_pdf:
        return _err(ErrorResponse(detail="Поддерживаются файлы .ifc и .pdf"), 400)

    f.seek(0, 2)
    size = f.tell()
    f.seek(0)

    max_size = 500 * 1024 * 1024
    if size > max_size:
        return _err(ErrorResponse(detail=f"Файл слишком большой. Максимум {max_size // (1024*1024)} МБ"), 413)

    # Получаем режим обработки (ЦС или ИИ; легаси-коды KR/AR принимаются
    # как синонимы и приводятся к внутренним кодам пайплайна)
    processing_type = to_pipeline_type(request.form.get("processingType"))

    logger.info(
        f"Загрузка файла {f.filename}, режим обработки: "
        f"{mode_name(processing_type)} ({to_public_type(processing_type)})"
    )

    try:
        if is_ifc:
            result = _get_manager().process_ifc(f, f.filename, processing_type)
            result["source_type"] = "ifc"
        else:
            # PDF: разбираем параметры легенды чертежа.
            # autoLegend="true" (по умолчанию) — легенда определяется
            # пайплайном как раньше. autoLegend="false" — используется
            # ручная легенда из параметра legend (JSON-строка).
            auto_legend = request.form.get("autoLegend", "true").strip().lower() != "false"
            manual_legend = None
            if not auto_legend:
                legend_raw = request.form.get("legend", "")
                if legend_raw:
                    try:
                        manual_legend = json.loads(legend_raw)
                    except json.JSONDecodeError:
                        return _err(ErrorResponse(detail="Некорректный формат легенды (ожидается JSON)"), 400)
                if manual_legend is None:
                    manual_legend = {"items": []}
                if not isinstance(manual_legend, dict):
                    return _err(ErrorResponse(detail="Легенда должна быть объектом"), 400)
                items = manual_legend.get("items")
                if not isinstance(items, list):
                    return _err(ErrorResponse(detail="Поле legend.items должно быть массивом"), 400)
                if not items:
                    return _err(ErrorResponse(detail="Легенда пуста: добавьте хотя бы один элемент или включите авто-определение"), 400)

                for idx, it in enumerate(items):
                    if not isinstance(it, dict):
                        return _err(ErrorResponse(detail=f"Элемент легенды #{idx + 1} должен быть объектом"), 400)
                    label = (it.get("label") or "").strip() if isinstance(it.get("label"), str) else ""
                    image = it.get("image")
                    if not label:
                        return _err(ErrorResponse(detail=f"У элемента легенды #{idx + 1} не указана подпись"), 400)
                    if not image or not isinstance(image, str):
                        return _err(ErrorResponse(detail=f"У элемента легенды #{idx + 1} отсутствует картинка"), 400)
                    if not image.startswith("data:"):
                        return _err(ErrorResponse(detail=f"Картинка элемента легенды #{idx + 1} должна быть data URL (base64)"), 400)
                    it["label"] = label  # нормализовали — убрали пробелы
                logger.info(
                    f"PDF: ручная легенда, элементов: {len(items)}"
                )
            else:
                logger.info("PDF: авто-определение легенды")

            result = _get_manager().process_pdf(
                f, f.filename, processing_type, manual_legend=manual_legend
            )
            result["source_type"] = "pdf"

        # Дополнительный файл ПОС (режим ИИ): разбирается в фоне,
        # константы сохраняются в ПОС_глобальные_константы.json
        pos_file = request.files.get("posFile")
        if pos_file and pos_file.filename and processing_type == "AR":
            try:
                _get_manager().upload_pos(result["session_id"], pos_file, pos_file.filename)
            except ValueError as e:
                logger.warning(f"Файл ПОС не принят: {e}")

        # Дополнительный файл пояснительной записки ПЗ (режим ИИ): разбирается
        # тем же пайплайном, что и ПОС, константы сохраняются в
        # ПЗ_глобальные_константы.json
        pz_file = request.files.get("pzFile")
        if pz_file and pz_file.filename and processing_type == "AR":
            try:
                _get_manager().upload_pz(result["session_id"], pz_file, pz_file.filename)
            except ValueError as e:
                logger.warning(f"Файл ПЗ не принят: {e}")

        # Дополнительный файл ВОР (режим ИИ): ведомость объёмов работ,
        # разбирается в фоне (doc_pdf_parser.parse_vor), результат —
        # ВОР_ведомость_объемов_работ.json/.xlsx
        vor_file = request.files.get("vorFile")
        if vor_file and vor_file.filename and processing_type == "AR":
            try:
                _get_manager().upload_vor(result["session_id"], vor_file, vor_file.filename)
            except ValueError as e:
                logger.warning(f"Файл ВОР не принят: {e}")

        # Дополнительный файл ПД (режим ИИ): проектная документация,
        # разбирается в фоне (doc_pdf_parser.parse_pd) — только текстовая часть,
        # результат — ПД_характеристики_элементов.json/.xlsx
        pd_file = request.files.get("pdFile")
        if pd_file and pd_file.filename and processing_type == "AR":
            try:
                _get_manager().upload_pd(result["session_id"], pd_file, pd_file.filename)
            except ValueError as e:
                logger.warning(f"Файл ПД не принят: {e}")

        # В ответе клиенту отдаём публичный код режима (CS/AI)
        if "processing_type" in result:
            result["processing_type"] = to_public_type(result["processing_type"])
        return _ok(UploadResponse(**result))
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 400)
    except Exception as e:
        logger.error(f"Ошибка загрузки файла: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Внутренняя ошибка: {str(e)}"), 500)


@bp.route("/api/reference", methods=["POST"])
def build_reference():
    """
    Запуск построения JSON-справочников из IFC или PDF.
    ---
    tags:
      - reference
    consumes:
      - multipart/form-data
    parameters:
      - name: file
        in: formData
        type: file
        required: true
        description: IFC-файл (.ifc) или PDF-чертёж (.pdf), максимум 500 МБ
      - name: processingType
        in: formData
        type: string
        required: false
        default: "CS"
        description: Режим обработки — CS (ЦС, цифровой сборник) или AI (ИИ,
          искусственный интеллект); легаси-коды KR/AR также принимаются
    responses:
      202:
        description: Файл принят, построение запущено
        schema:
          type: object
          properties:
            sessionId:
              type: string
            sourceType:
              type: string
              enum: [ifc, pdf]
            status:
              type: string
              example: reference_processing
            message:
              type: string
      400:
        description: Файл отсутствует или имеет неподдерживаемый формат
      413:
        description: Файл превышает допустимый размер
      500:
        description: Внутренняя ошибка сервера
    """
    if "file" not in request.files:
        return _err(ErrorResponse(detail="Файл не передан"), 400)

    uploaded_file = request.files["file"]
    if not uploaded_file.filename:
        return _err(ErrorResponse(detail="Пустое имя файла"), 400)

    extension = os.path.splitext(uploaded_file.filename)[1].lower()
    if extension not in (".ifc", ".pdf"):
        return _err(ErrorResponse(detail="Поддерживаются файлы .ifc и .pdf"), 400)

    uploaded_file.seek(0, 2)
    size = uploaded_file.tell()
    uploaded_file.seek(0)

    max_size = 500 * 1024 * 1024
    if size > max_size:
        return _err(
            ErrorResponse(
                detail=f"Файл слишком большой. Максимум {max_size // (1024 * 1024)} МБ"
            ),
            413,
        )

    # Получаем режим обработки для справочников (легаси-коды KR/AR
    # принимаются как синонимы CS/AI)
    processing_type = to_pipeline_type(request.form.get("processingType"))

    logger.info(
        f"Запуск построения справочников: {uploaded_file.filename}, режим: "
        f"{mode_name(processing_type)} ({to_public_type(processing_type)})"
    )

    try:
        result = _get_manager().build_reference(
            uploaded_file, uploaded_file.filename, processing_type
        )
        response = _ok(ReferenceAcceptedResponse(**result), status=202)
        response.headers["Location"] = (
            f"/ifc-vor/api/session/{result['session_id']}/reference"
        )
        response.headers["Retry-After"] = "2"
        return response
    except ValueError as exc:
        return _err(ErrorResponse(detail=str(exc)), 400)
    except Exception as exc:
        logger.error(f"Ошибка запуска построения справочника: {exc}", exc_info=True)
        return _err(ErrorResponse(detail=f"Внутренняя ошибка: {exc}"), 500)


@bp.route("/api/session/<session_id>/reference", methods=["GET"])
def get_reference_result(session_id: str):
    """
    Получение результата построения JSON-справочников.
    ---
    tags:
      - reference
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
    responses:
      200:
        description: Оба JSON-справочника готовы
        schema:
          type: object
          properties:
            sessionId:
              type: string
            sourceType:
              type: string
            ifcElementsOutput:
              type: array
              items:
                type: object
            ifcRawElementsGrouped:
              type: array
              items:
                type: object
      202:
        description: Обработка ещё выполняется
      404:
        description: Сессия не найдена
      409:
        description: Сессия создана не справочной ручкой
      422:
        description: Ошибка фоновой обработки
    """
    if not session_id or len(session_id) < 8:
        return _err(ErrorResponse(detail="Некорректный ID сессии"), 400)

    manager = _get_manager()
    session = manager.get(session_id)
    if not session:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    if not session.get("is_reference_session"):
        return _err(
            ErrorResponse(detail="Сессия не относится к построению справочников"),
            409,
        )
    if session.get("status") == "error":
        return _err(
            ErrorResponse(
                detail=session.get("error") or "Ошибка построения справочников"
            ),
            422,
        )

    try:
        result = manager.get_reference_result(session_id)
        if result is None:
            response = _ok(
                ReferenceAcceptedResponse(
                    session_id=session_id,
                    source_type=session.get("source_type", ""),
                    status=session.get("status", "reference_processing"),
                    message=session.get(
                        "progress_message",
                        "Формирование JSON-справочников выполняется",
                    ),
                ),
                status=202,
            )
            response.headers["Retry-After"] = "2"
            return response
        return _ok(ReferenceBuildResponse(**result))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"Не удалось получить JSON-справочники: {exc}", exc_info=True)
        return _err(ErrorResponse(detail=str(exc)), 422)
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)


@bp.route("/api/sessions", methods=["GET"])
def list_sessions():
    """
    Список всех сессий обработки.
    ---
    tags:
      - sessions
    responses:
      200:
        description: Массив сессий
        schema:
          type: object
          properties:
            sessions:
              type: array
              items:
                $ref: '#/definitions/SessionFull'
            total:
              type: integer
    definitions:
      SessionFile:
        type: object
        properties:
          path:
            type: string
          filename:
            type: string
          size:
            type: integer
          downloadUrl:
            type: string
      SessionFull:
        type: object
        properties:
          sessionId:
            type: string
          createdAt:
            type: string
          status:
            type: string
          ifcFileName:
            type: string
          excelFileName:
            type: string
          buildingHeight:
            type: number
          files:
            type: array
            items:
              $ref: '#/definitions/SessionFile'
          progress:
            type: integer
          progressMessage:
            type: string
          hasResults:
            type: boolean
          error:
            type: string
    """
    try:
        raw = _get_manager().list_sessions()
        # processing_type сессий и запусков приводим к публичным кодам CS/AI
        sessions = [SessionFull(**public_session(s)) for s in raw]
        return _ok(SessionListResponse(sessions=sessions, total=len(sessions)))
    except Exception as e:
        logger.error(f"Ошибка списка сессий: {e}", exc_info=True)
        return _err(ErrorResponse(detail=str(e)), 500)


@bp.route("/api/session/<session_id>/status", methods=["GET"])
def get_status(session_id: str):
    """
    Краткий статус сессии (для поллинга).
    ---
    tags:
      - sessions
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Статус сессии
        schema:
          type: object
          properties:
            sessionId:
              type: string
            status:
              type: string
              enum: [ifc_processing, selecting_rows, processing, completed, error]
            realStatus:
              type: string
            progress:
              type: integer
            progressMessage:
              type: string
            error:
              type: string
            hasResults:
              type: boolean
      400:
        description: Некорректный ID сессии
      404:
        description: Сессия не найдена
    """
    if not session_id or len(session_id) < 8:
        return _err(ErrorResponse(detail="Некорректный ID сессии"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    status_mapping = {
        "ifc_processing": "processing",
        "pdf_processing": "processing",
        "reference_processing": "processing",
        "ifc_processed": "selecting_rows",
        "selecting_rows": "selecting_rows",
        "filtering_type": "filtering_type",
        "filtering_height": "filtering_height",
        "processing": "processing",
        "completed": "completed",
        "error": "error",
    }

    return _ok(StatusResponse(
        session_id=s["session_id"],
        status=status_mapping.get(s["status"], s["status"]),
        real_status=s["status"],
        progress=s.get("progress", 0),
        progress_message=s.get("progress_message", ""),
        error=s.get("error"),
        has_results=s.get("has_results", False),
    ))


@bp.route("/api/session/<session_id>", methods=["GET"])
def get_session(session_id: str):
    """
    Полные данные сессии.
    ---
    tags:
      - sessions
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Данные сессии
        schema:
          $ref: '#/definitions/SessionFull'
      400:
        description: Некорректный ID
      404:
        description: Сессия не найдена
    """
    if not session_id or len(session_id) < 8:
        return _err(ErrorResponse(detail="Некорректный ID сессии"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    # processing_type сессии и запусков — в публичных кодах CS/AI
    return _ok(SessionFull(**public_session(s)))


@bp.route("/api/session/<session_id>", methods=["DELETE"])
def delete_session(session_id: str):
    """
    Удаление сессии и всех связанных файлов.
    ---
    tags:
      - sessions
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Сессия удалена
        schema:
          type: object
          properties:
            deleted:
              type: boolean
            sessionId:
              type: string
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    ok = _get_manager().delete(session_id)
    if not ok:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    return _ok(DeleteResponse(deleted=True, session_id=session_id))


@bp.route("/api/session/<session_id>/restore", methods=["POST"])
def restore_session(session_id: str):
    """
    Восстановить данные сессии для продолжения работы.
    ---
    tags:
      - sessions
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Данные для восстановления интерфейса
        schema:
          type: object
          properties:
            sessionId:
              type: string
            status:
              type: string
            progress:
              type: integer
            progressMessage:
              type: string
            hasResults:
              type: boolean
            buildingHeight:
              type: number
            selectedRowsCount:
              type: integer
            files:
              type: array
              items:
                $ref: '#/definitions/SessionFile'
            processingType:
              type: string
              example: "CS"
      400:
        description: Некорректный ID
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    # Получаем файлы текущего запуска
    current_files = []
    current_run_id = s.get("current_run_id")
    current_run_floor_height = None
    if current_run_id:
        runs = s.get("runs", [])
        for run in runs:
            if run.get("run_id") == current_run_id:
                current_files = run.get("files", [])
                current_run_floor_height = run.get("floor_height")
                break

    if not current_files:
        current_files = s.get("files", [])

    return _ok(RestoreResponse(
        session_id=s["session_id"],
        status=s["status"],
        progress=s.get("progress", 0),
        progress_message=s.get("progress_message", ""),
        has_results=s.get("has_results", False),
        files=current_files,
        construction_types=s.get("construction_types", {}),
        building_height=s.get("building_height"),
        floor_height=current_run_floor_height,
        selected_rows_count=len(s.get("selected_rows", []) or []),
        source_type=s.get("source_type"),
        runs=public_runs(s.get("runs", [])),
        current_run_id=s.get("current_run_id"),
        processing_type=to_public_type(s.get("processing_type")),
    ))


@bp.route("/api/session/<session_id>/preview", methods=["GET"])
def preview_excel(session_id: str):
    """
    Предпросмотр Excel-таблицы после обработки IFC (для выбора строк).
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Заголовки и строки таблицы
        schema:
          type: object
          properties:
            headers:
              type: array
              items:
                type: string
            rows:
              type: array
              items:
                type: array
                items:
                  type: string
            totalRows:
              type: integer
            savedTypes:
              type: object
            processingType:
              type: string
      404:
        description: Сессия или файл не найдены
      500:
        description: Ошибка чтения файла
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    # Ищем Excel в original/ директории — ТОЛЬКО исправленный или объединенный
    excel_path = None
    session_dir = os.path.join(_get_manager().output_folder, session_id)
    original_dir = os.path.join(session_dir, 'original')
    search_dir = original_dir if os.path.exists(original_dir) else session_dir

    if os.path.exists(search_dir):
        for f in os.listdir(search_dir):
            if f.endswith('.xlsx'):
                # ПРОПУСКАЕМ сгруппированные и сокращённые
                if 'сгруппированный' in f.lower() or 'сокращенный' in f.lower():
                    continue
                if 'ДЛЯ_СМЕТЧИКА' in f:
                    excel_path = os.path.join(search_dir, f)
                    logger.info(f"Найден Excel для preview: {f}")
                    break

    # Fallback: старый путь из сессии
    if not excel_path or not os.path.exists(excel_path):
        excel_path = s.get("excel_file_path")
        logger.info(f"Использую fallback Excel: {excel_path}")

    if not excel_path or not os.path.exists(excel_path):
        return _err(ErrorResponse(detail="Excel файл не найден"), 404)

    try:
        if excel_path.endswith(".csv"):
            df = pd.read_csv(excel_path)
        else:
            df = pd.read_excel(excel_path)
    except Exception as e:
        return _err(ErrorResponse(detail=f"Ошибка чтения файла: {str(e)}"), 500)

    headers = df.columns.tolist()
    rows = df.fillna("-").astype(str).values.tolist()
    saved_types = s.get("construction_types", {})

    # Проверяем наличие чертежа и условных обозначений
    has_blueprint_image = False
    has_materials_md = False
    has_ifc_elements_json = False
    has_ifc_grouped_json = False

    source_type = s.get("source_type")

    for f in os.listdir(search_dir) if os.path.exists(search_dir) else []:
        if f.startswith("blueprint_painted") and f.endswith(".png"):
            has_blueprint_image = True
        if f == "materials_colors.md":
            has_materials_md = True

    # JSON/XLSX-справочники лежат в корне директории сессии.
    # Показываем их наличие только для режима ЦС: в ИИ цифровой сборник не
    # используется, справочники не строятся и кнопки не отображаются.
    if os.path.exists(session_dir) and s.get("processing_type", "KR") == "KR":
        has_ifc_elements_json = os.path.exists(os.path.join(session_dir, "ifc_elements_output.json"))
        has_ifc_grouped_json = os.path.exists(os.path.join(session_dir, "ifc_raw_elements_grouped.json"))

    # После чтения основного Excel
    building_height = None
    floor_height = None
    height_sheet = None
    try:
        xls = pd.ExcelFile(excel_path)

        # Ищем лист с высотой
        height_sheet = None
        for sheet_name in xls.sheet_names:
            if 'высота' in sheet_name.lower():
                height_sheet = sheet_name
                break

        if height_sheet:
            df_height = pd.read_excel(excel_path, sheet_name=height_sheet)
            logger.info(f"Лист высоты: {height_sheet}, колонки: {df_height.columns.tolist()}")

            # Ищем значение высоты
            for col in df_height.columns:
                col_lower = str(col).lower()
                if 'значение' in col_lower or 'высота' in col_lower or 'height' in col_lower:
                    for _, row_data in df_height.iterrows():
                        try:
                            val = float(row_data[col])
                            if val > 0:
                                building_height = val
                                logger.info(f"Найдена высота: {building_height} м")
                                break
                        except (ValueError, TypeError):
                            pass
                    if building_height:
                        break

            # Если не нашли по колонкам — ищем в первой строке
            if not building_height:
                for _, row_data in df_height.iterrows():
                    for col in df_height.columns:
                        try:
                            val = float(row_data[col])
                            if 1 < val < 10000:
                                building_height = val
                                logger.info(f"Найдена высота (по значению): {building_height} м")
                                break
                        except (ValueError, TypeError):
                            pass
                    if building_height:
                        break
    except Exception as e:
        logger.warning(f"Не удалось прочитать высоту: {e}")

    # Высота основного этажа (режим ИИ) — строка «Высота основного этажа» листа высот
    if height_sheet:
        try:
            df_floor = pd.read_excel(excel_path, sheet_name=height_sheet)
            for _, row_data in df_floor.iterrows():
                row_values = [str(v).lower() for v in row_data.values]
                if not any("основного этажа" in v for v in row_values):
                    continue
                for v in row_data.values:
                    try:
                        val = float(v)
                    except (ValueError, TypeError):
                        continue
                    if 0 < val < 10000:
                        floor_height = val
                        break
                if floor_height is not None:
                    break
        except Exception as e:
            logger.warning(f"Не удалось прочитать высоту этажа: {e}")

    # Карта материалов для группировки превью по материалу
    materials_group_map = None
    if s.get("processing_type", "KR") in ("AR", "KR"):
        try:
            from src.services.materials_lookup import (
                build_materials_lookup,
                resolve_material_group_with_code,
                material_code_options,
            )
            mat_lookup, _ = build_materials_lookup()
            raw_map = _get_manager()._load_material_layer_map(session_id)
            if raw_map and 'GlobalId' in df.columns:
                materials_group_map = {}
                for gid in df['GlobalId'].dropna().astype(str).unique():
                    raw_val = raw_map.get(gid, '')
                    name, order, code = resolve_material_group_with_code(raw_val, mat_lookup)
                    # Неполный код МССК материала (напр. «СТ 00 15» —
                    # «Железобетон»): в превью строка группы подсвечивается,
                    # пользователь уточняет материал (сборный/монолитный).
                    options = material_code_options(code) if code else []
                    materials_group_map[gid] = {
                        "name": name,
                        "order": order,
                        "code": code,
                        "incomplete": len(options) >= 2,
                        "options": options,
                    }
        except Exception as e:
            logger.warning(f"Не удалось построить карту материалов для превью: {e}", exc_info=True)
            materials_group_map = None

    return _ok(PreviewResponse(
        headers=headers,
        rows=rows,
        total_rows=len(df),
        saved_types=saved_types,
        building_height=building_height,
        floor_height=floor_height,
        source_type=source_type,
        has_blueprint_image=has_blueprint_image,
        has_materials_md=has_materials_md,
        has_ifc_elements_json=has_ifc_elements_json,
        has_ifc_grouped_json=has_ifc_grouped_json,
        processing_type=to_public_type(s.get("processing_type")),
        mssk_code_map=get_mssk_code_map(),
        materials_group_map=materials_group_map,
    ))


@bp.route("/api/session/<session_id>/works_constants", methods=["GET"])
def get_works_constants(session_id: str):
    """
    Схема констант подбора работ (режим ИИ (AI), ранее АР) + значения,
    определённые по модели IFC.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Список констант (name, title, values, default) и
          определённые из IFC значения (высота здания, высота этажа)
        schema:
          type: object
          properties:
            sessionId:
              type: string
            processingType:
              type: string
            constants:
              type: array
              items:
                type: object
            detected:
              type: object
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    from src.services.works_table_selector import get_constants_schema

    detected: dict = {}

    # Значения высот из IFC_глобальные_константы.json (формируется zero_step
    # по отметкам элементов IFC; основной источник).
    ifc_constants_path = os.path.join(
        _get_manager().output_folder, session_id, "IFC_глобальные_константы.json"
    )
    if os.path.isfile(ifc_constants_path):
        try:
            with open(ifc_constants_path, "r", encoding="utf-8") as f:
                ifc_data = json.load(f)
            ifc_detected = {
                k: v.get("value")
                for k, v in (ifc_data.get("constants") or {}).items()
                if isinstance(v, dict)
            }
            for key in ("building_height_m", "floor_height", "total_height_m",
                        "top_elevation_m", "min_ground_elevation_m"):
                try:
                    val = float(ifc_detected.get(key))
                    # min_ground_elevation_m может быть 0 (отметка земли)
                    if val > 0 or (key == "min_ground_elevation_m" and val >= 0):
                        # В UI константа высоты этажа называется floor_height_m
                        detected["floor_height_m" if key == "floor_height" else key] = round(val, 3)
                except (TypeError, ValueError):
                    continue
            # Строковая константа «Этажность здания» (одноэтажное/многоэтажное),
            # определённая по колонке «Этаж» IFC — передаётся как есть.
            # Для старых сессий (без константы в IFC_глобальные_константы.json) —
            # fallback по Excel полной модели.
            storeys_type = ifc_detected.get("building_storeys_type")
            if not storeys_type:
                try:
                    from src.services.zero_step import detect_storeys_type_from_session

                    storeys_type = detect_storeys_type_from_session(
                        os.path.join(_get_manager().output_folder, session_id)
                    )
                except Exception as e:
                    logger.warning(f"Не удалось определить этажность здания: {e}")
            if storeys_type:
                detected["building_storeys_type"] = str(storeys_type)
        except Exception as e:
            logger.warning(f"Не удалось прочитать константы IFC: {e}")

    # Fallback: значения высот из листа 'Высота_здания' исходного Excel сессии
    # (для сессий, обработанных до появления IFC_глобальные_константы.json).
    if not detected:
        excel_path = _get_manager()._get_original_excel_path(session_id)
        if excel_path and os.path.exists(excel_path):
            try:
                df_height = pd.read_excel(excel_path, sheet_name="Высота_здания")
                label_col = None
                value_col = None
                for col in df_height.columns:
                    cl = str(col).lower()
                    if label_col is None and "параметр" in cl:
                        label_col = col
                    if value_col is None and "значение_м" in cl and not cl.endswith("_мм"):
                        value_col = col
                if label_col is not None and value_col is not None:
                    labels_map = {
                        "высота надземной части": "building_height_m",
                        "высота основного этажа": "floor_height_m",
                        "общая высота здания": "total_height_m",
                        "максимальная отметка надземной части": "top_elevation_m",
                    }
                    for _, row_data in df_height.iterrows():
                        label = str(row_data.get(label_col, "")).strip().lower()
                        key = labels_map.get(label)
                        if not key:
                            continue
                        try:
                            val = float(row_data.get(value_col))
                            if val > 0:
                                detected[key] = round(val, 3)
                        except (ValueError, TypeError):
                            continue
            except Exception as e:
                logger.warning(f"Не удалось прочитать высоты из Excel: {e}")

    # Значения констант из файла ПОС (ПОС_глобальные_константы.json в сессии)
    pos_detected: dict = {}
    pos_ready = False
    pos_file_name = None
    pos_constants_path = os.path.join(
        _get_manager().output_folder, session_id, POS_CONSTANTS_FILENAME
    )
    if os.path.isfile(pos_constants_path):
        try:
            with open(pos_constants_path, "r", encoding="utf-8") as f:
                pos_data = json.load(f)
            pos_detected = {
                k: v.get("value")
                for k, v in (pos_data.get("constants") or {}).items()
                if isinstance(v, dict) and v.get("value")
            }
            pos_ready = True
            pos_file_name = pos_data.get("file_name")
        except Exception as e:
            logger.warning(f"Не удалось прочитать константы ПОС: {e}")

    # Значения констант из файла пояснительной записки
    # (ПЗ_глобальные_константы.json в сессии)
    pz_detected: dict = {}
    pz_ready = False
    pz_file_name = None
    pz_constants_path = os.path.join(
        _get_manager().output_folder, session_id, PZ_CONSTANTS_FILENAME
    )
    if os.path.isfile(pz_constants_path):
        try:
            with open(pz_constants_path, "r", encoding="utf-8") as f:
                pz_data = json.load(f)
            pz_detected = {
                k: v.get("value")
                for k, v in (pz_data.get("constants") or {}).items()
                if isinstance(v, dict) and v.get("value")
            }
            pz_ready = True
            pz_file_name = pz_data.get("file_name")
        except Exception as e:
            logger.warning(f"Не удалось прочитать константы ПЗ: {e}")

    # Результаты разбора ВОР (ведомость объёмов работ, ВОР_ведомость_объемов_работ.json)
    vor_ready = False
    vor_file_name = None
    vor_count = None
    vor_result_path = os.path.join(
        _get_manager().output_folder, session_id, VOR_RESULT_JSON
    )
    if os.path.isfile(vor_result_path):
        try:
            with open(vor_result_path, "r", encoding="utf-8") as f:
                vor_data = json.load(f)
            vor_ready = True
            vor_file_name = vor_data.get("file_name")
            vor_count = len(vor_data.get("items") or [])
        except Exception as e:
            logger.warning(f"Не удалось прочитать результат ВОР: {e}")

    # Результаты разбора ПД (проектная документация, ПД_характеристики_элементов.json)
    pd_ready = False
    pd_file_name = None
    pd_count = None
    pd_result_path = os.path.join(
        _get_manager().output_folder, session_id, PD_RESULT_JSON
    )
    if os.path.isfile(pd_result_path):
        try:
            with open(pd_result_path, "r", encoding="utf-8") as f:
                pd_data = json.load(f)
            pd_ready = True
            pd_file_name = pd_data.get("file_name")
            pd_count = len(pd_data.get("items") or [])
        except Exception as e:
            logger.warning(f"Не удалось прочитать результат ПД: {e}")

    return _ok(WorksConstantsResponse(
        session_id=session_id,
        processing_type=to_public_type(s.get("processing_type")),
        constants=get_constants_schema(),
        detected=detected,
        pos_detected=pos_detected,
        pos_ready=pos_ready,
        pos_file_name=pos_file_name,
        pos_status=s.get("pos_status"),
        pz_detected=pz_detected,
        pz_ready=pz_ready,
        pz_file_name=pz_file_name,
        pz_status=s.get("pz_status"),
        vor_ready=vor_ready,
        vor_file_name=vor_file_name,
        vor_status=s.get("vor_status"),
        vor_count=vor_count,
        pd_ready=pd_ready,
        pd_file_name=pd_file_name,
        pd_status=s.get("pd_status"),
        pd_count=pd_count,
    ))


@bp.route("/api/session/<session_id>/upload_pos", methods=["POST"])
def upload_pos(session_id: str):
    """
    Загрузка PDF-файла ПОС (проект организации строительства) для сессии
    (режим ИИ (AI), ранее АР).
    ---
    tags:
      - upload
    consumes:
      - multipart/form-data
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
      - name: file
        in: formData
        type: file
        required: true
        description: PDF-файл ПОС, максимум 500 МБ
    responses:
      200:
        description: Файл ПОС принят, фоновый разбор глобальных констант запущен
        schema:
          type: object
          properties:
            sessionId:
              type: string
            status:
              type: string
              example: "pos_processing"
            message:
              type: string
      400:
        description: Ошибка валидации (не PDF, не режим ИИ (AI) и т.п.)
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)
    if "file" not in request.files:
        return _err(ErrorResponse(detail="Файл не передан"), 400)

    f = request.files["file"]
    if not f.filename:
        return _err(ErrorResponse(detail="Пустое имя файла"), 400)
    if not f.filename.lower().endswith(".pdf"):
        return _err(ErrorResponse(detail="Файл ПОС должен быть в формате PDF"), 400)

    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    max_size = 500 * 1024 * 1024
    if size > max_size:
        return _err(ErrorResponse(detail=f"Файл слишком большой. Максимум {max_size // (1024*1024)} МБ"), 413)

    try:
        result = _get_manager().upload_pos(session_id, f, f.filename)
        logger.info(f"Файл ПОС принят для сессии {session_id}: {f.filename}")
        return _ok(PosUploadResponse(**result))
    except ValueError as e:
        status = 404 if "не найдена" in str(e).lower() else 400
        return _err(ErrorResponse(detail=str(e)), status)
    except Exception as e:
        logger.error(f"Ошибка загрузки файла ПОС: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Внутренняя ошибка: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/upload_pz", methods=["POST"])
def upload_pz(session_id: str):
    """
    Загрузка PDF-файла пояснительной записки (ПЗ) для сессии
    (режим ИИ (AI), ранее АР).
    Разбирается тем же пайплайном и той же LLM, что и файл ПОС:
    из документа извлекаются глобальные константы подбора работ
    → ПЗ_глобальные_константы.json.
    ---
    tags:
      - upload
    consumes:
      - multipart/form-data
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
      - name: file
        in: formData
        type: file
        required: true
        description: PDF-файл пояснительной записки, максимум 500 МБ
    responses:
      200:
        description: Файл ПЗ принят, фоновый разбор глобальных констант запущен
        schema:
          type: object
          properties:
            sessionId:
              type: string
            status:
              type: string
              example: "pz_processing"
            message:
              type: string
      400:
        description: Ошибка валидации (не PDF, не режим ИИ (AI) и т.п.)
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)
    if "file" not in request.files:
        return _err(ErrorResponse(detail="Файл не передан"), 400)

    f = request.files["file"]
    if not f.filename:
        return _err(ErrorResponse(detail="Пустое имя файла"), 400)
    if not f.filename.lower().endswith(".pdf"):
        return _err(ErrorResponse(detail="Файл пояснительной записки должен быть в формате PDF"), 400)

    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    max_size = 500 * 1024 * 1024
    if size > max_size:
        return _err(ErrorResponse(detail=f"Файл слишком большой. Максимум {max_size // (1024*1024)} МБ"), 413)

    try:
        result = _get_manager().upload_pz(session_id, f, f.filename)
        logger.info(f"Файл ПЗ принят для сессии {session_id}: {f.filename}")
        return _ok(PosUploadResponse(**result))
    except ValueError as e:
        status = 404 if "не найдена" in str(e).lower() else 400
        return _err(ErrorResponse(detail=str(e)), status)
    except Exception as e:
        logger.error(f"Ошибка загрузки файла ПЗ: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Внутренняя ошибка: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/upload_vor", methods=["POST"])
def upload_vor(session_id: str):
    """
    Загрузка PDF-файла ВОР (ведомость объёмов работ) для сессии
    (режим ИИ (AI), ранее АР).
    Из документа извлекаются группы элементов, их объёмы работ и единицы
    измерения → ВОР_ведомость_объемов_работ.json и .xlsx в папке сессии.
    ---
    tags:
      - upload
    consumes:
      - multipart/form-data
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
      - name: file
        in: formData
        type: file
        required: true
        description: PDF-файл ВОР, максимум 500 МБ
    responses:
      200:
        description: Файл ВОР принят, фоновый разбор запущен
        schema:
          type: object
          properties:
            sessionId:
              type: string
            status:
              type: string
              example: "vor_processing"
            message:
              type: string
      400:
        description: Ошибка валидации (не PDF, не режим ИИ (AI) и т.п.)
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)
    if "file" not in request.files:
        return _err(ErrorResponse(detail="Файл не передан"), 400)

    f = request.files["file"]
    if not f.filename:
        return _err(ErrorResponse(detail="Пустое имя файла"), 400)
    if not f.filename.lower().endswith(".pdf"):
        return _err(ErrorResponse(detail="Файл ВОР должен быть в формате PDF"), 400)

    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    max_size = 500 * 1024 * 1024
    if size > max_size:
        return _err(ErrorResponse(detail=f"Файл слишком большой. Максимум {max_size // (1024*1024)} МБ"), 413)

    try:
        result = _get_manager().upload_vor(session_id, f, f.filename)
        logger.info(f"Файл ВОР принят для сессии {session_id}: {f.filename}")
        return _ok(PosUploadResponse(**result))
    except ValueError as e:
        status = 404 if "не найдена" in str(e).lower() else 400
        return _err(ErrorResponse(detail=str(e)), status)
    except Exception as e:
        logger.error(f"Ошибка загрузки файла ВОР: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Внутренняя ошибка: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/upload_pd", methods=["POST"])
def upload_pd(session_id: str):
    """
    Загрузка PDF-файла ПД (проектная документация) для сессии
    (режим ИИ (AI), ранее АР).
    Разбирается только текстовая часть (графические/альбомные страницы
    пропускаются): извлекаются характеристики элементов (класс бетона,
    прочность, пластичность, морозостойкость, водонепроницаемость)
    → ПД_характеристики_элементов.json и .xlsx в папке сессии.
    ---
    tags:
      - upload
    consumes:
      - multipart/form-data
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
      - name: file
        in: formData
        type: file
        required: true
        description: PDF-файл проектной документации, максимум 500 МБ
    responses:
      200:
        description: Файл ПД принят, фоновый разбор запущен
        schema:
          type: object
          properties:
            sessionId:
              type: string
            status:
              type: string
              example: "pd_processing"
            message:
              type: string
      400:
        description: Ошибка валидации (не PDF, не режим ИИ (AI) и т.п.)
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)
    if "file" not in request.files:
        return _err(ErrorResponse(detail="Файл не передан"), 400)

    f = request.files["file"]
    if not f.filename:
        return _err(ErrorResponse(detail="Пустое имя файла"), 400)
    if not f.filename.lower().endswith(".pdf"):
        return _err(ErrorResponse(detail="Файл ПД должен быть в формате PDF"), 400)

    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    max_size = 500 * 1024 * 1024
    if size > max_size:
        return _err(ErrorResponse(detail=f"Файл слишком большой. Максимум {max_size // (1024*1024)} МБ"), 413)

    try:
        result = _get_manager().upload_pd(session_id, f, f.filename)
        logger.info(f"Файл ПД принят для сессии {session_id}: {f.filename}")
        return _ok(PosUploadResponse(**result))
    except ValueError as e:
        status = 404 if "не найдена" in str(e).lower() else 400
        return _err(ErrorResponse(detail=str(e)), status)
    except Exception as e:
        logger.error(f"Ошибка загрузки файла ПД: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Внутренняя ошибка: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/position_links", methods=["GET"])
def get_position_links(session_id: str):
    """
    Ссылки на позиции цифрового сборника для групп элементов сессии.
    ---
    tags:
      - sessions
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: >
          Карта «имя элемента → список вариантов», вариант содержит
          контекст (part, geo), позиции ({id, name, group}) и reason.
          group — имя группы работ позиции по высоте здания
          («до 57», «более 75 до 105 м» …), параметр ?group= ссылки
          на карточку ЦС; пусто, если работы от высоты не зависят
          или высота здания не определена.
        schema:
          type: object
          properties:
            sessionId:
              type: string
            ready:
              type: boolean
            positionLinks:
              type: object
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    from src.services.position_links import read_position_links

    session_dir = os.path.join(_get_manager().output_folder, session_id)
    raw_links = read_position_links(session_dir)

    # Файл может быть ещё не построен (фоновая задача после обработки IFC/PDF)
    ready = os.path.isfile(os.path.join(session_dir, "position_links.json"))

    return _ok(PositionLinksResponse(
        session_id=session_id,
        ready=ready,
        position_links=raw_links,
    ))


@bp.route("/api/session/<session_id>/preview_result/<path:filename>", methods=["GET"])
def preview_result(session_id: str, filename: str):
    """
    Предпросмотр финального файла результатов (xlsx/csv/json).
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
      - name: filename
        in: path
        type: string
        required: true
        description: Имя файла
    responses:
      200:
        description: Содержимое файла
        schema:
          type: object
          properties:
            headers:
              type: array
              items:
                type: string
            rows:
              type: array
              items:
                type: array
                items:
                  type: string
            totalRows:
              type: integer
            isPreview:
              type: boolean
      400:
        description: Некорректное имя файла или формат не поддерживается
      404:
        description: Файл не найден
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return _err(ErrorResponse(detail="Некорректное имя файла"), 400)

    path = _get_manager().file_path(session_id, filename)
    if not path or not os.path.exists(path):
        # Fallback: ищем файл в корне директории сессии
        session_dir = os.path.join(_get_manager().output_folder, session_id)
        fallback_path = os.path.join(session_dir, filename)
        if os.path.exists(fallback_path) and os.path.isfile(fallback_path):
            path = fallback_path
    if not path or not os.path.exists(path):
        return _err(ErrorResponse(detail="Файл не найден"), 404)

    try:
        MAX_PREVIEW = 100

        if path.endswith(".csv"):
            df = pd.read_csv(path)
        elif path.endswith(".xlsx"):
            df = pd.read_excel(path)
        elif path.endswith(".json"):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            # Массив объектов (например, «все элементы» или «группы элементов»)
            if isinstance(data, list):
                if data and isinstance(data[0], dict):
                    headers = list(data[0].keys())
                else:
                    headers = ["№", "Значение"]
                rows = []
                for i, item in enumerate(data[:MAX_PREVIEW], 1):
                    if isinstance(item, dict):
                        rows.append([str(item.get(h, "-")) for h in headers])
                    else:
                        rows.append([str(i), str(item)])
                return _ok(PreviewResponse(
                    headers=headers,
                    rows=rows,
                    total_rows=len(data),
                    preview_rows=min(len(data), MAX_PREVIEW),
                    is_preview=len(data) > MAX_PREVIEW,
                ))

            # Словарь «ключ — значение»
            return _ok(PreviewResponse(
                headers=["Ключ", "Значение"],
                rows=[[str(k), str(v)] for k, v in data.items()],
                total_rows=len(data),
                is_preview=True,
            ))
        else:
            return _err(ErrorResponse(detail="Предпросмотр недоступен для этого типа файла"), 400)

        headers = df.columns.tolist()
        rows = df.fillna("-").astype(str).values.tolist()

        return _ok(PreviewResponse(
            headers=headers,
            rows=rows[:MAX_PREVIEW],
            total_rows=len(df),
            preview_rows=min(len(rows), MAX_PREVIEW),
            is_preview=len(rows) > MAX_PREVIEW,
        ))

    except Exception as e:
        return _err(ErrorResponse(detail=f"Ошибка чтения файла: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/blueprint_image", methods=["GET"])
def get_blueprint_image(session_id: str):
    """
    Получить изображение чертежа с отмеченными элементами (для PDF-сессий).
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    produces:
      - image/png
    responses:
      200:
        description: PNG-изображение чертежа
      404:
        description: Сессия или изображение не найдены
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    image_path = None

    # Ищем в original/ директории
    session_dir = os.path.join(_get_manager().output_folder, session_id)
    original_dir = os.path.join(session_dir, 'original')

    search_dirs = []
    if os.path.exists(original_dir):
        search_dirs.append(original_dir)
    search_dirs.append(session_dir)

    for search_dir in search_dirs:
        for fname in os.listdir(search_dir):
            if fname.startswith("blueprint_painted") and fname.endswith(".png"):
                image_path = os.path.join(search_dir, fname)
                break
        if image_path:
            break

    if not image_path or not os.path.exists(image_path):
        logger.warning(f"Изображение чертежа не найдено для сессии {session_id}. image_path={image_path}")
        return _err(ErrorResponse(detail="Изображение чертежа не найдено"), 404)

    return send_file(os.path.abspath(image_path), mimetype="image/png")


@bp.route("/api/session/<session_id>/materials_md", methods=["GET"])
def get_materials_md(session_id: str):
    """
    Получить условные обозначения материалов (markdown) для PDF-сессии.
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Markdown-таблица условных обозначений
        schema:
          type: object
          properties:
            markdown:
              type: string
      404:
        description: Сессия или файл не найдены
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    md_path = _get_manager().file_path(session_id, "materials_colors.md")

    # Fallback: ищем в директории сессии
    if not md_path or not os.path.exists(md_path):
        session_dir = os.path.join(_get_manager().output_folder, session_id)
        original_dir = os.path.join(session_dir, 'original')

        for search_dir in [original_dir, session_dir]:
            if os.path.exists(search_dir):
                fallback_path = os.path.join(search_dir, "materials_colors.md")
                if os.path.exists(fallback_path):
                    md_path = fallback_path
                    break

    if not md_path or not os.path.exists(md_path):
        return _err(ErrorResponse(detail="Условные обозначения не найдены"), 404)

    try:
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        return jsonify({"markdown": content})
    except Exception as e:
        return _err(ErrorResponse(detail=f"Ошибка чтения файла: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/download/<path:filename>", methods=["GET"])
def download_file(session_id: str, filename: str):
    """
    Скачать конкретный файл результата.
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
      - name: filename
        in: path
        type: string
        required: true
    produces:
      - application/octet-stream
    responses:
      200:
        description: Файл для скачивания
      400:
        description: Некорректное имя файла
      404:
        description: Файл не найден
    """
    if not filename or ".." in filename:
        return _err(ErrorResponse(detail="Некорректное имя файла"), 400)

    path = _get_manager().file_path(session_id, filename)
    if not path or not os.path.exists(path):
        # Fallback: ищем файл в корне директории сессии
        session_dir = os.path.join(_get_manager().output_folder, session_id)
        fallback_path = os.path.join(session_dir, filename)
        if os.path.exists(fallback_path) and os.path.isfile(fallback_path):
            path = fallback_path
    if not path or not os.path.exists(path):
        return _err(ErrorResponse(detail="Файл не найден"), 404)

    directory, name = os.path.split(path)
    return send_file(path, as_attachment=True, download_name=name, mimetype="application/octet-stream")


@bp.route("/api/session/<session_id>/download_all", methods=["GET"])
def download_all(session_id: str):
    """
    Скачать все файлы результатов сессии одним ZIP-архивом.
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
    produces:
      - application/zip
    responses:
      200:
        description: ZIP-архив со всеми файлами сессии
      404:
        description: Сессия не найдена или нет файлов
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    # Получаем файлы текущего запуска
    files = []
    current_run_id = s.get("current_run_id")
    if current_run_id:
        runs = s.get("runs", [])
        for run in runs:
            if run.get("run_id") == current_run_id:
                files = run.get("files", [])
                break

    if not files:
        files = s.get("files", [])

    if not files:
        return _err(ErrorResponse(detail="Нет файлов для скачивания"), 404)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            fp = f.get("path")
            fname = f.get("filename", "file")
            if fp and os.path.exists(fp):
                try:
                    zf.write(fp, arcname=fname)
                except Exception as e:
                    logger.warning(f"Ошибка добавления файла в архив {fname}: {e}")

    if buf.tell() == 0:
        return _err(ErrorResponse(detail="Не удалось создать архив"), 500)

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"session_{session_id[:8]}.zip",
    )


@bp.route("/api/session/<session_id>/select_rows", methods=["POST"])
def select_rows(session_id: str):
    """
    Выбор строк и запуск обработки.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии (должна быть в статусе selecting_rows)
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - rowIndices
          properties:
            rowIndices:
              type: array
              items:
                type: integer
              description: Индексы выбранных строк (0-based)
            allRows:
              type: boolean
              description: Выбрать все строки
              default: false
            rowTypes:
              type: object
              description: "Часть здания для каждой строки: {row_index: 'Надземная'|'Подземная'|'Цоколь'}"
            rowMaterials:
              type: object
              description: "Материал для каждой строки: {row_index: 'Бетон'|'Цемент'|'Кирпич'|'Дерево'|...}"
            rowMaterialCodes:
              type: object
              description: "Уточнение неполных МССК-кодов материала: {row_index: 'СТ 00 15 01'|'СТ 00 15 02'}"
            buildingHeight:
              type: number
              description: Высота здания в метрах (1-10000)
            floorHeight:
              type: number
              description: Высота этажа в метрах (1-10000, режим ИИ (AI))
            groupedData:
              type: object
            processingType:
              type: string
              enum: ["CS", "AI"]
              default: "CS"
              description: Режим обработки (легаси-коды KR/AR также принимаются)
    responses:
      200:
        description: Обработка запущена
      400:
        description: Ошибка валидации
      404:
        description: Сессия не найдена
      500:
        description: Ошибка сервера
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    data = request.get_json()
    if not data:
        return _err(ErrorResponse(detail="Тело запроса должно быть JSON"), 400)

    row_indices = data.get("rowIndices", data.get("row_indices", []))
    all_rows = data.get("allRows", data.get("all_rows", False))
    row_types = data.get("rowTypes", data.get("row_types", {}))
    row_materials = data.get("rowMaterials", data.get("row_materials", {}))
    row_material_codes = data.get("rowMaterialCodes", data.get("row_material_codes", {})) or {}
    building_height = data.get("buildingHeight", data.get("building_height"))
    floor_height = data.get("floorHeight", data.get("floor_height"))
    grouped_data = data.get("groupedData", data.get("grouped_data", {}))
    processing_type = to_pipeline_type(data.get("processingType"))
    global_constants = data.get("globalConstants", data.get("global_constants", {})) or {}

    # Валидация констант проекта (ИИ): словарь {имя константы: значение}
    if not isinstance(global_constants, dict):
        return _err(ErrorResponse(detail="globalConstants должен быть объектом {имя: значение}"), 400)
    global_constants = {
        str(k): str(v).strip()
        for k, v in global_constants.items()
        if v is not None and str(v).strip()
    }

    if not all_rows and (not row_indices or len(row_indices) == 0):
        return _err(ErrorResponse(detail="Выберите хотя бы одну строку"), 400)

    if building_height is not None:
        try:
            building_height = float(building_height)
            if building_height <= 0:
                return _err(ErrorResponse(detail="Высота должна быть положительным числом"), 400)
            if building_height > 10000:
                return _err(ErrorResponse(detail="Слишком большая высота здания"), 400)
        except (ValueError, TypeError):
            return _err(ErrorResponse(detail="Некорректное значение высоты"), 400)

    # Высота этажа (режим ИИ) — необязательный параметр
    if floor_height is not None:
        try:
            floor_height = float(floor_height)
            if floor_height <= 0:
                return _err(ErrorResponse(detail="Высота этажа должна быть положительным числом"), 400)
            if floor_height > 10000:
                return _err(ErrorResponse(detail="Слишком большая высота этажа"), 400)
        except (ValueError, TypeError):
            return _err(ErrorResponse(detail="Некорректное значение высоты этажа"), 400)

    # Валидация материалов
    if row_materials:
        validated_materials = {}
        for key, value in row_materials.items():
            try:
                int_key = int(key)
                if not isinstance(value, str):
                    return _err(ErrorResponse(detail=f"Некорректное значение материала для строки {key}"), 400)
                validated_materials[str(int_key)] = value.strip()
            except (ValueError, TypeError):
                return _err(ErrorResponse(detail=f"Некорректный индекс строки в материалах: {key}"), 400)
        row_materials = validated_materials

    # Валидация частей здания
    if row_types:
        validated_types = {}
        valid_parts = {"Надземная", "Подземная", "Цоколь"}
        for key, value in row_types.items():
            try:
                int_key = int(key)
                if value not in valid_parts:
                    return _err(ErrorResponse(detail=f"Некорректная часть здания для строки {key}: {value}"), 400)
                validated_types[str(int_key)] = value
            except (ValueError, TypeError):
                return _err(ErrorResponse(detail=f"Некорректный индекс строки в частях здания: {key}"), 400)
        row_types = validated_types

    # Валидация уточнений неполных МССК-кодов материала (ИИ и ЦС):
    # {индекс строки: код материала} — «СТ 00 15» → «СТ 00 15 01/02».
    if row_material_codes:
        if not isinstance(row_material_codes, dict):
            return _err(ErrorResponse(detail="rowMaterialCodes должен быть объектом {индекс: код МССК}"), 400)
        validated_codes = {}
        for key, value in row_material_codes.items():
            try:
                int_key = int(key)
            except (ValueError, TypeError):
                return _err(ErrorResponse(detail=f"Некорректный индекс строки в rowMaterialCodes: {key}"), 400)
            if value is None or not str(value).strip():
                continue
            validated_codes[str(int_key)] = str(value).strip()
        row_material_codes = validated_codes

    logger.info(f"select_rows: session={session_id}, строк={len(row_indices)}, тип={processing_type}")

    try:
        result = _get_manager().select_rows(
            session_id,
            row_indices,
            all_rows,
            row_types,
            row_materials,
            building_height,
            grouped_data,
            processing_type,
            global_constants,
            floor_height,
            row_material_codes
        )
        # В ответе клиенту отдаём публичный код режима (CS/AI)
        if "processing_type" in result:
            result["processing_type"] = to_public_type(result["processing_type"])
        return _ok(SelectRowsResponse(**result))
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 400)
    except Exception as e:
        logger.error(f"Ошибка выбора строк: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


# ========== ЭНДПОИНТЫ ДЛЯ ПОВТОРНЫХ ЗАПУСКОВ ==========

@bp.route("/api/session/<session_id>/new_run", methods=["POST"])
def new_run(session_id: str):
    """
    Создать новый запуск обработки с другими выбранными строками.
    Исходный IFC/PDF файл не обрабатывается заново.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - rowIndices
          properties:
            rowIndices:
              type: array
              items:
                type: integer
            rowTypes:
              type: object
            rowMaterials:
              type: object
            rowMaterialCodes:
              type: object
              description: "Уточнение неполных МССК-кодов материала: {row_index: 'СТ 00 15 01'|'СТ 00 15 02'}"
            buildingHeight:
              type: number
            floorHeight:
              type: number
              description: Высота этажа в метрах (1-10000, режим ИИ (AI))
            groupedData:
              type: object
            processingType:
              type: string
              enum: ["CS", "AI"]
              default: "CS"
              description: Режим обработки (легаси-коды KR/AR также принимаются)
    responses:
      200:
        description: Новый запуск создан
      400:
        description: Ошибка валидации
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    data = request.get_json()
    if not data:
        return _err(ErrorResponse(detail="Тело запроса должно быть JSON"), 400)

    row_indices = data.get("rowIndices", data.get("row_indices", []))
    row_types = data.get("rowTypes", data.get("row_types", {}))
    row_materials = data.get("rowMaterials", data.get("row_materials", {}))
    row_material_codes = data.get("rowMaterialCodes", data.get("row_material_codes", {})) or {}
    building_height = data.get("buildingHeight", data.get("building_height"))
    floor_height = data.get("floorHeight", data.get("floor_height"))
    grouped_data = data.get("groupedData", data.get("grouped_data", {}))
    processing_type = to_pipeline_type(data.get("processingType"))
    global_constants = data.get("globalConstants", data.get("global_constants", {})) or {}

    if not isinstance(global_constants, dict):
        return _err(ErrorResponse(detail="globalConstants должен быть объектом {имя: значение}"), 400)
    global_constants = {
        str(k): str(v).strip()
        for k, v in global_constants.items()
        if v is not None and str(v).strip()
    }

    if not row_indices or len(row_indices) == 0:
        return _err(ErrorResponse(detail="Выберите хотя бы одну строку"), 400)

    if building_height is not None:
        try:
            building_height = float(building_height)
            if building_height <= 0:
                return _err(ErrorResponse(detail="Высота должна быть положительным числом"), 400)
            if building_height > 10000:
                return _err(ErrorResponse(detail="Слишком большая высота здания"), 400)
        except (ValueError, TypeError):
            return _err(ErrorResponse(detail="Некорректное значение высоты"), 400)

    # Высота этажа (режим ИИ) — необязательный параметр
    if floor_height is not None:
        try:
            floor_height = float(floor_height)
            if floor_height <= 0:
                return _err(ErrorResponse(detail="Высота этажа должна быть положительным числом"), 400)
            if floor_height > 10000:
                return _err(ErrorResponse(detail="Слишком большая высота этажа"), 400)
        except (ValueError, TypeError):
            return _err(ErrorResponse(detail="Некорректное значение высоты этажа"), 400)

    # Валидация уточнений неполных МССК-кодов материала (ИИ и ЦС):
    # {индекс строки: код материала} — «СТ 00 15» → «СТ 00 15 01/02».
    if row_material_codes:
        if not isinstance(row_material_codes, dict):
            return _err(ErrorResponse(detail="rowMaterialCodes должен быть объектом {индекс: код МССК}"), 400)
        validated_codes = {}
        for key, value in row_material_codes.items():
            try:
                int_key = int(key)
            except (ValueError, TypeError):
                return _err(ErrorResponse(detail=f"Некорректный индекс строки в rowMaterialCodes: {key}"), 400)
            if value is None or not str(value).strip():
                continue
            validated_codes[str(int_key)] = str(value).strip()
        row_material_codes = validated_codes

    logger.info(f"new_run: session={session_id}, строк={len(row_indices)}, тип={processing_type}")

    try:
        result = _get_manager().new_run(
            session_id,
            row_indices,
            row_types or {},
            row_materials or {},
            building_height,
            grouped_data or {},
            processing_type,
            global_constants,
            floor_height,
            row_material_codes
        )
        # В ответе клиенту отдаём публичный код режима (CS/AI)
        if "processing_type" in result:
            result["processing_type"] = to_public_type(result["processing_type"])
        return _ok(NewRunResponse(**result))
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 400)
    except Exception as e:
        logger.error(f"Ошибка создания нового запуска: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/runs", methods=["GET"])
def list_runs(session_id: str):
    """
    Получить список всех запусков сессии.
    ---
    tags:
      - sessions
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
    responses:
      200:
        description: Список запусков
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    runs = _get_manager().list_runs(session_id)
    current_run_id = None

    s = _get_manager().get(session_id)
    if s:
        current_run_id = s.get("current_run_id")

    # Логируем для отладки
    logger.info(f"Runs для сессии {session_id}:")
    logger.info(f"  всего={len(runs)}, current_run_id={current_run_id}")
    for run in runs:
        logger.info(f"  Run: {json.dumps(run, ensure_ascii=False, default=str)[:200]}")

    # processing_type запусков — в публичных кодах CS/AI
    return _ok(RunsListResponse(
        runs=public_runs(runs),
        current_run_id=current_run_id,
        total=len(runs)
    ))


@bp.route("/api/session/<session_id>/switch_run/<run_id>", methods=["POST"])
def switch_run(session_id: str, run_id: str):
    """
    Переключиться на другой запуск.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
      - name: run_id
        in: path
        type: string
        required: true
    responses:
      200:
        description: Переключение выполнено
      404:
        description: Сессия или запуск не найдены
    """
    if not session_id or not run_id:
        return _err(ErrorResponse(detail="ID сессии и запуска обязательны"), 400)

    try:
        result = _get_manager().switch_run(session_id, run_id)
        # В ответе клиенту отдаём публичный код режима (CS/AI)
        if "processing_type" in result:
            result["processing_type"] = to_public_type(result["processing_type"])
        return _ok(RunSwitchResponse(**result))
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 404)
    except Exception as e:
        logger.error(f"Ошибка переключения запуска: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/filter_height", methods=["POST"])
def filter_by_height(session_id: str):
    """
    Фильтрация элементов по высоте здания.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            buildingHeight:
              type: number
    responses:
      200:
        description: Фильтрация выполнена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    data = request.get_json()
    if not data:
        return _err(ErrorResponse(detail="Тело запроса должно быть JSON"), 400)

    building_height = data.get("buildingHeight", data.get("building_height"))
    if building_height is None:
        return _err(ErrorResponse(detail="Укажите высоту здания"), 400)

    try:
        height = float(building_height)
        if height <= 0:
            return _err(ErrorResponse(detail="Высота должна быть положительным числом"), 400)
        if height > 10000:
            return _err(ErrorResponse(detail="Слишком большая высота здания"), 400)
    except (ValueError, TypeError):
        return _err(ErrorResponse(detail="Некорректное значение высоты"), 400)

    try:
        result = _get_manager().filter_by_height(session_id, height)
        return _ok(FilterHeightResponse(**result))
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 400)
    except Exception as e:
        logger.error(f"Ошибка фильтрации по высоте: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/3d_model", methods=["POST"])
def create_3d_model(session_id: str):
    """
    Создание 3D модели (GLB) по запросу пользователя.
    Файл формируется только для IFC-сессий и только при вызове этой ручки.
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Статус создания 3D модели
        schema:
          type: object
          properties:
            status:
              type: string
              enum: [creating, ready]
            filename:
              type: string
            downloadUrl:
              type: string
      400:
        description: 3D модель недоступна для этой сессии
      404:
        description: Сессия не найдена
      500:
        description: Внутренняя ошибка
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    try:
        result = _get_manager().ensure_3d_model(session_id)
        return jsonify(result), 200
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 400)
    except Exception as e:
        logger.error(f"Ошибка создания 3D модели: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/3d_model/status", methods=["GET"])
def get_3d_model_status(session_id: str):
    """
    Статус создания 3D модели (GLB).
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Статус генерации 3D модели
        schema:
          type: object
          properties:
            status:
              type: string
              enum: [none, creating, ready, error]
            filename:
              type: string
            error:
              type: string
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    try:
        result = _get_manager().get_3d_model_status(session_id)
        return jsonify(result), 200
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except Exception as e:
        logger.error(f"Ошибка получения статуса 3D модели: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


# ========== ЭНДПОИНТЫ ДЛЯ СБОРКИ ФИНАЛЬНОГО JSON (по номеру запуска) ==========

@bp.route("/api/session/<session_id>/run/<int:run_number>/build_final_json", methods=["POST"])
def build_final_json_by_number(session_id: str, run_number: int):
    """
    Запуск сборки финального JSON (режимы ИИ/ЦС) по номеру запуска.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
      - name: run_number
        in: path
        type: integer
        required: true
        description: Номер запуска (1, 2, 3, ...)
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            processingType:
              type: string
              enum: ["CS", "AI"]
              default: "CS"
              description: Режим обработки (легаси-коды KR/AR также принимаются)
    responses:
      200:
        description: Сборка запущена
      400:
        description: Ошибка валидации
      404:
        description: Сессия или запуск не найдены
      409:
        description: Запуск ещё не завершён
      500:
        description: Внутренняя ошибка
    """
    if not session_id or not run_number:
        return _err(ErrorResponse(detail="ID сессии и номер запуска обязательны"), 400)

    data = request.get_json() or {}
    processing_type = to_pipeline_type(data.get("processingType"))

    logger.info(
        f"build_final_json: session={session_id}, run_number={run_number}, "
        f"режим: {mode_name(processing_type)} ({to_public_type(processing_type)})"
    )

    try:
        result = _get_manager().build_final_json_by_number(session_id, run_number, processing_type)
        # В ответе клиенту отдаём публичный код режима (CS/AI)
        if "processing_type" in result:
            result["processing_type"] = to_public_type(result["processing_type"])
        return _ok(FinalJsonBuildResponse(**result))
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 404)
    except RuntimeError as e:
        return _err(ErrorResponse(detail=str(e)), 409)
    except Exception as e:
        logger.error(f"Ошибка запуска сборки финального JSON: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/run/<int:run_number>/final_json/status", methods=["GET"])
def get_final_json_status_by_number(session_id: str, run_number: int):
    """
    Получение статуса сборки финального JSON по номеру запуска.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
      - name: run_number
        in: path
        type: integer
        required: true
        description: Номер запуска (1, 2, 3, ...)
    responses:
      200:
        description: Статус сборки
      404:
        description: Сессия или запуск не найдены
    """
    if not session_id or not run_number:
        return _err(ErrorResponse(detail="ID сессии и номер запуска обязательны"), 400)

    try:
        result = _get_manager().get_final_json_status_by_number(session_id, run_number)
        # В ответе клиенту отдаём публичный код режима (CS/AI)
        if "processing_type" in result:
            result["processing_type"] = to_public_type(result["processing_type"])
        return _ok(FinalJsonStatusResponse(**result))
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 404)
    except Exception as e:
        logger.error(f"Ошибка получения статуса финального JSON: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/run/<int:run_number>/final_json/result", methods=["GET"])
def get_final_json_result_by_number(session_id: str, run_number: int):
    """
    Получение результата сборки финального JSON по номеру запуска.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
      - name: run_number
        in: path
        type: integer
        required: true
        description: Номер запуска (1, 2, 3, ...)
    responses:
      200:
        description: Результат сборки
      202:
        description: Сборка ещё выполняется
      404:
        description: Сессия, запуск или результат не найдены
      409:
        description: Сборка завершилась с ошибкой
    """
    if not session_id or not run_number:
        return _err(ErrorResponse(detail="ID сессии и номер запуска обязательны"), 400)

    try:
        manager = _get_manager()

        # Получаем статус
        status_result = manager.get_final_json_status_by_number(session_id, run_number)

        if status_result['status'] == 'error':
            return _err(
                ErrorResponse(detail=status_result.get('error') or "Ошибка сборки финального JSON"),
                409
            )

        # Получаем результат напрямую
        result = manager.get_final_json_result_by_number(session_id, run_number)

        if result is None:
            # Если статус completed, но результат не найден
            if status_result['status'] == 'completed':
                return _err(ErrorResponse(detail="Результат не найден"), 404)
            # Если сборка ещё выполняется
            response = _ok(
                FinalJsonResultResponse(
                    session_id=session_id,
                    run_id=str(run_number),
                    status=status_result['status'] or 'building',
                    processing_type=to_public_type(status_result.get('processing_type')),
                    result=None
                ),
                status=202
            )
            response.headers["Retry-After"] = "2"
            return response

        return _ok(FinalJsonResultResponse(
            session_id=session_id,
            run_id=str(run_number),
            status="completed",
            # discipline финального JSON («КР»/«АР») и код запуска приводим
            # к публичным кодам режимов CS/AI
            processing_type=to_public_type(
                result.get('discipline') or status_result.get('processing_type')
            ),
            result=result
        ))

    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 404)
    except Exception as e:
        logger.error(f"Ошибка получения результата финального JSON: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


# ==== PD_FULL: полное извлечение из ПОС/ПЗ (всё, что влияет на подбор работ и объёмы) ====
import threading as _pd_threading
from flask import jsonify as _pd_jsonify
_PD_RUNNING = set()


def _pd_find_docs(session_id):
    """Ищем PDF сессии: ПОС и ПЗ."""
    import glob
    mgr = _get_manager()
    dirs = [os.path.join(mgr.output_folder, session_id)]
    up = getattr(mgr, "upload_folder", None)
    if up:
        dirs.append(os.path.join(up, session_id))
    pdfs = []
    for d in dirs:
        pdfs += glob.glob(os.path.join(d, "**", "*.pdf"), recursive=True)
        pdfs += glob.glob(os.path.join(d, "**", "*.PDF"), recursive=True)
    for root in ("/app/uploads", "/app/outputs"):
        for f in glob.glob(os.path.join(root, "**", "*.pdf"), recursive=True):
            if session_id in f and f not in pdfs:
                pdfs.append(f)
    pos = pz = None
    for f in pdfs:
        n = os.path.basename(f).lower()
        if pos is None and ("пос" in n or "pos" in n):
            pos = f
        elif pz is None and ("пз" in n or "pz" in n or "пояснит" in n):
            pz = f
    rest = [f for f in pdfs if f not in (pos, pz)]
    if pz is None and rest:
        pz = rest[0]
    return pos, pz, pdfs


@bp.route("/api/session/<session_id>/pd_full", methods=["GET"])
def get_pd_full(session_id: str):
    """
    Полный разбор ПОС/ПЗ (≈40 параметров со страницами и цитатами) — ленивый
    запуск в фоне, UI опрашивает эндпоинт. Только режим ИИ (AI).
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
    responses:
      200:
        description: Статус разбора (running/ready/none) и найденные параметры
    """
    out = os.path.join(_get_manager().output_folder, session_id)
    fp = os.path.join(out, "pd_full.json")
    if os.path.isfile(fp):
        with open(fp, encoding="utf-8") as f:
            return _pd_jsonify({"status": "ready", "items": json.load(f)})
    if session_id in _PD_RUNNING:
        return _pd_jsonify({"status": "running"})
    pos, pz, pdfs = _pd_find_docs(session_id)
    if not pos and not pz:
        return _pd_jsonify({"status": "none", "found_pdfs": pdfs})

    def _job():
        try:
            from src.services import pd_full
            res = pd_full.extract(pos, pz, log=lambda m: None)
            os.makedirs(out, exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=1)
        except Exception as e:
            with open(fp + ".err", "w", encoding="utf-8") as f:
                f.write(str(e))
        finally:
            _PD_RUNNING.discard(session_id)

    _PD_RUNNING.add(session_id)
    _pd_threading.Thread(target=_job, daemon=True).start()
    return _pd_jsonify({"status": "running", "pos": pos and os.path.basename(pos), "pz": pz and os.path.basename(pz)})


# ==== DOC_FACTS: сводка фактов из ВОР и ПД (ведомость объёмов работ, характеристики) ====
@bp.route("/api/session/<session_id>/doc_facts", methods=["GET"])
def get_doc_facts(session_id: str):
    """
    Тезисная сводка фактов из дополнительных документов сессии: ВОР (ведомость
    объёмов работ) и ПД (характеристики конструкций) — наименование, объём,
    класс бетона, страница и цитата. Показывается в блоке «Всё найденное в ВОР
    и ПД» (только режим ИИ). Разбор уже выполнен на этапе 0 (doc_pdf_parser),
    поэтому эндпоинт читает готовые JSON сессии.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
    responses:
      200:
        description: Сводка (status=ready) либо status=none, если документов нет
    """
    base = os.path.join(_get_manager().output_folder, session_id)
    try:
        from src.services import doc_facts
        return _pd_jsonify(doc_facts.facts_items(base))
    except Exception as e:
        return _pd_jsonify({"status": "none", "items": [], "error": str(e)})


# ==== ANALYTIC: аналитическая справка последнего расчёта ====
@bp.route("/api/session/<session_id>/analytic", methods=["GET"])
def get_analytic(session_id: str):
    """
    Аналитическая справка последнего расчёта (analytic.json): что учтено,
    что требует проверки, что не вошло в перечень, противоречия. Только ИИ (AI).
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
    responses:
      200:
        description: Справка (status=ready) либо status=none
    """
    import glob
    base = os.path.join(_get_manager().output_folder, session_id)
    files = glob.glob(os.path.join(base, "**", "analytic.json"), recursive=True)
    if not files:
        return _pd_jsonify({"status": "none"})
    fp = max(files, key=os.path.getmtime)
    with open(fp, encoding="utf-8") as f:
        d = json.load(f)
    d["status"] = "ready"
    d["mtime"] = os.path.getmtime(fp)
    return _pd_jsonify(d)


# ==== COMPARE_VOR: сравнение работ/объёмов ВОР заказчика и сформированного перечня ====
@bp.route("/api/session/<session_id>/compare_vor", methods=["GET"])
def get_compare_vor(session_id: str):
    """
    Сравнение работ и объёмов ВОР заказчика и сформированного перечня работ
    (``Финальный_перечень_работ.json`` последнего запуска). ВОР — загруженный
    PDF (разбирается детерминированно: разделы, блоки/захватки, объёмы «стало»),
    при его отсутствии — ``ВОР_ведомость_объемов_работ.json``. Единицы
    приводятся к базовым (м2/м3/т/м/шт), работы сопоставляются **по каждой
    группе элементов и каждой работе** (группа — канонический вид конструкции:
    стены, плиты перекрытия, лестничные марши/площадки, балки и т.д.; разбивка
    ВОР по секциям/этажам приводится к части здания; арматура не сравнивается —
    «взято из ВОР»; ресурсы перечня — отдельным листом).
    Результат — ``Сравнение_ВОР_и_перечня.xlsx/.json`` в папке последнего
    запуска. Только режим ИИ (AI). Параметр ``?rebuild=1`` — пересобрать
    сравнение заново.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
      - name: rebuild
        in: query
        type: boolean
        required: false
    responses:
      200:
        description: Сравнение (status=ready) либо status=none, если нет ВОР/перечня
    """
    from src.services import vor_comparison

    base = os.path.join(_get_manager().output_folder, session_id)
    run_dir = vor_comparison.latest_run_with_final_works(base)
    if not run_dir:
        return _pd_jsonify({"status": "none",
                            "detail": "Нет запуска с Финальный_перечень_работ.json"})
    json_path = os.path.join(run_dir, vor_comparison.COMPARE_JSON)
    rebuild = str(request.args.get("rebuild", "")).lower() in ("1", "true", "yes")
    try:
        if rebuild or not os.path.isfile(json_path):
            result = vor_comparison.run(run_dir)
            if result is None:
                return _pd_jsonify({"status": "none",
                                    "detail": "Не найдены ВОР или перечень работ"})
        else:
            with open(json_path, encoding="utf-8") as f:
                result = json.load(f)
        xlsx_path = os.path.join(run_dir, vor_comparison.COMPARE_XLSX)
        _get_manager().register_file(session_id, xlsx_path)
        result["status"] = "ready"
        result["file"] = vor_comparison.COMPARE_XLSX
        result["download_url"] = (
            f"/ifc-vor/api/session/{session_id}/download/{vor_comparison.COMPARE_XLSX}"
        )
        return _pd_jsonify(result)
    except Exception as e:
        logger.error(f"compare_vor: ошибка сравнения ВОР и перечня: {e}", exc_info=True)
        return _pd_jsonify({"status": "error", "detail": str(e)})


# ==== TSN_MODE: список сборников ТСН и режим «с учётом / без» ====
@bp.route("/api/tsn/docs", methods=["GET"])
def get_tsn_docs():
    """
    Список загруженных сборников ТСН (data/tsn/*.json) для панели UI.
    ---
    tags:
      - processing
    responses:
      200:
        description: Список сборников (код, название, число поправок)
    """
    try:
        from src.services import tsn_coeffs
        return _pd_jsonify({"docs": tsn_coeffs.docs()})
    except Exception as e:
        return _pd_jsonify({"docs": [], "error": str(e)})


@bp.route("/api/session/<session_id>/tsn_mode", methods=["GET"])
def tsn_mode(session_id: str):
    """
    Переключатель «поправки из сборников ТСН: учитывать / не учитывать».
    Значение сохраняется в <сессия>/tsn_mode.json. Только режим ИИ (AI).
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
      - name: set
        in: query
        type: string
        enum: ["on", "off"]
        required: false
    responses:
      200:
        description: Текущий режим {"mode": "on"|"off"}
    """
    from flask import request
    out = os.path.join(_get_manager().output_folder, session_id)
    fp = os.path.join(out, "tsn_mode.json")
    v = request.args.get("set")
    if v in ("on", "off"):
        os.makedirs(out, exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"mode": v}, f)
    mode = "on"
    if os.path.isfile(fp):
        try:
            mode = json.load(open(fp, encoding="utf-8")).get("mode", "on")
        except Exception:
            pass
    return _pd_jsonify({"mode": mode})


# ==== PD_MODE: режим «с учётом ПОС и ПЗ / без» — запоминается на сервере ====
@bp.route("/api/session/<session_id>/pd_mode", methods=["GET"])
def pd_mode(session_id: str):
    """
    Переключатель «с учётом ПОС и ПЗ / без учёта (только модель)».
    Значение сохраняется в <сессия>/pd_mode.json. Только режим ИИ (AI).
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
      - name: set
        in: query
        type: string
        enum: ["on", "off"]
        required: false
    responses:
      200:
        description: Текущий режим {"mode": "on"|"off"}
    """
    from flask import request
    out = os.path.join(_get_manager().output_folder, session_id)
    fp = os.path.join(out, "pd_mode.json")
    v = request.args.get("set")
    if v in ("on", "off"):
        os.makedirs(out, exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"mode": v}, f)
    mode = "on"
    if os.path.isfile(fp):
        try:
            mode = json.load(open(fp, encoding="utf-8")).get("mode", "on")
        except Exception:
            pass
    return _pd_jsonify({"mode": mode})