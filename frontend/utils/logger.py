import logging
import os
from logging.handlers import RotatingFileHandler


def logger_setup(
    log_name: str, max_file_size: int = 100 * 1024 * 1024, backup_count: int = 3
):
    filename = f"./logs/{log_name}.log"
    logger = logging.getLogger(log_name)

    if not logger.hasHandlers():
        logger.setLevel(logging.DEBUG)

        formatter = logging.Formatter(
            "%(asctime)s:%(levelname)s:%(name)s: %(message)s @ %(filename)s__.%(funcName)s(%(lineno)d)"
        )

        # Ensure the log directory exists
        os.makedirs(os.path.dirname(filename), exist_ok=True)

        # File Handler
        file_handler = RotatingFileHandler(
            filename, maxBytes=max_file_size, backupCount=backup_count
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Console Handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # Prevent log messages from propagating to ancestor loggers
        logger.propagate = False

    return logger
