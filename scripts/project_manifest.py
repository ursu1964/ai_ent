from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_ent.project_manifest import (
    compile_project_manifest,
    validate_project_manifest,
    write_compiled_project,
)


def validate(args: argparse.Namespace) -> int:
    result = validate_project_manifest(Path(args.manifest_root))
    report = result.as_dict()
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result.ok else 1


def compile_manifest(args: argparse.Namespace) -> int:
    result = write_compiled_project(Path(args.manifest_root), Path(args.output_dir))
    print(
        json.dumps(
            {
                "compiled_hash": result.lock.compiled_hash,
                "compiler_version": result.lock.compiler_version,
                "manifest_schema_version": result.lock.manifest_schema_version,
                "output_dir": args.output_dir,
                "source_file_count": len(result.lock.source_file_hashes),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def show_hash(args: argparse.Namespace) -> int:
    result = compile_project_manifest(Path(args.manifest_root))
    print(result.lock.compiled_hash)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI-Enterprise project manifest tools")
    subparsers = parser.add_subparsers(required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    validate_parser.add_argument("--report")
    validate_parser.set_defaults(func=validate)

    compile_parser = subparsers.add_parser("compile")
    compile_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    compile_parser.add_argument("--output-dir", default=".build/compiled")
    compile_parser.set_defaults(func=compile_manifest)

    hash_parser = subparsers.add_parser("show-hash")
    hash_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    hash_parser.set_defaults(func=show_hash)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
