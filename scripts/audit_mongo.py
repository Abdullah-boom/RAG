"""Milestone 1 data audit: inspect the MongoDB schema read-only and write docs/data_audit.md.

Reads only: counts, a sample of documents per collection, inferred field types.
Values of fields that look like personal data are masked in the report.
"""
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pymongo import MongoClient, ReadPreference  # noqa: E402

from app import config  # noqa: E402

SAMPLE = 200
PII_KEY = re.compile(r"(name|phone|mobile|email|address|street|lat|lng|location|card|cnic|password|token|ip)", re.I)


def walk(doc, prefix="", out=None):
    out = out if out is not None else []
    if isinstance(doc, dict):
        for k, v in doc.items():
            path = f"{prefix}.{k}" if prefix else k
            out.append((path, type(v).__name__))
            if isinstance(v, dict):
                walk(v, path, out)
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                walk(v[0], path + "[]", out)
    return out


def mask(doc, key=""):
    if isinstance(doc, dict):
        return {k: mask(v, k) for k, v in doc.items()}
    if isinstance(doc, list):
        return [mask(v, key) for v in doc[:3]] + (["..."] if len(doc) > 3 else [])
    if PII_KEY.search(key) and not key.endswith("_id") and isinstance(doc, (str, int, float)):
        return "<masked>"
    s = str(doc)
    return s if len(s) <= 120 else s[:117] + "..."


def main():
    """Usage: audit_mongo.py db.collection [db.collection ...]"""
    targets = sys.argv[1:]
    if not config.MONGO_URI or not targets:
        sys.exit("Set MONGO_URI in .env and pass db.collection targets.")
    client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=10000,
                         read_preference=ReadPreference.SECONDARY_PREFERRED)
    lines = ["# Data audit", "", f"Server version: {client.server_info().get('version')}", ""]
    for name in targets:
        db_name, coll_name = name.split(".", 1)
        coll = client[db_name][coll_name]
        count = coll.estimated_document_count()
        docs = list(coll.find({}, limit=SAMPLE))
        fields = defaultdict(Counter)
        for d in docs:
            for path, t in walk(d):
                fields[path][t] += 1
        lines += [f"## {name}  ({count} docs)", "", "| field | types (in sample) | present | PII? |", "|---|---|---|---|"]
        for path, types in sorted(fields.items()):
            present = f"{max(types.values()) * 100 // max(len(docs), 1)}%"
            pii = "yes?" if PII_KEY.search(path.split(".")[-1]) and not path.endswith("_id") else ""
            lines.append(f"| `{path}` | {', '.join(f'{t}:{n}' for t, n in types.items())} | {present} | {pii} |")
        if docs:
            lines += ["", "Sample (PII-like fields masked):", "```", repr(mask(docs[0])), "```"]
        lines.append("")
    out = config.ROOT / "docs" / "data_audit.md"
    out.write_text("\n".join(lines))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
