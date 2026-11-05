from coordinator.api.app import create_app
from coordinator.logging_setup import setup_logging

setup_logging("api")
app = create_app()
