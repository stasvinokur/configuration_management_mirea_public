"""Stage 1 CLI prototype for the dependency visualization tool."""

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse


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

    for key, value in parameters.items():
        if value is None or value == "":
            print(f"{key}: <not set>")
        else:
            print(f"{key}: {value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
