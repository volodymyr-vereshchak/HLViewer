"""
Универсальный модуль для исправления кодировки в Windows консоли.
Импортируйте этот модуль в начале каждого скрипта:
    from utils.encoding_fix import setup_console_encoding
    setup_console_encoding()
"""

import sys
import io
import os


def setup_console_encoding():
    """
    Настройка UTF-8 кодировки для Windows консоли.

    Исправляет проблемы с:
    - Кириллицей (украинский, русский текст)
    - Эмодзи (✅, ⚠, 🚀 и т.д.)
    - Специальными символами (°, ², ³, →, × и т.д.)
    """
    if sys.platform == 'win32':
        # Устанавливаем UTF-8 для stdout
        try:
            if hasattr(sys.stdout, 'buffer') and not isinstance(sys.stdout, io.TextIOWrapper):
                sys.stdout = io.TextIOWrapper(
                    sys.stdout.buffer,
                    encoding='utf-8',
                    errors='replace',
                    line_buffering=True
                )
            elif hasattr(sys.stdout, 'reconfigure'):
                # Для Python 3.7+ можно использовать reconfigure
                sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, io.UnsupportedOperation, ValueError):
            # Если уже установлено или не поддерживается
            pass

        # Устанавливаем UTF-8 для stderr
        try:
            if hasattr(sys.stderr, 'buffer') and not isinstance(sys.stderr, io.TextIOWrapper):
                sys.stderr = io.TextIOWrapper(
                    sys.stderr.buffer,
                    encoding='utf-8',
                    errors='replace',
                    line_buffering=True
                )
            elif hasattr(sys.stderr, 'reconfigure'):
                # Для Python 3.7+ можно использовать reconfigure
                sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, io.UnsupportedOperation, ValueError):
            # Если уже установлено или не поддерживается
            pass

        # Устанавливаем кодовую страницу консоли на UTF-8
        try:
            os.system('chcp 65001 >nul 2>&1')
        except:
            pass


def safe_print(*args, **kwargs):
    """
    Безопасная версия print, которая обрабатывает ошибки кодировки.

    Использование:
        from utils.encoding_fix import safe_print
        safe_print('Текст с эмодзи ✅ и кириллицей')
    """
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        # Fallback: заменяем проблемные символы
        safe_args = []
        for arg in args:
            try:
                safe_args.append(str(arg).encode('cp1251', errors='replace').decode('cp1251'))
            except:
                safe_args.append(str(arg).encode('ascii', errors='replace').decode('ascii'))
        print(*safe_args, **kwargs)


# Автоматическая настройка при импорте модуля
setup_console_encoding()
