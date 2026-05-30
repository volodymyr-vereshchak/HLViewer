"""Проверка структуры БД"""
import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
import pandas as pd

# Загрузка .env
PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / '.env')

# Подключение к БД
DB_USER = os.getenv('POSTGRES_USER', 'postgres')
DB_PASSWORD = os.getenv('POSTGRES_PASSWORD', 'assembler')
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('POSTGRES_DB', 'hostlib_db')
DB_URL = f'postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'

engine = create_engine(DB_URL)
print(f'Подключение к БД: {DB_NAME}@{DB_HOST}:{DB_PORT}')
print('='*80)

# 1. Список всех таблиц
print('\n1. СПИСОК ТАБЛИЦ В БД:')
print('='*80)
tables_query = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
ORDER BY table_name
"""
with engine.connect() as conn:
    tables = pd.read_sql(text(tables_query), conn)
    for idx, table in enumerate(tables['table_name'], 1):
        print(f'{idx}. {table}')

# 2. Структура таблицы hourly_archive
print('\n2. СТРУКТУРА ТАБЛИЦЫ hourly_archive:')
print('='*80)
structure_query = """
SELECT
    column_name,
    data_type,
    is_nullable,
    column_default
FROM information_schema.columns
WHERE table_name = 'hourly_archive'
ORDER BY ordinal_position
"""
with engine.connect() as conn:
    structure = pd.read_sql(text(structure_query), conn)
    print(structure.to_string(index=False))

# 3. Примеры данных из hourly_archive
print('\n3. ПРИМЕРЫ ДАННЫХ ИЗ hourly_archive (первые 10 записей):')
print('='*80)
sample_query = """
SELECT *
FROM hourly_archive
ORDER BY period DESC
LIMIT 10
"""
with engine.connect() as conn:
    sample = pd.read_sql(text(sample_query), conn)
    print(sample.to_string(index=False))

# 4. Структура таблицы gas_volume_line
print('\n4. СТРУКТУРА ТАБЛИЦЫ gas_volume_line:')
print('='*80)
line_structure_query = """
SELECT
    column_name,
    data_type,
    is_nullable
FROM information_schema.columns
WHERE table_name = 'gas_volume_line'
ORDER BY ordinal_position
"""
with engine.connect() as conn:
    line_structure = pd.read_sql(text(line_structure_query), conn)
    print(line_structure.to_string(index=False))

# 5. Примеры данных из gas_volume_line
print('\n5. ДАННЫЕ ИЗ gas_volume_line:')
print('='*80)
line_sample_query = """
SELECT id, line, meter, name, gas_volume_calc_id
FROM gas_volume_line
ORDER BY line
LIMIT 20
"""
with engine.connect() as conn:
    line_sample = pd.read_sql(text(line_sample_query), conn)
    print(line_sample.to_string(index=False))

# 6. Связь между hourly_archive и gas_volume_line
print('\n6. СВЯЗЬ hourly_archive <-> gas_volume_line:')
print('='*80)
join_query = """
SELECT
    gvl.id as line_table_id,
    gvl.line as line_number,
    gvl.name as line_name,
    COUNT(ha.id) as hourly_records,
    MIN(ha.period) as min_date,
    MAX(ha.period) as max_date
FROM gas_volume_line gvl
LEFT JOIN hourly_archive ha ON ha.line_id = gvl.id
GROUP BY gvl.id, gvl.line, gvl.name
ORDER BY gvl.line
"""
with engine.connect() as conn:
    join_data = pd.read_sql(text(join_query), conn)
    print(join_data.to_string(index=False))

print('\n' + '='*80)
print('ГОТОВО')
