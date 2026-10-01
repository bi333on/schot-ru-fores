# -*- coding: utf-8 -*-
"""Приём счетов от контрагентов — Flask-приложение.

Открытая форма для контрагентов + защищённая админ-панель для сотрудников
ФОРЭС (просмотр, статусы, экспорт в Excel, совместимый с Реестром платежей).
"""
import io
import os
import re
import secrets
import time
import unicodedata
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    session, send_file, abort, send_from_directory,
)
from flask_wtf.csrf import CSRFProtect
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from config import config
from models import db, Invoice, Contragent
import parser

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY
app.config["SQLALCHEMY_DATABASE_URI"] = config.SQLALCHEMY_DATABASE_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_SIZE
app.config["UPLOAD_FOLDER"] = config.UPLOAD_FOLDER
app.config["EXPORT_FOLDER"] = config.EXPORT_FOLDER

# --- Безопасность: cookie -------------------------------------------------
_use_secure_cookie = config.SITE_URL.startswith("https://")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = _use_secure_cookie

# --- Безопасность: CSRF ----------------------------------------------------
csrf = CSRFProtect(app)

db.init_app(app)


@app.after_request
def set_security_headers(resp):
    """Заголовки безопасности (защита в глубину)."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; form-action 'self'; frame-ancestors 'self'",
    )
    return resp

# ---------------------------------------------------------------------------
# Справочники (совместимо с «Реестром платежей»)
# ---------------------------------------------------------------------------
CONTRAGENTS = [
    "ПАО МТС", "ПАО РОСТЕЛЕКОМ", "ООО ОВК", "ООО СИТИЛИНК",
    "УРАЛОРГТЕХНИКА", "ИНТЕРНЕТ-ПРО",
]

SUBDIVISIONS = [
    ("Центральный офис", "центр.офис"),
    ("Сухоложское подразделение", "Сухой-Лог"),
    ("Курьинское подразделение", "Курьи"),
    ("Асбестовское подразделение", "Асбест"),
    ("Каменск-Уральское подразделение", "Каменск"),
]

STATUSES = [
    ("новый", "Новый"),
    ("в обработке", "В обработке"),
    ("оплачен", "Оплачен"),
    ("не учитывать", "Не учитывать"),
]

STATUS_BADGES = {
    "новый": "badge-new",
    "в обработке": "badge-process",
    "оплачен": "badge-paid",
    "не учитывать": "badge-excl",
}

# ---------------------------------------------------------------------------
# Вспомогательные
# ---------------------------------------------------------------------------
def to_float(s):
    """Разбор суммы: '18 645,79', '1099-68', '18645.79' -> float."""
    if s is None:
        return None
    t = str(s).replace("\u00a0", " ").replace("\u202f", " ").strip()
    if not t:
        return None
    t = t.replace(" ", "").replace(",", ".")
    # дефис-разделитель копеек у Ростелекома: "1099-68"
    m = re.match(r"-?(\d+(?:\.\d{1,2})?)", t)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def norm(s):
    return (s or "").replace("\u00a0", " ").replace("\u202f", " ").strip()


def allowed_file(name):
    if not name or "." not in name:
        return False
    ext = name.rsplit(".", 1)[1].lower()
    return ext in config.ALLOWED_EXTENSIONS


def safe_basename(name):
    """Обезвредить имя файла: только ASCII-буквы/цифры, точка, дефис, подчёркивание."""
    name = unicodedata.normalize("NFKD", name or "")
    name = name.encode("ascii", "ignore").decode("ascii", "ignore")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or "file"


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def contragent_required(f):
    """Доступ только для вошедшего контрагента."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("contragent_id"):
            return redirect(url_for("contragent_login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def current_contragent():
    """Текущий контрагент из сессии (или None)."""
    cid = session.get("contragent_id")
    if cid is None:
        return None
    return db.session.get(Contragent, cid)


def is_valid_email(s):
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s or "") is not None


def safe_next(target):
    """Защита от открытого редиректа: разрешаем только внутренние пути."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


# Лимит попыток входа (против brute-force). Ключи в сессии.
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60


def login_blocked(key):
    """True, если для ключа превышен лимит попыток."""
    locked_until = session.get(f"{key}_locked_until", 0)
    now = time.time()
    if locked_until and now < locked_until:
        return True
    if locked_until and now >= locked_until:
        session.pop(f"{key}_attempts", None)
        session.pop(f"{key}_locked_until", None)
    return False


def login_failed(key):
    """Зафиксировать неудачную попытку входа."""
    attempts = session.get(f"{key}_attempts", 0) + 1
    session[f"{key}_attempts"] = attempts
    if attempts >= MAX_LOGIN_ATTEMPTS:
        session[f"{key}_locked_until"] = time.time() + LOGIN_LOCKOUT_SECONDS
        session[f"{key}_attempts"] = 0


def login_success(key):
    session.pop(f"{key}_attempts", None)
    session.pop(f"{key}_locked_until", None)


def upload_date_str(inv):
    """Дата загрузки в формате ДД.ММ.ГГГГ (из created_at)."""
    if not inv.created_at:
        return ""
    return inv.created_at.strftime("%d.%m.%Y")


def dates_display(inv):
    """
    Возвращает список строк для отображения дат:
    - если дата выставления == дата загрузки — только дату выставления;
    - иначе обе строки: «Дата выставления: …» и «Дата загрузки: …».
    """
    выставления = (inv.дата or "").strip()
    загрузки = upload_date_str(inv)
    if выставления and выставления == загрузки:
        return [("Дата выставления", выставления)]
    rows = []
    if выставления:
        rows.append(("Дата выставления", выставления))
    if загрузки:
        rows.append(("Дата загрузки", загрузки))
    return rows


def dates_cell(inv):
    """
    Для колонки списка: дата выставления; если дата загрузки отличается —
    второй строкой «загружен ДД.ММ.ГГГГ» (жирным). Если совпадают — только дата выставления.
    Возвращает безопасный Markup.
    """
    from markupsafe import Markup, escape
    выставления = escape((inv.дата or "").strip())
    загрузки = upload_date_str(inv)
    if not inv.дата or not (inv.дата or "").strip():
        return Markup(escape(загрузки or "—"))
    if загрузки and загрузки != (inv.дата or "").strip():
        return Markup(f"{выставления}<br><strong>загружен {escape(загрузки)}</strong>")
    return Markup(выставления)


# ---------------------------------------------------------------------------
# Jinja
# ---------------------------------------------------------------------------
@app.context_processor
def inject_globals():
    return {
        "site_name": config.SITE_NAME,
        "site_url": config.SITE_URL,
        "current_year": datetime.now().year,
        "contragents": CONTRAGENTS,
        "subdivisions": SUBDIVISIONS,
        "statuses": STATUSES,
        "status_badges": STATUS_BADGES,
        "current_contragent": current_contragent(),
        "dates_display": dates_display,
        "dates_cell": dates_cell,
    }


@app.template_filter("money")
def money_filter(v):
    if v is None:
        return ""
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")


@app.template_filter("nl2br")
def nl2br_filter(v):
    """Переносы строк в HTML (для многострочных списков товаров)."""
    from markupsafe import Markup, escape
    if not v:
        return ""
    return Markup("<br>").join(escape(part) for part in str(v).splitlines())


@app.template_filter("items_list")
def items_list_filter(v):
    """
    Список позиций товаров/услуг: каждая строка — отдельный <li>.
    Если одна строка — просто текст. Безопасно экранируется.
    """
    from markupsafe import Markup, escape
    if not v:
        return ""
    parts = [escape(p.strip()) for p in str(v).splitlines() if p.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return Markup(parts[0])
    items = "".join(f"<li>{p}</li>" for p in parts)
    return Markup(f"<ul class=\"items-list\">{items}</ul>")


# ---------------------------------------------------------------------------
# Публичная часть (личный кабинет контрагента)
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Главная: для вошедшего — кабинет, для гостя — приветственная страница."""
    if current_contragent():
        return redirect(url_for("cabinet"))
    return render_template("index.html", form={})


@app.route("/register", methods=["GET", "POST"])
def contragent_register():
    """Регистрация отключена: аккаунты контрагентов создаёт администратор."""
    return redirect(url_for("contragent_login"))


@app.route("/login", methods=["GET", "POST"])
def contragent_login():
    if current_contragent():
        return redirect(url_for("cabinet"))
    if request.method == "POST":
        email = norm(request.form.get("email", "")).lower()[:256]
        password = request.form.get("password", "")

        if login_blocked("contragent_login"):
            flash("Слишком много попыток входа. Попробуйте позже (15 минут).", "error")
            return render_template("login_contragent.html")

        user = Contragent.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            if not user.is_active:
                flash("Доступ к аккаунту заблокирован администратором.", "error")
                return render_template("login_contragent.html")
            session["contragent_id"] = user.id
            login_success("contragent_login")
            flash("Вы вошли в личный кабинет.", "ok")
            return redirect(safe_next(request.args.get("next")) or url_for("cabinet"))
        login_failed("contragent_login")
        flash("Неверный email или пароль.", "error")
    return render_template("login_contragent.html")


@app.route("/logout")
def contragent_logout():
    session.pop("contragent_id", None)
    flash("Вы вышли из личного кабинета.", "ok")
    return redirect(url_for("index"))


@app.route("/cabinet")
@contragent_required
def cabinet():
    c = current_contragent()
    q = norm(request.args.get("q", ""))
    status = norm(request.args.get("status", ""))

    query = c.invoices
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Invoice.номер_счета.ilike(like),
                Invoice.товар_услуга.ilike(like),
                Invoice.примечание.ilike(like),
                Invoice.назначение.ilike(like),
            )
        )
    if status in [s[0] for s in STATUSES]:
        query = query.filter(Invoice.статус == status)

    query = query.order_by(Invoice.created_at.desc())

    page = request.args.get("page", 1, type=int)
    pagination = query.paginate(page=page, per_page=15, error_out=False)
    invoices = pagination.items

    # итоги по ВСЕМ счетам контрагента (не только на текущей странице)
    all_inv = c.invoices.all()
    total = sum((i.сумма or 0) for i in all_inv)
    paid = sum((i.сумма or 0) for i in all_inv if i.статус == "оплачен")
    excluded = sum((i.сумма or 0) for i in all_inv if i.статус == "не учитывать")
    payable = total - paid - excluded

    return render_template(
        "cabinet.html",
        contragent=c,
        invoices=invoices,
        pagination=pagination,
        q=q,
        status=status,
        total=total,
        paid=paid,
        excluded=excluded,
        payable=payable,
    )


