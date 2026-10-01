# Установка на VPS (Ubuntu 24.04) + Caddy

Сайт: **https://scheta.netfree.pro**

## 0. Предварительно

- Домен `scheta.netfree.pro` должен иметь A-запись на IP-адрес VPS.
- Доступ по SSH к серверу.

## 1. Установка системных пакетов

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git tesseract-ocr tesseract-ocr-rus
```

`tesseract-ocr-rus` нужен для распознавания сканов счетов (OCR).

## 2. Клонирование репозитория

```bash
sudo mkdir -p /opt/schet
sudo chown $USER:$USER /opt/schet
git clone https://github.com/bi333on/schot-ru-fores.git /opt/schet
cd /opt/schet
```

## 3. Виртуальное окружение и зависимости

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 4. Настройка .env

```bash
cd /opt/schet
cat > .env << 'EOF'
SECRET_KEY=<длинная случайная строка>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<ваш надёжный пароль>
SITE_URL=https://scheta.netfree.pro
MAX_UPLOAD_SIZE=15728640
EOF
```

Сгенерировать секретный ключ:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

## 5. Инициализация БД

```bash
cd /opt/schet
.venv/bin/python -c "from app import init_db; init_db()"
```

## 6. Права на папки

```bash
sudo mkdir -p /opt/schet/instance /opt/schet/uploads /opt/schet/exports
sudo chown -R www-data:www-data /opt/schet/instance /opt/schet/uploads /opt/schet/exports
```

## 7. systemd-сервис

```bash
sudo cp /opt/schet/schet.service /etc/systemd/system/schet.service
sudo systemctl daemon-reload
sudo systemctl enable --now schet
sudo systemctl status schet
```

Проверить, что слушает порт 5000:
```bash
curl -s http://127.0.0.1:5000/ | head
```

## 8. Caddy

Установка Caddy (если ещё не установлен):
```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy
```

Настройка:
```bash
sudo mkdir -p /var/log/caddy
sudo cp /opt/schet/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy автоматически получит и будет обновлять HTTPS-сертификат Let's Encrypt для `scheta.netfree.pro`.

## 9. Проверка

Откройте в браузере:
- `https://scheta.netfree.pro` — форма регистрации контрагента
- `https://scheta.netfree.pro/admin/login` — вход для сотрудников (логин `admin` + пароль из `.env`)

## Обновление сайта

```bash
cd /opt/schet
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart schet
```

## Примечания

- Файл `.env` НЕ хранится в git — создаётся вручную на сервере.
- `instance/`, `uploads/`, `exports/` — рабочие данные, не в git.
- OCR: на Linux используется `pytesseract` (нужен пакет `tesseract-ocr-rus`), на Windows — `winocr`.
