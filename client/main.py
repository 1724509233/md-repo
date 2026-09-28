"""Client entry point: python -m client.main [config.yaml]"""
from __future__ import annotations

import sys
from pathlib import Path

from .config import load_client_config
from .sync import run_polling_loop


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    config_path = argv[0] if argv else "config.yaml"

    config = load_client_config(config_path)
    (config.vault_dir / ".sync").mkdir(parents=True, exist_ok=True)

    try:
        run_polling_loop(config)
    except KeyboardInterrupt:
        print("Stopped")


if __name__ == "__main__":
    main()
