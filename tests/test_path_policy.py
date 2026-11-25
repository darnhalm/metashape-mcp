import tempfile
import unittest
from pathlib import Path

from path_policy import describe_roots, resolve_scoped


class PathPolicyTests(unittest.TestCase):
    def test_allows_descendant_and_rejects_outside(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "allowed"
            root.mkdir()
            inside = root / "project.psx"
            outside = base / "outside.psx"
            self.assertEqual(resolve_scoped(str(inside), [str(root)]), inside.resolve())
            with self.assertRaisesRegex(ValueError, "вне разрешённых"):
                resolve_scoped(str(outside), [str(root)])

    def test_requires_absolute_path_and_configured_roots(self):
        with self.assertRaisesRegex(ValueError, "абсолютным"):
            resolve_scoped("relative/project.psx", ["/tmp"])
        with self.assertRaisesRegex(ValueError, "не выбраны"):
            resolve_scoped("/tmp/project.psx", [])

    def test_symlink_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "allowed"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "вне разрешённых"):
                resolve_scoped(str(root / "link" / "project.psx"), [str(root)])

    def test_describes_missing_root(self):
        missing = Path(tempfile.gettempdir()) / "metashape-mcp-missing-root"
        result = describe_roots([str(missing)])
        self.assertEqual(result, [{"path": str(missing.resolve()), "exists": False}])


if __name__ == "__main__":
    unittest.main()
