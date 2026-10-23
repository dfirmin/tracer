#!/usr/bin/env python3
"""finding_hash — a stable identity for one finding's content.

    finding-hash <finding.json>      prints the hash

The hash covers the finding the tracer produced, not the review stamped onto it afterwards,
so a review can say exactly which finding version it accepted. assemble.py imports
finding_hash(); review-one runs this file as a script.
"""
import hashlib, json, sys

STAMPED = ("review",)  # keys the harness adds after the tracer returned


def finding_hash(finding: dict) -> str:
    body = {k: v for k, v in finding.items() if k not in STAMPED}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


if __name__ == "__main__":
    with open(sys.argv[1]) as fh:
        print(finding_hash(json.load(fh)))
