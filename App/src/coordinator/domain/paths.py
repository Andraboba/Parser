"""Проверка путей от агента: файл должен лежать внутри каталога хранения (ТЗ, п. 14)."""
from pathlib import PurePosixPath


def is_inside(root: str | None, path: str) -> bool:
    """True, если абсолютный path лежит строго внутри root. Обрабатывает только запись пути,
    диска координатор не видит. Символы, которых не бывает в нормальном пути, отклоняются."""
    if not root or not path or "\x00" in path or "\\" in path:
        return False
    target = PurePosixPath(path)
    base = PurePosixPath(root)
    if not target.is_absolute() or not base.is_absolute() or ".." in target.parts:
        return False
    return target != base and target.is_relative_to(base)
