#!/usr/bin/env python3
"""Stage 3 CLI prototype for the dependency visualization tool."""

import argparse
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse
from urllib.request import urlopen

import tomllib


def package_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise argparse.ArgumentTypeError("package name must not be empty")
    for chunk in name.split("-"):
        if not chunk.replace("_", "").isalnum():
            raise argparse.ArgumentTypeError(
                "package name may only contain letters, digits, '_' or '-'"
            )
    return name


def version_string(value: str) -> str:
    text = value.strip()
    parts = text.split(".")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(
            "version must contain at least major and minor parts, e.g. 1.0"
        )
    if not all(part.isdigit() for part in parts):
        raise argparse.ArgumentTypeError("version must contain only digits and dots")
    return text


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("max depth must be an integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("max depth must be positive")
    return number


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Minimal prototype for the dependency visualization tool",
    )
    parser.add_argument(
        "--package",
        required=True,
        type=package_name,
        help="Имя анализируемого пакета",
    )
    parser.add_argument(
        "--repository",
        required=True,
        help="URL репозитория или путь к файлу тестового репозитория",
    )
    parser.add_argument(
        "--test-mode",
        choices=("real", "file"),
        default="real",
        help="Режим работы с тестовым репозиторием",
    )
    parser.add_argument(
        "--version",
        required=True,
        type=version_string,
        help="Версия анализируемого пакета",
    )
    parser.add_argument(
        "--ascii-mode",
        choices=("disabled", "compact", "full"),
        default="disabled",
        help="Режим вывода зависимостей в формате ASCII-дерева",
    )
    parser.add_argument(
        "--max-depth",
        type=positive_int,
        default=1,
        help="Максимальная глубина анализа зависимостей",
    )
    parser.add_argument(
        "--filter",
        dest="filter_substring",
        default=None,
        help="Подстрока для фильтрации пакетов",
    )

    args = parser.parse_args(argv)

    if args.test_mode == "real":
        if not is_url(args.repository):
            parser.error("--repository must be a valid URL when --test-mode is 'real'")
    else:
        path = Path(args.repository)
        if not path.exists():
            parser.error("test repository file not found")
        if not path.is_file():
            parser.error("test repository path must point to a file")

    if args.filter_substring is not None and args.filter_substring.strip() == "":
        parser.error("--filter must not be empty when provided")

    return args


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


@dataclass
class DirectDependency:
    name: str
    requirement: str


@dataclass
class GraphEntry:
    name: str
    version: str | None
    depth: int


def load_manifest(path: str, mode: str) -> dict:
    try:
        if mode == "real":
            return tomllib.loads(read_url(path))
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"cannot read manifest file: {path}") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to read manifest: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"invalid Cargo.toml format: {exc}") from exc


def read_url(url: str) -> str:
    try:
        with urlopen(url) as response:  # type: ignore[arg-type]
            data = response.read()
    except OSError as exc:  # network failure or unreachable host
        raise RuntimeError(f"failed to fetch manifest from URL: {exc}") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("manifest must be utf-8 encoded") from exc


def extract_direct_dependencies(manifest: dict) -> list[DirectDependency]:
    deps_section = manifest.get("dependencies", {})
    if not isinstance(deps_section, dict):
        return []

    dependencies: list[DirectDependency] = []
    for name, spec in deps_section.items():
        requirement = dependency_requirement(spec)
        dependencies.append(DirectDependency(name=name, requirement=requirement))
    return dependencies


def dependency_requirement(spec: object) -> str:
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        value = spec.get("version")
        if isinstance(value, str) and value.strip():
            return value
        return "<unspecified>"
    return "<unknown>"


def validate_manifest(manifest: dict, *, expected_name: str, expected_version: str) -> None:
    package = manifest.get("package", {})
    if not isinstance(package, dict):
        raise RuntimeError("manifest does not contain [package] section")

    name = package.get("name")
    if name != expected_name:
        raise RuntimeError(
            f"manifest package name '{name}' does not match requested '{expected_name}'"
        )

    version = package.get("version")
    if version != expected_version:
        raise RuntimeError(
            f"manifest version '{version}' does not match requested '{expected_version}'"
        )


def print_configuration(parameters: dict[str, object]) -> None:
    for key, value in parameters.items():
        if value is None or value == "":
            print(f"{key}: <not set>")
        else:
            print(f"{key}: {value}")


def print_dependencies(dependencies: Iterable[DirectDependency]) -> None:
    deps = list(dependencies)
    if not deps:
        print("Direct dependencies: <none>")
        return
    print("Direct dependencies:")
    for dep in deps:
        print(f"- {dep.name}: {dep.requirement}")


def read_test_repository(path: Path) -> tuple[str, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"cannot read manifest file: {path}") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to read manifest: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise RuntimeError("manifest must be utf-8 encoded") from exc

    if "[package]" in content:
        return "manifest", content
    return "graph", content


