"""Permite executar `python3 -m ctx` diretamente a partir do terminal."""

from __future__ import annotations

import sys
from ctx.cli import main

if __name__ == "__main__":
    sys.exit(main())
