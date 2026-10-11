"""Enterprise workspace shell. Reads existing Founder OS authority; does not grant it."""

from src.ui.enterprise.catalog import API_GAPS, resolve_surface
from src.ui.enterprise.compose import compose_surface

__all__ = ["API_GAPS", "compose_surface", "resolve_surface"]