@app.route("/cabinet/add", methods=["GET", "POST"])
@contragent_required
def cabinet_add():
    c = current_contragent()
    form = {}
    if request.method == "POST":
        # honeypot
        if request.form.get("website", ""):
            return redirect(url_for("cabinet"))

        # вручную заполняется только назначение (подразделение)
        назначение = norm(request.form.get("назначение", ""))[:256]
        form = {"назначение": назначение}

        file = request.files.get("file")
        errors = []
        if not file or not file.filename:
            errors.append("Приложите файл счёта (PDF или скан).")

        original_name = ""
        saved_name = ""
        if file and file.filename:
            if not allowed_file(file.filename):
                errors.append("Недопустимый формат файла (разрешён только PDF).")
            else:
                original_name = norm(file.filename)[:512]
                ext = original_name.rsplit(".", 1)[1].lower()
                saved_name = f"{secrets.token_hex(8)}.{ext}"
                file.save(os.path.join(app.config["UPLOAD_FOLDER"], saved_name))

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("cabinet_add.html", form=form)

        # разбор счёта из файла (PDF со слоем текста или скан через OCR)
        parsed = parser.parse_invoice_file(
            os.path.join(app.config["UPLOAD_FOLDER"], saved_name)
        )

        номер = norm(parsed.get("номер_счета", ""))[:128]
        дата = norm(parsed.get("дата", ""))[:32]
        сумма = parsed.get("сумма")

        # защита от дубликатов: номер + дата + сумма уже есть в базе
        if номер and дата and сумма is not None:
            dup = Invoice.query.filter(
                Invoice.номер_счета == номер,
                Invoice.дата == дата,
                Invoice.сумма == сумма,
            ).first()
            if dup:
                # удаляем только что сохранённый файл
                fp = os.path.join(app.config["UPLOAD_FOLDER"], saved_name)
                if os.path.exists(fp):
                    os.remove(fp)
                сумма_str = f"{сумма:,.2f}".replace(",", " ").replace(".", ",")
                flash(
                    f"Счёт № {номер} от {дата} на сумму {сумма_str} ₽ уже загружен ранее. "
                    "Повторная загрузка невозможна.",
                    "error",
                )
                return render_template("cabinet_add.html", form=form)

        # назначение: приоритет у ручного выбора, иначе из разбора
        purpose = назначение or parsed.get("назначение", "")

        inv = Invoice(
            contragent_id=c.id,
            номер_счета=номер,
            дата=дата,
            контрагент=norm(parsed.get("контрагент", ""))[:256] or (c.контрагент or c.название),
            товар_услуга=norm(parsed.get("товар_услуга", ""))[:512],
            сумма=сумма,
            примечание=norm(parsed.get("примечание", ""))[:512],
            назначение=purpose,
            файл=saved_name,
            оригинал_файла=original_name,
            отправитель=c.название,
            email=c.email,
            телефон=c.телефон,
            статус="новый",
        )
        db.session.add(inv)
        db.session.commit()

        flash(f"Счёт добавлен (регистрационный номер #{inv.id}).", "ok")
        return redirect(url_for("cabinet"))

    return render_template("cabinet_add.html", form={})


