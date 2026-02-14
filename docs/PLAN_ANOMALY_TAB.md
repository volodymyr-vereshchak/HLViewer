# Вкладка "Анализ аномалий" на фронтенде

## Контекст

Бэкенд уже имеет `backend/anomaly_analysis.py` — CLI-скрипт анализа аномалий (43 аномалии за последний месяц). Нужно вывести результаты на фронтенд: новая вкладка в существующем приложении React с хорошим UX.

**Фронтенд** — отдельный репо: `D:/Projects/HLViewer/frontend/`
**Бэкенд** — `D:/Projects/HLViewer/HLViewer/`

## Архитектура

### Бэкенд: новый API эндпоинт

**Файл:** `backend/api/endpoints/anomaly_ep.py` (новый)

Паттерн: как `enterprise_ep.py` — класс-роутер, Pydantic-модели, без DAO (данные вычисляются on-the-fly).

Эндпоинт `GET /anomalies/`:
- Параметры: `start_date`, `end_date`, `days` (default 30), `line_id[]` (optional)
- Вызывает `run_anomaly_analysis()` из `anomaly_analysis.py`
- Возвращает JSON список аномалий

**Рефакторинг** `backend/anomaly_analysis.py`:
- `run_anomaly_analysis()` уже возвращает DataFrame — нужно чтобы он возвращал его (сейчас возвращает, но `__main__` не использует return value)
- Добавить Pydantic-модели ответа в отдельный файл или прямо в endpoint

**Файл:** `backend/db/models/anomaly_models.py` (новый)

```python
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class AnomalyResponse(BaseModel):
    line_id: int
    grs_name: str
    category: str
    anomaly_type: str
    start_datetime: datetime
    end_datetime: datetime
    duration_hours: float
    affected_field: str
    detected_value: Optional[float]
    expected_value: Optional[float]
    deviation_pct: float
    severity_label: str
    severity_score: float
    description: str
    recommended_action: str

class AnomalySummary(BaseModel):
    total: int
    critical: int
    high: int
    medium: int
    low: int
    by_category: dict
    by_line: dict
    analysis_start: str
    analysis_end: str
```

**Регистрация** в `backend/api/main.py` — добавить import и `app.include_router()`

### Фронтенд: новая вкладка

Полноэкранная вкладка (как OverviewTab, EnterprisePollAnalysis) — без sidebar/TreeView.

#### 1. TopMenu.jsx — добавить кнопку

```jsx
// Новая иконка — щит с восклицательным знаком (аномалии/предупреждения)
const AnomalyIcon = ({ color = "#B9E42B" }) => (
  <svg width="20" height="20" viewBox="0 0 40 40" fill="none">
    <path d="M20 3.33L35 11.67V20C35 28.33 28.5 35.83 20 38.33C11.5 35.83 5 28.33 5 20V11.67L20 3.33Z"
          stroke={color} strokeWidth="1.5" strokeLinejoin="round" fill="none"/>
    <path d="M20 13V22M20 27V27.02" stroke={color} strokeWidth="2" strokeLinecap="round"/>
  </svg>
);
```

Кнопка `{ id: 'anomalies', label: t('anomalyAnalysis'), disabled: false }` — после `poll`.

В `archiveTypeMap`: `'anomalies': 'anomalies'`

В `getButtonIcon`: `case 'anomalies': return <AnomalyIcon color={iconColor} />;`

#### 2. App.jsx — роутинг

```jsx
import AnomalyTab from './components/AnomalyTab'

// В условиях full-width:
{archiveType === 'anomalies' && <AnomalyTab />}

// Обновить условие скрытия sidebar:
{archiveType !== 'overview' && archiveType !== 'poll' && archiveType !== 'anomalies' && (
```

#### 3. AnomalyTab.jsx (новый) — основной компонент

**Файл:** `D:/Projects/HLViewer/frontend/react-frontend/src/components/AnomalyTab.jsx`

**Структура UI:**

```
┌─────────────────────────────────────────────────────────┐
│ Анализ аномалий                 [Период: ▼] [Обновить] │
├─────────────────────────────────────────────────────────┤
│ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐                    │
│ │ 43   │ │  2   │ │ 11   │ │ 30   │  Summary cards     │
│ │Всего │ │CRIT  │ │HIGH  │ │MED   │                    │
│ └──────┘ └──────┘ └──────┘ └──────┘                    │
├─────────────────────────────────────────────────────────┤
│ Фильтры: [Severity ▼] [Категория ▼] [Линия ▼] [Поиск] │
├─────────────────────────────────────────────────────────┤
│ Таблица аномалий (ag-Grid)                              │
│ ┌────┬───────┬──────┬──────┬─────┬──────┬──────┬──────┐│
│ │ #  │Severity│Линия │Тип   │Опис.│Период│Откл. │Рек.  ││
│ ├────┼───────┼──────┼──────┼─────┼──────┼──────┼──────┤│
│ │ 1  │CRIT   │ГРС   │CUSUM │...  │01-02 │...   │...   ││
│ │ 2  │HIGH   │Кольцо│PRESS │...  │01-28 │...   │...   ││
│ │... │       │      │      │     │      │      │      ││
│ └────┴───────┴──────┴──────┴─────┴──────┴──────┴──────┘│
├─────────────────────────────────────────────────────────┤
│ Распределение по категориям (donut) │ По линиям (bar)   │
└─────────────────────────────────────────────────────────┘
```

