"""
Post-process generated MIMIC concept_events without rerunning mimic_pipeline.py.

Commands:
  append-expanded-emar-meds
    Re-scan EMAR for the expanded SGLT2 / potassium-binder patterns and append
    only missing rows that satisfy the existing admission windows.

  drop-low-support
    Remove concepts whose patient support is below a threshold percentage.

Both commands rewrite only files under output/.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
MIMIC_HOSP = ROOT / "mimic-iv/physionet.org/files/mimiciv/3.1/hosp"
OUT = ROOT / "output"
CONCEPT_EVENTS = OUT / "concept_events.csv"
CONTEXT_DATA = OUT / "context_data.csv"

CHUNK = 1_000_000

EMAR_ADMIN_STATUSES = {
    "Administered",
    "Confirmed",
    "Administered in Other Location",
    "Partial Administered",
    "Restarted",
}

EXPANDED_EMAR_PATTERNS = {
    "SGLT2_HOSPITAL_BITZUA": (
        "dapagliflozin|canagliflozin|empagliflozin|ertugliflozin|farxiga|invokana|"
        "jardiance|steglatro|synjardy|xigduo|glyxambi|sotagliflozin|bexagliflozin"
    ),
    "K-BINDER_BITZUA": (
        "kayexalate|sodium polystyrene|polystyrene sulfonate|patiromer|veltassa|"
        "lokelma|sodium zirconium|zirconium cyclosilicate"
    ),
}

INTENTIONAL_OUTPUT_REMOVALS = {
    "K-BINDER_BITZUA": "current generated output had <1% patient support after expanded EMAR matching",
    "SGLT2_HOSPITAL_BITZUA": "current generated output had <1% patient support after expanded EMAR matching",
}

INTENTIONAL_TAK_REPO_REMOVALS = {
    "DIABETIC_COMA": "rare complication removed from the tak-repo output contract",
    "OTHER_COMPLICATION": "rare catch-all complication removed from the tak-repo output contract",
}


def print_intentional_removal_warning() -> None:
    print("WARNING: intentional removals recorded for this dataset:")
    for concept, reason in sorted(INTENTIONAL_OUTPUT_REMOVALS.items()):
        print(f"  output-only removal: {concept} - {reason}")
    for concept, reason in sorted(INTENTIONAL_TAK_REPO_REMOVALS.items()):
        print(f"  tak-repo removal: {concept} - {reason}")


def _load_admission_windows() -> pd.DataFrame:
    terms = []
    cols = ["PatientId", "ConceptName", "StartDateTime"]
    for chunk in pd.read_csv(CONCEPT_EVENTS, usecols=cols, chunksize=CHUNK):
        sub = chunk[chunk["ConceptName"].isin(["ADMISSION", "RELEASE", "DEATH"])].copy()
        if not sub.empty:
            terms.append(sub)
    if not terms:
        raise RuntimeError("No ADMISSION/RELEASE/DEATH events found in concept_events.csv")

    term = pd.concat(terms, ignore_index=True)
    term["StartDateTime"] = pd.to_datetime(term["StartDateTime"], errors="coerce")
    admit = (
        term[term["ConceptName"].eq("ADMISSION")][["PatientId", "StartDateTime"]]
        .rename(columns={"StartDateTime": "admittime"})
    )
    end = (
        term[term["ConceptName"].isin(["RELEASE", "DEATH"])][["PatientId", "StartDateTime"]]
        .rename(columns={"StartDateTime": "end_time"})
    )
    return admit.merge(end, on="PatientId", how="inner")


def refresh_concept_summary() -> pd.DataFrame:
    context = pd.read_csv(CONTEXT_DATA, usecols=["PatientId"])
    total_patients = context["PatientId"].nunique()
    counts: Counter[str] = Counter()
    patients: dict[str, set[int]] = defaultdict(set)
    first_time = {}
    last_time = {}

    for chunk in pd.read_csv(
        CONCEPT_EVENTS,
        usecols=["PatientId", "ConceptName", "StartDateTime"],
        chunksize=CHUNK,
        parse_dates=["StartDateTime"],
    ):
        counts.update({str(k): int(v) for k, v in chunk["ConceptName"].value_counts().items()})
        for concept, ids in chunk.groupby("ConceptName")["PatientId"]:
            patients[str(concept)].update(ids.astype(int).unique().tolist())
        for concept, times in chunk.groupby("ConceptName")["StartDateTime"]:
            concept = str(concept)
            mn = times.min()
            mx = times.max()
            first_time[concept] = mn if concept not in first_time or mn < first_time[concept] else first_time[concept]
            last_time[concept] = mx if concept not in last_time or mx > last_time[concept] else last_time[concept]

    summary = pd.DataFrame(
        [
            (
                concept,
                counts[concept],
                len(patients[concept]),
                100.0 * len(patients[concept]) / total_patients,
                first_time.get(concept),
                last_time.get(concept),
            )
            for concept in sorted(counts)
        ],
        columns=["ConceptName", "rows", "patients", "patient_pct", "first", "last"],
    )
    summary.to_csv(OUT / "validation_concept_summary.csv", index=False)
    return summary


def append_expanded_emar_meds() -> None:
    windows = _load_admission_windows()
    valid_hadm = set(windows["PatientId"].astype(int))

    existing = set()
    cols = ["PatientId", "ConceptName", "StartDateTime"]
    for chunk in pd.read_csv(CONCEPT_EVENTS, usecols=cols, chunksize=CHUNK):
        sub = chunk[chunk["ConceptName"].isin(EXPANDED_EMAR_PATTERNS)]
        for row in sub.itertuples(index=False):
            existing.add((int(row.PatientId), str(row.ConceptName), str(row.StartDateTime)))

    new_parts = []
    usecols = ["hadm_id", "charttime", "medication", "event_txt"]
    dtypes = {"hadm_id": "Int64", "medication": "string", "event_txt": "string"}
    for chunk in pd.read_csv(
        MIMIC_HOSP / "emar.csv.gz",
        compression="gzip",
        usecols=usecols,
        dtype=dtypes,
        parse_dates=["charttime"],
        chunksize=CHUNK,
    ):
        chunk = chunk[
            chunk["hadm_id"].isin(valid_hadm)
            & chunk["event_txt"].isin(EMAR_ADMIN_STATUSES)
            & chunk["medication"].notna()
        ].copy()
        if chunk.empty:
            continue

        medication = chunk["medication"].str.lower()
        for concept, pattern in EXPANDED_EMAR_PATTERNS.items():
            sub = chunk[medication.str.contains(pattern, regex=True, na=False)].copy()
            if sub.empty:
                continue
            sub = sub.merge(windows, left_on="hadm_id", right_on="PatientId", how="inner")
            sub = sub[(sub["charttime"] >= sub["admittime"]) & (sub["charttime"] <= sub["end_time"])]
            if sub.empty:
                continue

            out = pd.DataFrame(
                {
                    "PatientId": sub["hadm_id"].astype(int),
                    "ConceptName": concept,
                    "StartDateTime": sub["charttime"],
                    "EndDateTime": sub["charttime"] + pd.Timedelta(seconds=1),
                    "Value": "True",
                }
            )
            out["StartDateTime"] = out["StartDateTime"].dt.strftime("%Y-%m-%d %H:%M:%S")
            out["EndDateTime"] = out["EndDateTime"].dt.strftime("%Y-%m-%d %H:%M:%S")
            out = out.drop_duplicates(["PatientId", "ConceptName", "StartDateTime"], keep="first")
            out = out[
                ~out.apply(
                    lambda row: (int(row.PatientId), row.ConceptName, row.StartDateTime) in existing,
                    axis=1,
                )
            ]
            for row in out[["PatientId", "ConceptName", "StartDateTime"]].itertuples(index=False):
                existing.add((int(row.PatientId), str(row.ConceptName), str(row.StartDateTime)))
            if not out.empty:
                new_parts.append(out)

    new_rows = (
        pd.concat(new_parts, ignore_index=True)
        if new_parts
        else pd.DataFrame(columns=["PatientId", "ConceptName", "StartDateTime", "EndDateTime", "Value"])
    )
    if new_rows.empty:
        print("No new rows found.")
        return

    new_rows.to_csv(CONCEPT_EVENTS, mode="a", header=False, index=False)
    print("Appended rows by concept:")
    print(new_rows.groupby("ConceptName").size().to_string())
    refresh_concept_summary()


def drop_low_support(threshold_pct: float) -> None:
    summary = refresh_concept_summary()
    drop_concepts = set(summary.loc[summary["patient_pct"] < threshold_pct, "ConceptName"])
    if not drop_concepts:
        print(f"No concepts below {threshold_pct}% patient support.")
        return

    removed_counts: Counter[str] = Counter()
    tmp = CONCEPT_EVENTS.with_suffix(".tmp.csv")
    first = True
    for chunk in pd.read_csv(CONCEPT_EVENTS, chunksize=CHUNK):
        mask = chunk["ConceptName"].isin(drop_concepts)
        if mask.any():
            removed_counts.update(chunk.loc[mask, "ConceptName"].value_counts().to_dict())
        kept = chunk.loc[~mask]
        kept.to_csv(tmp, mode="w" if first else "a", header=first, index=False)
        first = False

    tmp.replace(CONCEPT_EVENTS)
    pd.DataFrame(
        sorted(removed_counts.items()),
        columns=["ConceptName", "removed_rows"],
    ).to_csv(OUT / "low_support_removed.csv", index=False)
    refresh_concept_summary()

    print(f"Removed concepts below {threshold_pct}% patient support:")
    for concept, rows in sorted(removed_counts.items()):
        print(f"  {concept}: {rows:,} rows")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("append-expanded-emar-meds")
    drop = sub.add_parser("drop-low-support")
    drop.add_argument("--threshold-pct", type=float, default=1.0)
    sub.add_parser("refresh-summary")
    sub.add_parser("removed-warning")
    args = parser.parse_args()

    print_intentional_removal_warning()

    if args.command == "append-expanded-emar-meds":
        append_expanded_emar_meds()
    elif args.command == "drop-low-support":
        drop_low_support(args.threshold_pct)
    elif args.command == "refresh-summary":
        refresh_concept_summary()
    elif args.command == "removed-warning":
        return


if __name__ == "__main__":
    main()
