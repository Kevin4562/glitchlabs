"""Deprecated names for the public :mod:`glitchlab.connections` SDK.

New modules must import the Connection* names. These aliases exist only so old
campaign records, scripts, and third-party imports continue to load.
"""
from __future__ import annotations

from ...connections import (
    VALID_VERDICTS,
    ConnectionCapabilities as OracleCapabilities,
    ConnectionModule as Oracle,
    ConnectionReading as OracleReading,
)

# Historical raw-worker records used this schema identifier. It remains frozen
# for read-side compatibility; new connector records use connection-reading/v1.
ORACLE_READING_SCHEMA_VERSION = "glitchlab.oracle-reading/v2"

__all__ = [
    "ORACLE_READING_SCHEMA_VERSION",
    "VALID_VERDICTS",
    "Oracle",
    "OracleCapabilities",
    "OracleReading",
]
