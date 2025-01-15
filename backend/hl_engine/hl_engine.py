import os
from utils.logger import logger_setup


class Hostlib:
    def __init__(self, path: str = "./", chunk_size: int = 900) -> None:
        self.logger = logger_setup("backend")
        self.path = path
        self.chunk_size = chunk_size

    @staticmethod
    def get_params_from_file_name(path_to_file: str) -> dict:
        filename = os.path.basename(path_to_file)
        address = int(filename[1:4])
        line = int(filename[5:6])
        return {"address": address, "line": line}


if __name__ == "__main__":
    path_dir = "D:/Projects/HLViewer/HLViewer/develop_data/11Листопад/Zaporizgaz_2024_11_29_8/Zaporizgaz/56ZOPZAP4003301T"
