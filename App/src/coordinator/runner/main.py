from coordinator.config import Settings
from coordinator.logging_setup import setup_logging
from coordinator.runner.app import create_runner_app

setup_logging("runner")

app = create_runner_app(Settings())
