# Офлайн Python пакети

Ця папка містить всі необхідні Python пакети для роботи HLViewer без доступу до інтернету.

## Що містить

- **Всі залежності з requirements.txt** (~95 пакетів)
- **Тестові залежності:** pytest 8.4.1, httpx 0.28.1
- **Пакети для Python 3.13 на Windows**

## Встановлення

### Окремий пакет (наприклад, openpyxl):
```bash
cd packages
pip install --no-index --find-links=. openpyxl==3.1.5
```

### Тільки тестові залежності:
```bash
pip install --no-index --find-links=. pytest==8.4.1 httpx==0.28.1
```

### Всі залежності проекту:
```bash
pip install --no-index --find-links=. -r ../requirements.txt
```

## Оновлення пакетів

Виконувати на машині з інтернетом:

```bash
# З кореневої папки проекту
pip download -r requirements.txt -d packages/
```

## Примітки

- Розмір папки: ~150-200 MB
- Сумісність: Python 3.13, Windows
- Для Linux може знадобитись інша версія деяких пакетів
