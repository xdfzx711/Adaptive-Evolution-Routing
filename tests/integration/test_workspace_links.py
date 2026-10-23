"""Repository link compatibility without external processes or model calls."""

import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

from aer_core.errors import ExecutionError
from plugins.workspace.git import git
from plugins.workspace.local import export_tree


def archive(linkname, *, name="binding/alias.py"):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tree:
        link = tarfile.TarInfo(name)
        link.type, link.linkname = tarfile.SYMTYPE, linkname
        tree.addfile(link)
        content = b"answer = 1\n"
        source = tarfile.TarInfo("module/source.py")
        source.size = len(content)
        tree.addfile(source, io.BytesIO(content))
    return data.getvalue()


class RepositoryLinkTests(unittest.TestCase):
    def test_relative_file_link_is_preserved_even_when_target_appears_later(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            try:
                os.symlink("probe", root / "probe-link")
            except OSError as error:
                self.skipTest(f"Filesystem cannot create symlinks: {error}")
            (root / "probe-link").unlink()
            self.assertTrue(export_tree(archive("../module/source.py"), root))
            alias = root / "binding/alias.py"
            self.assertTrue(alias.is_symlink())
            self.assertEqual(Path(os.readlink(alias)).as_posix(), "../module/source.py")
            self.assertEqual(alias.read_bytes(), b"answer = 1\n")
            git(root, "init", "--quiet")
            git(root, "config", "core.symlinks", "true")
            git(root, "add", "--all")
            self.assertTrue(git(root, "ls-files", "--stage", "binding/alias.py").startswith("120000 "))
            self.assertEqual(git(root, "show", ":binding/alias.py"), "../module/source.py")

    def test_external_and_dangling_links_are_rejected(self):
        for target in ("../../outside.py", "/outside.py", "../missing.py"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ExecutionError):
                    export_tree(archive(target), Path(directory))

    def test_archive_member_cannot_escape_the_workspace(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ExecutionError):
            export_tree(archive("module/source.py", name="../outside.py"), Path(directory))
