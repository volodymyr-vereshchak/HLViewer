"""
Функции для очистки аномалий в данных потребления газа

Можно использовать в hourly_prediction.py для предобработки данных
"""
import pandas as pd
import numpy as np


def detect_anomalies(df, line_id_col='line_id', value_col='population_volume',
                     pressure_col='pressure', datetime_col='datetime',
                     month_col='month', verbose=False):
    """
    Обнаруживает аномалии в данных потребления

    Args:
        df: DataFrame с данными
        line_id_col: название колонки с ID линии
        value_col: название колонки с потреблением
        pressure_col: название колонки с давлением
        datetime_col: название колонки с датой/временем
        month_col: название колонки с месяцем
        verbose: выводить статистику

    Returns:
        DataFrame с добавленными колонками:
        - is_anomaly: bool, True если запись - аномалия
        - anomaly_type: str, тип аномалии
    """
    df = df.copy()
    df['is_anomaly'] = False
    df['anomaly_type'] = ''

    total_anomalies = 0

    for line_id in df[line_id_col].unique():
        line_mask = df[line_id_col] == line_id
        line_data = df[line_mask].copy()
        line_data = line_data.sort_values(datetime_col).reset_index(drop=True)

        # 1. НУЛИ ВНЕ ЛЕТА (июнь-август)
        is_summer = line_data[month_col].isin([6, 7, 8])
        zero_volume = line_data[value_col] == 0
        anomaly_zeros_nonsummer = zero_volume & ~is_summer

        # 2. РЕЗКИЕ СКАЧКИ ПОТРЕБЛЕНИЯ
        line_data['volume_diff'] = line_data[value_col].diff()
        volume_std = line_data[value_col].std()

        # Скачок если изменение > 3*std и значение не нулевое
        anomaly_spike = (
            (np.abs(line_data['volume_diff']) > 3 * volume_std) &
            (line_data[value_col] > 0)
        )

        # 3. РЕЗКИЕ СКАЧКИ ДАВЛЕНИЯ
        if pressure_col in line_data.columns and line_data[pressure_col].notna().any():
            line_data['pressure_diff'] = line_data[pressure_col].diff()
            pressure_std = line_data[pressure_col].std()

            # Скачок давления > 3*std (исключая технические нули)
            anomaly_pressure_spike = (
                (np.abs(line_data['pressure_diff']) > 3 * pressure_std) &
                (line_data[pressure_col] > 1.0)
            )
        else:
            anomaly_pressure_spike = pd.Series([False] * len(line_data))

        # 4. ДЛИТЕЛЬНЫЕ ПОСЛЕДОВАТЕЛЬНОСТИ НУЛЕЙ (>=3 часа подряд вне лета)
        line_data['zero_group'] = (zero_volume & ~is_summer).astype(int)
        line_data['zero_streak'] = line_data.groupby(
            (line_data['zero_group'] != line_data['zero_group'].shift()).cumsum()
        )['zero_group'].transform('sum')

        anomaly_zero_streak = (line_data['zero_streak'] >= 3) & (line_data['zero_group'] == 1)

        # Маркируем аномалии
        line_indices = df[line_mask].index

        for idx, orig_idx in enumerate(line_indices):
            types = []
            if anomaly_zeros_nonsummer.iloc[idx]:
                types.append('ZERO_NONSUMMER')
            if anomaly_spike.iloc[idx]:
                types.append('VOLUME_SPIKE')
            if anomaly_pressure_spike.iloc[idx]:
                types.append('PRESSURE_SPIKE')
            if anomaly_zero_streak.iloc[idx]:
                types.append('ZERO_STREAK')

            if types:
                df.at[orig_idx, 'is_anomaly'] = True
                df.at[orig_idx, 'anomaly_type'] = ';'.join(types)
                total_anomalies += 1

    if verbose:
        n_anomalies = df['is_anomaly'].sum()
        pct_anomalies = (n_anomalies / len(df)) * 100
        print(f'Обнаружено аномалий: {n_anomalies} из {len(df)} ({pct_anomalies:.2f}%)')

        # По типам
        for atype in ['ZERO_NONSUMMER', 'VOLUME_SPIKE', 'PRESSURE_SPIKE', 'ZERO_STREAK']:
            count = df[df['is_anomaly']]['anomaly_type'].str.contains(atype).sum()
            if count > 0:
                print(f'  {atype}: {count}')

    return df


