"""Configuration module for feibot."""

from feibot.config.loader import load_config
from feibot.config.schema import Config

__all__ = ["Config", "load_config"]
