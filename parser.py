# -*- coding: utf-8 -*-
"""
Разбор загруженного счёта (PDF со слоем текста или скан через OCR).

Возвращает dict с полями, совместимыми с «Реестром платежей»:
  номер_счета, дата, контрагент, товар_услуга, сумма, примечание, назначение

Портировано из Реестр/приложение/логика.py.
"""
import datetime as dt
import re
from pathlib import Path

import pdfplumber

# ----------------------------------------------------------------------------
# Справочники (синхронизировано с «Реестром платежей»)
# ----------------------------------------------------------------------------

# Лицевой счёт -> (назначение, товар/услуга)
ACCOUNTS = {
    "566002992955": ("центр.офис", "услуги интернет"),
    "266383423919": ("центр.офис", "услуги интернет"),
    "566000014116": ("центр.офис", "услуги связи"),
    "50108024545": ("центр.офис", "услуги связи"),
    "50108024562": ("центр.офис", "услуги связи"),
    "566001818972": ("Сухой-Лог", "услуги интернет"),
    "566003707463": ("Сухой-Лог", "услуги связи"),
    "566002868230": ("Курьи", "услуги интернет"),
    "266392447698": ("Курьи", "услуги интернет"),
    "566002868241": ("Курьи", "услуги связи"),
    "566002076465": ("Асбест", "услуги интернет"),
    "566003566459": ("Асбест", "вирт-ная АТС"),
    "566002213705": ("Каменск", "услуги связи"),
}

# Подразделения по номеру счёта для счетов ОВК (подписано от руки).
SCAN_PURPOSE_BY_NUM = {
    "3013": "Сухой-Лог",
    "3028": "Асбест",
    "2856": "центр.офис",
    "2825": "Сухой-Лог",
    "2800": "Сухой-Лог",
    "2753": "Сухой-Лог",
    "2754": "Сухой-Лог",
    "3036": "Сухой-Лог",
    "3035": "Сухой-Лог",
    "3041": "Сухой-Лог",
    "3082": "Сухой-Лог",
    "3081": "Асбест",
}

# Точные значения для известных счетов ОВК (номер счёта -> (товар_услуга, примечание)).
SCAN_INFO_BY_NUM = {
    "3013": ("ремонт", "Диагностика монитора Samsung S24D300"),
    "3028": ("заправка", "Заправка картриджей (32 поз.)"),
    "2856": ("ремонт", "Замена платы БП монитора Samsung 940T"),
    "2825": ("ремонт", "Замена главной платы МФУ HP M426fdn"),
    "2800": ("обслуживание", "ТО МФУ HP M177fw"),
    "2753": ("заправка", "Заправка картриджей (27 поз.)"),
    "2754": ("обслуживание", "ТО, замена картриджей МФУ HP W76dw"),
    "3036": ("заправка", "Заправка картриджей (27 поз.)"),
    "3035": ("расходники", "Тонер-картриджи Kyocera PA3500"),
    "3041": ("заправка", "Заправка МФУ HP Neverstop 1200w"),
    "3082": ("ремонт", "Ремонт принтера HP M608"),
    "3081": ("ремонт", "Ремонт подсветки монитора Samsung 940fn"),
}

# ----------------------------------------------------------------------------
# Вспомогательные
# ----------------------------------------------------------------------------

def norm(s):
    return (s or "").replace("\u00a0", " ").replace("\u202f", " ").strip()


