"""Prepare the pinned IFBench source and 80 exclusive current/similar/regression triples."""

import argparse
from collections import Counter
from importlib.util import find_spec
import json
import os
from pathlib import Path
import urllib.request

from plugins.benchmarks.ifbench_selection import select_episodes, validate_rows
from plugins.benchmarks.manifest import DatasetSource, TaskManifest, file_digest


UPSTREAM_COMMIT = "cd189a9c2889526d8af901d2103fd3e1ffcea6df"
SOURCE_SHA256 = "a2bf7b8e7ed11d39c65b4ec781da8b99663ed0121f97033d70fcecb61f8a9d85"
SOURCE_URL = f"https://raw.githubusercontent.com/allenai/IFBench/{UPSTREAM_COMMIT}/data/IFBench_test.jsonl"


def read_rows(path):
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    validate_rows(rows)
    return rows


def prepare_source(root, download=False):
    target = Path(root) / "IFBench_test.jsonl"
    if not target.exists():
        package = find_spec("ifbench")
        bundled = Path(package.origin).parent / "data/IFBench_test.jsonl" if package else None
        if bundled is not None and bundled.is_file() and file_digest(bundled) == SOURCE_SHA256:
            content = bundled.read_bytes()
        elif download:
            with urllib.request.urlopen(SOURCE_URL, timeout=60) as response:
                content = response.read()
        else:
            raise ValueError("Install [ifbench] with the pinned dataset or pass --download")
        from hashlib import sha256
        if sha256(content).hexdigest() != SOURCE_SHA256:
            raise ValueError("Downloaded IFBench source checksum mismatch")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if file_digest(target) != SOURCE_SHA256:
        raise ValueError("Existing IFBench source checksum mismatch")
    return target


def prepare_manifest(source, output, *, task_count=80, seed=42):
    source, output = Path(source).resolve(), Path(output).resolve()
    rows = read_rows(source)
    roles, selections, policy = select_episodes(rows, task_count=task_count, seed=seed)
    source_hash = file_digest(source)
    pinned = source_hash == SOURCE_SHA256
    manifest = TaskManifest(benchmark_id="ifbench", sources=[DatasetSource(
        path=Path(os.path.relpath(source, output.parent)).as_posix(), sha256=source_hash,
        source_split="official_test" if pinned else "provided_local")], roles=roles, validation_sets=selections,
        holdout_reuse="forbidden", selection_policy=policy)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    by_id = {str(row["key"]): row for row in rows}
    report = {"manifest": output.name, "source_sha256": source_hash,
        "upstream_commit": UPSTREAM_COMMIT if pinned else None,
        "source_url": SOURCE_URL if pinned else None, "data_license": "ODC-BY-1.0" if pinned else "user-supplied",
        "role_counts": dict(Counter(roles.values())),
        "policy": policy, "episodes": [{"current_id": key, "similar_id": pair.similar[0],
            "regression_id": pair.regression[0], "prompts": {role: by_id[other]["prompt"] for role, other in (
                ("current", key), ("similar", pair.similar[0]), ("regression", pair.regression[0]))}}
            for key, pair in selections.items()]}
    output.with_name(output.stem.replace("_manifest", "") + "_selection_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path(".aer-cache/ifbench"))
    parser.add_argument("--source", type=Path, help="Explicit local JSONL source (hash recorded in manifest)")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("experiments/data/ifbench_80_manifest.json"))
    parser.add_argument("--tasks", type=int, default=80)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    try:
        source = args.source or prepare_source(args.data_root, args.download)
        manifest = prepare_manifest(source, args.output, task_count=args.tasks, seed=args.seed)
        print(json.dumps({"manifest": str(args.output), "roles": dict(Counter(manifest.roles.values())),
            "constraint_count_quotas": manifest.selection_policy["constraint_count_quotas"],
            "unused": len(manifest.selection_policy["unused_ids"])}, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"IFBench preparation failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
