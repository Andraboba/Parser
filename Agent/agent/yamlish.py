# -*- coding: utf-8 -*-
"""Минимальный разборщик YAML для файлов конфигурации агента.

Поддерживается ровно тот подмножество, которое используется в config/*.yaml:

* комментарии ``#`` (в том числе в конце строки);
* вложенные отображения по отступам;
* списки ``- значение`` и списки отображений ``- ключ: значение``;
* скаляры: строки (в кавычках и без), целые и дробные числа,
  ``true``/``false``, ``null``/``~``, пустые ``[]`` и ``{}``.

Отдельная зависимость не нужна: пакет остаётся на стандартной библиотеке.
Якоря, многострочные строки и flow-коллекции не поддерживаются намеренно —
если они понадобятся, лучше перейти на json или полноценный YAML-парсер.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["YamlError", "loads", "load_file"]


class YamlError(ValueError):
    """Файл не соответствует поддерживаемому подмножеству YAML."""


class _Line:
    __slots__ = ("indent", "text", "number")

    def __init__(self, indent: int, text: str, number: int) -> None:
        self.indent = indent
        self.text = text
        self.number = number


def _strip_comment(raw: str) -> str:
    """Убрать комментарий, не задевая ``#`` внутри кавычек."""
    quote: str | None = None
    for index, char in enumerate(raw):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or raw[index - 1] in " \t"):
            return raw[:index]
    return raw


def _tokenize(text: str) -> list[_Line]:
    lines: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        without_comment = _strip_comment(raw).rstrip()
        if not without_comment.strip():
            continue
        if "\t" in without_comment[: len(without_comment) - len(without_comment.lstrip())]:
            raise YamlError(f"строка {number}: отступы табуляцией не поддерживаются")
        indent = len(without_comment) - len(without_comment.lstrip(" "))
        lines.append(_Line(indent, without_comment.strip(), number))
    return lines


def _split_key(text: str) -> tuple[str, str] | None:
    """Разделить ``ключ: значение``. Возвращает None, если это не пара."""
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == ":" and (index + 1 == len(text) or text[index + 1] in " \t"):
            return text[:index].strip(), text[index + 1 :].strip()
    return None


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        return inner.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    return value


def _scalar(value: str) -> Any:
    if value == "":
        return None
    if value in ("null", "Null", "NULL", "~"):
        return None
    if value in ("true", "True", "TRUE", "yes", "on"):
        return True
    if value in ("false", "False", "FALSE", "no", "off"):
        return False
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value[0] in ("'", '"') and value[-1] == value[0] and len(value) >= 2:
        return _unquote(value)
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


class _Parser:
    def __init__(self, lines: list[_Line]) -> None:
        self._lines = lines
        self._i = 0

    def parse(self) -> Any:
        if not self._lines:
            return {}
        node = self._block(self._lines[0].indent)
        if self._i != len(self._lines):
            line = self._lines[self._i]
            raise YamlError(f"строка {line.number}: неожиданный отступ")
        return node

    def _peek(self) -> _Line | None:
        return self._lines[self._i] if self._i < len(self._lines) else None

    def _block(self, indent: int) -> Any:
        line = self._peek()
        if line is None:
            return None
        if line.text == "-" or line.text.startswith("- "):
            return self._sequence(indent)
        return self._mapping(indent)

    def _mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            line = self._peek()
            if line is None or line.indent < indent:
                return result
            if line.indent > indent:
                raise YamlError(f"строка {line.number}: неожиданный отступ")
            if line.text == "-" or line.text.startswith("- "):
                raise YamlError(f"строка {line.number}: элемент списка там, где ожидался ключ")
            pair = _split_key(line.text)
            if pair is None:
                raise YamlError(f"строка {line.number}: ожидалось «ключ: значение»")
            key, raw_value = pair
            if not key:
                raise YamlError(f"строка {line.number}: пустой ключ")
            self._i += 1
            if raw_value == "":
                following = self._peek()
                if following is not None and following.indent > indent:
                    result[key] = self._block(following.indent)
                elif (
                    following is not None
                    and following.indent == indent
                    and (following.text == "-" or following.text.startswith("- "))
                ):
                    # список на том же уровне отступа, что и ключ
                    result[key] = self._sequence(indent)
                else:
                    result[key] = None
            else:
                result[key] = _scalar(raw_value)

    def _sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while True:
            line = self._peek()
            if line is None or line.indent != indent:
                return items
            if not (line.text == "-" or line.text.startswith("- ")):
                return items
            item_text = line.text[1:].strip()
            self._i += 1
            if item_text == "":
                following = self._peek()
                if following is not None and following.indent > indent:
                    items.append(self._block(following.indent))
                else:
                    items.append(None)
                continue
            pair = _split_key(item_text)
            if pair is None:
                items.append(_scalar(item_text))
                continue
            key, raw_value = pair
            mapping: dict[str, Any] = {}
            if raw_value == "":
                following = self._peek()
                if following is not None and following.indent > indent + 1:
                    mapping[key] = self._block(following.indent)
                else:
                    mapping[key] = None
            else:
                mapping[key] = _scalar(raw_value)
            # остальные ключи того же элемента списка идут с большим отступом
            base_indent = self._peek().indent if self._peek() is not None else indent
            while True:
                following = self._peek()
                if following is None or following.indent <= indent:
                    break
                if following.text == "-" or following.text.startswith("- "):
                    break
                nested = _split_key(following.text)
                if nested is None:
                    raise YamlError(f"строка {following.number}: ожидалось «ключ: значение»")
                sub_key, sub_raw = nested
                self._i += 1
                if sub_raw == "":
                    deeper = self._peek()
                    if deeper is not None and deeper.indent > following.indent:
                        mapping[sub_key] = self._block(deeper.indent)
                    elif (
                        deeper is not None
                        and deeper.indent == following.indent
                        and (deeper.text == "-" or deeper.text.startswith("- "))
                    ):
                        mapping[sub_key] = self._sequence(following.indent)
                    else:
                        mapping[sub_key] = None
                else:
                    mapping[sub_key] = _scalar(sub_raw)
            items.append(mapping)
            del base_indent


def loads(text: str) -> Any:
    """Разобрать текст YAML поддерживаемого подмножества."""
    return _Parser(_tokenize(text)).parse()


def load_file(path: str | Path) -> Any:
    """Прочитать YAML или JSON из файла."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    stripped = text.lstrip()
    if path.suffix.lower() == ".json" or stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise YamlError(f"{path}: некорректный JSON — {exc}") from exc
    return loads(text)
