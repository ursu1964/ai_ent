from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_ent.project_manifest import (
    compile_project_manifest,
    resolve_capabilities,
    validate_project_manifest,
    write_capability_resolution,
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


def resolve_capabilities_command(args: argparse.Namespace) -> int:
    resolution = write_capability_resolution(Path(args.manifest_root), Path(args.output_dir))
    print(
        json.dumps(
            {
                "capability_resolution_hash": resolution.capability_resolution_hash,
                "resolver_version": resolution.resolver_version,
                "source_compiled_hash": resolution.source_compiled_hash,
                "capabilities_resolved": len(resolution.capabilities),
                "direct_dependency_edges": len(resolution.direct_edges),
                "transitive_dependency_edges": len(resolution.transitive_edges),
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def capability_gaps(args: argparse.Namespace) -> int:
    resolution = resolve_capabilities(Path(args.manifest_root))
    print(
        json.dumps(
            {
                "capability_resolution_hash": resolution.capability_resolution_hash,
                "required_capabilities": list(resolution.required_capabilities),
                "satisfied_capabilities": list(resolution.satisfied_capabilities),
                "partially_satisfied_capabilities": list(resolution.partially_satisfied_capabilities),
                "unsatisfied_capabilities": list(resolution.unsatisfied_capabilities),
                "deferred_capabilities": list(resolution.deferred_capabilities),
                "blocked_capabilities": list(resolution.blocked_capabilities),
            },
            indent=2,
            sort_keys=True,
        )
    )
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

    resolve_parser = subparsers.add_parser("resolve-capabilities")
    resolve_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    resolve_parser.add_argument("--output-dir", default=".build/compiled")
    resolve_parser.set_defaults(func=resolve_capabilities_command)

    gaps_parser = subparsers.add_parser("capability-gaps")
    gaps_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    gaps_parser.set_defaults(func=capability_gaps)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
