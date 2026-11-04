import copy
import json
from pathlib import Path
import tempfile
import unittest

from plugins.benchmarks.hotpotqa_selection import (
    normalized_question, question_form, select_exclusive_episodes, validate_selected_rows,
)
from plugins.benchmarks.manifest import DatasetSource, TaskManifest


def public_rows():
    return [{"_id": f"{kind}-{i}", "type": kind, "level": "hard",
        "question": (f"Where is the rotating globe monument number {i} located?" if kind == "bridge" else
                     f"Are publisher {i} and journal {i} both American publications?"),
        "answer": "DO_NOT_USE", "supporting_facts": [["PRIVATE", 42]]}
        for kind in ("bridge", "comparison") for i in range(24)]


class ExclusiveHotpotSelectionTests(unittest.TestCase):
    def select(self, rows=None):
        return select_exclusive_episodes(public_rows() if rows is None else rows, seed=42, task_count=6)

    def manifest(self):
        roles, selections, policy = self.select()
        return TaskManifest(benchmark_id="hotpotqa", sources=[DatasetSource(path="train.json", sha256="0" * 64,
            source_split="official_train")], roles=roles, validation_sets=selections,
            holdout_reuse="forbidden", selection_policy=policy)

    def test_reproducible_label_free_selection_and_global_exclusivity(self):
        rows = public_rows()
        first = self.select(rows)
        changed = copy.deepcopy(list(reversed(rows)))
        for row in changed:
            row["answer"] = "MUTATED_PRIVATE_LABEL"
            row["supporting_facts"] = []
        self.assertEqual(first, self.select(changed))
        roles, selections, policy = first
        ids = [key for key in selections]
        for selection in selections.values():
            ids += selection.similar + selection.regression
        self.assertEqual(len(ids), 18)
        self.assertEqual(len(set(ids)), 18)
        self.assertEqual(set(ids), set(roles))
        validate_selected_rows({row["_id"]: row for row in rows if row["_id"] in roles}, self.manifest())
        self.assertTrue(all(e["similar"][0]["question_cosine"] >= 0.1 for e in policy["assignments"].values()))

    def test_duplicate_questions_are_removed_across_ids(self):
        rows = public_rows()
        rows.append(dict(rows[0], _id="another-id", question=rows[0]["question"].upper() + "!!"))
        roles, _, policy = self.select(rows)
        selected = [normalized_question(row["question"]) for row in rows if row["_id"] in roles]
        self.assertEqual(len(selected), len(set(selected)))
        self.assertEqual(policy["unique_source_questions"], len(rows) - 1)

    def test_reuse_rejected_for_exclusive_but_legacy_remains_supported(self):
        data = self.manifest().model_dump()
        first, second = list(data["validation_sets"])[:2]
        data["validation_sets"][second]["similar"] = data["validation_sets"][first]["similar"]
        with self.assertRaisesRegex(ValueError, "exactly one current"):
            TaskManifest.model_validate(data)
        data["holdout_reuse"] = "allowed"
        TaskManifest.model_validate(data)

    def test_startup_rechecks_actual_matching_features(self):
        manifest = self.manifest()
        rows = {row["_id"]: row for row in public_rows() if row["_id"] in manifest.roles}
        current = next(iter(manifest.validation_sets))
        similar = manifest.validation_sets[current].similar[0]
        rows[similar]["level"] = "easy"
        with self.assertRaisesRegex(ValueError, "violates declared"):
            validate_selected_rows(rows, manifest)

    def test_impossible_selection_fails_without_relaxation(self):
        rows = public_rows()
        for row in rows:
            row["question"] = "Where is monument " + row["_id"] + " located?"
        with self.assertRaisesRegex(ValueError, "regression tasks"):
            self.select(rows)
        with self.assertRaisesRegex(ValueError, "unique"):
            self.select(rows + [rows[0]])

    def test_question_form_rules_use_only_question_wording(self):
        cases = {"Are A and B both American?": "yes_no", "Where is A?": "location",
            "In what year was A born?": "time", "How many A exist?": "quantity",
            "Who founded A?": "person", "Why did A close?": "reason", "Which event caused A?": "entity_or_other"}
        for question, expected in cases.items():
            self.assertEqual(question_form(question), expected)

    def test_preparation_records_invalid_sources_and_reproduces_without_overwrite(self):
        from experiments.prepare_hotpotqa import prepare_manifest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = public_rows()
            for row in rows:
                row["context"] = [["PRIVATE", ["Public source sentence."]]]
                row["supporting_facts"] = [["PRIVATE", 0]]
            rows.append(dict(rows[0], _id="invalid-evidence-reference", supporting_facts=[["PRIVATE", 99]]))
            (root / "train.json").write_text(json.dumps(rows), encoding="utf-8")
            (root / "dev.json").write_text("[]", encoding="utf-8")
            kwargs = dict(task_count=6, similar_count=1, regression_count=1, test_count=0, selection="exclusive")
            a = prepare_manifest(root / "train.json", root / "dev.json", root / "one.json", **kwargs)
            b = prepare_manifest(root / "train.json", root / "dev.json", root / "two.json", **kwargs)
            self.assertEqual(a, b)
            self.assertEqual(a.selection_policy["source_integrity"]["excluded"][0]["task_id"], "invalid-evidence-reference")
            self.assertNotIn("invalid-evidence-reference", a.roles)
            self.assertEqual((root / "one.json").read_bytes(), (root / "two.json").read_bytes())
            with self.assertRaisesRegex(ValueError, "new manifest"):
                prepare_manifest(root / "train.json", root / "dev.json", root / "one.json", **kwargs)