@app.route("/cabinet/file/<int:id>")
@contragent_required
def cabinet_file(id):
    """Скачивание файла своего счёта."""
    c = current_contragent()
    inv = db.session.get(Invoice, id)
    if inv is None or inv.contragent_id != c.id or not inv.файл:
        abort(404)
    fp = os.path.join(app.config["UPLOAD_FOLDER"], inv.файл)
    if not os.path.exists(fp):
        abort(404)
    download_name = safe_basename(inv.оригинал_файла or inv.файл)
    return send_from_directory(
        app.config["UPLOAD_FOLDER"], inv.файл, as_attachment=True, download_name=download_name
    )


# ---------------------------------------------------------------------------
# Админ-панель
# ---------------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin"))
    if request.method == "POST":
        username = norm(request.form.get("username", ""))
        password = request.form.get("password", "")

        if login_blocked("admin_login"):
            flash("Слишком много попыток входа. Попробуйте позже (15 минут).", "error")
            return render_template("login.html")

        # сравнение по времени-безопасности через сохранённый хеш
        if check_password_hash(config.ADMIN_PASSWORD_HASH, password) and username == config.ADMIN_USERNAME:
            session["admin_logged_in"] = True
            login_success("admin_login")
            flash("Вы вошли в систему.", "ok")
            return redirect(url_for("admin"))
        # Считаем хеш в любом случае, чтобы не раскрывать существование пользователя
        _ = check_password_hash(config.ADMIN_PASSWORD_HASH, "dummy")
        login_failed("admin_login")
        flash("Неверный логин или пароль.", "error")
    return render_template("login.html")


