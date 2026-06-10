# MIMIC-IV ETL

This repository converts local MIMIC-IV v3.1 CSV files into two flat temporal-model inputs using agent acionable logic:

- `context_data.csv`: one row per admission (`hadm_id`), numeric static context.
- `concept_events.csv`: timestamped raw-concept events and measurements.

The concept vocabulary is defined by raw-concept attributes in `tak-repo-portable.json`. The ETL validates `ConceptName` values against TAK objects where `family == "raw-concept"` and `derived_from == null`, using each object's `attributes` list. This pipeline is meant to be similar to the one used on my thesis data (private), so results are reproduceable and pipeline is ~transparent. The implementation logic and mapping decisions are documented in `CLAUDE.md` so future agents can reproduce or audit changes.

## Context vs Temporal Split

The two outputs answer two different questions about each admission, and the split is intentional:

- **`context_data.csv` — patient *background* (static, no timestamp).** Demographics and *chronic* comorbidity flags that describe who the patient was on arrival. These are derived from `diagnoses_icd` (which has no per-row timestamp in MIMIC-IV) and treated as background story rather than in-hospital outcomes. They are **safe to feed a model as features**: they are not events the model is trying to predict.
- **`concept_events.csv` — in-hospital *trajectory* (timestamped, canonic temporal data).** Lab measurements, vitals, drug administrations, meals, admission/discharge/death, and *acute* complication events (e.g. `KETOACIDOSIS`, `ACIDOSIS`, `HYPEROSMOLALITY`, `CARDIOVASCULAR_DISORDER`) derived from labs at their qualifying draw time. Anything here has a real or near-real onset and can be a prediction target.

## Pipeline vs Model Data-Flow: What Belongs Where

The dataset feeds two downstream stages — the **abstraction mediator** (rule layer that derives features from raw concepts) and the **model's data-process** (further transforms / labels before model input). The rule for what this ETL hand-crafts is:

- **In this ETL (pre-computed and emitted).** Events that the mediator *cannot* compute from already-emitted concepts, **and** that the downstream prediction model needs to exist before its input stage. In practice this means composite complication events that the mediator's rule layer can't cleanly express — typically because they require multi-source concurrency joins across labs / vitals / drugs / ICD / procedures, untimed-ICD-stamp gating, or other ETL-specific data the mediator never sees. **Composite complication events emitted here are intended to be usable as downstream prediction targets**; a small number of non-target events also live here when they are structural framing (admission/discharge/death/meal) or temporal context used by other rules (diabetes diagnosis as a DKA trigger).
- **Outside this ETL (computed by the mediator at model time, never pre-emitted here).** Anything the mediator can derive from already-emitted raw concepts — single-channel thresholds (e.g. AKI from `CREATININE_SERUM_MEASURE`/`E-GFR_MEASURE`), simple deltas, single-parameter rules. These are model-side concepts; if they happen to be prediction targets, they are derived in the model's data-process from the raw concepts emitted here.
- **Concepts (not events) belong wherever they're discovered.** Raw measurements and drug administrations are always emitted here because they're the substrate for everything downstream. The mediator may derive additional non-target concepts from them; that's the mediator's job, not the ETL's.

Concretely: **if a complication can be expressed as a single-channel threshold on something already emitted, it stays out of this ETL**. If it needs multi-source joins the mediator can't do, and it's a planned prediction target, it lives here.

### Hand-crafted events emitted by this ETL

These are the only composite/derived events the ETL produces. All others in `concept_events.csv` are raw measurements, raw drug administrations, or structural framing (admission/discharge/death/meal).