**Компоненты:**

1. **Header** — заголовок + выбор периода (dropdown: 7/14/30/90 дней) + кнопка "Обновить"
2. **Summary Cards** — 4 карточки со счётчиками (Всего, CRITICAL, HIGH, MEDIUM) с цветовой индикацией
3. **Фильтры** — dropdown фильтры по severity, category, line_id + текстовый поиск по описанию
4. **Таблица** — ag-Grid с колонками:
   - Severity (цветной бейдж: красный/оранжевый/жёлтый)
   - Линия (line_id + grs_name)
   - Тип (anomaly_type)
   - Описание (description, усечённое с tooltip)
   - Период (start_datetime — end_datetime)
   - Длительность (duration_hours)
   - Отклонение (deviation_pct%)
   - Рекомендация (recommended_action)
5. **Графики** (Recharts, внизу):
   - Donut chart — распределение по категориям
   - Horizontal bar chart — кол-во аномалий по линиям

**Логика:**
- При монтировании загружает данные через `anomalyApi.getAnomalies(days)`
- Фильтрация клиентская (данных мало, ~50-100 записей)
- Загрузка ~3-5 сек (анализ на бэке), показываем спиннер
- Экспорт в Excel (кнопка, использует xlsx как в DataTable)

#### 4. AnomalyTab.css (новый)

Тёмная тема в стиле существующих компонентов:
- Background: `#1a1a1a`, карточки: `#2a2a2a`
- Accent: `#B9E42B`
- Severity цвета: CRITICAL `#ff4444`, HIGH `#ff8c00`, MEDIUM `#ffd700`, LOW `#4caf50`
- Бордеры: `#3E3E3E`
- Border-radius: `8px`

#### 5. api.js — новый API модуль

```javascript
export const anomalyApi = {
  async getAnomalies(days = 30, startDate = null, endDate = null, lineIds = null) {
    const params = {};
    if (startDate && endDate) {
      params.start_date = startDate;
      params.end_date = endDate;
    } else {
      params.days = days;
    }
    if (lineIds) params.line_id = lineIds;
    return await apiClient.get('/anomalies/', params);
  }
};
```

#### 6. Локализация — ru.js и uk.js

Добавить ключи:
```javascript
// ru.js
anomalyAnalysis: "Аномалии",
anomalyTitle: "Анализ аномалий",
anomalyTotal: "Всего",
anomalyCritical: "Критические",
anomalyHigh: "Высокие",
anomalyMedium: "Средние",
anomalyLow: "Низкие",
anomalySeverity: "Критичность",
anomalyCategory: "Категория",
anomalyType: "Тип",
anomalyLine: "Линия",
anomalyPeriod: "Период",
anomalyDuration: "Длительность",
anomalyDeviation: "Отклонение",
anomalyRecommendation: "Рекомендация",
anomalyDescription: "Описание",
anomalyFilterAll: "Все",
anomalyDays7: "7 дней",
anomalyDays14: "14 дней",
anomalyDays30: "30 дней",
anomalyDays90: "90 дней",
anomalyByCategory: "По категориям",
anomalyByLine: "По линиям",
anomalyNoData: "Аномалий не обнаружено",
anomalyLoading: "Выполняется анализ...",
anomalyExport: "Экспорт",
```

## Файлы — итого

### Новые файлы
| Файл | Описание |
|------|----------|
| `backend/api/endpoints/anomaly_ep.py` | FastAPI эндпоинт |
| `backend/db/models/anomaly_models.py` | Pydantic-модели ответа |
| `frontend/.../components/AnomalyTab.jsx` | React компонент вкладки |
| `frontend/.../components/AnomalyTab.css` | Стили вкладки |

### Модифицируемые файлы
| Файл | Что меняем |
|------|-----------|
| `backend/api/main.py` | Добавить import + include_router |
| `backend/anomaly_analysis.py` | Убрать print-шум при вызове из API (добавить параметр `quiet=True`) |
| `frontend/.../App.jsx` | Добавить import + роутинг |
| `frontend/.../components/TopMenu.jsx` | Добавить кнопку + иконку |
| `frontend/.../services/api.js` | Добавить `anomalyApi` |
| `frontend/.../locales/ru.js` | Добавить переводы |
| `frontend/.../locales/uk.js` | Добавить переводы |

## Порядок реализации

1. **Бэкенд: модели + эндпоинт** — `anomaly_models.py`, `anomaly_ep.py`, регистрация в `main.py`, `quiet` параметр в `anomaly_analysis.py`
2. **Фронтенд: API** — добавить `anomalyApi` в `api.js`
3. **Фронтенд: навигация** — TopMenu.jsx (иконка + кнопка), App.jsx (роутинг)
4. **Фронтенд: компонент** — `AnomalyTab.jsx` + `AnomalyTab.css`
5. **Фронтенд: локализация** — `ru.js`, `uk.js`

## Верификация

1. Запустить бэкенд: `python -m uvicorn backend.api.main:app --reload`
2. Проверить API: `curl http://localhost:8000/anomalies/?days=30` — должен вернуть JSON с ~43 аномалиями
3. Собрать фронтенд: `cd frontend/react-frontend && npm run build`
4. Открыть в браузере, нажать на вкладку "Аномалии"
5. Проверить: загрузку данных, фильтры, графики, экспорт в Excel