def parse_graph_definition(text: str) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {}
    referenced: set[str] = set()
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise RuntimeError(f"line {idx}: expected 'PACKAGE:DEP1,DEP2' format")
        name, remainder = line.split(":", 1)
        package = name.strip()
        if not package:
            raise RuntimeError(f"line {idx}: package name is empty")
        deps = []
        for chunk in remainder.split(","):
            dep = chunk.strip()
            if dep:
                deps.append(dep)
                referenced.add(dep)
        graph[package] = deps

    for dep in referenced:
        graph.setdefault(dep, [])

    if not graph:
        raise RuntimeError("graph definition is empty")

    return graph


def create_manifest_provider(
    root_name: str, root_version: str, manifest: dict
) -> tuple[list[DirectDependency], Callable[[str, str | None], list[DirectDependency]]]:
    direct = extract_direct_dependencies(manifest)
    cache: dict[tuple[str, str | None], list[DirectDependency]] = {
        (root_name.lower(), root_version): direct
    }

    def provider(name: str, version: str | None) -> list[DirectDependency]:
        return cache.get((name.lower(), version), [])

    return direct, provider


def create_graph_provider(graph: dict[str, list[str]]) -> Callable[[str, str | None], list[DirectDependency]]:
    def provider(name: str, version: str | None) -> list[DirectDependency]:
        return [DirectDependency(dep, "<graph>") for dep in graph.get(name, [])]

    return provider


def should_filter(name: str, substring: str | None) -> bool:
    if not substring:
        return False
    return substring.lower() in name.lower()


def build_dependency_graph(
    root_name: str,
    root_version: str | None,
    provider: Callable[[str, str | None], list[DirectDependency]],
    *,
    max_depth: int,
    filter_substring: str | None,
) -> tuple[list[GraphEntry], dict[tuple[str, str | None], list[str]], list[str]]:
    queue: deque[tuple[str, str | None, int]] = deque([(root_name, root_version, 0)])
    visited: set[tuple[str, str | None]] = set()
    entries: list[GraphEntry] = []
    edges: dict[tuple[str, str | None], list[str]] = {}
    skipped: list[str] = []

    while queue:
        name, version, depth = queue.popleft()
        key = (name.lower(), version)
        if key in visited:
            continue
        visited.add(key)
        entries.append(GraphEntry(name, version, depth))

        dependencies = provider(name, version)
        children: list[str] = []
        if depth < max_depth:
            for dep in dependencies:
                if should_filter(dep.name, filter_substring):
                    skipped.append(dep.name)
                    continue
                children.append(dep.name)
                version_value = dep.requirement if dep.requirement else None
                if version_value and version_value.startswith("<"):
                    version_value = None
                queue.append((dep.name, version_value, depth + 1))
        edges[(name.lower(), version)] = children

    return entries, edges, skipped


def format_label(name: str, version: str | None) -> str:
    if not version:
        return name
    if version.startswith("<"):
        return f"{name} ({version})"
    return f"{name}@{version}"


def print_graph(
    entries: list[GraphEntry],
    edges: dict[tuple[str, str | None], list[str]],
    *,
    max_depth: int,
    skipped: Iterable[str],
) -> None:
    print(f"Resolved dependency graph (depth limit {max_depth}):")
    for entry in entries:
        label = format_label(entry.name, entry.version)
        children = edges.get((entry.name.lower(), entry.version), [])
        if children:
            print(f"- depth {entry.depth}: {label} -> {', '.join(children)}")
        else:
            print(f"- depth {entry.depth}: {label} -> <none>")

    skipped_set = {name for name in skipped}
    if skipped_set:
        print("Skipped by filter:")
        for name in sorted(skipped_set):
            print(f"- {name}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    parameters = {
        "package": args.package,
        "repository": args.repository,
        "test_mode": args.test_mode,
        "version": args.version,
        "ascii_mode": args.ascii_mode,
        "max_depth": args.max_depth,
        "filter_substring": args.filter_substring,
    }

    print_configuration(parameters)

    try:
        if args.test_mode == "real":
            manifest = load_manifest(args.repository, args.test_mode)
            validate_manifest(
                manifest,
                expected_name=args.package,
                expected_version=args.version,
            )
            dependencies, provider = create_manifest_provider(
                args.package, args.version, manifest
            )
            root_version: str | None = args.version
        else:
            kind, content = read_test_repository(Path(args.repository))
            if kind == "manifest":
                manifest = tomllib.loads(content)
                validate_manifest(
                    manifest,
                    expected_name=args.package,
                    expected_version=args.version,
                )
                dependencies, provider = create_manifest_provider(
                    args.package, args.version, manifest
                )
                root_version = args.version
            else:
                graph = parse_graph_definition(content)
                if args.package not in graph:
                    raise RuntimeError(
                        f"root package '{args.package}' is not defined in test graph"
                    )
                dependencies = [
                    DirectDependency(dep, "<graph>") for dep in graph.get(args.package, [])
                ]
                provider = create_graph_provider(graph)
                root_version = None

        entries, edges, skipped = build_dependency_graph(
            args.package,
            root_version,
            provider,
            max_depth=args.max_depth,
            filter_substring=args.filter_substring,
        )
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print_dependencies(dependencies)
    print_graph(entries, edges, max_depth=args.max_depth, skipped=skipped)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
