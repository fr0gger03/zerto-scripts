"""Keep the portable requirements export pinned to the authoritative uv lock."""

from pathlib import Path
import re
import shlex
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RequirementsTests(unittest.TestCase):
    def test_export_matches_locked_versions_and_hashes(self):
        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        expected = {}
        for package in lock["package"]:
            if "registry" not in package["source"]:
                continue
            artifacts = package.get("wheels", []) + (
                [package["sdist"]] if "sdist" in package else []
            )
            expected[package["name"]] = (
                package["version"], {artifact["hash"] for artifact in artifacts}
            )
        actual = {}
        text = (ROOT / "requirements.txt").read_text().replace("\\\n", " ")
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            tokens = shlex.split(line)
            self.assertRegex(tokens[0], r"^[a-z0-9-]+==[^\s]+$")
            name, version = tokens[0].split("==", 1)
            self.assertNotIn(name, actual, "Duplicate requirement")
            self.assertTrue(tokens[1:], f"Missing hashes for {name}")
            for token in tokens[1:]:
                self.assertRegex(token, r"^--hash=sha256:[a-f0-9]{64}$")
            actual[name] = (version, {token.removeprefix("--hash=") for token in tokens[1:]})
        self.assertEqual(actual, expected, "Re-export requirements.txt from uv.lock")

    def test_export_has_no_index_or_download_locations(self):
        text = (ROOT / "requirements.txt").read_text()
        self.assertIsNone(re.search(r"https?://|--(?:extra-)?index-url|--default-index|--find-links", text))


if __name__ == "__main__":
    unittest.main()
