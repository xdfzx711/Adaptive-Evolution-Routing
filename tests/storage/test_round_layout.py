import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aer_core.schemas.references import ArtifactRef
from experiments.reorganize import reorganize_run
from experiments.reporting import summarize_saved_run
from plugins.storage._files import FileRoot, canonical_bytes
from plugins.storage.artifacts import LocalArtifactStore
from plugins.storage.layout import RunLayout
from plugins.storage.versions import LocalVersionStore
from tests.orchestration.helpers import attach_model, configured


def legacy_run(directory):
    # Exercise the supported legacy backend without relying on user run data.
    with patch.object(RunLayout, "initialize", return_value=None):
        _, startup = configured(directory, rounds=2)
        task = startup.prepare_task("dev-001")
        attach_model(task, local_failure=True)
        result = task.run()
    root = startup.execution.storage.run_root
    files = FileRoot(root)
    consolidated = files.read_json("run_manifest.json")
    files.immutable("storage_manifest.json", canonical_bytes(startup.execution.storage.manifest.model_dump(mode="json")))
    files.immutable("execution_manifest.json", canonical_bytes({
        **consolidated["execution"], "benchmark": startup.execution.benchmark.as_dict(),
        "plugins": startup.execution.built_plugins.manifest.model_dump(mode="json")}))
    files.immutable("evolution_manifest.json", canonical_bytes(task.evolution.built_plugins.manifest.model_dump(mode="json")))
    files.immutable("orchestrator_manifest.json", canonical_bytes(consolidated["orchestrator"]))
    files.immutable("validation_manifest.json", canonical_bytes({"budget_enforced": False,
        "plugins": task.validation.built_plugins.manifest.model_dump(mode="json"),
        "conditions": task.validation.built_plugins.instances["validation"].conditions.model_dump(mode="json")["conditions"]}))
    (root / "run_manifest.json").unlink()
    return startup, task, result


