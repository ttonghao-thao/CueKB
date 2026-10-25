#!/usr/bin/env python3
"""Fit bounded empirical relevance gates from authorized, human-labelled scores.

Every record binds scores to the same model/generation/path. Only calibration
rows fit thresholds. Held-out families are checked for leakage, never used to fit.
No model, search engine or database is contacted.
"""
import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictBool

from cuekb.services.selection import CalibrationStats, ProfileBinding, RelevanceProfile


class ScoreRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_id: str = Field(min_length=1, max_length=200)
    family: str = Field(min_length=1, max_length=200)
    split: Literal["calibration", "test"]
    channel: Literal["vector", "rerank", "lexical"]
    score: FiniteFloat
    relevant: StrictBool
    binding: ProfileBinding


def fit(records: list[ScoreRecord], target_fpr: float, dataset_hash: str) -> RelevanceProfile:
    if not records or len(records) > 100000 or not math.isfinite(target_fpr) or not 0 <= target_fpr <= 1:
        raise ValueError("invalid_calibration_input")
    binding = records[0].binding
    if any(row.binding != binding for row in records):
        raise ValueError("mixed_runtime_bindings")
    families = defaultdict(set)
    channels = defaultdict(list)
    seen = set()
    for row in records:
        key = row.sample_id, row.channel
        if key in seen:
            raise ValueError("duplicate_sample_channel")
        seen.add(key)
        families[row.family].add(row.split)
        if row.channel == "lexical" and not 0 <= row.score <= 1:
            raise ValueError("lexical_coverage_out_of_range")
        if row.split == "calibration":
            channels[row.channel].append(row)
    if any(len(splits) > 1 for splits in families.values()):
        raise ValueError("calibration_test_family_leakage")
    if not channels:
        raise ValueError("calibration_split_empty")
    thresholds, statistics = {}, {}
    for channel, rows in channels.items():
        positives = [row.score for row in rows if row.relevant]
        negatives = [row.score for row in rows if not row.relevant]
        n_families = len({row.family for row in rows})
        if len(positives) < 5 or len(negatives) < 5 or n_families < 2:
            raise ValueError("insufficient_calibration_labels")
        choices = []
        for threshold in sorted({row.score for row in rows}):
            false_positive_rate = sum(score >= threshold for score in negatives) / len(negatives)
            recall = sum(score >= threshold for score in positives) / len(positives)
            if false_positive_rate <= target_fpr and recall > 0:
                choices.append((recall, -false_positive_rate, threshold))
        if not choices:
            raise ValueError("no_usable_threshold_at_target_false_positive_rate")
        recall, negative_fpr, threshold = max(choices)
        thresholds[f"{channel}_min_score"] = threshold
        statistics[channel] = CalibrationStats(dataset_sha256=dataset_hash,
            positives=len(positives), negatives=len(negatives), families=n_families,
            target_false_positive_rate=target_fpr, observed_false_positive_rate=-negative_fpr,
            observed_recall=recall)
    return RelevanceProfile(binding=binding, calibration=statistics, **thresholds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-false-positive-rate", type=float, default=.05)
    args = parser.parse_args()
    if args.records.stat().st_size > 64 * 1024 * 1024:
        parser.error("score recording exceeds 64 MiB")
    content = args.records.read_bytes()
    try:
        records = [ScoreRecord.model_validate_json(line) for line in content.splitlines() if line.strip()]
        profile = fit(records, args.max_false_positive_rate, hashlib.sha256(content).hexdigest())
    except ValueError as exc:
        parser.error(str(exc))
    with args.output.open("x") as output:
        output.write(profile.model_dump_json(indent=2) + "\n")
    print(json.dumps({"profile_id": profile.fingerprint, "calibration": {
        name: stats.model_dump() for name, stats in profile.calibration.items()},
        "held_out_records": sum(row.split == "test" for row in records),
        "note": "empirical calibration fit; held-out and live acceptance still required"}))


if __name__ == "__main__":
    main()
