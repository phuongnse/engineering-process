#!/usr/bin/env python3
"""Build, install, and exercise the exact wheel distribution."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from verification.normalize_sdist import normalize  # noqa: E402


def _utf8_lf(path: Path) -> str:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{path}: distribution text must be UTF-8 without BOM and use LF") from error
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        raise RuntimeError(f"{path}: distribution text must be UTF-8 without BOM and use LF")
    return text


def validate_distribution_text(root: Path) -> None:
    metadata = tomllib.loads(_utf8_lf(root / "pyproject.toml"))
    paths = ["release.json", "engineering_process/__init__.py"]
    for declared in metadata["tool"]["setuptools"]["data-files"].values():
        paths.extend(declared)
    for relative in dict.fromkeys(paths):
        _utf8_lf(root / relative)


def run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 300,
    environment: dict[str, str] | None = None,
) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"command failed with exit {result.returncode}: {command}")


def main() -> int:
    validate_distribution_text(PROJECT_ROOT)
    source_skill_root = PROJECT_ROOT / "process_assets" / "skills"
    source_skills = {
        path.relative_to(source_skill_root).as_posix(): path.read_bytes()
        for path in source_skill_root.rglob("*") if path.is_file()
    }
    source_standards = {
        path.name: path.read_bytes()
        for path in (PROJECT_ROOT / "process_assets" / "standards").glob("*.json")
    }
    with tempfile.TemporaryDirectory(prefix="engineering-process-dist-") as directory:
        root = Path(directory)
        artifacts = root / "dist"
        epoch = int(
            subprocess.check_output(
                ["git", "show", "-s", "--format=%ct", "HEAD"],
                cwd=PROJECT_ROOT,
                text=True,
            ).strip()
        )
        build_environment = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch)}
        run(
            [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(artifacts)],
            cwd=PROJECT_ROOT,
            environment=build_environment,
        )
        wheels = list(artifacts.glob("*.whl"))
        sdists = list(artifacts.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise RuntimeError("build must produce exactly one wheel and one sdist")
        normalize(sdists[0], epoch)
        expected_preset = (PROJECT_ROOT / "templates" / "renovate.json").read_bytes()
        with tarfile.open(sdists[0]) as archive:
            notes = [name for name in archive.getnames() if name.endswith("/RELEASE_NOTES.md")]
            if len(notes) != 1:
                raise RuntimeError("sdist must contain exactly one reviewed release notes file")
            with archive.extractfile(notes[0]) as content:
                if content.read() != (PROJECT_ROOT / "RELEASE_NOTES.md").read_bytes():
                    raise RuntimeError("sdist release notes differ from the reviewed source")
            presets = [name for name in archive.getnames() if name.endswith("/templates/renovate.json")]
            if len(presets) != 1:
                raise RuntimeError("sdist must contain exactly one generated Renovate preset")
            with archive.extractfile(presets[0]) as preset:
                if preset.read() != expected_preset:
                    raise RuntimeError("sdist Renovate preset differs from the canonical source")
            sdist_skills = {}
            for member in archive.getmembers():
                _prefix, separator, relative = member.name.partition("/process_assets/skills/")
                if separator and member.isfile():
                    with archive.extractfile(member) as stream:
                        sdist_skills[relative] = stream.read()
            if sdist_skills != source_skills:
                raise RuntimeError("sdist skill catalog differs from the canonical source")
            sdist_standards = {}
            for member in archive.getmembers():
                _prefix, separator, relative = member.name.partition("/process_assets/standards/")
                if separator and member.isfile():
                    with archive.extractfile(member) as stream:
                        sdist_standards[relative] = stream.read()
            if sdist_standards != source_standards:
                raise RuntimeError("sdist standards differ from the canonical source")

        environment = root / "venv"
        run([sys.executable, "-m", "venv", str(environment)], cwd=root)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        processctl = environment / ("Scripts/processctl.exe" if os.name == "nt" else "bin/processctl")
        run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheels[0])],
            cwd=root,
        )
        run([str(processctl), "--version"], cwd=root, timeout=30)
        installed_preset = environment / "share" / "engineering-process" / "templates" / "renovate.json"
        if installed_preset.read_bytes() != expected_preset:
            raise RuntimeError("installed wheel Renovate preset differs from the canonical source")
        installed_skill_root = environment / "share" / "engineering-process" / "skills"
        installed_skills = {
            path.relative_to(installed_skill_root).as_posix(): path.read_bytes()
            for path in installed_skill_root.rglob("*") if path.is_file()
        }
        if installed_skills != source_skills:
            raise RuntimeError("installed wheel skill catalog differs from the canonical source")
        installed_standard_root = environment / "share" / "engineering-process" / "process_assets" / "standards"
        if {path.name: path.read_bytes() for path in installed_standard_root.glob("*.json")} != source_standards:
            raise RuntimeError("installed wheel standards differ from the canonical source")
        for artifact in ("pull-request", "release-notes", "automation-name"):
            run([str(processctl), "artifact", "show", "--artifact", artifact, "--json"], cwd=root, timeout=30)
        run([str(python), "-I", "-c", """
