# Розгортання HLViewer на Ubuntu з Docker (офлайн)

Повна інструкція розгортання через Docker Compose на Ubuntu Server без доступу до інтернету.

---

## Архітектура (Docker)

```
[Браузер] → frontend_v2:3001 (nginx) → fastapi_v2:8001
                                      → db_v2:5432 (PostgreSQL 15)
fastapi_v2 → db_v2
scheduler_v2 → db_v2
```

Чотири контейнери:
| Контейнер | Образ | Порт |
|---|---|---|
| `postgres_db_v2` | postgres:15 | 5434→5432 |
| `fastapi_app_v2` | hlviewer-fastapi-v2 | 8001 |
| `frontend_app_v2` | hlviewer-frontend-v2 | 3001→80 |
| `scheduler_app_v2` | hlviewer-fastapi-v2 | — |

---

## Стратегія офлайн-розгортання

Є два варіанти:

**Варіант А (рекомендований):** Зібрати образи на локальній машині → експортувати `.tar` → імпортувати на сервері.

**Варіант Б:** Скопіювати репозиторії на сервер → зібрати образи там (потребує Linux wheels та npm пакетів).

---

## 1. Підготовка на локальній машині

### 1.1. Зібрати Docker-образи

На локальній машині де є Docker і інтернет:

```bash
cd D:/Projects/HLViewer/HLViewer

# Зібрати образи (або вони вже зібрані після docker compose up)
docker compose -f docker-compose.v2.yml build

# Переконатись що образи є
docker images | grep hlviewer
# hlviewer-fastapi-v2    latest   ...
# hlviewer-frontend-v2   latest   ...
```

Також потрібен образ PostgreSQL:
```bash
docker pull postgres:15
```

### 1.2. Експортувати образи у файли

```bash
mkdir -p docker-export

docker save hlviewer-fastapi-v2 -o docker-export/hlviewer-fastapi-v2.tar
docker save hlviewer-frontend-v2 -o docker-export/hlviewer-frontend-v2.tar
docker save postgres:15 -o docker-export/postgres-15.tar

# Стиснути для зручності
tar -czf docker-export.tar.gz docker-export/
```

Розмір: приблизно 1–2 ГБ.

### 1.3. Підготувати файли проекту

```bash
mkdir -p hlviewer-server-package

# Скопіювати docker-compose файл та конфіги
cp docker-compose.v2.yml hlviewer-server-package/
cp .env.v2 hlviewer-server-package/.env.v2.template
cp wait_for_it.sh hlviewer-server-package/

# Скопіювати nginx конфіг фронтенду
cp ../frontend/react-frontend/nginx.v2.conf hlviewer-server-package/

# Скопіювати preload JSON-файли довідників
mkdir -p hlviewer-server-package/backend/db/preload_db
cp backend/db/preload_db/FLOWTYPE.json hlviewer-server-package/backend/db/preload_db/
cp backend/db/preload_db/SYSNAME.json hlviewer-server-package/backend/db/preload_db/
cp backend/db/preload_db/EDITNAME.json hlviewer-server-package/backend/db/preload_db/

tar -czf hlviewer-server-package.tar.gz hlviewer-server-package/
```

---

## 2. Встановлення Docker на сервері (офлайн)

### 2.1. Завантажити пакети Docker на машині з інтернетом

```bash
# На Ubuntu 22.04
apt-get download \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin \
    docker-ce-rootless-extras

# Або завантажити статичний бінарник Docker з:
# https://download.docker.com/linux/static/stable/x86_64/
```