@app.route("/admin/logout")
def logout():
    session.pop("admin_logged_in", None)
    flash("Вы вышли из системы.", "ok")
    return redirect(url_for("login"))


@app.route("/admin")
@login_required
def admin():
    q = norm(request.args.get("q", ""))
    status = norm(request.args.get("status", ""))
    contragent = norm(request.args.get("contragent", ""))
    sort = norm(request.args.get("sort", "newest"))

    query = Invoice.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Invoice.номер_счета.ilike(like),
                Invoice.контрагент.ilike(like),
                Invoice.отправитель.ilike(like),
                Invoice.товар_услуга.ilike(like),
                Invoice.примечание.ilike(like),
                Invoice.назначение.ilike(like),
            )
        )
    if status and status in [s[0] for s in STATUSES]:
        query = query.filter(Invoice.статус == status)
    if contragent:
        query = query.filter(Invoice.контрагент.ilike(f"%{contragent}%"))

    # сортировка
    if sort == "date_desc":
        query = query.order_by(Invoice.дата.desc(), Invoice.created_at.desc())
    elif sort == "date_asc":
        query = query.order_by(Invoice.дата.asc(), Invoice.created_at.asc())
    elif sort == "amount_desc":
        query = query.order_by(Invoice.сумма.desc(), Invoice.created_at.desc())
    elif sort == "amount_asc":
        query = query.order_by(Invoice.сумма.asc(), Invoice.created_at.asc())
    elif sort == "contragent":
        query = query.order_by(Invoice.контрагент.asc(), Invoice.created_at.desc())
    else:
        query = query.order_by(Invoice.created_at.desc())

    page = request.args.get("page", 1, type=int)
    pagination = query.paginate(page=page, per_page=15, error_out=False)
    invoices = pagination.items

    # итоги по всем счетам (с учётом фильтров, без пагинации)
    base = Invoice.query
    if q:
        like = f"%{q}%"
        base = base.filter(
            db.or_(
                Invoice.номер_счета.ilike(like),
                Invoice.контрагент.ilike(like),
                Invoice.отправитель.ilike(like),
                Invoice.товар_услуга.ilike(like),
                Invoice.примечание.ilike(like),
                Invoice.назначение.ilike(like),
            )
        )
    if status and status in [s[0] for s in STATUSES]:
        base = base.filter(Invoice.статус == status)
    if contragent:
        base = base.filter(Invoice.контрагент.ilike(f"%{contragent}%"))
    all_rows = base.all()
    total = sum((i.сумма or 0) for i in all_rows)
    paid = sum((i.сумма or 0) for i in all_rows if i.статус == "оплачен")
    excluded = sum((i.сумма or 0) for i in all_rows if i.статус == "не учитывать")
    payable = total - paid - excluded

    return render_template(
        "admin/index.html",
        invoices=invoices,
        pagination=pagination,
        q=q,
        status=status,
        contragent=contragent,
        sort=sort,
        total=total,
        paid=paid,
        excluded=excluded,
        payable=payable,
    )