from pathlib import Path
import subprocess
from engineering_process.artifact_standards import resolve_standard
from engineering_process.contracts import write_json_atomic
from engineering_process.distribution import distribution_root
from engineering_process.pr_description import body_issues, render_description
from engineering_process.automation_name import render_name
consumer = Path.cwd() / 'consumer'
consumer.mkdir()
subprocess.run(['git', 'init', '-q', str(consumer)], check=True, capture_output=True, timeout=30)
assets = distribution_root()
document = resolve_standard(None, assets, 'pull-request').document
document['id'] = 'installed.consumer-pr'
document['rules']['sections'][0]['heading'] = '## Installed consumer changes'
write_json_atomic(consumer / '.process' / 'pr.json', document)
write_json_atomic(consumer / '.process' / 'standards.json', {
    'schemaVersion': 1, 'artifacts': {'pull-request': {'path': '.process/pr.json'}}})
standard = resolve_standard(consumer, assets, 'pull-request')
body = render_description(standard).replace('- [ ]', '- [x]')
assert '## Installed consumer changes' in body
assert body_issues(body, 'draft', standard) == []
assert any('unresolved value' in issue for issue in body_issues(body, 'ready', standard))
name_standard = resolve_standard(consumer, assets, 'automation-name')
assert render_name(name_standard, {'schemaVersion': 1, 'components': {'owner': 'Acme', 'role': 'Dependency-Updates'}}) == 'acme-dependency-updates\\n'
print('Installed consumer standard and draft/ready checks: PASSED')
"""], cwd=root, timeout=30)
        consumer = root / "consumer"
        name_data = consumer / "name-data.json"
        name_data.write_text(json.dumps({"schemaVersion": 1, "components": {"owner": "Acme", "role": "Dependency-Updates"}}), encoding="utf-8")
        release_data = consumer / "release-data.json"
        release_data.write_text(json.dumps({
            "schemaVersion": 1, "title": "Fixture release", "introduction": "Reviewed fixture changes.",
            "changes": [{"type": "fix", "summary": "Preserve behavior.", "source": "fixture-change"}],
            "sections": {"upgrade": "No migration required."},
        }), encoding="utf-8")
        installed_root = environment / "share" / "engineering-process"
        for artifact, data in (("automation-name", name_data), ("release-notes", release_data)):
            body = consumer / f"{artifact}.txt"
            run([str(processctl), "artifact", "render", "--artifact", artifact, "--data-file", str(data), "--output", str(body), "--json"], cwd=consumer, timeout=30)
            run([str(processctl), "artifact", "validate", "--artifact", artifact, "--data-file", str(data), "--body-file", str(body), "--process-root", str(installed_root), "--json"], cwd=consumer, timeout=30)
        template = consumer / "template.md"
        run([str(processctl), "artifact", "template", "--artifact", "pull-request", "--output", str(template)], cwd=consumer, timeout=30)
        run([str(processctl), "artifact", "validate", "--artifact", "pull-request", "--state", "draft", "--body-file", str(template)], cwd=consumer, timeout=30)
        # The fixture lock binds this test wheel; public release locks are verified separately.
        version = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        requirements = consumer / "process.txt"
        requirements.write_text(f"engineering-process=={version} --hash=sha256:{hashlib.sha256(wheels[0].read_bytes()).hexdigest()}\n", encoding="utf-8")
        for operation in ("apply", "check", "apply", "check"):
            run([str(processctl), "adoption", operation, "--requirements-lock", str(requirements), "--json"], cwd=consumer, timeout=60)
        run([str(processctl), "skills", "validate", "--json"], cwd=root, timeout=30)
        run(
            [
                str(processctl),
                "publication",
                "validate-branch",
                "--branch",
                "fix/distribution-check",
                "--json",
            ],
            cwd=root,
            timeout=30,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"distribution verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
