"""Real-filesystem checks for the fixed public runtime source profile.

Every filesystem mutation starts from a successful inventory. An unsupported
ancestor owner therefore fails the test setup; it cannot masquerade as a
successful attack rejection. No ownership checks are replaced or skipped.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import unittest

from tools.runtime_source_inventory import (
    RuntimeSourceError, current_runtime_provider, parse_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = "runtime-source-files.json"
PROFILE = "current-runtime-v1"
SCHEMA = "keel.runtime-source-files.v1"
REJECTED = (ValueError, OSError)
RESOURCES = (
    "VERSION",
    "engines/ats_matrix.json",
    "engines/fit-scoring-model.md",
    "fixtures/grounding_eval/dataset.json",
    "keel.config.json",
    "keel_live/static/review.html",
    "keel_live/static/review.js",
    "keel_loki/fixtures/browser_lab.html",
    "keel_workbench/static/app.js",
    "keel_workbench/static/style.css",
    "maintenance_workbench/demo_source/policies/fixture-policy.json",
    "security/policy/rules/base_rules.json",
    "tools/browser_worker.mjs",
    "worker-charter/CHARTER_TEMPLATE.md",
)
ARCHIVES = (
    "engines/build_dashboard_backup-20260918-k31k32-import-safe.py",
    "engines/build_dashboard_backup-20260918-k33-html-injection.py",
    "engines/live_cache_backup-20260918-k21k22.py",
    "engines/page_capture_backup-20260918-k23.py",
    "engines/queue_io_backup-20260918-k38-k50.py",
    "flow_board_pulse-backup-20260922-arm05-p1.py",
    "flow_board_pulse-backup-20260922-fix4.py",
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def manifest_bytes(names):
    document = {"schema": SCHEMA, "profile": PROFILE, "files": names}
    return (json.dumps(document, indent=2) + "\n").encode()


class ApprovedRuntimeMembershipTests(unittest.TestCase):
    """Independent coverage assertions; no external proposal file is needed."""

    def setUp(self):
        self.names = json.loads((ROOT / ALLOWLIST).read_bytes())["files"]

    def test_fixed_reviewed_members_and_resource_counts(self):
        self.assertIs(type(self.names), list)
        self.assertEqual(len(self.names), 628)
        self.assertEqual(self.names, sorted(set(self.names)))
        self.assertEqual(sum(name.endswith(".py") for name in self.names), 554)
        self.assertEqual(sum(not name.endswith(".py") for name in self.names), 74)

    def test_cli_provider_and_runtime_resources_are_required(self):
        required = {
            ALLOWLIST, "tools/runtime_source_inventory.py",
            "keel_muse/__main__.py", "keel_operational/__main__.py",
            "security/data/dlp.py", "security/ledger/hashchain.py",
            *RESOURCES,
        }
        self.assertTrue(required.issubset(self.names), required - set(self.names))

    def test_paths_are_canonical_relative_files(self):
        for name in self.names:
            with self.subTest(name=name):
                path = PurePosixPath(name)
                self.assertEqual(str(path), name)
                self.assertFalse(path.is_absolute())
                self.assertNotIn("..", path.parts)
                self.assertNotIn("\\", name)
                self.assertTrue(all(ord(character) >= 32 for character in name))
                self.assertTrue((ROOT / name).is_file())

    def test_historical_manifests_tests_and_archives_are_not_enrolled(self):
        self.assertTrue(set(ARCHIVES).isdisjoint(self.names))
        for name in self.names:
            with self.subTest(name=name):
                parts = PurePosixPath(name).parts
                self.assertNotIn("tests", parts)
                self.assertNotEqual(parts[0], "remediation")
                self.assertFalse(parts[-1].startswith("test_"))
        for historical in (
            "muse-base-manifest.json", "muse-release-files.json",
            "operational-base-manifest.json", "operational-release-files.json",
            "release-files.json", "MANIFEST.json",
        ):
            self.assertNotIn(historical, self.names)


class RuntimeManifestParsingTests(unittest.TestCase):
    """Policy validation can be tested without replacing filesystem guards."""

    def setUp(self):
        self.raw = (ROOT / ALLOWLIST).read_bytes()
        self.document = json.loads(self.raw)
        self.names = self.document["files"]

    def test_approved_policy_parses_as_an_immutable_ordered_sequence(self):
        self.assertEqual(parse_manifest(self.raw), tuple(self.names))

    def test_initial_whitespace_is_valid_but_still_part_of_raw_binding(self):
        reformatted = json.dumps(self.document, separators=(",", ":")).encode()
        self.assertNotEqual(reformatted, self.raw)
        self.assertEqual(parse_manifest(reformatted), tuple(self.names))

    def test_each_sampled_runtime_resource_is_mandatory(self):
        for resource in RESOURCES:
            with self.subTest(resource=resource):
                names = [name for name in self.names if name != resource]
                with self.assertRaises(RuntimeSourceError):
                    parse_manifest(manifest_bytes(names))

    def test_critical_members_cannot_be_removed(self):
        for name in (ALLOWLIST, "tools/runtime_source_inventory.py",
                     "keel_muse/__main__.py", "keel_operational/__main__.py"):
            with self.subTest(name=name):
                with self.assertRaises(RuntimeSourceError):
                    parse_manifest(manifest_bytes([n for n in self.names if n != name]))

    def test_same_count_sorted_replacement_cannot_redefine_policy(self):
        names = sorted((set(self.names) - {"keel_muse/common.py"}) |
                       {"keel_muse/replacement.py"})
        self.assertEqual(len(names), len(self.names))
        with self.assertRaises(RuntimeSourceError):
            parse_manifest(manifest_bytes(names))

    def test_resource_replacement_is_rejected_even_with_same_member_count(self):
        for resource in RESOURCES:
            with self.subTest(resource=resource):
                names = sorted((set(self.names) - {resource}) |
                               {"fixtures/grounding_eval/unreviewed.json"})
                self.assertEqual(len(names), len(self.names))
                with self.assertRaises(RuntimeSourceError):
                    parse_manifest(manifest_bytes(names))

    def test_additional_member_cannot_self_enroll(self):
        with self.assertRaises(RuntimeSourceError):
            parse_manifest(manifest_bytes(sorted(self.names + ["keel_muse/extra.py"])))

    def test_aliased_and_noncanonical_names_are_rejected(self):
        invalid = (
            "../outside.py", "/outside.py", "keel_muse//common.py",
            "keel_muse/./common.py", "keel_muse/../outside.py",
            "keel_muse\\common.py", "keel_muse/bad\nname.py", "",
        )
        for name in invalid:
            with self.subTest(name=name):
                with self.assertRaisesRegex(RuntimeSourceError, "runtime_source_member_path"):
                    parse_manifest(manifest_bytes([name, *self.names[1:]]))

    def test_duplicate_out_of_order_and_nonstring_members_are_rejected(self):
        values = [self.names + [self.names[0]], list(reversed(self.names)),
                  [*self.names[:-1], 3], [*self.names[:-1], None]]
        for index, names in enumerate(values):
            with self.subTest(index=index):
                with self.assertRaises(RuntimeSourceError):
                    parse_manifest(manifest_bytes(names))

    def test_wrong_shape_schema_profile_and_extra_keys_are_rejected(self):
        values = [self.names, {}, None, True,
                  {**self.document, "schema": "other"},
                  {**self.document, "profile": "other"},
                  {**self.document, "files": {}},
                  {**self.document, "root": "/outside"}]
        for index, value in enumerate(values):
            with self.subTest(index=index):
                with self.assertRaises(RuntimeSourceError):
                    parse_manifest(json.dumps(value).encode())

    def test_duplicate_json_keys_invalid_utf8_and_incomplete_json_are_rejected(self):
        duplicate = (b'{"schema":"other",' +
                     json.dumps(self.document).encode()[1:])
        for raw in (duplicate, b"\xff", b'{"schema":'):
            with self.subTest(raw_prefix=raw[:30]):
                with self.assertRaises(RuntimeSourceError):
                    parse_manifest(raw)

    def test_manifest_and_path_bounds_are_enforced(self):
        with self.assertRaisesRegex(RuntimeSourceError, "runtime_source_manifest_size"):
            parse_manifest(b" " * (256 * 1024 + 1))
        for name in ("x" * 1025, "/".join(["part"] * 17), "nonascii-\u00e9.py"):
            with self.subTest(name_prefix=name[:40]):
                with self.assertRaisesRegex(RuntimeSourceError, "runtime_source_member_path"):
                    parse_manifest(manifest_bytes([name, *self.names[1:]]))


class RuntimeSourceInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="keel-runtime-inventory-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "source"
        self.root.mkdir(mode=0o700)
        self.raw_list = (ROOT / ALLOWLIST).read_bytes()
        self.names = json.loads(self.raw_list)["files"]
        self.assertEqual(len(self.names), 628)
        for name in self.names:
            target = self.root / name
            parent = self.root
            for component in PurePosixPath(name).parts[:-1]:
                parent /= component
                parent.mkdir(mode=0o700, exist_ok=True)
            target.touch(mode=0o600, exist_ok=False)
            target.write_bytes((ROOT / name).read_bytes())
        self.provider = current_runtime_provider(self.root)
        self.initial = self.provider()
        self.assertEqual(self.initial["file_count"], 628)
        self.assertEqual(set(self.initial["files"]), set(self.names))

    def reject(self):
        with self.assertRaises(REJECTED):
            self.provider()

    def write(self, name, body=b"# unexpected source\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def test_source_only_snapshot_needs_no_git_or_historical_payload(self):
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / "muse-base-manifest.json").exists())
        self.assertFalse((self.root / "operational-base-manifest.json").exists())
        self.assertEqual(self.initial["schema"], SCHEMA)
        self.assertEqual(self.initial["profile"], PROFILE)
        self.assertEqual(self.initial["allowlist_sha256"], sha(self.raw_list))
        self.assertEqual(self.provider(), self.initial)

    def test_every_file_hash_matches_independently_read_bytes(self):
        expected = {
            name: sha((self.root / name).read_bytes()) for name in self.names
        }
        self.assertEqual(self.initial["files"], expected)

    def test_fingerprint_binds_profile_allowlist_and_observed_files(self):
        body = {
            "schema": SCHEMA,
            "profile": PROFILE,
            "allowlist_sha256": sha(self.raw_list),
            "files": self.initial["files"],
        }
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode()
        self.assertEqual(self.initial["sha256"], sha(encoded))

    def test_returned_data_cannot_mutate_the_frozen_provider(self):
        expected = json.loads(json.dumps(self.initial))
        self.initial["files"].clear()
        self.initial["profile"] = "caller-controlled"
        self.initial["sha256"] = "0" * 64
        self.assertEqual(self.provider(), expected)

    def test_changed_python_is_observed(self):
        name = "keel_muse/common.py"
        path = self.root / name
        path.write_bytes(path.read_bytes() + b"\n# changed source\n")
        observed = self.provider()
        self.assertNotEqual(observed["sha256"], self.initial["sha256"])
        self.assertEqual(observed["files"][name], sha(path.read_bytes()))

    def test_changed_javascript_is_observed(self):
        name = "tools/browser_worker.mjs"
        path = self.root / name
        path.write_bytes(path.read_bytes() + b"\n// changed worker\n")
        observed = self.provider()
        self.assertNotEqual(observed["sha256"], self.initial["sha256"])
        self.assertEqual(observed["files"][name], sha(path.read_bytes()))

    def test_changed_resource_is_observed(self):
        name = "fixtures/grounding_eval/dataset.json"
        path = self.root / name
        path.write_bytes(path.read_bytes() + b"\n")
        observed = self.provider()
        self.assertNotEqual(observed["sha256"], self.initial["sha256"])
        self.assertEqual(observed["files"][name], sha(path.read_bytes()))

    def test_changed_then_restored_source_remains_quarantined(self):
        name = "keel_muse/common.py"
        path = self.root / name
        original = path.read_bytes()
        path.write_bytes(original + b"\n# transient source change\n")
        changed = self.provider()
        self.assertNotEqual(changed["sha256"], self.initial["sha256"])
        self.reject()
        path.write_bytes(original)
        self.reject()
        self.assertEqual(current_runtime_provider(self.root)(), self.initial)

    def test_missing_then_restored_resource_remains_quarantined(self):
        path = self.root / "keel_workbench/static/app.js"
        original = path.read_bytes()
        path.unlink()
        self.reject()
        path.write_bytes(original)
        self.reject()
        self.assertEqual(current_runtime_provider(self.root)(), self.initial)

    def test_failed_then_restored_allowlist_remains_quarantined(self):
        self.write(ALLOWLIST, self.raw_list + b"\n")
        self.reject()
        self.write(ALLOWLIST, self.raw_list)
        self.reject()
        self.assertEqual(current_runtime_provider(self.root)(), self.initial)

    def test_allowlist_whitespace_change_is_rejected_after_freeze(self):
        self.write(ALLOWLIST, self.raw_list + b"\n")
        self.reject()

    def test_new_provider_binds_initial_allowlist_whitespace(self):
        changed = self.raw_list + b"\n"
        self.write(ALLOWLIST, changed)
        observed = current_runtime_provider(self.root)()
        self.assertEqual(observed["allowlist_sha256"], sha(changed))
        self.assertNotEqual(observed["sha256"], self.initial["sha256"])

    def test_same_count_sorted_replacement_cannot_redefine_the_policy(self):
        (self.root / "keel_muse/common.py").rename(self.root / "keel_muse/replacement.py")
        names = sorted((set(self.names) - {"keel_muse/common.py"}) |
                       {"keel_muse/replacement.py"})
        self.assertEqual(len(names), len(self.names))
        self.write(ALLOWLIST, manifest_bytes(names))
        with self.assertRaises(REJECTED):
            current_runtime_provider(self.root)()

    def test_missing_provider_is_rejected(self):
        (self.root / "tools/runtime_source_inventory.py").unlink()
        self.reject()

    def test_missing_allowlist_is_rejected(self):
        (self.root / ALLOWLIST).unlink()
        self.reject()

    def test_missing_resource_is_rejected(self):
        (self.root / "keel_workbench/static/app.js").unlink()
        self.reject()

    def test_oversized_member_is_rejected(self):
        self.write("keel_muse/common.py", b"x" * (8 * 1024 * 1024 + 1))
        self.reject()

    def test_total_source_byte_bound_is_enforced(self):
        body = b"x" * (8 * 1024 * 1024)
        for name in ("keel_muse/common.py", "keel_muse/review.py",
                     "keel_operational/common.py", "tools/browser_worker.mjs"):
            self.write(name, body)
        self.reject()

    def test_unlisted_python_and_misleading_test_name_are_rejected(self):
        self.write("keel_muse/test_injected.py")
        self.reject()

    def test_unlisted_initializer_is_rejected(self):
        self.write("tools/__init__.py")
        self.reject()

    def test_package_shadow_file_is_rejected(self):
        self.write("keel_muse.py")
        self.reject()

    def test_standard_library_shadow_file_is_rejected(self):
        self.write("json.py")
        self.reject()

    def test_unlisted_javascript_is_rejected(self):
        self.write("tools/extra.mjs", b"throw new Error('unexpected');\n")
        self.reject()

    def test_unlisted_shell_is_rejected(self):
        self.write("tools/extra.sh", b"#!/bin/sh\nexit 1\n")
        self.reject()

    def test_extensionless_executable_is_rejected(self):
        path = self.write("tools/extra-command", b"#!/bin/sh\nexit 1\n")
        path.chmod(0o700)
        self.reject()

    def test_bytecode_is_rejected(self):
        self.write("keel_muse/common.pyc", b"not executed\n")
        self.reject()

    def test_native_extension_shadow_is_rejected(self):
        self.write("keel_muse/common.cpython-312-x86_64-linux-gnu.so", b"not loaded\n")
        self.reject()

    def test_pycache_directory_is_rejected(self):
        (self.root / "keel_muse/__pycache__").mkdir()
        self.reject()

    def test_unexpected_directory_is_rejected_even_when_empty(self):
        (self.root / "keel_muse/unreviewed").mkdir()
        self.reject()

    def test_missing_directory_is_rejected(self):
        shutil.rmtree(self.root / "keel_muse")
        self.reject()

    def test_file_symlink_is_rejected(self):
        path = self.root / "keel_muse/common.py"
        outside = self.parent / "outside.py"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        self.reject()

    def test_directory_symlink_is_rejected(self):
        path = self.root / "keel_muse"
        outside = self.parent / "outside-package"
        path.rename(outside)
        path.symlink_to(outside, target_is_directory=True)
        self.reject()

    def test_failed_directory_open_remains_quarantined_after_restore(self):
        path = self.root / "keel_muse"
        original = self.parent / "original-package"
        path.rename(original)
        path.symlink_to(original, target_is_directory=True)
        self.reject()
        path.unlink()
        original.rename(path)
        self.reject()
        self.assertEqual(current_runtime_provider(self.root)(), self.initial)

    def test_file_hardlink_is_rejected(self):
        path = self.root / "keel_muse/common.py"
        os.link(path, self.parent / "same-inode.py")
        self.assertEqual(path.stat().st_nlink, 2)
        self.reject()

    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        path = self.root / "keel_muse/common.py"
        path.unlink()
        os.mkfifo(path, 0o600)
        self.reject()

    def test_file_writable_by_group_is_rejected(self):
        (self.root / "keel_muse/common.py").chmod(0o660)
        self.reject()

    def test_file_writable_by_others_is_rejected(self):
        (self.root / "keel_muse/common.py").chmod(0o646)
        self.reject()

    def test_file_special_permission_bits_are_rejected(self):
        path = self.root / "keel_muse/common.py"
        for mode in (0o4644, 0o2644, 0o1644):
            with self.subTest(mode=oct(mode)):
                self.provider = current_runtime_provider(self.root)
                self.assertEqual(self.provider(), self.initial)
                try:
                    path.chmod(mode)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                    self.reject()
                finally:
                    path.chmod(0o644)

    def test_intermediate_directory_permissions_are_rechecked(self):
        (self.root / "keel_muse").chmod(0o777)
        self.reject()

    def test_parent_permissions_are_rechecked(self):
        self.parent.chmod(0o777)
        try:
            self.reject()
        finally:
            self.parent.chmod(0o700)

    def test_replaced_root_identity_is_rejected(self):
        original = self.parent / "original-source"
        self.root.rename(original)
        shutil.copytree(original, self.root)
        self.reject()

    def test_replaced_intermediate_directory_is_reopened(self):
        original = self.parent / "original-package"
        package = self.root / "keel_muse"
        package.rename(original)
        shutil.copytree(original, package)
        name = "keel_muse/common.py"
        path = self.root / name
        path.write_bytes(path.read_bytes() + b"\n# replacement directory\n")
        observed = self.provider()
        self.assertNotEqual(observed["sha256"], self.initial["sha256"])
        self.assertEqual(observed["files"][name], sha(path.read_bytes()))


if __name__ == "__main__":
    unittest.main()
