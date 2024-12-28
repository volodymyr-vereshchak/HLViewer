import logging
import os


def logger_setup(log_name: str):
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
        file_handler = logging.FileHandler(filename)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Console Handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # Prevent log messages from propagating to ancestor loggers
        logger.propagate = False

    return logger
