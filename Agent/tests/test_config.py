# -*- coding: utf-8 -*-
"""Конфигурация: разбор YAML-подмножества, значения по умолчанию, проверки."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.config import describe, load_config, parse_config
from agent.errors import ConfigError
from agent.yamlish import YamlError, loads
from tests.support.tmpdirs import make_temp_dir

EXAMPLE = """
# комментарий
agent:
  name: worker-01
  threads: 8
  storage_path: /data/videos
  min_free_space_gb: 2

coordinator:
  base_url: http://coordinator:8000
  token: ${AGENT_TOKEN}
  heartbeat_interval: 5
  lease_ttl: 60

limits:
  rps: 20
  max_connections: 16

site_adapter:
  page_url_template: "http://kinescope.io/{id}"
  user_agent: "lab-agent/1.0"
  follow_redirects: false
  allowed_redirect_hosts: []
  missing_markers:
    - "страница не найдена"
    - "not found"
  download:
    policy: public_only
    max_file_size_bytes: 1048576
"""


class YamlishTests(unittest.TestCase):
    def test_nested_mapping_and_list(self) -> None:
        data = loads(EXAMPLE)
        self.assertEqual(data["agent"]["name"], "worker-01")
        self.assertEqual(data["agent"]["threads"], 8)
        self.assertEqual(data["site_adapter"]["allowed_redirect_hosts"], [])
        self.assertEqual(len(data["site_adapter"]["missing_markers"]), 2)
        self.assertFalse(data["site_adapter"]["follow_redirects"])

    def test_list_of_mappings(self) -> None:
        data = loads(
            """
servers:
  - name: worker-01
    threads: 8
    limits:
      cpus: 1.0
  - name: worker-02
    threads: 16
"""
        )
        self.assertEqual(len(data["servers"]), 2)
        self.assertEqual(data["servers"][0]["limits"]["cpus"], 1.0)
        self.assertEqual(data["servers"][1]["threads"], 16)

    def test_comments_and_scalars(self) -> None:
        data = loads('a: 1  # число\nb: "текст # не комментарий"\nc: null\nd: true\n')
        self.assertEqual(data["a"], 1)
        self.assertEqual(data["b"], "текст # не комментарий")
        self.assertIsNone(data["c"])
        self.assertIs(data["d"], True)

    def test_broken_indentation(self) -> None:
        with self.assertRaises(YamlError):
            loads("a:\n    b: 1\n  c: 2\n")


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        with make_temp_dir() as tmp:
            cfg = parse_config({"agent": {"storage_path": tmp}})
        self.assertEqual(cfg.name, "worker-01")
        self.assertEqual(cfg.site.page_url_template, "http://kinescope.io/{id}")
        self.assertEqual(cfg.site.download.policy, "public_only")
        self.assertEqual(cfg.state_dir, cfg.storage_path / ".agent")

    def test_load_from_file_with_env(self) -> None:
        with make_temp_dir() as tmp:
            path = Path(tmp) / "agent.yaml"
            text = EXAMPLE.replace("/data/videos", str(Path(tmp) / "videos").replace("\\", "/"))
            path.write_text(text, encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_TOKEN": "secret-token", "AGENT_THREADS": "3"}):
                cfg = load_config(path)
        self.assertEqual(cfg.coordinator.token, "secret-token")
        self.assertEqual(cfg.threads, 3)
        self.assertEqual(cfg.coordinator.base_url, "http://coordinator:8000")
        self.assertEqual(cfg.site.download.max_file_size_bytes, 1048576)

    def test_template_requires_id(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config({"site_adapter": {"page_url_template": "http://example.test/page"}})

    def test_bad_policy(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config({"site_adapter": {"download": {"policy": "everything"}}})

    def test_lease_shorter_than_two_heartbeats(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config(
                {"coordinator": {"heartbeat_interval": 10, "lease_ttl": 5}}
            )

    def test_describe_hides_token(self) -> None:
        cfg = parse_config({"coordinator": {"token": "abc"}})
        described = describe(cfg)
        self.assertEqual(described["coordinator"]["token"], "***")


if __name__ == "__main__":
    unittest.main()