Або скачати офлайн-інсталятор через скрипт [get.docker.com](https://get.docker.com) і зберегти.

### 2.2. Встановити Docker на сервері

```bash
# Варіант зі статичним бінарником:
tar -xzf docker-25.x.x.tgz
sudo cp docker/* /usr/local/bin/

# Варіант з .deb пакетами:
sudo dpkg -i *.deb

# Запустити dockerd
sudo dockerd &
# або через systemd (якщо встановлено через deb):
sudo systemctl start docker
sudo systemctl enable docker
```

### 2.3. Встановити Docker Compose Plugin

```bash
# Завантажити з GitHub (docker/compose releases) окремо:
sudo mkdir -p /usr/local/lib/docker/cli-plugins/
sudo cp docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Перевірка
docker compose version
```

---

## 3. Копіювання файлів на сервер

> Якщо Ubuntu без GUI і доступ через Remote Desktop — оберіть один з варіантів нижче.

### Варіант А — scp з Windows PowerShell (рекомендовано)

```powershell
scp D:\Projects\HLViewer\docker-export.tar.gz user@192.168.1.100:/tmp/
scp D:\Projects\HLViewer\hlviewer-server-package.tar.gz user@192.168.1.100:/tmp/
```

Якщо SSH на нестандартному порту:
```powershell
scp -P 2222 D:\Projects\HLViewer\docker-export.tar.gz user@192.168.1.100:/tmp/
```

### Варіант Б — WinSCP (GUI)

1. Встановити **WinSCP** з [winscp.net](https://winscp.net)
2. Підключитись: протокол **SFTP**, IP сервера, порт 22
3. Перетягнути `docker-export.tar.gz` і `hlviewer-server-package.tar.gz` в `/tmp/`

### Варіант В — через RDP drive sharing

Якщо підключаєтесь до Ubuntu через RDP (xrdp):

1. У Windows клієнті RDP: **"Параметри"** → **"Локальные ресурсы"** → **"Подробнее"** → поставити галочку на диску `D:`
2. У терміналі Ubuntu:
```bash
cp /mnt/tsclient/D/Projects/HLViewer/docker-export.tar.gz /tmp/
cp /mnt/tsclient/D/Projects/HLViewer/hlviewer-server-package.tar.gz /tmp/
```

### Варіант Г — USB-носій

```bash
sudo mkdir -p /mnt/usb
sudo mount /dev/sdb1 /mnt/usb
cp /mnt/usb/docker-export.tar.gz /tmp/
cp /mnt/usb/hlviewer-server-package.tar.gz /tmp/
sudo umount /mnt/usb
```

На сервері розпакувати:
```bash
cd /tmp
tar -xzf docker-export.tar.gz
tar -xzf hlviewer-server-package.tar.gz
```

---

## 4. Імпорт Docker-образів на сервері

```bash
docker load -i /tmp/docker-export/postgres-15.tar
docker load -i /tmp/docker-export/hlviewer-fastapi-v2.tar
docker load -i /tmp/docker-export/hlviewer-frontend-v2.tar

# Перевірка
docker images
# postgres              15       ...
# hlviewer-fastapi-v2   latest   ...
# hlviewer-frontend-v2  latest   ...
```

---

## 5. Налаштування проекту на сервері

### 5.1. Структура папок

```bash
sudo mkdir -p /opt/hlviewer
sudo chown $USER:$USER /opt/hlviewer
cd /opt/hlviewer

mkdir -p backend/db/preload_db
mkdir -p backend/data/askcfgs
mkdir -p hostlibs
mkdir -p logs
```

### 5.2. Скопіювати файли конфігурації

```bash
cp /tmp/hlviewer-server-package/docker-compose.v2.yml /opt/hlviewer/
cp /tmp/hlviewer-server-package/wait_for_it.sh /opt/hlviewer/
chmod +x /opt/hlviewer/wait_for_it.sh

# Preload довідники
cp /tmp/hlviewer-server-package/backend/db/preload_db/* /opt/hlviewer/backend/db/preload_db/
```

### 5.3. Налаштувати .env файл

```bash
cp /tmp/hlviewer-server-package/.env.v2.template /opt/hlviewer/.env.v2
nano /opt/hlviewer/.env.v2
```

Заповнити (змінити значення за замовчуванням!):
```env
POSTGRES_DB=hostlib_db_v2
POSTGRES_USER=hlviewer
POSTGRES_PASSWORD=НАДІЙНИЙ_ПАРОЛЬ_БД

DB_HOST=localhost
DB_PORT=5434
DATABASE_URL=postgresql+asyncpg://hlviewer:НАДІЙНИЙ_ПАРОЛЬ_БД@localhost:5434/hostlib_db_v2

JWT_SECRET=МІНІМУМ_32_ВИПАДКОВИХ_СИМВОЛИ
ADMIN_USERNAME=admin
ADMIN_PASSWORD=НАДІЙНИЙ_ПАРОЛЬ_АДМІНА
DEFAULT_USERNAME=viewer
DEFAULT_PASSWORD=ПАРОЛЬ_VIEWER
AUTO_LOGIN=false

CORS_ORIGINS=http://ВАШ_IP:3001,http://ВАШ_ДОМЕН

TZ=Europe/Kyiv
DEBUG=false

BOT_TOKEN=           # опціонально: Telegram бот
EMAIL_PASSWORD=      # опціонально: email нотифікації
DPD_USERNAME=        # опціонально: DPD API
DPD_PASSWORD=
```

### 5.4. Оновити docker-compose.v2.yml

```bash
nano /opt/hlviewer/docker-compose.v2.yml
```

Змінити паролі БД щоб відповідали `.env.v2`:
```yaml
  db_v2:
    environment:
      POSTGRES_DB: hostlib_db_v2
      POSTGRES_USER: hlviewer               # ← змінити
      POSTGRES_PASSWORD: НАДІЙНИЙ_ПАРОЛЬ_БД # ← змінити
```

І в fastapi_v2/scheduler_v2:
```yaml
    environment:
      - DATABASE_URL=postgresql+asyncpg://hlviewer:НАДІЙНИЙ_ПАРОЛЬ_БД@db_v2:5432/hostlib_db_v2
```

### 5.5. Налаштувати nginx конфіг

```bash
cp /tmp/hlviewer-server-package/nginx.v2.conf /opt/hlviewer/
```

Якщо потрібно змінити порт або host — відредагувати відповідно.

---

## 6. Перший запуск

```bash
cd /opt/hlviewer

# Запустити тільки БД спочатку (ініціалізація)
docker compose -f docker-compose.v2.yml up -d db_v2
sleep 5

# Перевірити БД
docker compose -f docker-compose.v2.yml logs db_v2

# Запустити решту
docker compose -f docker-compose.v2.yml up -d

# Перевірити всі контейнери
docker compose -f docker-compose.v2.yml ps
```

Очікуваний результат:
```
NAME                SERVICE       STATUS    PORTS
fastapi_app_v2      fastapi_v2    running   0.0.0.0:8001->8001/tcp
frontend_app_v2     frontend_v2   running   0.0.0.0:3001->80/tcp
postgres_db_v2      db_v2         running   0.0.0.0:5434->5432/tcp
scheduler_app_v2    scheduler_v2  running
```

---

## 7. Перевірка після запуску

```bash
# Логи бекенду (має бути "Application startup complete")
docker compose -f docker-compose.v2.yml logs fastapi_v2 --tail=30

# Логи фронтенду
docker compose -f docker-compose.v2.yml logs frontend_v2 --tail=10

# Логи планувальника
docker compose -f docker-compose.v2.yml logs scheduler_v2 --tail=10
```

Відкрити в браузері:
- Frontend: `http://ВАШ_IP:3001/`
- API Swagger: `http://ВАШ_IP:8001/docs`

---

## 8. Налаштування firewall

```bash
sudo ufw allow 3001/tcp    # фронтенд
sudo ufw allow 22/tcp      # SSH
# 8001 та 5434 НЕ відкривати — доступні тільки всередині Docker мережі
sudo ufw enable
```

Або якщо nginx на хості (не в Docker) проксіює :80 → :3001:
```bash
sudo ufw allow 80/tcp
sudo ufw deny 3001/tcp     # закрити прямий доступ до Docker порту
```

---

## 9. Автозапуск після перезавантаження сервера

Docker restart policy `always` вже налаштована в compose-файлі. Але потрібно щоб Docker запускався автоматично:

```bash
sudo systemctl enable docker
```

Перевірка після ребуту:
```bash
docker compose -f /opt/hlviewer/docker-compose.v2.yml ps
```

Або додати systemd сервіс для автозапуску compose:

```bash
sudo nano /etc/systemd/system/hlviewer-docker.service
```

```ini
[Unit]
Description=HLViewer Docker Compose
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/hlviewer
ExecStart=docker compose -f docker-compose.v2.yml up -d
ExecStop=docker compose -f docker-compose.v2.yml down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable hlviewer-docker.service
```

---

## 10. Оновлення (наступний раз)

### Оновлення образів (після змін в коді)

На локальній машині:
```bash
# Перезібрати
docker compose -f docker-compose.v2.yml build fastapi_v2
docker compose -f docker-compose.v2.yml build frontend_v2

# Експортувати
docker save hlviewer-fastapi-v2 -o hlviewer-fastapi-v2-new.tar
docker save hlviewer-frontend-v2 -o hlviewer-frontend-v2-new.tar
```

На сервері:
```bash
# Завантажити нові образи
docker load -i /tmp/hlviewer-fastapi-v2-new.tar
docker load -i /tmp/hlviewer-frontend-v2-new.tar

# Перезапустити
cd /opt/hlviewer
docker compose -f docker-compose.v2.yml up -d --no-deps fastapi_v2 scheduler_v2
docker compose -f docker-compose.v2.yml up -d --no-deps frontend_v2
```

### Оновлення preload-довідників (SYSNAME, EDITNAME, FLOWTYPE)

```bash
# Скопіювати нові JSON
cp /tmp/new-json/*.json /opt/hlviewer/backend/db/preload_db/

# Викликати preload через API:
curl -X POST http://localhost:8001/preload_data/ \
     -H "Cookie: hlviewer_token=ВАШ_ТОКЕН"
# або через Swagger UI: http://ВАШ_IP:8001/docs → POST /preload_data/
```

---

## 11. Корисні команди

```bash
cd /opt/hlviewer

# Статус контейнерів
docker compose -f docker-compose.v2.yml ps

# Логи всіх сервісів
docker compose -f docker-compose.v2.yml logs --tail=50

# Логи конкретного сервісу
docker compose -f docker-compose.v2.yml logs -f fastapi_v2

# Увійти в контейнер бекенду
docker exec -it fastapi_app_v2 bash

# Увійти в PostgreSQL
docker exec -it postgres_db_v2 psql -U hlviewer -d hostlib_db_v2

# Перезапустити конкретний сервіс
docker compose -f docker-compose.v2.yml restart fastapi_v2

# Зупинити всі
docker compose -f docker-compose.v2.yml down

# Запустити всі
docker compose -f docker-compose.v2.yml up -d
```

---

## 12. Структура на сервері

```
/opt/hlviewer/
├── docker-compose.v2.yml
├── wait_for_it.sh
├── nginx.v2.conf
├── .env.v2
├── backend/
│   ├── db/
│   │   └── preload_db/
│   │       ├── FLOWTYPE.json
│   │       ├── SYSNAME.json
│   │       └── EDITNAME.json
│   └── data/
│       └── askcfgs/          # CFG файли (монтуються в контейнер)
└── hostlibs/                 # архіви хостлібів (монтуються в контейнер)
```

> **Примітка:** Завдяки `volumes: - .:/app` в compose-файлі, папки `backend/data/askcfgs/` та `hostlibs/` на хості автоматично видно в контейнері. Файли туди можна класти прямо на сервері без перезапуску контейнерів.
