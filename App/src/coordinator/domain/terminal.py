"""Терминал в контейнер: разбор ввода по строкам и политика опасных команд (ТЗ, п. 11.4).

Журнал видит то, что набрано на клавиатуре, а не то, что выполнила оболочка: история по стрелкам
и автодополнение ему не видны. Политика поэтому страховка от ошибки, а не граница безопасности:
граница — роль администратора и непривилегированный пользователь внутри контейнера.
"""
import re
from dataclasses import dataclass

# Стирает набранную строку в оболочке: так запрещённая команда не выполнится.
KILL_LINE = b"\x15"

_DENIED = (
    (re.compile(r"\brm\s+(-[a-z]*\s+)*-[a-z]*r[a-z]*\s+(-[a-z]+\s+)*(/|/\*|~)(\s|$)", re.I),
     "удаление корневого каталога"),
    (re.compile(r"\b(mkfs|fdisk|parted|wipefs)\b"), "работа с дисками"),
    (re.compile(r"\bdd\b.*\bof=/dev/"), "запись на устройство"),
    (re.compile(r"\b(shutdown|reboot|poweroff|halt|init\s+[06])\b"), "выключение"),
    (re.compile(r":\(\)\s*\{"), "вилочная бомба"),
    (re.compile(r"\bchmod\s+(-[a-z]+\s+)*[0-7]*777\s+/(\s|$)", re.I), "права на весь корень"),
    (re.compile(r"\b(curl|wget)\b.*\|\s*(ba|z|da)?sh\b"), "выполнение кода из сети"),
    (re.compile(r"\b(nc|ncat|netcat)\b.*\s-[a-z]*e\b"), "обратная оболочка"),
    (re.compile(r"/dev/tcp/"), "обратная оболочка"),
    (re.compile(r"/run/secrets|/proc/\d+/environ"), "чтение секретов"),
)


def denied_reason(command: str) -> str | None:
    """Почему команду нельзя выполнять. None — можно."""
    for pattern, reason in _DENIED:
        if pattern.search(command):
            return reason
    return None


@dataclass(frozen=True)
class InputResult:
    forward: bytes
    commands: tuple[str, ...]
    denied: tuple[tuple[str, str], ...]


class CommandLine:
    """Собирает ввод пользователя в команды и решает, что передавать оболочке."""

    def __init__(self) -> None:
        self._line = bytearray()
        self._in_escape = False

    def feed(self, data: bytes) -> InputResult:
        forward = bytearray()
        commands: list[str] = []
        denied: list[tuple[str, str]] = []
        for byte in data:
            if self._in_escape:
                # Управляющая последовательность: стрелки, Home и прочее. Заканчивается буквой.
                forward.append(byte)
                if chr(byte).isalpha() or byte == 0x7e:
                    self._in_escape = False
                continue
            if byte == 0x1b:
                self._in_escape = True
                forward.append(byte)
            elif byte in (0x0d, 0x0a):
                command = self._line.decode("utf-8", errors="replace").strip()
                self._line.clear()
                reason = denied_reason(command) if command else None
                if reason:
                    denied.append((command, reason))
                    forward += KILL_LINE
                    continue
                if command:
                    commands.append(command)
                forward.append(byte)
            elif byte in (0x7f, 0x08):
                # UTF-8: символ убирается целиком, а не последний байт.
                while self._line and self._line[-1] & 0xc0 == 0x80:
                    self._line.pop()
                if self._line:
                    self._line.pop()
                forward.append(byte)
            elif byte in (0x03, 0x15):
                self._line.clear()
                forward.append(byte)
            else:
                if byte >= 0x20:
                    self._line.append(byte)
                forward.append(byte)
        return InputResult(bytes(forward), tuple(commands), tuple(denied))
