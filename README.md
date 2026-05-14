# MIMIC-IV ETL

This repository converts local MIMIC-IV v3.1 CSV files into two flat temporal-model inputs:

- `context_data.csv`: one row per admission (`hadm_id`), numeric static context.
- `concept_events.csv`: timestamped raw-concept events and measurements.

The concept vocabulary is defined by `rawconcept-tak-repo-portable.json`. The implementation logic and mapping decisions are documented in `CLAUDE.md` so future agents can reproduce or audit changes.

## Data Expectations

MIMIC data is not included in this repository. Place the PhysioNet mirror locally at:

```text
mimic-iv/physionet.org/files/mimiciv/3.1/
```

Expected modules:

```text
hosp/admissions.csv.gz
hosp/patients.csv.gz
hosp/labevents.csv.gz
hosp/diagnoses_icd.csv.gz
hosp/d_labitems.csv.gz
hosp/emar.csv.gz
hosp/pharmacy.csv.gz
hosp/omr.csv.gz
icu/chartevents.csv.gz
icu/inputevents.csv.gz
icu/d_items.csv.gz
icu/icustays.csv.gz
```

Generated outputs are written to `output/` and are intentionally git-ignored.

## Setup

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Run The ETL

```bash
python mimic_pipeline.py
```

The script streams large MIMIC tables in chunks and writes:

```text
output/context_data.csv
output/concept_events.csv
```

Validation behavior:

- If a concept exists in the tak repo but receives zero rows, the script prints a warning.
- If the pipeline emits a concept not defined in the tak repo, it prints a warning and omits those rows before writing output.

## Post-Processing

Some output-only maintenance can be done without rerunning the full ETL:

```bash
python scripts/output_maintenance.py append-expanded-emar-meds
python scripts/output_maintenance.py drop-low-support --threshold-pct 1
python scripts/output_maintenance.py refresh-summary
```

The maintenance script records intentional removals:

- `K-BINDER_BITZUA` and `SGLT2_HOSPITAL_BITZUA` were removed from the generated dataset when patient support remained below 1%.
- `DIABETIC_COMA` and `OTHER_COMPLICATION` were removed from the tak-repo output contract because they were rare / low-value complication concepts.

## Repository Files

- `mimic_pipeline.py`: main ETL.
- `scripts/output_maintenance.py`: output-only maintenance and validation summaries.
- `rawconcept-tak-repo-portable.json`: raw concept vocabulary.
- `complications.json`: active acute complication concepts used as documentation for context-vs-event separation.
- `CLAUDE.md`: full agent-facing mapping guide and implementation requirements.

## Current Local Output Snapshot

After local post-processing, the current generated dataset has:

- `57,078` admissions.
- `21,033,781` temporal event rows.
- `58` emitted concepts.
- No emitted concept below 1% patient support.

These CSV outputs are not committed because they are generated artifacts.