| Event | Type | Rule (summary) | Why it's in the ETL |
|---|---|---|---|
| `KETOACIDOSIS` | Acute complication, prediction target | Ketones high (serum ≥ 31.2 mg/dL OR urine ≥ Small) AND (glucose ≥ 180 OR diabetes diagnosis) AND (pH < 7.30 OR HCO3 < 18), all within ±6h of the ketone draw | Three-axis concurrency join across labs + untimed ICD; mediator can't express it cleanly |
| `HYPEROSMOLALITY` | Acute complication (HHS-like), prediction target | Glucose ≥ 600 AND effective osm `2·Na + glu/18` ≥ 320 at the same chemistry draw, AND no strong ketotic acidosis within ±6h | Requires same-`charttime` Na+glu pair + NOT-DKA gate using ketones/pH/HCO3 |
| `CARDIOVASCULAR_DISORDER` | Acute complication, prediction target | First troponin ≥ 600 ng/L AND (broad CV diagnosis OR ischemic/MI diagnosis OR coronary intervention procedure on this admission) | Joins lab signal with untimed `diagnoses_icd` + `procedures_icd` |
| `ACIDOSIS` | Acute complication, prediction target | pH ≤ 7.3 AND HCO3 ≤ 10 within ±1h AND insulin-IV within ±2h of the pH draw | Lab+drug concurrency rule; needs insulin-IV times that are loaded mid-pipeline |
| `MEAL` | Structural input event (not a target) | Time-of-day → subtype: Breakfast / Lunch / Dinner / Night-Snack from `inputevents` PO Intake | Mediator never sees raw PO-Intake itemids; the time-of-day → meal-name derivation must happen here |
| `DIABETES_DIAGNOSIS` | Temporal context (not a target) | First matching diabetes ICD on the admission, stamped at `admittime` | Mirrors the `has_diabetes_type*` context flag as a timestamped temporal marker; used both as a DKA-rule trigger and as standalone context inside the trajectory |
| `ADMISSION` / `RELEASE` / `DEATH` | Structural framing; `DEATH` is a target | Per the terminus rule (`hospital_expire_flag`, `deathtime`, 30-day post-discharge `dod`) | Defines the admission timeline; cannot be derived from raw concepts |

> **Events specifically *not* in the ETL (derived by the model from raw concepts instead):** `KIDNEY_COMPLICATION` (computed from `CREATININE_SERUM_MEASURE` and `E-GFR_MEASURE`), and any shock / SIRS / sepsis composite (pure threshold rules over already-emitted vitals and labs).

Chronic-vs-acute pairs are deliberately separated so the same pathology doesn't appear on both sides:

| Pathology | Static (`context_data`) | Temporal (`concept_events`) |
|---|---|---|
| Diabetes | `has_diabetes_type1`, `has_diabetes_type2` | `DIABETES_DIAGNOSIS`, `HYPERGLYCEMIA`, `HYPOGLYCEMIA`, `GLUCOSE_MEASURE`, dosing concepts |
| Kidney | `has_ckd` (chronic, ICD `N18`/`585`) | `CREATININE_SERUM_MEASURE`, `E-GFR_MEASURE` (raw labs only — the model derives the `KIDNEY_COMPLICATION` event from these) |
| Heart | `has_chf`, `has_cad`, `has_afib` (chronic) | `CARDIOVASCULAR_DISORDER` (gated: first troponin ≥ 600 ng/L AND a CV/ischemic diagnosis or revascularization procedure on this admission) |
| Lungs | `has_copd`, `has_asthma` (chronic) | `ACUTE_RESPIRATORY_DISORDER` (when emitted) |
| Metabolic acidosis | — | `ACIDOSIS` (multi-signal: pH ≤ 7.3 + HCO3 ≤ 10 + insulin-IV within ±2h) |
| Hyperosmolar state (HHS) | — | `HYPEROSMOLALITY` (gated HHS-like: glucose ≥ 600 AND effective osm `2·Na + glu/18` ≥ 320 at the same chemistry draw, AND no strong ketotic acidosis within ±6h) |
| DKA | — | `KETOACIDOSIS` (gated: ketones high AND (glucose ≥ 180 OR diabetes diagnosis) AND (pH < 7.30 OR HCO3 < 18), all within ±6h of the ketone draw) |

### `context_data.csv` columns

