"""Перегенерация gRPC-заглушек Finam Trade API в ``src/finam/proto/`` (add-finam-probe, решение 1).

Источник — checkout официального репозитория ``github.com/FinamWeb/finam-trade-api``
на нужном релизе. Генератор — ``grpcio-tools`` из ``.venv`` (стоит как зависимость
``iprotopy`` ← ``t-tech-investments``), новых пакетов не нужно.

Корни импорта ``grpc.tradeapi``, ``grpc.gateway``, ``google.api`` и ``google.type``
переписываются на ``src.finam.proto.…``: ``grpc.tradeapi`` иначе спорит с пакетом
``grpc``, а ``google.api`` / ``google.type`` потребовали бы ``googleapis-common-protos``.
``google.protobuf`` берётся из рантайма protobuf.

Пример:
    python tools/finam_proto.py ~/src/finam-trade-api --version 2.24.0
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "finam" / "proto"
PACKAGE = "src.finam.proto."
REWRITE_ROOTS = ("grpc.tradeapi", "grpc.gateway", "google.api", "google.type")
EXTRA_PROTOS = (
    "google/api/annotations.proto",
    "google/api/http.proto",
    "google/type/date.proto",
    "google/type/decimal.proto",
    "google/type/interval.proto",
    "google/type/money.proto",
)
_ROOTS = "|".join(re.escape(root) for root in REWRITE_ROOTS)
_IMPORT = re.compile(rf"^(from\s+|import\s+)({_ROOTS})(?=[.\s])", re.MULTILINE)


def rewrite_imports(text: str) -> str:
    """Абсолютные импорты корней proto → пакет ``src.finam.proto``."""
    return _IMPORT.sub(lambda match: f"{match.group(1)}{PACKAGE}{match.group(2)}", text)


def _commit(checkout: Path) -> str:
    try:
        result = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return "неизвестен"
    return result.stdout.strip()


def generate(checkout: Path, version: str) -> int:
    proto_root = checkout / "proto"
    if not (proto_root / "grpc" / "tradeapi" / "v1").is_dir():
        print(f"Нет {proto_root}/grpc/tradeapi/v1 — это не checkout finam-trade-api")
        return 2
    files = sorted(str(path.relative_to(proto_root))
                   for base in ("grpc/tradeapi/v1", "grpc/gateway")
                   for path in (proto_root / base).rglob("*.proto"))
    files.extend(EXTRA_PROTOS)
    tools_version = metadata.version("grpcio-tools")
    header = (f"# Finam Trade API {version} (github.com/FinamWeb/finam-trade-api {_commit(checkout)}), "
              f"grpcio-tools {tools_version}.\n"
              "# Сгенерировано tools/finam_proto.py — не править вручную.\n"
              "# ruff: noqa\n")
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    from grpc_tools import protoc  # noqa: PLC0415 — нужен только здесь

    args = ["protoc", f"-I{proto_root}", f"--python_out={OUT}", f"--grpc_python_out={OUT}", *files]
    if protoc.main(args) != 0:
        print("protoc завершился с ошибкой")
        return 1
    for directory in [OUT, *[path for path in OUT.rglob("*") if path.is_dir()]]:
        (directory / "__init__.py").touch()
    for path in OUT.rglob("*_pb2*.py"):
        path.write_text(header + rewrite_imports(path.read_text(encoding="utf-8")), encoding="utf-8")
    (OUT / "__init__.py").write_text(
        '"""gRPC-заглушки Finam Trade API, версия — в ``VERSION``; перегенерация — tools/finam_proto.py."""\n',
        encoding="utf-8")
    (OUT / "VERSION").write_text(
        f"Finam Trade API {version}\nисточник github.com/FinamWeb/finam-trade-api {_commit(checkout)}\n"
        f"grpcio-tools {tools_version}\n", encoding="utf-8")
    print(f"Заглушки Finam Trade API {version}: {len(list(OUT.rglob('*_pb2*.py')))} файлов в {OUT}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("checkout", type=Path, help="checkout finam-trade-api")
    parser.add_argument("--version", required=True, help="релиз proto, например 2.24.0")
    args = parser.parse_args(argv)
    return generate(args.checkout.expanduser().resolve(), args.version)


if __name__ == "__main__":
    sys.exit(main())
