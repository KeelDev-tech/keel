"""Content-only profile freshness binding; never an assertion of truth."""
from pathlib import Path
import stat

from safe_io import contained_path, digest, read_json


def profile_dependency(workspace):
    """Bind absent or object-valued profile state without retaining personal data.

    JSON formatting/key order is immaterial. Missing and an empty object differ.
    Invalid, unreadable and symlink profiles must not become an absent binding.
    """
    workspace = Path(workspace).resolve()
    path = workspace / "data" / "applicant_profile.json"
    contained_path(workspace, path, must_exist=False)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return {"state": "absent"}
    if not stat.S_ISREG(mode):
        raise ValueError("applicant profile must be a regular file, not a symlink")
    profile = read_json(path)
    if not isinstance(profile, dict):
        raise ValueError("applicant profile must contain an object")
    return {"state": "present", "sha256": digest(profile)}
