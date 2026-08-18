"""Glitcher adapter registry.

Live adapter construction fails closed.  A missing Husky, driver error, or bad
configuration must never be silently replaced with a simulator: that makes an
AI believe a hardware campaign is running when it is not.  Simulation is
available only through an explicit simulator id.
"""
from .base import GlitchParams, GlitchResult, GlitcherAdapter
from .simulator import SimulatorGlitcher


class GlitcherUnavailable(RuntimeError):
    """A requested live adapter could not be constructed."""


def make_glitcher(glitcher_id: str, **kw):
    """Construct the requested adapter; never downgrade live work to simulation."""
    gid = (glitcher_id or "simulator").lower()
    if gid in ("chipwhisperer_husky", "husky", "chipwhisperer"):
        try:
            from .husky import HuskyGlitcher
            return HuskyGlitcher(**kw)
        except Exception as exc:  # pragma: no cover - depends on live driver
            raise GlitcherUnavailable(f"ChipWhisperer Husky unavailable: {exc}") from exc
    if gid in ("simulator", "sim"):
        return SimulatorGlitcher(**kw)
    try:
        from ...connections.registry import load_private_glitcher_class
        cls = load_private_glitcher_class(gid)
        return cls(**kw)
    except Exception as exc:
        raise GlitcherUnavailable(
            f"private glitcher adapter {glitcher_id!r} unavailable: {exc}"
        ) from exc
