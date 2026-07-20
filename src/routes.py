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
from src.services.session_manager import SessionManager
from src.core.logger import setup_logger
from src.core.config import load_config
from src.schemas import (
    ErrorResponse, SessionFull, SessionListResponse,
    UploadResponse, StatusResponse, DeleteResponse,
    PreviewResponse, RestoreResponse, HealthResponse,
    SelectRowsResponse, FilterHeightResponse,
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


def _ok(schema_instance) -> Response:
    return Response(
        schema_instance.model_dump_json(exclude_none=False, by_alias=True),
        status=200,
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

    try:
        if is_ifc:
            result = _get_manager().process_ifc(f, f.filename)
            result["source_type"] = "ifc"
        else:
            result = _get_manager().process_pdf(f, f.filename)
            result["source_type"] = "pdf"
        return _ok(UploadResponse(**result))
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 400)
    except Exception as e:
        logger.error(f"Ошибка загрузки файла: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Внутренняя ошибка: {str(e)}"), 500)


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
        sessions = [SessionFull(**s) for s in raw]
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

    return _ok(SessionFull(**s))


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

    return _ok(RestoreResponse(
        session_id=s["session_id"],
        status=s["status"],
        progress=s.get("progress", 0),
        progress_message=s.get("progress_message", ""),
        has_results=s.get("has_results", False),
        files=s.get("files", []),
        construction_types=s.get("construction_types", {}),
        building_height=s.get("building_height"),
        selected_rows_count=len(s.get("selected_rows", []) or []),
        source_type=s.get("source_type"),
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

    excel_path = s.get("excel_file_path")
    if not excel_path or not os.path.exists(excel_path):
        csv_path = excel_path.replace(".xlsx", ".csv") if excel_path else None
        if csv_path and os.path.exists(csv_path):
            excel_path = csv_path
        else:
            return _err(ErrorResponse(detail="Excel файл не найден"), 404)

    try:
        if excel_path.endswith(".csv"):
            df = pd.read_csv(excel_path)
        else:
            df = pd.read_excel(excel_path)
          
        #df = df.drop(['GlobalId'], axis=1, errors='ignore')
    except Exception as e:
        return _err(ErrorResponse(detail=f"Ошибка чтения файла: {str(e)}"), 500)

    headers = df.columns.tolist()
    rows = df.fillna("-").astype(str).values.tolist()
    saved_types = s.get("construction_types", {})

    # Проверяем наличие чертежа и условных обозначений (для PDF-сессий)
    has_blueprint_image = False
    has_materials_md = False
    source_type = s.get("source_type")
    for f in s.get("files", []):
        fname = f.get("filename", "")
        if fname.startswith("blueprint_painted") and fname.endswith(".png"):
            has_blueprint_image = True
        if fname == "materials_colors.md":
            has_materials_md = True

        # После чтения основного Excel
    building_height = None
    try:
        # Пробуем прочитать лист "Высота здания"
        xls = pd.ExcelFile(excel_path)
        if 'Высота_здания' in xls.sheet_names:
            df_height = pd.read_excel(excel_path, sheet_name='Высота_здания')
            if 'Значение_м' in df_height.columns:
                for _, row_data in df_height.iterrows():
                    if 'Высота надземной части' in str(row_data.iloc[0]):
                        building_height = float(row_data['Значение_м'])
                        break
    except Exception as e:
        logger.warning(f"Не удалось прочитать высоту: {e}")

    return _ok(PreviewResponse(
        headers=headers,
        rows=rows,
        total_rows=len(df),
        saved_types=saved_types,
        building_height=building_height,
        source_type=source_type,
        has_blueprint_image=has_blueprint_image,
        has_materials_md=has_materials_md,
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
        return _err(ErrorResponse(detail="Файл не найден"), 404)

    try:
        if path.endswith(".csv"):
            df = pd.read_csv(path)
        elif path.endswith(".xlsx"):
            df = pd.read_excel(path)
        elif path.endswith(".json"):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return _ok(PreviewResponse(
                headers=["Ключ", "Значение"],
                rows=[[k, str(v)] for k, v in data.items()],
                total_rows=len(data),
                is_preview=True,
            ))
        else:
            return _err(ErrorResponse(detail="Предпросмотр недоступен для этого типа файла"), 400)

        headers = df.columns.tolist()
        rows = df.fillna("-").astype(str).values.tolist()
        MAX_PREVIEW = 100

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

    # Ищем файл с изображением чертежа в списке файлов сессии
    image_path = None
    for f in s.get("files", []):
        fname = f.get("filename", "")
        if fname.startswith("blueprint_painted") and fname.endswith(".png"):
            image_path = f.get("path")
            break

    # Fallback: ищем напрямую в директории сессии
    if not image_path or not os.path.exists(image_path):
        manager = _get_manager()
        session_dir = os.path.join(manager.output_folder, session_id)
        if os.path.isdir(session_dir):
            for fname in os.listdir(session_dir):
                if fname.startswith("blueprint_painted") and fname.endswith(".png"):
                    image_path = os.path.join(session_dir, fname)
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

    # Fallback: ищем напрямую в директории сессии
    if not md_path or not os.path.exists(md_path):
        manager = _get_manager()
        session_dir = os.path.join(manager.output_folder, session_id)
        fallback_path = os.path.join(session_dir, "materials_colors.md")
        if os.path.exists(fallback_path):
            md_path = fallback_path

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
    Выбрать строки Excel-таблицы и запустить пайплайн обработки (этапы 1-6).
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
            buildingHeight:
              type: number
              description: Высота здания в метрах (1-10000)
            groupedData:
              type: object
              description: Данные сгруппированных элементов
    responses:
      200:
        description: Строки выбраны, обработка запущена
        schema:
          type: object
          properties:
            sessionId:
              type: string
            status:
              type: string
              example: processing
            selectedRows:
              type: integer
            message:
              type: string
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
    building_height = data.get("buildingHeight", data.get("building_height"))
    grouped_data = data.get("groupedData", data.get("grouped_data", {}))

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

    # Валидация материалов
    if row_materials:
        # Проверяем, что все ключи - строки с числами
        validated_materials = {}
        for key, value in row_materials.items():
            try:
                # Пробуем преобразовать ключ в int
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
                    return _err(ErrorResponse(detail=f"Некорректная часть здания для строки {key}: {value}. Допустимые значения: {', '.join(valid_parts)}"), 400)
                validated_types[str(int_key)] = value
            except (ValueError, TypeError):
                return _err(ErrorResponse(detail=f"Некорректный индекс строки в частях здания: {key}"), 400)
        row_types = validated_types

    try:
        result = _get_manager().select_rows(
            session_id, 
            row_indices, 
            all_rows, 
            row_types, 
            row_materials,
            building_height, 
            grouped_data
        )
        return _ok(SelectRowsResponse(**result))
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 400)
    except Exception as e:
        logger.error(f"Ошибка выбора строк: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/filter_height", methods=["POST"])
def filter_by_height(session_id: str):
    """
    Запустить фильтрацию по высоте здания (этапы 5-6).
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
            - buildingHeight
          properties:
            buildingHeight:
              type: number
              description: Высота здания в метрах (1-10000)
    responses:
      200:
        description: Фильтрация запущена
        schema:
          type: object
          properties:
            sessionId:
              type: string
            status:
              type: string
              example: processing
            buildingHeight:
              type: number
            message:
              type: string
      400:
        description: Ошибка валидации или неверный статус сессии
      404:
        description: Сессия не найдена
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
