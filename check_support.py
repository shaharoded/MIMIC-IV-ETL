"""
Report patient-level support per ConceptName in concept_events.csv:

  - total_patients:        admissions with >=1 row of the concept anywhere in their window
  - patients_after_48h:    admissions with >=1 row of the concept at StartDateTime >= admittime + 48h
  - rows_total / rows_after_48h
  - pct_total / pct_after_48h (% of the full cohort)

The 48-hour cutoff is the same horizon as the inclusion-gate look-back, and is the
relevant window for "in-hospital onset" events (after the early-admission period
where context-leakage from admittime-stamped data is most concerning).
"""
import pandas as pd

ROOT = "c:/Users/shaha/Work/Personal/mimic-dataset"
OUT  = f"{ROOT}/output"

ce  = pd.read_csv(f"{OUT}/concept_events.csv", parse_dates=["StartDateTime", "EndDateTime"])
ctx = pd.read_csv(f"{OUT}/context_data.csv")

# Admission-start times come from the ADMISSION concept (one per PatientId, Value="True")
adm_start = (
    ce[ce["ConceptName"] == "ADMISSION"]
      .groupby("PatientId")["StartDateTime"].min()
      .rename("admittime")
)
ce = ce.merge(adm_start, on="PatientId", how="left")
ce["hours_in"] = (ce["StartDateTime"] - ce["admittime"]).dt.total_seconds() / 3600.0

N = ctx["PatientId"].nunique()
print(f"Cohort size: {N:,} admissions")
print(f"concept_events rows: {len(ce):,}")
print()

total = (
    ce.groupby("ConceptName")
      .agg(rows_total=("PatientId", "size"),
           patients_total=("PatientId", "nunique"))
)
after = (
    ce[ce["hours_in"] >= 48]
      .groupby("ConceptName")
      .agg(rows_after_48h=("PatientId", "size"),
           patients_after_48h=("PatientId", "nunique"))
)
sup = total.join(after, how="left").fillna(0).astype({"rows_after_48h": int, "patients_after_48h": int})
sup["pct_total"]     = (100.0 * sup["patients_total"]     / N).round(2)
sup["pct_after_48h"] = (100.0 * sup["patients_after_48h"] / N).round(2)
sup = sup.sort_values("patients_total", ascending=False)

with pd.option_context("display.max_rows", None, "display.width", 160):
    print(sup.to_string())

sup.to_csv(f"{OUT}/support_report.csv")
print(f"\nWritten to {OUT}/support_report.csv")
