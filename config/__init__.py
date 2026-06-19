"""
config/ — configuration loader for the engine.
Loads config.yaml and makes CONFIG available to all modules.
"""
import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    """Load config.yaml. Startup fails loudly if the file is missing or malformed."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.yaml not found at {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        raw = yaml.safe_load(f)
    if not raw:
        raise ValueError("config.yaml is empty or not valid YAML")
    return raw


# Raise immediately on startup if config is missing/broken — no silent fallbacks
CONFIG: dict = load_config()
