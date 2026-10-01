# -*- coding: utf-8 -*-
"""Модели данных приложения «Приём счетов»."""
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


class Contragent(db.Model):
    """Аккаунт контрагента (поставщика) для личного кабинета."""
    __tablename__ = "contragents"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(256), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    название = db.Column(db.String(256), default="")       # название организации
    контрагент = db.Column(db.String(256), default="")     # наименование из справочника (МТС и т.п.)
    телефон = db.Column(db.String(64), default="")
    is_active = db.Column(db.Boolean, default=True)        # False — доступ заблокирован админом
    last_seen = db.Column(db.DateTime, nullable=True)      # последняя активность (UTC)
    created_at = db.Column(db.DateTime, default=utcnow)

    invoices = db.relationship("Invoice", backref="contragent_user", lazy="dynamic")

    def __repr__(self):
        return f"<Contragent {self.email}>"


class Invoice(db.Model):
    """Счёт, загруженный контрагентом через форму."""
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    contragent_id = db.Column(db.Integer, db.ForeignKey("contragents.id"), nullable=True, index=True)
    номер_счета = db.Column(db.String(128), default="", index=True)
    дата = db.Column(db.String(32), default="")          # ДД.ММ.ГГГГ
    контрагент = db.Column(db.String(256), default="", index=True)
    товар_услуга = db.Column(db.String(512), default="")
    сумма = db.Column(db.Float, nullable=True)
    примечание = db.Column(db.String(512), default="")
    назначение = db.Column(db.String(256), default="")
    файл = db.Column(db.String(512), default="")         # сохранённое имя файла
    оригинал_файла = db.Column(db.String(512), default="")  # имя файла от отправителя

    # Контакты отправителя
    отправитель = db.Column(db.String(256), default="")   # ФИО / название
    email = db.Column(db.String(256), default="")
    телефон = db.Column(db.String(64), default="")

    статус = db.Column(db.String(32), default="новый", index=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "номер_счета": self.номер_счета,
            "дата": self.дата,
            "контрагент": self.контрагент,
            "товар_услуга": self.товар_услуга,
            "сумма": self.сумма,
            "примечание": self.примечание,
            "назначение": self.назначение,
            "файл": self.файл,
            "оригинал_файла": self.оригинал_файла,
            "отправитель": self.отправитель,
            "email": self.email,
            "телефон": self.телефон,
            "статус": self.статус,
            "created_at": self.created_at.strftime("%d.%m.%Y %H:%M") if self.created_at else "",
        }

    def __repr__(self):
        return f"<Invoice #{self.id} {self.контрагент}>"
