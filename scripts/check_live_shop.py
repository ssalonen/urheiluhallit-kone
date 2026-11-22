#!/usr/bin/env python3
"""Check the live shop's public GET sources without sessions or purchases."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uhkone.shop_contract import ContractError, run_public_contract


def main() -> int:
    try:
        result = run_public_contract()
    except ContractError as exc:
        print(f"Julkisen kaupan tarkistus epäonnistui: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
