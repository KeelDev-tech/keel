"""Synthetic additive installation boundaries; no live repository is used."""
import hashlib
import json
import os

import pytest

from tools import install_live as installer


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def setup_patch(tmp_path):
    target, patch = tmp_path / "target", tmp_path / "patch"
    target.mkdir()
    patch.mkdir()
    (target / "VERSION").write_text("0.7.0-review.1\n")
    (target / "keel_agent").mkdir()
    (target / "keel_agent/base.py").write_text("BASE SOURCE\n")
    files = {
        "keel_sources/added.py": "SOURCE DEPENDENCY\n",
        "keel_workbench/added.py": "WORKBENCH DEPENDENCY\n",
        "keel_live/added.py": "LIVE CONNECTOR\n",
        "docs/LIVE_HANDOFF.md": "SYNTHETIC HANDOFF\n",
    }
    for name, content in files.items():
        path = patch / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(content)
    manifest = {"schema": "keel.additive_patch.v1", "version": "0.9.0-review.1",
        "base_files": {name: sha(target / name) for name in ("VERSION", "keel_agent/base.py")},
        "files": {name: sha(patch / name) for name in files}}
    (patch / installer.MANIFEST).write_text(json.dumps(manifest))
    return patch, target


def file_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def edit_manifest(patch, change):
    path = patch / installer.MANIFEST
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest))


def test_verification_default_does_not_create_directories_or_change_files(tmp_path):
    patch, target = setup_patch(tmp_path)
    before = file_bytes(target)
    result = installer.install(patch, target)
    assert result["status"] == "VERIFIED_FOR_ADDITION"
    assert result["new_files"] == 5
    assert result["base_files_verified"] == 2
    assert result["execution_authorized"] is False
    assert result["production_deployed"] is False
    assert file_bytes(target) == before
    assert not (target / "keel_live").exists()
    assert not (target / "keel_sources").exists()
    assert not (target / "keel_workbench").exists()


def test_adds_missing_dependencies_and_repeat_is_identical(tmp_path):
    patch, target = setup_patch(tmp_path)
    before = file_bytes(target)
    first = installer.install(patch, target, write=True)
    installed = file_bytes(target)
    assert first["new_files"] == 5
    assert first["existing_files_modified"] == 0
    for name, content in before.items():
        assert installed[name] == content
    for name, content in file_bytes(patch).items():
        assert installed[name] == content
    repeat = installer.install(patch, target, write=True)
    assert repeat["new_files"] == 0
    assert repeat["identical_files"] == 5
    assert file_bytes(target) == installed


@pytest.mark.parametrize("name", ["VERSION", "keel_agent/base.py"])
def test_changed_base_dependency_refuses_every_addition(tmp_path, name):
    patch, target = setup_patch(tmp_path)
    (target / name).write_text("HOST CUSTOMIZATION MUST SURVIVE\n")
    before = file_bytes(target)
    with pytest.raises(ValueError, match="base dependency differs"):
        installer.install(patch, target, write=True)
    assert file_bytes(target) == before
    assert not (target / "keel_live").exists()


def test_missing_base_dependency_refuses_every_addition(tmp_path):
    patch, target = setup_patch(tmp_path)
    (target / "VERSION").unlink()
    before = file_bytes(target)
    with pytest.raises(OSError):
        installer.install(patch, target, write=True)
    assert file_bytes(target) == before


def test_conflicting_existing_dependency_is_preserved_without_partial_additions(tmp_path):
    patch, target = setup_patch(tmp_path)
    (target / "keel_workbench").mkdir()
    (target / "keel_workbench/added.py").write_text("EXISTING HOST WORK\n")
    before = file_bytes(target)
    with pytest.raises(ValueError, match="existing file conflict"):
        installer.install(patch, target, write=True)
    assert file_bytes(target) == before
    assert not (target / "keel_sources").exists()


def test_tampered_payload_is_rejected_before_creating_any_path(tmp_path):
    patch, target = setup_patch(tmp_path)
    (patch / "keel_live/added.py").write_text("TAMPERED\n")
    before = file_bytes(target)
    with pytest.raises(ValueError, match="patch digest mismatch"):
        installer.install(patch, target, write=True)
    assert file_bytes(target) == before
    assert not (target / "keel_sources").exists()


@pytest.mark.parametrize("name", ["../outside.py", "/outside.py", "tools/../outside.py", "tools\\outside.py", "tools//bad.py", "tools/bad\n.py"])
def test_manifest_path_traversal_and_noncanonical_names_are_rejected(tmp_path, name):
    patch, target = setup_patch(tmp_path)
    edit_manifest(patch, lambda manifest: manifest["files"].update({name: "0" * 64}))
    before = file_bytes(target)
    with pytest.raises(ValueError):
        installer.install(patch, target, write=True)
    assert file_bytes(target) == before
    assert not (target / "keel_sources").exists()


@pytest.mark.parametrize("name", ["VERSION", "keel_agent/base.py", "unauthorized/added.py"])
def test_patch_cannot_replace_base_or_write_outside_additive_directories(tmp_path, name):
    patch, target = setup_patch(tmp_path)
    edit_manifest(patch, lambda manifest: manifest["files"].update({name: "0" * 64}))
    before = file_bytes(target)
    with pytest.raises(ValueError, match="replace base or unsupported path"):
        installer.install(patch, target, write=True)
    assert file_bytes(target) == before


