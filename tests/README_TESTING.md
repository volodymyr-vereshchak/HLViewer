# Тестування Enterprise Volumes API

## Як запустити тести на сервері

### Крок 1: Перейти в гілку
```bash
git checkout feature/dpd-integration
git pull
```

### Крок 2: Активувати віртуальне оточення
```bash
# Windows
venv\Scripts\activate

# Linux
source venv/bin/activate
```

### Крок 3: Встановити залежності (якщо потрібно)
```bash
pip install pytest httpx
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
