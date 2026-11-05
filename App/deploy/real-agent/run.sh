#!/bin/sh
# Запуск настоящего агента на сервере. Настройки уже в окружении, их выставляет runner из agent.env.
exec python3 -m agent run --config agent.yaml