class RoundLayoutTests(unittest.TestCase):
    def test_new_run_places_all_artifacts_versions_validation_and_cache_under_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=2)
            task = startup.prepare_task("dev-001")
            attach_model(task, local_failure=True)
            result = task.run()
            root = startup.execution.storage.run_root
            startup.save_record(result)
            for old in ("artifacts", "versions", "validation"):
                self.assertFalse((root / old).exists())
            manifests = list(root.glob("*manifest.json"))
            self.assertEqual([path.name for path in manifests], ["run_manifest.json"])
            text = manifests[0].read_text(encoding="utf-8")
            self.assertNotIn("config_schema", text)
            self.assertNotIn('"factory"', text)
            data = json.loads(text)
            self.assertEqual(set(data) - {"schema_version"}, {"storage", "execution", "evolution", "validation", "orchestrator"})
            for path in root.rglob("*"):
                self.assertFalse(re.fullmatch(r"[0-9a-f]{64}", path.stem), str(path))
            episode = next((root / "episodes").glob("*.json"))
            self.assertEqual(episode.name, task.context.episode_id + ".json")
            index = RunLayout(root).read()
            for ref in index["artifacts"].values():
                self.assertTrue(ref["uri"].startswith("rounds/"))
                startup.execution.storage.artifact_store.get(ArtifactRef.model_validate(ref))
            for category, entries in index["records"].items():
                if category != "episodes":
                    self.assertTrue(all(uri.startswith("rounds/") for uri in entries.values()), category)
            round1 = next(root.glob("rounds/dev-001-*/round-001"))
            round2 = next(root.glob("rounds/dev-001-*/round-002"))
            self.assertTrue(list((round1 / "validation/cache").glob("*.json")))
            self.assertTrue(list((round2 / "validation/cache").glob("*.json")))
            self.assertTrue(list((round1 / "versions").glob("commit-receipt-*.json")))
            self.assertFalse(list((round2 / "versions").glob("commit-receipt-*.json")))

    def test_legacy_migration_preserves_bytes_refs_lineage_and_offline_export(self):
        with tempfile.TemporaryDirectory() as directory:
            startup, task, result = legacy_run(Path(directory))
            root = startup.execution.storage.run_root
            artifacts = startup.execution.storage.artifact_store
            refs = [ArtifactRef.model_validate(ref) for ref in artifacts._index().values()]
            original_artifacts = {ref.artifact_id: artifacts.get(ref) for ref in refs}
            original_events = (root / "experience/events.jsonl").read_bytes()
            old_snapshot = task.execution.storage.version_store.stable()
            result_ref = next(event.payload.result_ref for event in startup.execution.storage.experience_store.events()
                              if event.record_type == "task_finished")
            info = reorganize_run(root)
            self.assertTrue(Path(info["backup"]).is_file())
            self.assertEqual((root / "experience/events.jsonl").read_bytes(), original_events)
            reopened = LocalArtifactStore(root)
            for ref in refs:
                self.assertEqual(reopened.get(ref), original_artifacts[ref.artifact_id])
            versions = LocalVersionStore(root, reopened, scope_id=task.scope_id)
            self.assertEqual(versions.stable(), old_snapshot)
            self.assertEqual(versions.load(result.initial_version_id).parent_version_id, None)
            for old in ("artifacts", "versions", "validation"):
                self.assertFalse((root / old).exists())
            self.assertEqual(reorganize_run(root)["status"], "already_organized")
            source = Path(directory) / "result.json"
            source.write_text(json.dumps({"run_root": str(root), "record": result.model_dump(mode="json"),
                "record_ref": result_ref.model_dump(mode="json")}), encoding="utf-8")
            (root / "evolution.txt").unlink()
            with patch.object(task.execution.agent.model, "generate", side_effect=AssertionError("must be offline")):
                summarize_saved_run(source)
            text = (root / "evolution.txt").read_text(encoding="utf-8")
            self.assertIn("实际提交：成功", text)
            self.assertNotIn(str(root / "artifacts"), text)

    def test_failed_migration_restores_original_files(self):
        with tempfile.TemporaryDirectory() as directory:
            startup, _, _ = legacy_run(Path(directory))
            root = startup.execution.storage.run_root
            originals = {path.relative_to(root): path.read_bytes() for path in root.rglob("*")
                         if path.is_file() and path.name != ".storage.lock"}
            with patch.object(LocalVersionStore, "load", side_effect=RuntimeError("fixture verification failure")):
                with self.assertRaisesRegex(RuntimeError, "fixture verification failure"):
                    reorganize_run(root)
            self.assertFalse((root / "storage_index.json").exists())
            self.assertFalse((root / "run_manifest.json").exists())
            for name, content in originals.items():
                self.assertEqual((root / name).read_bytes(), content, name)

    def test_frozen_import_from_migrated_legacy_refs_stays_in_rounds(self):
        import yaml
        from experiments.storage import prepare_storage
        with tempfile.TemporaryDirectory() as directory:
            startup, task, result = legacy_run(Path(directory))
            root = startup.execution.storage.run_root
            reorganize_run(root)
            layout = RunLayout(root)
            source = root / layout.read()["records"]["snapshots"][result.final_stable_version_id]
            data = yaml.safe_load(startup.execution.benchmark.loaded_config.source_path.read_text(encoding="utf-8"))
            data["evolution"]["mode"] = "disabled"
            data["limits"]["max_evolution_rounds"] = 0
            data["routing"] = {"plugin": None, "fixed_target": None, "manual_decisions": []}
            data["targets"] = {"allowed": [], "default_operators": {}}
            data["benchmark"]["execution_split"] = "test"
            data["agent"]["initial_prompt"] = None
            data["agent"]["initial_snapshot_ref"] = str(source)
            config = Path(directory) / "frozen.yaml"
            config.write_text(yaml.safe_dump(data), encoding="utf-8")
            imported = prepare_storage(config, run_id="frozen")
            self.assertEqual(imported.version_store.stable(), task.execution.storage.version_store.stable())
            self.assertFalse((imported.run_root / "artifacts").exists())
            self.assertTrue(all(path.is_relative_to(imported.run_root / "rounds")
                                for path in imported.run_root.rglob("*.bin")))


if __name__ == "__main__":
    unittest.main()
