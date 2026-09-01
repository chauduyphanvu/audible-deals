"""Allow running as: python -m audible_deals"""

import sys
import types


def _install_frozen_pillow_stub() -> None:
    pil = types.ModuleType("PIL")
    image = types.ModuleType("PIL.Image")
    pil.Image = image
    sys.modules["PIL"] = pil
    sys.modules["PIL.Image"] = image


if getattr(sys, "frozen", False):
    _install_frozen_pillow_stub()

from audible_deals.cli import cli  # noqa: E402

cli()
