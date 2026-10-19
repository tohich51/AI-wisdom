"""Build-only exact patch over the pinned OpenViking package."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
expected = {
    "server/routers/filesystem.py",
    "service/fs_service.py",
    "config/account_vector.py",
    "config/vector.py",
}
if (
    manifest["schema_version"] != 1
    or manifest["package"] != "openviking"
    or set(manifest["sources"]) != expected
):
    raise RuntimeError("unexpected OpenViking patch scope")
spec = importlib.util.find_spec("openviking")
if spec is None or spec.origin is None:
    raise RuntimeError("OpenViking package unavailable")
root = Path(spec.origin).parent
staged = []
for name in sorted(expected):
    entry = manifest["sources"][name]
    path = root / name
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise RuntimeError("OpenViking source version mismatch: " + name)
    changed = raw.decode("utf-8")
    for replacement in entry["replacements"]:
        old, new = replacement["old"], replacement["new"]
        if not old or old == new or changed.count(old) != 1:
            raise RuntimeError("OpenViking patch anchor mismatch: " + name)
        changed = changed.replace(old, new, 1)
    compile(changed, name, "exec")
    staged.append((path, changed.encode("utf-8")))
for path, content in staged:
    path.write_bytes(content)
print(f"Applied {len(expected)} pinned OpenViking source patches")
