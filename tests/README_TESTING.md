# Тестування Enterprise Volumes API

## Як запустити тести на сервері

### Крок 1: Pull останні зміни
```bash
# Backend
cd /path/to/HLViewer
git pull back_repo master --no-edit

# Frontend
cd /path/to/frontend
git pull offline_repo master --no-edit
```

### Крок 2: Активувати віртуальне оточення
```bash
# Windows
venv\Scripts\activate

# Linux
source venv/bin/activate
```

### Крок 3: Встановити залежності для тестування

#### Варіант А: Офлайн встановлення (без інтернету) - РЕКОМЕНДОВАНО
```bash
# Встановити всі залежності з локальної папки packages
pip install --no-index --find-links=packages/ pytest==8.4.1 httpx==0.28.1

# Або встановити все з requirements.txt
pip install --no-index --find-links=packages/ -r requirements.txt
```

#### Варіант Б: З інтернетом
```bash
pip install pytest==8.4.1 httpx==0.28.1
```

### Крок 4: Запустити тести
```bash
# Запустити всі тести з детальним виводом
pytest tests/test_enterprise_integration.py -v

# Або тільки основні тести API
pytest tests/test_enterprise_integration.py::TestEnterpriseAPI -v

# Зупинити на першій помилці
pytest tests/test_enterprise_integration.py -v -x
```

---

## Що перевіряють тести

### TestEnterpriseAPI (основні тести)
- ✅ API запущено і доступне
- ✅ Enterprise endpoint зареєстровано
- ✅ Запит з валідними параметрами працює
- ✅ Запит для однієї лінії працює
- ✅ Помилка при невалідній даті
- ✅ Помилка при відсутності line_id
- ✅ Помилка при відсутності дат
- ✅ Помилка при from_date > to_date
- ✅ Помилка при негативному line_id
- ✅ Запит для кількох ліній працює
- ✅ Агрегація по датам коректна
- ✅ total_volume = сума device volumes
- ✅ device_count = кількість devices

### TestEnterpriseMappings
- ✅ Файл enterprise_mappings.xlsx існує
- ✅ Файл не порожній

### TestDPDIntegration
- ✅ DPD credentials налаштовані

---

## Ручне тестування через Swagger

1. Відкрити браузер: `http://localhost:8000/docs`
2. Знайти секцію **enterprise**
3. Розгорнути `GET /enterprise/volumes/`
4. Натиснути **Try it out**
5. Заповнити параметри:
   - **line_id**: 1, 6, 10
   - **from_date**: 2025-12-01
   - **to_date**: 2025-12-21
6. Натиснути **Execute**
7. Перевірити відповідь (має бути HTTP 200 і JSON масив)

---

## Очікувана відповідь API

```json
[
  {
    "line_id": 1,
    "period": "2025-12-01",
    "total_volume": 1234.56,
    "device_count": 2,
    "devices": [
      {
        "serNum": 8189,
        "mfDev": 1,
        "typeDev": 5,
        "chNum": 0,
        "enterprise_name": "Автосистем Дп ТОВ",
        "volume": 1000.00
      }
    ]
  }
]
```

---

## Якщо тести не проходять

### "Connection refused"
- Перевірити, що backend API запущено
- Команда: `uvicorn backend.api.main:app --reload`

### "404 Not Found"
- Перевірити, що enterprise router зареєстровано в `backend/api/main.py`
- Перезапустити API

### "500 Internal Server Error"
- Перевірити логи backend
- Перевірити, що файл `backend/data/enterprise_mappings.xlsx` існує
- Перевірити DPD credentials в `.env`

### "503 Service Unavailable"
- DPD API недоступний
- Перевірити credentials в `.env`
- Перевірити мережеве з'єднання з DPD API

### Порожня відповідь []
- Це нормально! Означає, що немає маппінгів для вказаних line_ids
- Перевірити `backend/data/enterprise_mappings.xlsx` і заповнити колонку line_id
- Перевірити, що в DPD API є дані за вказаний період

---

## Чеклист перед деплоєм

- [ ] Всі pytest тести пройшли
- [ ] Файл `backend/data/enterprise_mappings.xlsx` існує і заповнений
- [ ] DPD credentials налаштовані в `.env`
- [ ] API повертає дані для відомих line_ids
- [ ] Frontend GRS Trends працює коректно
- [ ] Логіка віднімання об'ємів коректна
- [ ] Немає помилок в логах backend
- [ ] Продуктивність прийнятна (< 5 секунд)

---

## Офлайн пакети (для серверів без інтернету)

### Структура папки packages/

Папка `packages/` містить всі необхідні Python пакети для роботи:
- **pytest 8.4.1** - фреймворк для тестування
- **httpx 0.28.1** - HTTP клієнт для тестів
- Всі залежності проекту з `requirements.txt`

**Загальна кількість:** ~95 пакетів

### Як використовувати офлайн пакети:

```bash
# Встановити тільки тестові залежності
pip install --no-index --find-links=packages/ pytest==8.4.1 httpx==0.28.1

# Встановити всі залежності проекту
pip install --no-index --find-links=packages/ -r requirements.txt

# Перевірити що встановлено
pip list | grep -E "pytest|httpx"
```

### Список основних пакетів для тестування:

- `pytest-8.4.1-py3-none-any.whl`
- `httpx-0.28.1-py3-none-any.whl`
- `httpcore-1.0.7-py3-none-any.whl`
- `anyio-4.7.0-py3-none-any.whl`
- `h11-0.14.0-py3-none-any.whl`
- `pluggy-1.6.0-py3-none-any.whl`
- `iniconfig-2.3.0-py3-none-any.whl`
- `packaging-25.0-py3-none-any.whl`
- `pygments-2.19.2-py3-none-any.whl`

### Оновлення пакетів (на машині з інтернетом):

```bash
# Завантажити всі пакети з requirements.txt
pip download -r requirements.txt -d packages/

# Завантажити тільки тестові залежності
pip download pytest==8.4.1 httpx==0.28.1 -d packages/

# Перевірити кількість пакетів
ls packages/ | wc -l
```

### Примітки:

- Папка `packages/` вже включена в репозиторій
- Пакети сумісні з Python 3.13 на Windows
- Для Linux може знадобитись інша версія деяких пакетів (з `-linux` суфіксом)
- Розмір папки: ~150-200 MB