def to_float(s):
    s = norm(str(s)).replace(" ", "")
    s = s.replace(",", ".").replace("-", ".")
    m = re.match(r"-?(\d+(?:\.\d{1,2})?)", s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_date(s):
    s = (s or "").strip()
    m = re.match(r"(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})", s)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def _find_account(text):
    compact = text.replace(" ", "")
    for acc in ACCOUNTS:
        if acc in compact:
            return acc
    return None


def _find_account_in_filename(name):
    compact = name.replace(" ", "")
    for acc in ACCOUNTS:
        if acc in compact:
            return acc
    return None


def _last_num(s):
    nums = re.findall(r"\d[\d\s]*(?:[.,\-]\d{1,2})?", s)
    if not nums:
        return None
    return to_float(nums[-1])


def _extract_goods_from_items(text):
    """Извлечь наименования позиций товаров из таблицы счёта (ОВК и др.)."""
    items = []
    for line in text.splitlines():
        t = norm(line)
        m = re.match(r"^(\d{1,3})\s+(.+)$", t)
        if not m:
            continue
        rest = m.group(2)
        # отрезаем хвост: "1 шт 87 900,00 87 900,00"
        rest = re.sub(
            r"\s+\d+(?:[.,]\d+)?\s*(?:шт|усл\.ед|ед\.?)?\s*[\d\s.,]*$",
            "", rest,
        )
        rest = rest.strip(" ,")
        if not rest:
            continue
        items.append(rest[:60].rstrip(" ,"))

    items = items[:20]
    if not items:
        return ""
    # одна позиция — просто название; несколько — нумеруем с новой строки
    if len(items) == 1:
        return items[0]
    return "\n".join(f"{i}. {name}" for i, name in enumerate(items, 1))


# ----------------------------------------------------------------------------
# Разбор текстового PDF
# ----------------------------------------------------------------------------

def _parse_invoice_pdf(path):
    with pdfplumber.open(str(path)) as pdf:
        text = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
    name = Path(path).name

    acc = _find_account(text) or _find_account_in_filename(name)
    purpose, goods = ACCOUNTS.get(acc, ("", ""))
    note = f"л/с {acc}" if acc else ""

    # контрагент
    if acc in ("266383423919", "266392447698") or "МТС" in text or "Мобильные Телесистемы" in text:
        supplier = "ПАО МТС"
    elif "Ростелеком" in text:
        supplier = "ПАО РОСТЕЛЕКОМ"
    elif "Интернет-Про" in text or "Интернет Про" in text:
        supplier = "ИНТЕРНЕТ-ПРО"
        if not goods:
            goods = "хостинг"
        if not purpose:
            purpose = "центр.офис"
    else:
        supplier = ""
        # Поставщик из счёта: "Поставщик ООО "ОВК", ИНН ..."
        mm = re.search(r"Поставщик\s*(?:\(Исполнитель\):?\s*)?(.+?)\s*,\s*ИНН", text)
        if mm:
            supplier = norm(mm.group(1).replace('"', '').strip())
        elif "ОВК" in text:
            supplier = "ООО ОВК"

    # товары для счетов с таблицей позиций (ОВК и др.)
    if not goods:
        g = _extract_goods_from_items(text)
        if g:
            goods = g

    # дата
    date_s = ""
    months_re = "(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
    m = None
    for line in text.splitlines():
        if re.search(r"счет|счёт", line, re.I):
            m = re.search(r"от\s+(\d{1,2})\s+" + months_re + r"\s+(\d{4})", line, re.I)
            if m:
                break
    if not m:
        m = re.search(r"от\s+(\d{1,2})\s+" + months_re + r"\s+(\d{4})", text, re.I)
    if m:
        months = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5,
                  "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10,
                  "ноября": 11, "декабря": 12}
        d = int(m.group(1))
        mo = months[m.group(2).lower()]
        y = int(m.group(3))
        date_s = f"{d:02d}.{mo:02d}.{y}"
    else:
        m = re.search(r"(?:СЧЕТ|Счет|счет)\s*[N№][^\n]{0,40}?от\s+(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})", text)
        if not m:
            m = re.search(r"(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})", text)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000
            date_s = f"{d:02d}.{mo:02d}.{y}"

    # номер счёта
    num = ""
    # 1) МТС: "№ 266383423919 / 6675506378" — номер счёта идёт после слэша
    mm = re.search(r"№\s*\d{6,}\s*/\s*(\d[\d\-]*)", text)
    if mm:
        num = mm.group(1)
    # 2) "Счет на оплату № 3086" (ОВК)
    if not num:
        mm = re.search(r"Счет\s+на\s+оплату\s+[N№]+\s*([A-Za-zА-Яа-я0-9/\-_.]+)", text, re.I)
        if mm:
            num = mm.group(1).strip(" ,.")
    # 3) "Счет № <номер>" / "СЧЕТ No <номер>" (Ростелеком, Интернет-Про)
    if not num:
        mm = re.search(r"СЧЕТ\s*(?:No|NQ|N2|№|N)\s*([A-Za-zА-Яа-я0-9/\-_.]+)", text, re.I)
        if mm:
            num = mm.group(1).strip(" ,.")
    # 4) из имени файла: "...счет6675506378..."
    if not num:
        mf = re.search(r"счет[_\s]*(?:No|N|NQ|N2|№)?[_\s]*(\d{4,})", name, re.I)
        if mf:
            num = mf.group(1)

    # сумма
    amount = None
    lines = [norm(x) for x in text.splitlines()]

    # 1) МТС-контракт: "К оплате 31.08.2026 18645,79 руб."
    mm = re.search(r"К оплате\s+\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}\s+(\d[\d\s]*[.,]\d{2})\s*руб", text)
    if mm:
        amount = to_float(mm.group(1))

    # 2) Ростелеком: "Итого к оплате по счету ..."
    if amount is None:
        last_rt = None
        for l in lines:
            if "Итого к оплате по счету" in l:
                v = _last_num(l)
                if v is not None:
                    last_rt = v
        amount = last_rt

    # 3) "Итого к оплате"
    if amount is None:
        for l in lines:
            if "Итого к оплате" in l:
                v = _last_num(l)
                if v is not None:
                    amount = v
                break

    # 4) "Всего к оплате"
    if amount is None:
        for l in lines:
            if "Всего к оплате" in l:
                v = _last_num(l)
                if v is not None:
                    amount = v
                break

    # 5) "на сумму ... руб."
    if amount is None:
        mm = re.search(r"на сумму\s+([\d\s]+[.,]\d{2})", text)
        if mm:
            amount = to_float(mm.group(1))

    return {
        "номер_счета": num,
        "дата": date_s,
        "контрагент": supplier,
        "товар_услуга": goods,
        "сумма": amount,
        "примечание": note,
        "назначение": purpose,
    }


