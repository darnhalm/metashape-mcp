import re
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.46.0"


class ReleaseMetadataTests(unittest.TestCase):
    def test_public_release_files_exist(self):
        required = {
            "LICENSE",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "README.md",
            "README.en.md",
            "pyproject.toml",
            ".github/workflows/tests.yml",
        }
        missing = sorted(path for path in required if not (ROOT / path).is_file())
        self.assertEqual([], missing)

    def test_current_version_is_consistent(self):
        with (ROOT / "pyproject.toml").open("rb") as stream:
            self.assertEqual(VERSION, tomllib.load(stream)["project"]["version"])

        checks = {
            "start_metashape_mcp.py": rf'revision = "{re.escape(VERSION)}"',
            "metashape_mcp_server.py": rf'version="{re.escape(VERSION)}"',
            "workflow_catalog.py": rf'VERSION = "{re.escape(VERSION)}"',
            "README.md": rf'версии {re.escape(VERSION)}',
            "README.en.md": rf'Version\s+{re.escape(VERSION)}',
        }
        for relative, pattern in checks.items():
            with self.subTest(file=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertRegex(text, pattern)

    def test_public_text_has_no_private_paths_or_tokens(self):
        forbidden = (
            "/" + "Volumes/gra",
            "/" + "Users/darnhalm",
            "darnhalm" + "@gmail.com",
            "Authorization: Bearer " + "ey",
        )
        candidates = [
            *ROOT.glob("*.md"),
            *ROOT.glob("*.py"),
            *ROOT.glob("tests/*.py"),
            *ROOT.glob("docs/**/*.md"),
            *ROOT.glob("docs/**/*.json"),
        ]
        leaks = []
        for path in candidates:
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                if value in text:
                    leaks.append(f"{path.relative_to(ROOT)}: {value}")
        self.assertEqual([], leaks)


if __name__ == "__main__":
    unittest.main()
