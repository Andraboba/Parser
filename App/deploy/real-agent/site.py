"""Управляемый тестовый сайт из каталога Agent/ как отдельный контейнер стенда.

Использует тот же MockSite, что и тесты агента, только слушает все интерфейсы постоянного порта
и отдаёт ссылки с именем сервиса: иначе контейнеры агентов не смогут скачать файлы.
"""
import threading

import mock_services

HOST = "site"
PORT = 8081


class PublicServer(mock_services._QuietServer):
    def __init__(self, _address, handler):
        super().__init__(("0.0.0.0", PORT), handler)


mock_services._QuietServer = PublicServer


class PublicSite(mock_services.MockSite):
    @property
    def url(self) -> str:
        return f"http://{HOST}:{PORT}"


PublicSite().start()
print(f"тестовый сайт: http://{HOST}:{PORT}", flush=True)
threading.Event().wait()
