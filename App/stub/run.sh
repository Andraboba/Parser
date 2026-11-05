#!/bin/sh
# Запуск заглушки на сервере. Настройки уже в окружении, их выставляет runner из agent.env.
exec python3 agent_stub.py