@pytest.mark.parametrize("location", ["source", "target_parent", "base", "root"])
def test_symlink_boundaries_are_rejected(tmp_path, location):
    patch, target = setup_patch(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    if location == "source":
        source = patch / "keel_sources/added.py"
        moved = outside / "source.py"
        source.rename(moved)
        source.symlink_to(moved)
    elif location == "target_parent":
        (target / "keel_sources").symlink_to(outside, target_is_directory=True)
    elif location == "base":
        source = target / "VERSION"
        moved = outside / "VERSION"
        source.rename(moved)
        source.symlink_to(moved)
    else:
        alias = tmp_path / "target-alias"
        alias.symlink_to(target, target_is_directory=True)
        target = alias
    before = file_bytes(outside)
    with pytest.raises(ValueError, match="symlink|canonical existing directories"):
        installer.install(patch, target, write=True)
    assert file_bytes(outside) == before


def test_hardlinked_payload_is_not_a_safe_regular_source(tmp_path):
    patch, target = setup_patch(tmp_path)
    os.link(patch / "keel_live/added.py", tmp_path / "linked.py")
    with pytest.raises(ValueError, match="bounded regular source"):
        installer.install(patch, target, write=True)
    assert not (target / "keel_sources").exists()


def test_write_failure_rolls_back_own_files_and_preserves_identical_existing_files(tmp_path, monkeypatch):
    patch, target = setup_patch(tmp_path)
    (target / "keel_sources").mkdir()
    (target / "keel_sources/added.py").write_bytes((patch / "keel_sources/added.py").read_bytes())
    before = file_bytes(target)
    original = installer._publish
    calls = []
    def publish(temporary, destination):
        calls.append(destination)
        if len(calls) == 2:
            raise OSError("synthetic publication failure")
        return original(temporary, destination)
    monkeypatch.setattr(installer, "_publish", publish)
    with pytest.raises(OSError, match="synthetic publication failure"):
        installer.install(patch, target, write=True)
    assert len(calls) == 2
    assert file_bytes(target) == before
    assert not list(target.rglob(".keel-addition-*"))


def test_rollback_preserves_concurrently_replaced_file(tmp_path, monkeypatch):
    patch, target = setup_patch(tmp_path)
    original = installer._publish
    first = []
    def publish(temporary, destination):
        if not first:
            first.append(destination)
            return original(temporary, destination)
        replacement = first[0].with_name("replacement.py")
        replacement.write_text("CONCURRENT HOST FILE\n")
        os.replace(replacement, first[0])
        raise OSError("synthetic later publication failure")
    monkeypatch.setattr(installer, "_publish", publish)
    with pytest.raises(OSError, match="synthetic later publication failure"):
        installer.install(patch, target, write=True)
    assert first[0].read_text() == "CONCURRENT HOST FILE\n"
    assert (target / "VERSION").read_text() == "0.7.0-review.1\n"
    assert not (target / "keel_workbench/added.py").exists()


def test_atomic_publication_never_overwrites_a_concurrent_new_destination(tmp_path, monkeypatch):
    patch, target = setup_patch(tmp_path)
    original = installer._publish
    attempted = []
    def publish(temporary, destination):
        attempted.append(destination)
        destination.write_text("CONCURRENT HOST FILE\n")
        return original(temporary, destination)
    monkeypatch.setattr(installer, "_publish", publish)
    with pytest.raises(OSError):
        installer.install(patch, target, write=True)
    assert attempted[0].read_text() == "CONCURRENT HOST FILE\n"
    assert not list(target.rglob(".keel-addition-*"))


def test_source_changed_after_inspection_rolls_back_partial_install(tmp_path, monkeypatch):
    patch, target = setup_patch(tmp_path)
    before = file_bytes(target)
    original = installer.inspect
    def inspect_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        (patch / "keel_workbench/added.py").write_text("CHANGED AFTER INSPECTION\n")
        return result
    monkeypatch.setattr(installer, "inspect", inspect_then_change)
    with pytest.raises(ValueError, match="patch changed after verification"):
        installer.install(patch, target, write=True)
    assert file_bytes(target) == before
    assert not list(target.rglob(".keel-addition-*"))


def test_manifest_changed_after_inspection_is_not_silently_installed(tmp_path, monkeypatch):
    patch, target = setup_patch(tmp_path)
    before = file_bytes(target)
    original = installer.inspect
    changed = False
    def inspect_then_change_manifest(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            edit_manifest(patch, lambda manifest: manifest.update(version="UNVERIFIED-REPLACEMENT"))
            changed = True
        return result
    monkeypatch.setattr(installer, "inspect", inspect_then_change_manifest)
    with pytest.raises(ValueError, match="patch changed after verification|manifest changed"):
        installer.install(patch, target, write=True)
    assert file_bytes(target) == before


def test_cli_verifies_by_default_and_explicit_install_is_idempotent(tmp_path, capsys):
    patch, target = setup_patch(tmp_path)
    args = ["--patch-root", str(patch), "--target", str(target)]
    assert installer.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VERIFIED_FOR_ADDITION"
    assert not (target / "keel_live").exists()
    assert installer.main([*args, "--install"]) == 0
    assert json.loads(capsys.readouterr().out)["new_files"] == 5
    assert installer.main([*args, "--install"]) == 0
    assert json.loads(capsys.readouterr().out)["new_files"] == 0