def clean_anomalies(df, line_id_col='line_id', value_col='population_volume',
                    datetime_col='datetime', month_col='month',
                    method='interpolate', max_gap=2, verbose=False):
    """
    Очищает аномалии в данных

    Args:
        df: DataFrame с данными (должен содержать колонки is_anomaly, anomaly_type)
        line_id_col: название колонки с ID линии
        value_col: название колонки с потреблением
        datetime_col: название колонки с датой/временем
        month_col: название колонки с месяцем
        method: метод очистки:
            - 'remove': удалить аномальные записи
            - 'interpolate': интерполировать короткие последовательности (<= max_gap)
            - 'hybrid': интерполировать короткие, удалить длинные
        max_gap: максимальная длина последовательности для интерполяции (часов)
        verbose: выводить статистику

    Returns:
        Очищенный DataFrame
    """
    if 'is_anomaly' not in df.columns:
        raise ValueError('DataFrame должен содержать колонку is_anomaly. Запустите detect_anomalies() сначала.')

    df = df.copy()

    initial_count = len(df)
    removed_count = 0
    interpolated_count = 0

    if method == 'remove':
        # Просто удаляем все аномалии
        df = df[~df['is_anomaly']].copy()
        removed_count = initial_count - len(df)

    elif method == 'interpolate':
        # Интерполируем все аномалии по каждой линии
        for line_id in df[line_id_col].unique():
            line_mask = df[line_id_col] == line_id
            line_data = df[line_mask].copy()
            line_data = line_data.sort_values(datetime_col).reset_index()

            # Заменяем аномалии на NaN
            line_data.loc[line_data['is_anomaly'], value_col] = np.nan

            # Линейная интерполяция
            line_data[value_col] = line_data[value_col].interpolate(method='linear', limit_direction='both')

            # Обновляем в исходном датафрейме
            df.loc[line_mask, value_col] = line_data.set_index('index')[value_col]

            interpolated_count += line_data['is_anomaly'].sum()

        # Убираем флаги аномалий
        df['is_anomaly'] = False
        df['anomaly_type'] = ''

    elif method == 'hybrid':
        # Интерполируем короткие последовательности, удаляем длинные
        to_remove = []

        for line_id in df[line_id_col].unique():
            line_mask = df[line_id_col] == line_id
            line_data = df[line_mask].copy()
            line_data = line_data.sort_values(datetime_col).reset_index()

            # Находим последовательности аномалий
            line_data['anomaly_group'] = (
                line_data['is_anomaly'] != line_data['is_anomaly'].shift()
            ).cumsum()

            for group_id in line_data[line_data['is_anomaly']]['anomaly_group'].unique():
                group_mask = line_data['anomaly_group'] == group_id
                group_size = group_mask.sum()
                group_indices = line_data[group_mask]['index'].values

                if group_size <= max_gap:
                    # Короткая последовательность - интерполируем
                    # Заменяем на NaN только эту группу
                    for idx in group_indices:
                        df.at[idx, value_col] = np.nan
                    interpolated_count += group_size
                else:
                    # Длинная последовательность - удаляем
                    to_remove.extend(group_indices)
                    removed_count += group_size

        # Удаляем длинные последовательности
        if to_remove:
            df = df.drop(to_remove)

        # Интерполируем NaN по каждой линии
        for line_id in df[line_id_col].unique():
            line_mask = df[line_id_col] == line_id
            line_data = df[line_mask].copy()
            line_data = line_data.sort_values(datetime_col)

            line_data[value_col] = line_data[value_col].interpolate(method='linear', limit_direction='both')

            df.loc[line_mask, value_col] = line_data[value_col]

        # Убираем флаги аномалий
        df['is_anomaly'] = False
        df['anomaly_type'] = ''

    else:
        raise ValueError(f'Неизвестный метод: {method}. Используйте: remove, interpolate, hybrid')

    final_count = len(df)

    if verbose:
        print(f'\nОчистка данных ({method}):')
        print(f'  Исходных записей: {initial_count}')
        print(f'  Интерполировано: {interpolated_count}')
        print(f'  Удалено: {removed_count}')
        print(f'  Осталось: {final_count}')
        print(f'  Потеря данных: {(removed_count / initial_count) * 100:.2f}%')

    return df


def clean_data_pipeline(df, line_id_col='line_id', value_col='population_volume',
                        pressure_col='pressure', datetime_col='datetime',
                        month_col='month', method='hybrid', max_gap=2, verbose=True):
    """
    Полный пайплайн очистки данных: обнаружение + очистка

    Args:
        df: DataFrame с данными
        line_id_col: название колонки с ID линии
        value_col: название колонки с потреблением
        pressure_col: название колонки с давлением
        datetime_col: название колонки с датой/временем
        month_col: название колонки с месяцем
        method: метод очистки ('remove', 'interpolate', 'hybrid')
        max_gap: максимальная длина для интерполяции (только для hybrid)
        verbose: выводить статистику

    Returns:
        Очищенный DataFrame
    """
    if verbose:
        print('='*80)
        print('ОЧИСТКА ДАННЫХ ОТ АНОМАЛИЙ')
        print('='*80)

    # Шаг 1: Обнаружение
    df = detect_anomalies(
        df,
        line_id_col=line_id_col,
        value_col=value_col,
        pressure_col=pressure_col,
        datetime_col=datetime_col,
        month_col=month_col,
        verbose=verbose
    )

    # Шаг 2: Очистка
    df = clean_anomalies(
        df,
        line_id_col=line_id_col,
        value_col=value_col,
        datetime_col=datetime_col,
        month_col=month_col,
        method=method,
        max_gap=max_gap,
        verbose=verbose
    )

    if verbose:
        print('='*80)

    return df


# Пример использования
if __name__ == '__main__':
    print('Модуль data_cleaning.py')
    print('Импортируйте функции:')
    print('  - detect_anomalies(): обнаружение аномалий')
    print('  - clean_anomalies(): очистка аномалий')
    print('  - clean_data_pipeline(): полный пайплайн')
