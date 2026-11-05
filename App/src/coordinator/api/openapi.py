"""Печатает OpenAPI-описание: артефакт для разработчика агента (docs/openapi.json)."""
import json
import sys

from coordinator.api.app import create_app


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    json.dump(create_app().openapi(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
