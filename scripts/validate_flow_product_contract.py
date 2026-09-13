#!/usr/bin/env python3
"""Validate and fingerprint the side-effect-free v0.4 product contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.product_contract import canonical_contract_sha256, load_product_contract


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("config/flow-product-contract.json"),
        help="Path to the machine-readable product contract",
    )
    args = parser.parse_args()
    contract = load_product_contract(args.contract)
    print(
        json.dumps(
            {
                "schemaVersion": contract["schemaVersion"],
                "contractVersion": contract["contractVersion"],
                "classificationStatus": contract["classificationStatus"],
                "canonicalSha256": canonical_contract_sha256(contract),
                "capabilityCount": len(contract["capabilities"]),
                "activation": contract["activation"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
