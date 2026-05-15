# MIMIC-IV ETL

This repository converts local MIMIC-IV v3.1 CSV files into two flat temporal-model inputs using agent acionable logic:

- `context_data.csv`: one row per admission (`hadm_id`), numeric static context.
- `concept_events.csv`: timestamped raw-concept events and measurements.

The concept vocabulary is defined by `rawconcept-tak-repo-portable.json`. This pipeline is meant to be similar to the one used on my thesis data (private), so results are reproduceable and pipeline is ~transparent. The implementation logic and mapping decisions are documented in `CLAUDE.md` so future agents can reproduce or audit changes.

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
- If an emitted concept has less than 1% patient support, it prints a warning and omits those rows before writing output.

Current support-filtered concepts:

- `K_BINDER_BITZUA`, `SGLT2_HOSPITAL_BITZUA`, `DIABETIC_COMA`, and `OTHER_COMPLICATION` are in the tak repo and mapped in the pipeline, but are omitted from final output when they remain below the 1% support threshold.

## Repository Files

- `mimic_pipeline.py`: main ETL.
- `rawconcept-tak-repo-portable.json`: raw concept vocabulary.
- `CLAUDE.md`: full agent-facing mapping guide and implementation requirements.

## Current Local Output Snapshot

After local support filtering, the current generated dataset has:

- `57,078` admissions/patient timelines.
- `21,028,957` temporal event rows.
- `58` emitted concepts.
- No emitted concept below 1% patient support.

Rows per patient timeline:

| Statistic | Rows |
|-----------|------|
| Mean | 368.4 |
| Std | 262.9 |
| Min | 12 |
| 1% | 84 |
| 5% | 115.9 |
| 25% | 197 |
| Median | 291 |
| 75% | 452 |
| 95% | 891 |
| 99% | 1,360 |
| Max | 3,587 |

Largest event concepts by row count:

| Concept | Rows |
|---------|------|
| `HEART_RATE_MEASURE` | 3,902,365 |
| `BLOOD_PRESSURE_SYSTOLIC_MEASURE` | 3,705,203 |
| `BLOOD_PRESSURE_DIASTOLIC_MEASURE` | 3,703,372 |
| `BODY_TEMPERATURE` | 1,139,351 |
| `GLUCOSE_MEASURE` | 1,045,468 |
| `POTASSIUM_MEASURE` | 605,599 |
| `SODIUM_MEASURE` | 556,694 |
| `HEMATOCRIT_MEASURE` | 555,707 |
| `DEXTROSE_BITZUA` | 520,499 |
| `HEMOGLOBIN_MEASURE` | 513,549 |
