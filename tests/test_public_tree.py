"""The public-repository check reads staged blobs and never reports values."""
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_public_tree.py"
spec = importlib.util.spec_from_file_location("public_tree_guard", SCRIPT)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = guard
spec.loader.exec_module(guard)


class PublicTreeTests(unittest.TestCase):
    def test_public_sources_and_synthetic_examples_are_allowed(self):
        for path in ("README.md", "uhkone/console.py", "tests/test_console.py", ".githooks/pre-commit",
                     ".github/dependabot.yml", "docs/esimerkkiajo.html"):
            self.assertTrue(guard.allowed_path(path), path)
        content = 'child_name = "Testilapsi"\nemail = "person@example.com"\n'
        self.assertEqual(guard.check_blob("tests/test_example.py", content.encode()), [])
        self.assertEqual(guard.check_blob("uv.lock", b"010101" + b"-123N"), [])

    def test_runtime_artifacts_and_private_captures_are_rejected(self):
        for path in (".state/state.json", "testiraportit/run.json", "profiles/Default/Preferences",
                     "uhkone/cookies.json", "uhkone/shop.html", "docs/other-capture.html", "capture.png", ".env",
                     "tests/test_secret.pem", "../README.md"):
            self.assertFalse(guard.allowed_path(path), path)

    def test_sensitive_content_is_reported_without_its_value(self):
        token = "ghp_" + "A" * 36
        findings = guard.check_blob("README.md", f"token: {token}\n".encode())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].category, "GitHub token")
        self.assertNotIn(token, str(findings))

    def test_non_example_email_is_rejected(self):
        address = "person" + "@" + "non-example-domain.fi"
        findings = guard.check_blob("README.md", address.encode())
        self.assertEqual([f.category for f in findings], ["non-example email address"])

    def test_credential_leak_test_sentinel_is_allowed_only_in_test_source(self):
        content = b'access_token = "SECRET TOKEN"\n'
        self.assertEqual(guard.check_blob("tests/test_example.py", content), [])
        self.assertEqual(len(guard.check_blob("uhkone/example.py", content)), 1)

    def test_payment_card_number_is_rejected_without_echoing_digits(self):
        card = "4242" * 4
        findings = guard.check_blob("README.md", card.encode())
        self.assertEqual([f.category for f in findings], ["possible payment card number"])
        self.assertNotIn(card, str(findings))

    def test_seed_accepts_only_explicit_public_fields(self):
        document = {
            "version": 1, "source_url": "https://kauppa.urheiluhallit.fi/webshop/reservations2/apieventgroups?service_id=9",
            "fetched_at": "2026-01-01T00:00:00+00:00", "data_sha256": "0" * 64,
            "courses": [{"reservation_event_group_id": 1, "name": "Testikurssi", "fare_products": []}],
        }
        self.assertEqual(guard.check_blob("uhkone/data/courses.json", json.dumps(document).encode()), [])
        document["courses"][0]["instructors"] = ["Fictional Instructor"]
        findings = guard.check_blob("uhkone/data/courses.json", json.dumps(document).encode())
        self.assertEqual(len(findings), 1)
        self.assertIn("allowlist", findings[0].category)

    def test_binary_is_rejected_even_with_source_extension(self):
        self.assertEqual(guard.check_blob("uhkone/example.py", b"\x00")[0].category,
                         "binary or unexpectedly large file")

    def test_staged_snapshot_is_checked_instead_of_changed_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            document = root / "README.md"
            token = "ghp_" + "B" * 36
            document.write_text(token + "\n")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            document.write_text("Safe working-tree text\n")
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                code = guard.main(["--root", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("README.md:1: GitHub token", error.getvalue())
            self.assertNotIn(token, error.getvalue())


if __name__ == "__main__":
    unittest.main()
