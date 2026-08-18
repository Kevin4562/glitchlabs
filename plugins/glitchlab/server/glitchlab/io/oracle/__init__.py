"""Deprecated compatibility aliases; use :mod:`glitchlab.connections`."""

from .base import (
    ORACLE_READING_SCHEMA_VERSION,
    VALID_VERDICTS,
    Oracle,
    OracleCapabilities,
    OracleReading,
)
from .registry import (
    describe_oracle_plugins,
    make_oracle,
    make_oracle_from_config,
    make_oracle_from_project,
    register_oracle_plugin,
)

__all__ = [
    "ORACLE_READING_SCHEMA_VERSION",
    "Oracle",
    "OracleCapabilities",
    "OracleReading",
    "VALID_VERDICTS",
    "describe_oracle_plugins",
    "make_oracle",
    "make_oracle_from_config",
    "make_oracle_from_project",
    "register_oracle_plugin",
]