@app.route("/admin/invoice/<int:id>")
@login_required
def invoice_detail(id):
    inv = db.session.get(Invoice, id)
    if inv is None:
        abort(404)
    return render_template("admin/detail.html", invoice=inv)


@app.route("/admin/invoice/<int:id>/status", methods=["POST"])
@login_required
def invoice_status(id):
    inv = db.session.get(Invoice, id)
    if inv is None:
        abort(404)
    status = norm(request.form.get("status", ""))
    if status in [s[0] for s in STATUSES]:
        inv.статус = status
        db.session.commit()
        flash(f"Статус счёта #{inv.id} обновлён.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/invoice/<int:id>/delete", methods=["POST"])
@login_required
def invoice_delete(id):
    inv = db.session.get(Invoice, id)
    if inv is None:
        abort(404)
    if inv.файл:
        fp = os.path.join(app.config["UPLOAD_FOLDER"], inv.файл)
        if os.path.exists(fp):
            os.remove(fp)
    db.session.delete(inv)
    db.session.commit()
    flash(f"Счёт #{id} удалён.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/file/<int:id>")
@login_required
def invoice_file(id):
    inv = db.session.get(Invoice, id)
    if inv is None or not inv.файл:
        abort(404)
    fp = os.path.join(app.config["UPLOAD_FOLDER"], inv.файл)
    if not os.path.exists(fp):
        abort(404)
    download_name = safe_basename(inv.оригинал_файла or inv.файл)
    return send_from_directory(
        app.config["UPLOAD_FOLDER"], inv.файл, as_attachment=True, download_name=download_name
    )


# ---------------------------------------------------------------------------
# Управление контрагентами (пользователями) в админке
# ---------------------------------------------------------------------------
@app.route("/admin/users")
@login_required
def admin_users():
    users = Contragent.query.order_by(Contragent.created_at.desc()).all()
    return render_template("admin/users.html", users=users)


