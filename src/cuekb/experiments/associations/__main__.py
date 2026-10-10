from __future__ import annotations

import argparse
import hashlib
import json
import sys
import subprocess
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

from .corpus import load_dataset, read_jsonl, read_model, write_json
from .discovery import build
from .evaluation import association_scores, report
from .models import Artifact, PathLabel, RelationLabel, Result, digest
from .query import VARIANTS, run_query


def runtime_provenance() -> dict:
    package = Path(__file__).parent
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(package.glob("*.py"))}
    root = package.parents[3]
    commit = None
    dirty = None
    if (root / ".git").exists():
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip())
    lock = root / "requirements.lock"
    return {"implementation_sha256": digest(files), "source_files": files,
            "git_commit": commit, "git_dirty": dirty, "python": sys.version.split()[0],
            "pydantic": version("pydantic"), "cuekb": version("cuekb"),
            "dependency_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None}


def parser():
    command = argparse.ArgumentParser(description="CueKB offline association experiments; no production writes or model calls")
    commands = command.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a frozen manifest/corpus/query set")
    validate.add_argument("dataset", type=Path)
    builder = commands.add_parser("build", help="build bounded navigation links and anchored rule claims")
    builder.add_argument("dataset", type=Path)
    builder.add_argument("output", type=Path)
    builder.add_argument("--overwrite", action="store_true")
    runner = commands.add_parser("run", help="compare H, H+A, H+B, H+C and H-wide on the same frozen input")
    runner.add_argument("dataset", type=Path)
    runner.add_argument("artifact", type=Path)
    runner.add_argument("output", type=Path)
    runner.add_argument("--split", choices=["development", "calibration", "test"], default="test")
    runner.add_argument("--mode", choices=["off", "auto", "on"], default="on")
    runner.add_argument("--dynamic", action="store_true")
    runner.add_argument("--k", type=int, choices=range(1, 21), default=8)
    runner.add_argument("--overwrite", action="store_true")
    return command


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        snapshot, cases, search = load_dataset(args.dataset)
        if args.command == "validate":
            print(json.dumps({"status": "valid", "dataset_id": snapshot.manifest.dataset_id,
                "provenance": snapshot.manifest.provenance, "chunks": len(snapshot.rows),
                "queries": len(cases), "snapshot_sha256": snapshot.sha256}, ensure_ascii=False))
        elif args.command == "build":
            artifact = build(snapshot, search)
            write_json(args.output, artifact.model_dump(mode="json"), overwrite=args.overwrite)
            print(json.dumps({"status": "built", "artifact_id": str(artifact.id), "counters": artifact.counters}))
        else:
            artifact = read_model(args.artifact, Artifact)
            if artifact.manifest_sha256 != digest(snapshot.manifest) or artifact.corpus_sha256 != snapshot.corpus_sha256:
                raise ValueError("artifact_snapshot_mismatch")
            selected = [case for case in cases if case.split == args.split]
            if not selected:
                raise ValueError("selected_split_is_empty")
            results = []
            for case in selected:
                if case.request.mode not in {"hybrid", "auto", "exact"}:
                    raise ValueError("offline_runner_does_not_emulate_manual_related")
                for variant in VARIANTS:
                    started = perf_counter()
                    try:
                        results.append(run_query(snapshot, search, artifact, case, variant,
                                                  mode=args.mode, dynamic=args.dynamic))
                    except PermissionError:
                        results.append(Result(query_id=case.id, variant=variant, hits=[], counters={},
                            stop_reasons=["scope_denied"], error_code="experiment_scope_denied",
                            timings_ms={"replay_cpu_total": (perf_counter() - started) * 1000},
                            snapshot_sha256=snapshot.sha256, artifact_id=None))
            output = report(selected, results, k=args.k)
            relation_file = args.dataset / "relation_labels.jsonl"
            path_file = args.dataset / "path_labels.jsonl"
            relations = [label for label in read_jsonl(relation_file, RelationLabel, 10000) if label.split == args.split] if relation_file.exists() else []
            labels = read_jsonl(path_file, PathLabel, 10000) if path_file.exists() else []
            output.update(association_scores(artifact, results, relations, labels))
            output["run_manifest"] = {"dataset": snapshot.manifest.model_dump(mode="json"),
                "runtime": runtime_provenance(),
                "snapshot_sha256": snapshot.sha256, "artifact_id": str(artifact.id),
                "artifact_sha256": hashlib.sha256(args.artifact.read_bytes()).hexdigest(),
                "queries_sha256": hashlib.sha256((args.dataset / "queries.jsonl").read_bytes()).hexdigest(),
                "split": args.split, "mode": args.mode, "dynamic": args.dynamic, "k": args.k,
                "reranker": "disabled", "model_calls": 0,
                "baseline_kind": "local_bm25_cosine_fixture" if snapshot.manifest.provenance == "synthetic_fixture" else "recorded_candidate_replay"}
            output["results"] = [result.model_dump(mode="json") for result in results]
            for result in output["results"]:
                for hit in result["hits"]:
                    for path in hit["paths"]:
                        path["label_sha256"] = digest(path)
            write_json(args.output, output, overwrite=args.overwrite)
            print(json.dumps({"status": "evaluated", "queries": len(selected), "variants": len(VARIANTS),
                              "provenance": snapshot.manifest.provenance, "production_gates": "not_evaluated"}))
        return 0
    except (ValueError, OSError) as exc:
        # Validation errors may contain private input. Print only stable codes.
        code = str(exc) if type(exc) is ValueError and "\n" not in str(exc) else type(exc).__name__
        print(json.dumps({"status": "error", "error_code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
