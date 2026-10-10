"""Parse dependency manifests deterministically. A manifest that cannot be
parsed is recorded with its error, never dropped. XML manifests with a
DOCTYPE are refused (no entity expansion on untrusted input)."""

from __future__ import annotations

import fnmatch
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from typing import Callable

from backend.contracts import Dependency, Manifest

MAX_MANIFESTS = 40


def _deps(path, eco, items, direct=True):
    return [Dependency(manifest_path=path, ecosystem=eco, name=n, version_spec=v, is_direct=direct) for n, v in items]


def parse_package_json(path: str, text: str) -> list[Dependency]:
    data = json.loads(text)
    out = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        out += _deps(path, "npm", (data.get(section) or {}).items())
    return out


def parse_composer_json(path: str, text: str) -> list[Dependency]:
    data = json.loads(text)
    out = []
    for section in ("require", "require-dev"):
        out += _deps(path, "composer", ((n, v) for n, v in (data.get(section) or {}).items() if n != "php"))
    return out


_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$")


def parse_requirements(path: str, text: str) -> list[Dependency]:
    out = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-", "git+", "http:", "https:")):
            continue
        line = line.split(";", 1)[0].strip()
        m = _REQ_LINE.match(line)
        if m:
            out.append(Dependency(manifest_path=path, ecosystem="pypi", name=m.group(1),
                                  version_spec=m.group(3).strip() or None))
    return out


def _pep508(spec: str) -> tuple[str, str | None]:
    m = _REQ_LINE.match(spec.split(";", 1)[0].strip())
    return (m.group(1), m.group(3).strip() or None) if m else (spec.strip(), None)


def parse_pyproject(path: str, text: str) -> list[Dependency]:
    data = tomllib.loads(text)
    out = []
    project = data.get("project") or {}
    out += _deps(path, "pypi", (_pep508(s) for s in project.get("dependencies") or []))
    for group in (project.get("optional-dependencies") or {}).values():
        out += _deps(path, "pypi", (_pep508(s) for s in group))
    poetry = (data.get("tool") or {}).get("poetry") or {}
    tables = [poetry.get("dependencies") or {}, poetry.get("dev-dependencies") or {}]
    tables += [(g or {}).get("dependencies") or {} for g in (poetry.get("group") or {}).values()]
    for table in tables:
        for name, val in table.items():
            if name.lower() == "python":
                continue
            ver = val if isinstance(val, str) else (val.get("version") if isinstance(val, dict) else None)
            out.append(Dependency(manifest_path=path, ecosystem="pypi", name=name, version_spec=ver))
    return out


def parse_cargo_toml(path: str, text: str) -> list[Dependency]:
    data = tomllib.loads(text)
    out = []
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        for name, val in (data.get(section) or {}).items():
            ver = val if isinstance(val, str) else (val.get("version") if isinstance(val, dict) else None)
            out.append(Dependency(manifest_path=path, ecosystem="cargo", name=name, version_spec=ver))
    return out


_GO_REQ = re.compile(r"^\s*([^\s()]+)\s+(v[^\s]+)(\s*//\s*indirect)?")


def parse_go_mod(path: str, text: str) -> list[Dependency]:
    out, in_block = [], False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        body = line[len("require "):] if line.startswith("require ") and "(" not in line else (line if in_block else None)
        if body:
            m = _GO_REQ.match(body)
            if m:
                out.append(Dependency(manifest_path=path, ecosystem="go", name=m.group(1), version_spec=m.group(2),
                                      is_direct=not m.group(3)))
    return out


_GEM = re.compile(r"""^\s*gem\s+['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?""")


def parse_gemfile(path: str, text: str) -> list[Dependency]:
    return [Dependency(manifest_path=path, ecosystem="rubygems", name=m.group(1), version_spec=m.group(2))
            for m in (_GEM.match(line) for line in text.splitlines()) if m]


_GRADLE = re.compile(r"""\b(implementation|api|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|"""
                     r"""annotationProcessor|kapt|ksp|compile|testCompile)\s*\(?\s*['"]([^'":]+):([^'":]+)(?::([^'"]+))?['"]""")


def parse_gradle(path: str, text: str) -> list[Dependency]:
    return [Dependency(manifest_path=path, ecosystem="maven", name=f"{m.group(2)}:{m.group(3)}",
                       version_spec=m.group(4)) for m in _GRADLE.finditer(text)]


def _xml(text: str) -> ET.Element:
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.IGNORECASE):
        raise ValueError("XML with DOCTYPE/ENTITY declarations is refused")
    return ET.fromstring(text)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_pom(path: str, text: str) -> list[Dependency]:
    root = _xml(text)
    out = []
    for dep in root.iter():
        if _local(dep.tag) != "dependency":
            continue
        fields = {_local(c.tag): (c.text or "").strip() for c in dep}
        if fields.get("groupId") and fields.get("artifactId"):
            out.append(Dependency(manifest_path=path, ecosystem="maven",
                                  name=f"{fields['groupId']}:{fields['artifactId']}",
                                  version_spec=fields.get("version") or None))
    return out


def parse_csproj(path: str, text: str) -> list[Dependency]:
    root = _xml(text)
    return [Dependency(manifest_path=path, ecosystem="nuget", name=el.get("Include"),
                       version_spec=el.get("Version") or next((c.text for c in el if _local(c.tag) == "Version"), None))
            for el in root.iter() if _local(el.tag) == "PackageReference" and el.get("Include")]


PARSERS: list[tuple[str, str, Callable[[str, str], list[Dependency]]]] = [
    ("package.json", "npm", parse_package_json),
    ("composer.json", "composer", parse_composer_json),
    ("requirements*.txt", "pypi", parse_requirements),
    ("pyproject.toml", "pypi", parse_pyproject),
    ("pom.xml", "maven", parse_pom),
    ("build.gradle", "maven", parse_gradle),
    ("build.gradle.kts", "maven", parse_gradle),
    ("go.mod", "go", parse_go_mod),
    ("Cargo.toml", "cargo", parse_cargo_toml),
    ("Gemfile", "rubygems", parse_gemfile),
    ("*.csproj", "nuget", parse_csproj),
]


def manifest_parser(path: str):
    name = PurePosixPath(path).name
    for pattern, eco, fn in PARSERS:
        if fnmatch.fnmatchcase(name, pattern):
            return eco, fn
    return None


def parse_manifests(files: dict[str, str]) -> tuple[list[Manifest], list[Dependency]]:
    """``files`` maps manifest path -> text. Returns manifests (with any parse
    error) and every dependency found."""
    manifests, deps = [], []
    for path in sorted(files)[:MAX_MANIFESTS]:
        found = manifest_parser(path)
        if not found:
            continue
        eco, fn = found
        try:
            parsed = fn(path, files[path])
            manifests.append(Manifest(manifest_path=path, ecosystem=eco, dependency_count=len(parsed)))
            deps += parsed
        except Exception as exc:  # noqa: BLE001 - malformed untrusted manifest: record, do not drop
            manifests.append(Manifest(manifest_path=path, ecosystem=eco, parse_error=f"{type(exc).__name__}: {exc}"[:300]))
    return manifests, deps