# ----------------------------------------------------------------------------
# Разбор скана (PDF-изображения) через OCR
# ----------------------------------------------------------------------------

def _is_scan(path):
    try:
        with pdfplumber.open(str(path)) as pdf:
            if not pdf.pages:
                return True
            for pg in pdf.pages:
                if (pg.extract_text() or "").strip():
                    return False
    except Exception:
        return True
    return True


def _purpose_from_text(text):
    low = (text or "").lower()
    if "сухой" in low or "сухолож" in low:
        return "Сухой-Лог"
    if "асбест" in low:
        return "Асбест"
    if "курь" in low:
        return "Курьи"
    if "каменск" in low:
        return "Каменск"
    return ""


def _goods_category(text_low):
    if "тонер" in text_low:
        return "расходники"
    if "заправ" in text_low:
        return "заправка"
    if "обслуживан" in text_low or "технич" in text_low:
        return "обслуживание"
    if any(k in text_low for k in ("ремонт", "замена", "диагност", "восстанов")):
        return "ремонт"
    return ""


def _note_from_text(text):
    keys = ("монитор", "мфу", "принтер", "картридж", "тонер", "плат",
            "ролик", "заправка", "ремонт", "замена", "диагностик",
            "обслуживан", "samsung", "hp", "canon", "kyocera", "lbp")
    for line in text.splitlines():
        t = re.sub(r"\s+", " ", line.strip()).strip()
        lt = t.lower()
        if len(t) < 6:
            continue
        if re.search(r"ол-во|ед\.|ена\b|итого|всего|наименован|оплат|люа", lt):
            continue
        if any(k in lt for k in keys):
            return t
    return ""


