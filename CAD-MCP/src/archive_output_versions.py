from __future__ import annotations

import json
import re
import shutil
from pathlib import Path


VERSIONED_NAME_RE = re.compile(r"^(?P<prefix>.+?)_v(?P<version>\d+)(?P<suffix>\.[^.]+)$")


def is_archivable_case_directory(case_dir: Path) -> bool:
    """Return whether the directory already looks like a versioned case folder."""

    for item in case_dir.iterdir():
        if not item.is_dir():
            continue
        if item.name == "base" or item.name == "shared":
            return True
        if re.fullmatch(r"v\d+", item.name):
            return True
    return False


def normalize_versioned_name(file_name: str) -> str | None:
    """Return the non-versioned family name for a versioned artifact."""

    match = VERSIONED_NAME_RE.match(file_name)
    if not match:
        return None
    return f"{match.group('prefix')}{match.group('suffix')}"


def move_file(source: Path, destination_dir: Path) -> str:
    """Move one file into a destination directory and return its new relative path."""

    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.exists():
        raise FileExistsError(f"目标文件已存在，停止归档: {destination}")
    shutil.move(str(source), str(destination))
    return destination.name


def move_file_with_dedup(source: Path, destination_dir: Path) -> str | None:
    """Move one file into a destination directory, skipping exact duplicates."""

    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.exists():
        if source.stat().st_size == destination.stat().st_size:
            source.unlink()
            return None
        raise FileExistsError(f"目标文件已存在，停止归档: {destination}")
    shutil.move(str(source), str(destination))
    return destination.name


def collect_shared_files(case_dir: Path, version_family_names: set[str]) -> list[str]:
    """Move top-level shared files into the shared directory."""

    shared_dir = case_dir / "shared"
    moved_files = []

    top_level_files = [path for path in case_dir.iterdir() if path.is_file()]
    for file_path in sorted(top_level_files, key=lambda item: item.name):
        if file_path.name in version_family_names:
            continue
        try:
            moved_name = move_file_with_dedup(file_path, shared_dir)
            if moved_name:
                moved_files.append(moved_name)
        except (PermissionError, FileExistsError):
            continue

    return moved_files


def archive_case_directory(case_dir: Path) -> dict[str, object] | None:
    """Archive one case directory into version folders."""

    if not is_archivable_case_directory(case_dir):
        return None

    top_level_files = [path for path in case_dir.iterdir() if path.is_file()]
    versioned_files = []
    version_family_names = set()

    for file_path in top_level_files:
        match = VERSIONED_NAME_RE.match(file_path.name)
        if not match:
            continue
        versioned_files.append((file_path, match.group("version")))
        normalized = normalize_versioned_name(file_path.name)
        if normalized:
            version_family_names.add(normalized)

    summary: dict[str, object] = {
        "case": case_dir.name,
        "versions": {},
        "base": [],
        "shared": [],
    }

    for file_path, version in sorted(versioned_files, key=lambda item: (int(item[1]), item[0].name)):
        moved_name = move_file(file_path, case_dir / f"v{version}")
        summary["versions"].setdefault(f"v{version}", []).append(moved_name)

    remaining_top_level_files = [path for path in case_dir.iterdir() if path.is_file()]
    for file_path in sorted(remaining_top_level_files, key=lambda item: item.name):
        if file_path.name in version_family_names:
            moved_name = move_file(file_path, case_dir / "base")
            summary["base"].append(moved_name)

    summary["shared"] = collect_shared_files(case_dir, version_family_names)

    if not summary["versions"] and not summary["base"] and not summary["shared"]:
        return None

    return summary


def main() -> None:
    """Archive all versioned output examples under the CAD output root."""

    output_root = Path(__file__).resolve().parent.parent / "output"
    summaries = []

    for case_dir in sorted(path for path in output_root.iterdir() if path.is_dir()):
        summary = archive_case_directory(case_dir)

        if summary:
            summaries.append(summary)

    print(json.dumps({"archived_cases": summaries}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
