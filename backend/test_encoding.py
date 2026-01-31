#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тест корректности отображения кириллицы и эмодзи
"""

import sys
import os

# Импорт модуля исправления кодировки
from utils.encoding_fix import setup_console_encoding
setup_console_encoding()

print('='*80)
print('ТЕСТ КОДИРОВКИ')
print('='*80)
print()

print(f'PYTHONIOENCODING: {os.environ.get("PYTHONIOENCODING", "not set")}')
print(f'sys.stdout.encoding: {sys.stdout.encoding}')
print(f'sys.stderr.encoding: {sys.stderr.encoding}')
print()

print('=== 1. Тест кириллицы (украинский) ===')
print('ГРС Хмельник')
print('ГРС Вендичани')
print('ГРС Калуш')
print('ГРС Надвірна')
print('ГРС Тлумач-Іваст')
print('ГРС Тисмениця')
print('ГРС Деренівка')
print('ГРС Відродження')
print('ГРС Бурштинська')
print()

print('=== 2. Тест эмодзи ===')
print('✅ Успешно (check mark)')
print('⚠ Предупреждение (warning)')
print('❌ Ошибка (cross mark)')
print('🚀 Запуск (rocket)')
print('📊 Статистика (chart)')
print('🔥 Важно (fire)')
print('💡 Идея (lightbulb)')
print('🎯 Цель (target)')
print()

print('=== 3. Тест специальных символов ===')
print('Температура: 15°C (градус Цельсия)')
print("Объем газа: 1000 м³ (кубометры)")
print('R² = 0.85 (R-квадрат)')
print('Площадь: 100 м² (квадратные метры)')
print('Δ (дельта) - изменение')
print('× (умножение) 2 × 3 = 6')
print('÷ (деление) 10 ÷ 2 = 5')
print('→ (стрелка вправо)')
print('← (стрелка влево)')
print('σ (сигма) - стандартное отклонение')
print('μ (мю) - среднее значение')
print('α (альфа), β (бета), γ (гамма)')
print()

print('=== 4. Тест таблицы с кириллицей ===')
print('┌─────────────────────┬──────────┬──────────┐')
print('│ ГРС                 │ R²       │ MAE (м³) │')
print('├─────────────────────┼──────────┼──────────┤')
print('│ ГРС Хмельник        │ 0.8156   │ 757      │')
print('│ ГРС Калуш           │ 0.7552   │ 1040     │')
print('│ ГРС Надвірна        │ 0.7297   │ 746      │')
print('└─────────────────────┴──────────┴──────────┘')
print()

print('=== 5. Тест сложного форматирования ===')
grs_data = [
    ('ГРС Хмельник', 0.8156, 757, '✅'),
    ('ГРС Калуш', 0.7552, 1040, '✅'),
    ('ГРС Надвірна', 0.7297, 746, '✅'),
    ('ГРС Тисмениця', 0.6069, 2104, '⚠'),
]

for grs, r2, mae, status in grs_data:
    print(f'{status} {grs:20s} → R²={r2:.4f}, MAE={mae:5d} м³')
print()

print('='*80)
print('✅ ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО!')
print('='*80)
