# -*- coding: utf-8 -*-
"""Настройка тестовой среды."""

import logging

# Тесты не настраивают журналирование: гасим вывод «последней надежды».
logging.getLogger().addHandler(logging.NullHandler())
