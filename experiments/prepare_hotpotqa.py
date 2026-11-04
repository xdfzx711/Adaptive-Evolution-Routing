"""Prepare local HotpotQA sources and deterministic task-specific holdouts.

Run with --download to fetch the checksummed HF copy of the official dataset.
Existing official JSON files can instead be supplied with --train and --dev.
This command never calls models. Dataset contents are excluded from Git.
"""

import argparse
import json
import os
from pathlib import Path
import urllib.request

from plugins.benchmarks.manifest import DatasetSource, TaskManifest, file_digest, read_json, select_holdouts


HF_REVISION = "1908d6afbbead072334abe2965f91bd2709910ab"
FILES = {
    "train-00000-of-00002.parquet": "76d3bb3048a7cc73c1958107c0c5872a00d7e7d00c105b81e92f6769e7822e68",
    "train-00001-of-00002.parquet": "713661628434fbb19fff7392e2e321e4ed107e3c7c7784d0690946e5f722763f",
    "validation-00000-of-00001.parquet": "c20b638ca82b21d04fe12e14ff417ad05153d4d215a65de54497fca4e972f7c6",
}


def download_sources(root):
    import pyarrow.parquet as pq

    root.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        target = root / name
        if target.exists():
            if file_digest(target) != expected:
                raise ValueError(f"Existing source has incorrect checksum: {name}")
            continue
        errors = []
        for host in ("huggingface.co", "hf-mirror.com"):
            url = f"https://{host}/datasets/hotpotqa/hotpot_qa/resolve/{HF_REVISION}/distractor/{name}?download=true"
            try:
                print(f"Downloading {name} from {host}", flush=True)
                partial = target.with_suffix(".part")
                with urllib.request.urlopen(url, timeout=60) as source, partial.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                if file_digest(partial) != expected:
                    raise ValueError(f"Downloaded source checksum mismatch: {name}")
                partial.replace(target)
                break
            except (OSError, ValueError) as error:
                errors.append(str(error))
        else:
            raise OSError(f"Cannot download {name}: {'; '.join(errors)}")
    paths = []
    for split in ("train", "dev"):
        target = root / f"hotpot_{split}_distractor.json"
        parquet_paths = [root / name for name in FILES if (name.startswith("train") if split == "train" else name.startswith("validation"))]
        partial = target.with_suffix(".part")
        count = 0
        with partial.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write("[")
            for path in parquet_paths:
                for batch in pq.ParquetFile(path).iter_batches(batch_size=256):
                    for row in batch.to_pylist():
                        context, facts = row["context"], row["supporting_facts"]
                        native = {"_id": row["id"], "question": row["question"], "answer": row["answer"],
                            "type": row["type"], "level": row["level"],
                            "context": list(zip(context["title"], context["sentences"], strict=True)),
                            "supporting_facts": list(zip(facts["title"], facts["sent_id"], strict=True))}
                        stream.write(("," if count else "") + json.dumps(native, ensure_ascii=False))
                        count += 1
            stream.write("]\n")
        partial.replace(target)
        paths.append(target)
        print(f"Prepared {count} {split} rows", flush=True)
    receipt = {"dataset": "hotpotqa/hotpot_qa", "revision": HF_REVISION, "setting": "distractor",
               "parquet_sha256": FILES, "converted_json_sha256": {path.name: file_digest(path) for path in paths},
               "dataset_license": "CC BY-SA 4.0", "source": "https://hotpotqa.github.io/"}
    (root / "download_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return paths


def prepare_manifest(train, dev, output, *, seed=42, task_count=4, similar_count=2, regression_count=2,
                     test_count=4, selection="legacy-shared"):
    train, dev, output = Path(train).resolve(), Path(dev).resolve(), Path(output).resolve()
    if output in {train, dev} or train == dev or output.exists():
        raise ValueError("Use distinct sources and a new manifest output path")
    if test_count < 0:
        raise ValueError("test_count must be nonnegative")
    if selection not in {"legacy-shared", "exclusive"}:
        raise ValueError("unknown selection policy")
    training = read_json(train)
    development = read_json(dev)
    categories = {row["_id"]: row["type"] for row in training}
    test_ids = [row["_id"] for row in development]
    if len(categories) != len(training) or len(set(test_ids)) != len(test_ids):
        raise ValueError("duplicate source task IDs")
    if set(categories) & set(test_ids):
        raise ValueError("official train and dev source IDs must be disjoint")
    if len(test_ids) < test_count:
        raise ValueError("not enough official dev examples for the reserved test subset")
    from hashlib import sha256
    test_ids = sorted(test_ids, key=lambda key: sha256(f"{seed}:{key}".encode()).hexdigest())[:test_count]
    policy = {"version": "public_category_stratified.v1", "seed": seed, "category": "type",
        "dev_count": task_count, "similar_per_task": similar_count, "regression_per_task": regression_count}
    if selection == "exclusive":
        from plugins.benchmarks.hotpotqa_selection import select_exclusive_episodes
        from plugins.benchmarks.hotpotqa import validate_row
        eligible, invalid = [], []
        for row in training:
            try:
                validate_row(row)
            except ValueError as error:
                invalid.append({"task_id": row["_id"], "reason": str(error)})
            else:
                eligible.append(row)
        selected_tests = set(test_ids)
        roles, selections, policy = select_exclusive_episodes(eligible, seed=seed, task_count=task_count,
            similar_count=similar_count, regression_count=regression_count,
            test_rows=[row for row in development if row["_id"] in selected_tests])
        policy["source_integrity"] = {"rule": "valid official row structure and supporting-fact references within supplied context; no answer-content or performance filtering",
            "source_count": len(training), "eligible_count": len(eligible), "excluded": invalid}
    else:
        roles, selections = select_holdouts(categories, seed=seed, task_count=task_count,
            similar_count=similar_count, regression_count=regression_count, test_ids=test_ids)
    sources = [DatasetSource(path=Path(os.path.relpath(path, output.parent)).as_posix(), sha256=file_digest(path), source_split=split)
               for path, split in ((train, "official_train"), (dev, "official_dev"))]
    manifest = TaskManifest(benchmark_id="hotpotqa", sources=sources, roles=roles, validation_sets=selections,
        holdout_reuse="forbidden" if selection == "exclusive" else "allowed",
        selection_policy={**policy, "test_count": test_count, "setting": "distractor",
            "test_note": "Reserved official-dev subset; not the hidden official test set"})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(manifest.model_dump_json(indent=2) + "\n")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--download-only", action="store_true", help="restore pinned source files without changing an existing manifest")
    parser.add_argument("--data-root", type=Path, default=Path(".aer-cache/hotpotqa"))
    parser.add_argument("--train", type=Path)
    parser.add_argument("--dev", type=Path)
    parser.add_argument("--output", type=Path, default=Path("experiments/data/hotpotqa_300_manifest.json"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--tasks", type=int, default=300)
    parser.add_argument("--similar", type=int, default=1)
    parser.add_argument("--regression", type=int, default=1)
    parser.add_argument("--test", type=int, default=0)
    parser.add_argument("--selection", choices=("exclusive", "legacy-shared"), default="exclusive",
                        help="exclusive: label-free matching with no holdout or normalized-question reuse")
    args = parser.parse_args(argv)
    if args.output.exists() and not args.download_only:
        parser.error("manifest already exists; choose a new --output to preserve the experiment split")
    if (args.download or args.download_only) and (args.train or args.dev):
        parser.error("use --download or explicit --train/--dev, not both")
    try:
        if args.download or args.download_only:
            train, dev = download_sources(args.data_root)
            if args.download_only:
                return 0
        else:
            train = args.train or args.data_root / "hotpot_train_distractor.json"
            dev = args.dev or args.data_root / "hotpot_dev_distractor.json"
        manifest = prepare_manifest(train, dev, args.output, seed=args.seed, task_count=args.tasks,
            similar_count=args.similar, regression_count=args.regression, test_count=args.test, selection=args.selection)
        from collections import Counter
        print(json.dumps({"manifest": str(args.output.resolve()), "role_counts": Counter(manifest.roles.values()),
                          "selection": manifest.selection_policy["version"], "holdout_reuse": manifest.holdout_reuse}, indent=2))
    except (OSError, ValueError, KeyError, ImportError) as error:
        parser.exit(2, f"HotpotQA preparation failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