def _extract_scan_images(pdf_path, out_dir):
    import pypdf
    reader = pypdf.PdfReader(str(pdf_path))
    saved = []
    for i, pg in enumerate(reader.pages):
        for j, img in enumerate(pg.images):
            name = f"page{i + 1:02d}.jpg"
            fp = Path(out_dir) / name
            fp.write_bytes(img.data)
            saved.append(fp)
    return saved


def _ocr_image_text(img):
    """OCR одного изображения в текст. Кросс-платформенно: winocr (Windows) / pytesseract (Linux)."""
    # 1) Windows: winocr
    try:
        import winocr
        r = winocr.recognize_pil_sync(img, "ru")
        return "\n".join(ln["text"] for ln in r["lines"])
    except Exception:
        pass
    # 2) Linux: pytesseract + установленный tesseract-ocr
    try:
        import pytesseract
        return pytesseract.image_to_string(img, lang="rus")
    except Exception as e:
        raise RuntimeError(
            "OCR недоступен. Установите tesseract-ocr (Linux: apt install tesseract-ocr tesseract-ocr-rus) "
            "или winocr (Windows)."
        ) from e


def _parse_scan_pdf(path):
    """OCR скана: возвращает dict с разобранными полями."""
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("Не установлен Pillow.") from e

    scan_path = Path(path)
    tmp = scan_path.parent / "_scan_pages"
    tmp.mkdir(exist_ok=True)

    try:
        images = _extract_scan_images(scan_path, tmp)
    except Exception as e:
        raise RuntimeError(f"Не удалось прочитать скан: {e}") from e

    if not images:
        return {}

    # распознаём первую страницу (основные данные счёта)
    jpg = sorted(images)[0]
    img = Image.open(jpg)
    text = _ocr_image_text(img)

    num = ""
    m = re.search(r"Счет на оплату[^\d]{0,5}(\d{3,4})", text, re.I)
    if not m:
        m = re.search(r"(?:Счет|Счёт)\s*(?:на оплату\s*)?[N№NQ2]{1,3}\s*(\d{3,4})", text, re.I)
    if m:
        num = m.group(1)

    date_s = ""
    m = re.search(r"от\s+(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})", text, re.I)
    if m:
        months = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5,
                  "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10,
                  "ноября": 11, "декабря": 12}
        d = int(m.group(1))
        mo = months[m.group(2).lower()]
        y = int(m.group(3))
        date_s = f"{d:02d}.{mo:02d}.{y}"
    else:
        m = re.search(r"(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})", text)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000
            date_s = f"{d:02d}.{mo:02d}.{y}"

    amount = None
    m = re.search(r"на сумму\s+(.+?)руб", text, re.I)
    if m:
        chunk = m.group(1)
        chunk = chunk.replace("З", "3").replace("з", "3").replace("О", "0").replace("о", "0")
        amount = to_float(chunk)

    text_low = text.lower()
    known = SCAN_INFO_BY_NUM.get(num)
    if known:
        goods, note = known
    else:
        goods = _goods_category(text_low)
        note = _note_from_text(text)
    purpose = _purpose_from_text(text) or SCAN_PURPOSE_BY_NUM.get(num, "")
    supplier = "ООО ОВК"

    return {
        "номер_счета": num,
        "дата": date_s,
        "контрагент": supplier,
        "товар_услуга": goods,
        "сумма": amount,
        "примечание": note,
        "назначение": purpose,
    }


# ----------------------------------------------------------------------------
# Главная функция
# ----------------------------------------------------------------------------

def parse_invoice_file(path):
    """Разбор загруженного файла счёта. Возвращает dict полей."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        if _is_scan(path):
            return _parse_scan_pdf(path)
        return _parse_invoice_pdf(path)
    except Exception:
        # не удалось распознать — возвращаем пустые поля (заполнит вручную)
        return {}
