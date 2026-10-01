# -*- coding: utf-8 -*-
"""Конфигурация приложения «Приём счетов»."""
import os
import secrets
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    SECRET_KEY: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    SQLALCHEMY_DATABASE_URI: str = os.getenv(
        "DATABASE_URL", "sqlite:///" + os.path.join(os.path.dirname(__file__), "instance", "scheta.db")
    )

    # Админ (сотрудник ФОРЭС), вход по логину/паролю
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")
    # Хеш пароля администратора (вычисляется в __post_init__)
    ADMIN_PASSWORD_HASH: str = ""

    SITE_URL: str = os.getenv("SITE_URL", "http://127.0.0.1:5000")
    SITE_NAME: str = "Приём счетов — ООО «ФОРЭС»"

    # Максимальный размер загружаемого файла, байт (по умолчанию 15 МБ)
    MAX_UPLOAD_SIZE: int = int(os.getenv("MAX_UPLOAD_SIZE", str(15 * 1024 * 1024)))

    # Разрешённые расширения вложений
    ALLOWED_EXTENSIONS: tuple = ("pdf", "jpg", "jpeg", "png", "doc", "docx", "xls", "xlsx")

    # Папки для загрузок и экспорта
    UPLOAD_FOLDER: str = os.path.join(os.path.dirname(__file__), "uploads")
    EXPORT_FOLDER: str = os.path.join(os.path.dirname(__file__), "exports")

    def __post_init__(self) -> None:
        if not self.SECRET_KEY:
            object.__setattr__(self, "SECRET_KEY", secrets.token_hex(32))
            import sys
            print(
                "ВНИМАНИЕ: SECRET_KEY не задан в .env — сессии будут сбрасываться "
                "при каждом перезапуске. Задайте постоянный SECRET_KEY.",
                file=sys.stderr,
            )
        # Пароль администратора должен быть задан надёжным (мин. 8 символов).
        if len(self.ADMIN_PASSWORD) < 8:
            raise RuntimeError(
                "ADMIN_PASSWORD не задан или слишком короткий (мин. 8 символов). "
                "Укажите его в файле .env"
            )
        if self.ADMIN_PASSWORD in ("", "change-me-please", "admin", "password", "12345678"):
            raise RuntimeError(
                "ADMIN_PASSWORD является дефолтным/слабым. Задайте уникальный пароль в .env."
            )
        from werkzeug.security import generate_password_hash
        object.__setattr__(self, "ADMIN_PASSWORD_HASH", generate_password_hash(self.ADMIN_PASSWORD))
        # создаём каталог БД (instance) и рабочие папки
        db_dir = os.path.dirname(self.SQLALCHEMY_DATABASE_URI.replace("sqlite:///", ""))
        for folder in (db_dir, self.UPLOAD_FOLDER, self.EXPORT_FOLDER):
            os.makedirs(folder, exist_ok=True)


config = Config()