| Column | Type | Meaning |
|---|---|---|
| `PatientId` | int | `admissions.hadm_id` |
| `age_at_admission` | float | `anchor_age + (admittime.year − anchor_year)`, clamped > 120 → 91 |
| `gender` | int | F=0, M=1 |
| `admission_type` | int | EMER\*→0, ELECTIVE→1, URGENT→2, else→3 |
| `has_diabetes_type1` | 0/1 | ICD-10 `E10*`; ICD-9 `250.x1`/`250.x3` |
| `has_diabetes_type2` | 0/1 | ICD-10 `E08*`/`E09*`/`E11*`/`E13*`; ICD-9 `249*`, `250.x0`/`250.x2` |
| `has_hypertension` | 0/1 | ICD-10 `I10`–`I15`; ICD-9 `401`–`405` |
| `has_obesity` | 0/1 | ICD-10 `E66*`; ICD-9 `278.0*` |
| `has_ckd` | 0/1 | Chronic kidney disease. ICD-10 `N18*`; ICD-9 `585*`. Excludes `N17` (AKI — already an event) |
| `has_chf` | 0/1 | Chronic heart failure. ICD-10 `I50*`; ICD-9 `428*` |
| `has_cad` | 0/1 | Chronic ischemic heart disease. ICD-10 `I25*`; ICD-9 `414*`. Excludes acute MI (`I20`–`I24`) |
| `has_copd` | 0/1 | COPD + emphysema. ICD-10 `J44*`/`J43*`; ICD-9 `491*`/`492*`/`496*` |
| `has_asthma` | 0/1 | ICD-10 `J45*`; ICD-9 `493*` |
| `has_afib` | 0/1 | Atrial fibrillation. ICD-10 `I48*`; ICD-9 `42731` |
| `has_dyslipidemia` | 0/1 | ICD-10 `E78*`; ICD-9 `272*` |
| `has_stroke_history` | 0/1 | Cerebrovascular history. ICD-10 `I60*`–`I69*`, `Z8673`; ICD-9 `430`–`438*` |
| `has_chronic_liver` | 0/1 | NAFLD / cirrhosis. ICD-10 `K70*`/`K74*`; ICD-9 `571*` |
| `has_atherosclerosis` | 0/1 | Chronic atherosclerosis. ICD-10 `I70*`; ICD-9 `440*` |
| `has_retinopathy_history` | 0/1 | Retinopathy/background retinal disease. ICD-10 `H35*`/`H36*`; ICD-9 `362.0*`/`362.1*`; excludes diabetic complication `E08`-`E13` codes |
| `has_neuropathy_history` | 0/1 | Neuropathy/background nerve disease. ICD-10 `G60*`-`G63*`; ICD-9 `356*`-`358*`; excludes diabetic complication `E08`-`E13` codes |
| `has_peripheral_vascular_disease` | 0/1 | Peripheral vascular disease. ICD-10 `I73*`; ICD-9 `443*`; excludes diabetic complication `E08`-`E13` codes |
| `has_skin_ulcer_history` | 0/1 | Skin ulcer/background wound disease. ICD-10 `L97*`/`L98.4*`; ICD-9 `707*`; excludes diabetic complication `E08`-`E13` codes |

> **Note on ICD timing.** Because `diagnoses_icd` has no per-row timestamp, a chronic flag flips on if the ICD code appears anywhere in the admission's billing record — *including* a first-time diagnosis made during this stay. This is acceptable for static *background* features but is the reason these concepts are not duplicated as timestamped events.

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
- Low-support concepts are retained; downstream models decide how to handle sparse concepts.

Previously support-filtered concepts:

- `K_BINDER_BITZUA`, `SGLT2_HOSPITAL_BITZUA`, `DIABETIC_COMA`, and `OTHER_COMPLICATION` may have low support, but the ETL no longer removes concepts solely because they are sparse.

## Repository Files

- `mimic_pipeline.py`: main ETL.
- `tak-repo-portable.json`: portable TAK repository; raw-concept attributes are the output `ConceptName` contract.
- `CLAUDE.md`: full agent-facing mapping guide and implementation requirements.

## Current Local Output Snapshot

The current generated dataset has:

- `57,078` admissions/patient timelines.
- `21,028,957` temporal event rows.
- `58` emitted concepts.
- Low-support emitted concepts are retained.

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
