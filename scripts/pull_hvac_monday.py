#!/usr/bin/env python3
"""Pull all HVAC scheduled jobs for Monday (or N days ahead)."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from servicetitan_dossier import main, load_env  # noqa: E402

ENV_PATHS = [
    "/workspace/openclaw/MOVING/credentials/MASTER.env",
    "/workspace/apps/openclaw-credential-archive/20260526T032211Z/secrets/MOVING/credentials/MASTER.env",
    "/workspace/.secrets/hermes.env",
    str(ROOT / ".env"),
]


def load_all_env() -> None:
    for p in ENV_PATHS:
        load_env(p, override=(p == str(ROOT / ".env")))


if __name__ == "__main__":
    load_all_env()
    days = 3  # Friday -> Monday
    if len(sys.argv) > 1:
        days = int(sys.argv[1])
    main(days_ahead=days, max_jobs=400, trade_filter="hvac")
