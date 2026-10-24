from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SQLC_VERSION = "v1.31.1"
GENERATED_ROOT = ROOT / "src/generated/stack_check"
EXPECTED_FILES = {"models.py", "stack_check.py"}


def generated_hashes() -> dict[str, str]:
    files = sorted(path for path in GENERATED_ROOT.rglob("*.py") if path.is_file())
    names = {path.relative_to(GENERATED_ROOT).as_posix() for path in files}
    if names != EXPECTED_FILES:
        raise RuntimeError(f"Unexpected generated Python files: {sorted(names)}")
    return {
        path.relative_to(GENERATED_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }


def main() -> int:
    try:
        version = subprocess.run(
            ["sqlc", "version"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.strip()
    except FileNotFoundError:
        print("sqlc executable not found; install sqlc 1.31.1", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(exc.stderr, file=sys.stderr)
        return exc.returncode

    if version != EXPECTED_SQLC_VERSION:
        print(f"Expected sqlc {EXPECTED_SQLC_VERSION}, found {version}", file=sys.stderr)
        return 2

    try:
        before = generated_hashes()
    except (OSError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 2

    env = os.environ.copy()
    env.setdefault("XDG_CACHE_HOME", str(Path(tempfile.gettempdir()) / "hackathon-sqlc-cache"))
    try:
        subprocess.run(
            ["sqlc", "generate", "-f", "sqlc.yaml"], cwd=ROOT, env=env, check=True
        )
        after = generated_hashes()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"sqlc generation check failed: {exc}", file=sys.stderr)
        return 1

    if before != after:
        print("Generated files changed; regenerate and review the sqlc output:", file=sys.stderr)
        for name in sorted(before.keys() | after.keys()):
            if before.get(name) != after.get(name):
                print(f"  {name}: {before.get(name, 'missing')} -> {after.get(name, 'missing')}", file=sys.stderr)
        return 1

    print(f"sqlc {version}: generated file set and hashes are stable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