@app.route("/admin/users/add", methods=["POST"])
@login_required
def admin_user_add():
    email = norm(request.form.get("email", "")).lower()[:256]
    password = request.form.get("password", "")
    название = norm(request.form.get("название", ""))[:256]
    контрагент = norm(request.form.get("контрагент", ""))[:256]
    телефон = norm(request.form.get("телефон", ""))[:64]

    errors = []
    if not is_valid_email(email):
        errors.append("Укажите корректный email.")
    if len(password) < 8:
        errors.append("Пароль должен быть не короче 8 символов.")
    if not название:
        errors.append("Укажите название организации.")
    if Contragent.query.filter_by(email=email).first():
        errors.append("Этот email уже зарегистрирован.")

    if errors:
        for e in errors:
            flash(e, "error")
    else:
        c = Contragent(
            email=email,
            password_hash=generate_password_hash(password),
            название=название,
            контрагент=контрагент or название,
            телефон=телефон,
        )
        db.session.add(c)
        db.session.commit()
        flash(f"Контрагент {email} создан.", "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:id>/toggle", methods=["POST"])
@login_required
def admin_user_toggle(id):
    user = db.session.get(Contragent, id)
    if user is None:
        abort(404)
    user.is_active = not user.is_active
    db.session.commit()
    state = "разблокирован" if user.is_active else "заблокирован"
    flash(f"Доступ пользователя {user.email} {state}.", "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:id>/reset", methods=["POST"])
@login_required
def admin_user_reset(id):
    user = db.session.get(Contragent, id)
    if user is None:
        abort(404)
    password = request.form.get("password", "")
    if len(password) < 8:
        flash("Новый пароль должен быть не короче 8 символов.", "error")
    else:
        user.password_hash = generate_password_hash(password)
        db.session.commit()
        flash(f"Пароль пользователя {user.email} изменён.", "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:id>/delete", methods=["POST"])
@login_required
def admin_user_delete(id):
    user = db.session.get(Contragent, id)
    if user is None:
        abort(404)
    if user.invoices.count() > 0:
        flash(
            f"Нельзя удалить: у пользователя {user.email} есть счета. "
            "Сначала заблокируйте доступ.",
            "error",
        )
        return redirect(url_for("admin_users"))
    db.session.delete(user)
    db.session.commit()
    flash(f"Пользователь {user.email} удалён.", "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/export")
@login_required
def export():
    """Экспорт счетов в XLSX (совместимо с Реестром платежей)."""
    invoices = Invoice.query.order_by(Invoice.created_at.asc()).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Счета"

    headers = ["№ счета", "Дата выставления", "Контрагент", "Товар/услуга",
               "Сумма", "Примечание", "Назначение", "Файл", "Статус", "Отправитель", "Email", "Телефон", "Дата загрузки"]

    thin = Side(style="thin", color="B0B0B0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(bold=True, color="FFFFFF")

    for col, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = border

    for r, inv in enumerate(invoices, 2):
        values = [
            inv.номер_счета,
            inv.дата,
            inv.контрагент,
            inv.товар_услуга,
            inv.сумма,
            inv.примечание,
            inv.назначение,
            inv.оригинал_файла or inv.файл,
            inv.статус,
            inv.отправитель,
            inv.email,
            inv.телефон,
            inv.created_at.strftime("%d.%m.%Y %H:%M") if inv.created_at else "",
        ]
        for col, v in enumerate(values, 1):
            c = ws.cell(row=r, column=col, value=v)
            c.border = border
            if col == 5 and isinstance(v, (int, float)):
                c.number_format = '#,##0.00'
                c.alignment = Alignment(horizontal="right")

    widths = [18, 12, 22, 28, 12, 28, 18, 28, 14, 22, 22, 14, 16]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"Счета_{stamp}.xlsx"
    out_path = os.path.join(app.config["EXPORT_FOLDER"], out_name)
    wb.save(out_path)

    return send_file(out_path, as_attachment=True, download_name=out_name)


# ---------------------------------------------------------------------------
# Инициализация БД
# ---------------------------------------------------------------------------
def init_db():
    with app.app_context():
        db.create_all()


if __name__ == "__main__":
    init_db()
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug)
