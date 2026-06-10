# MIMIC-IV → TAK Raw Concept Pipeline: Requirements & Mapping Guide

## Overview

This pipeline transforms **MIMIC-IV v3.1** CSV files into two flat tables that mimic the output of my thesis's OMOP-based data pipeline. The output is used for downstream model testing and improvement.

All concept names in the output **must exactly match** a raw-concept attribute name from `tak-repo-portable.json`. The ETL only uses TAK objects where `family == "raw-concept"` and `derived_from == null`, so parameterized raw concepts are not treated as input concepts.

The companion script is `mimic_pipeline.py` — it currently targets MIMIC-III and must be ported to MIMIC-IV per this spec.

---

## Input Files (MIMIC-IV v3.1)

All files are gzipped CSVs (`.csv.gz`). Load with `pandas.read_csv(path, compression="gzip")`. **All column names are lowercase** in MIMIC-IV (unlike MIMIC-III).

Root: `mimic-iv/physionet.org/files/mimiciv/3.1/`

### Hospital module (`hosp/`)

| File | Key Columns | Used For |
|------|-------------|----------|
| `admissions.csv.gz` | `hadm_id`, `subject_id`, `admittime`, `dischtime`, `deathtime`, `admission_type`, `hospital_expire_flag` | Patient framing, ADMISSION/RELEASE/DEATH events |
| `patients.csv.gz` | `subject_id`, `gender`, `anchor_age`, `anchor_year`, `anchor_year_group`, `dod` | Age calculation, sex |
| `labevents.csv.gz` | `hadm_id`, `subject_id`, `itemid`, `charttime`, `valuenum`, `valueuom` | Lab measurements |
| `diagnoses_icd.csv.gz` | `hadm_id`, `icd_code`, `icd_version` | Diagnoses, complications, comorbidities (note: **both ICD-9 and ICD-10**) |
| `d_labitems.csv.gz` | `itemid`, `label`, `fluid`, `category` | Lab item ID lookup |
| `emar.csv.gz` | `hadm_id`, `charttime`, `medication`, `event_txt`, `pharmacy_id` | Confirmed med-administration events (PO / SC drugs not flowing through ICU `inputevents`) |
| `omr.csv.gz` | `subject_id`, `chartdate`, `result_name`, `result_value` | Optional: BMI / height / weight outpatient measurements |
| ~~`prescriptions.csv.gz`~~ | — | **Not used** — orders, not administrations. Would contaminate dose data. |
| `pharmacy.csv.gz` | `pharmacy_id`, `route` | **Route-lookup only** for EMAR rows (steroids/antibiotics/heparin IV vs PO vs SC split). Dose/medication columns ignored. |

### ICU module (`icu/`)

| File | Key Columns | Used For |
|------|-------------|----------|
| `chartevents.csv.gz` | `hadm_id`, `stay_id`, `itemid`, `charttime`, `valuenum`, `valueuom` | Vitals, bedside measurements |
| `inputevents.csv.gz` | `hadm_id`, `stay_id`, `starttime`, `endtime`, `itemid`, `amount`, `amountuom`, `rate` | IV / documented infusions (Metavision only — no CV/MV split in MIMIC-IV) |
| `d_items.csv.gz` | `itemid`, `label`, `category`, `unitname` | Chart/input item ID lookup |
| `icustays.csv.gz` | `hadm_id`, `stay_id`, `intime`, `outtime` | ICU period framing (optional context) |
| `outputevents.csv.gz` | `hadm_id`, `charttime`, `itemid`, `value` | Optional: fluid output |

**MIMIC-IV consolidation note**: MIMIC-IV merged the MIMIC-III `INPUTEVENTS_MV` and `INPUTEVENTS_CV` into a single Metavision-era `inputevents` table. Older CareVue itemids (e.g., `211`, `51`, `442`, `30105`) are **not** present — use only Metavision itemids (`220xxx` / `223xxx` / `225xxx` / `226xxx` / `227xxx` etc.).

---

## Patient Inclusion Criteria

Only include admissions (`hadm_id`) that satisfy **all** of the following:
1. Present in `admissions.csv.gz` with a valid `admittime`
2. Have either a valid `dischtime` (release) **or** a `deathtime` / `hospital_expire_flag=1` (death)
3. Admission window is at least 48 hours and no more than 14 days (336 hours). If the patient was released from this admission and died (per `patients.dod`) within 30 days of `dischtime`, recast the admission terminus as DEATH at `dod` (emit `DEATH` instead of `RELEASE`).
4. Have at least 2 glucose measurements (ConceptName=`GLUCOSE_MEASURE`) OR a diabetes diagnosis within the first 48 hours of the admission window
5. Only adults (18+ at admission). Exclude gestational diabetes diagnoses (ICD-10 `O24*`, ICD-9 `648.8*`).
6. Only inpatients who actually stayed in the ICU — the admission must have **at least one row in `icustays.csv.gz`**. This is what makes the "actual administered medication & dose" data available, since dosage is only recorded in `icu/inputevents.csv.gz`.

The unit of analysis is the **admission** (`hadm_id`), referred to as `PatientId` in both output tables. An admission may have multiple `stay_id` rows in `icustays`; treat the admission as a single timeline and union events across its ICU stays.

### Age computation in MIMIC-IV
MIMIC-IV does not provide `dob`. Use the anchor convention:

```
age_at_admission = anchor_age + (admittime.year - anchor_year)
```

Where `anchor_age` is the patient's age in `anchor_year` (provided in `patients.csv.gz`). Ages > 89 are already censored to 91 by PhysioNet in MIMIC-IV, so no extra clamping is required, but defensively clamp anything > 120 to 91.

---

## Output Table 1: `context_data.csv`

One row per `hadm_id`. All values must be numeric (int or float). Boolean fields should be encoded as 0/1. Fill missing values with the **column median** across all admissions.

### Required Columns

**Important**: `context_data` must only contain **chronic background disease** present at admission. Acute diagnosis-derived conditions are emitted as timestamped `concept_events` and must not be duplicated as static context.

| Column | Source | Notes |
|--------|--------|-------|
| `PatientId` | `admissions.hadm_id` | Primary key |
| `age_at_admission` | `patients.anchor_age` + (`admittime.year` − `anchor_year`) | Clamp >120 to 91 |
| `gender` | `patients.gender` | F=0, M=1 |
| `admission_type` | `admissions.admission_type` | MIMIC-IV values include `EMERGENCY`, `EW EMER.`, `ELECTIVE`, `URGENT`, `OBSERVATION ADMIT`, `DIRECT EMER.`, `DIRECT OBSERVATION`, `AMBULATORY OBSERVATION`, `SURGICAL SAME DAY ADMISSION`. Encode: anything containing `EMER`→0, `ELECTIVE`→1, `URGENT`→2, else→3 |
| `has_diabetes_type1` | `diagnoses_icd` | ICD-9 `250.x1`/`250.x3`; ICD-10 `E10*` |
| `has_diabetes_type2` | `diagnoses_icd` | ICD-9 `250.x0`/`250.x2`, `249.xx`; ICD-10 `E11*` (also `E08*`, `E09*`, `E13*` for secondary diabetes) |
| `has_hypertension` | `diagnoses_icd` | ICD-9 `401`–`405`; ICD-10 `I10`–`I15` |
| `has_obesity` | `diagnoses_icd` | ICD-9 `278.0x`; ICD-10 `E66*` |
| `has_ckd` | `diagnoses_icd` | Chronic kidney disease. ICD-9 `585*`; ICD-10 `N18*`. Excludes `N17` (AKI — captured downstream by the model from raw `CREATININE_SERUM_MEASURE` / `E-GFR_MEASURE`) |
| `has_chf` | `diagnoses_icd` | Chronic heart failure. ICD-9 `428*`; ICD-10 `I50*` |
| `has_cad` | `diagnoses_icd` | Chronic ischemic heart disease. ICD-9 `414*`; ICD-10 `I25*`. Excludes acute MI/ACS (`I20`–`I24`) |
| `has_copd` | `diagnoses_icd` | COPD + emphysema. ICD-9 `491*`, `492*`, `496*`; ICD-10 `J44*`, `J43*` |
| `has_asthma` | `diagnoses_icd` | ICD-9 `493*`; ICD-10 `J45*` |
| `has_afib` | `diagnoses_icd` | Atrial fibrillation. ICD-9 `42731`; ICD-10 `I48*` |
| `has_dyslipidemia` | `diagnoses_icd` | ICD-9 `272*`; ICD-10 `E78*` |
| `has_stroke_history` | `diagnoses_icd` | Cerebrovascular history. ICD-9 `430`–`438*`; ICD-10 `I60*`–`I69*`, `Z8673` |
| `has_chronic_liver` | `diagnoses_icd` | NAFLD / cirrhosis. ICD-9 `571*`; ICD-10 `K70*`, `K74*` |
| `has_atherosclerosis` | `diagnoses_icd` | Chronic atherosclerosis. ICD-9 `440*`; ICD-10 `I70*` |
| `has_retinopathy_history` | `diagnoses_icd` | Retinopathy/background retinal disease. ICD-9 `362.0*`, `362.1*`; ICD-10 `H35*`, `H36*`. Excludes diabetic complication `E08`-`E13` codes |
| `has_neuropathy_history` | `diagnoses_icd` | Neuropathy/background nerve disease. ICD-9 `356*`-`358*`; ICD-10 `G60*`-`G63*`. Excludes diabetic complication `E08`-`E13` codes |
| `has_peripheral_vascular_disease` | `diagnoses_icd` | Peripheral vascular disease. ICD-9 `443*`; ICD-10 `I73*`. Excludes diabetic complication `E08`-`E13` codes |
| `has_skin_ulcer_history` | `diagnoses_icd` | Skin ulcer/background wound disease. ICD-9 `707*`; ICD-10 `L97*`, `L984*`. Excludes diabetic complication `E08`-`E13` codes |

**Chronic-flag rule.** Background flags in `context_data` are intentionally restricted to *chronic* code ranges so they do not double-count an acute lab-derived event already emitted to `concept_events` (e.g. `N18` chronic CKD goes to context, `N17` AKI is captured as the lab-derived `KIDNEY_COMPLICATION` event). Because `diagnoses_icd` has no per-row timestamp, a flag flips on whenever the code appears in this admission's billing record — including first-time diagnoses made during this stay. This is acceptable for static background features and is the reason these chronic concepts are not duplicated as timestamped events.

> `hospital_expire_flag`, `first_glucose`, and `first_potassium` are **deliberately excluded** from `context_data` — the first two would leak the outcome the downstream model is meant to predict, and the lab values already appear in `concept_events` as time-stamped rows so duplicating them as static context is redundant.

### Notes
- ICD-9 codes in MIMIC-IV are stored without the decimal point (e.g., `25001` = `250.01`).
- ICD-10 codes are also stored without the decimal point (e.g., `E1100` = `E11.00`).
- Always filter on `icd_version` when applying ICD-9- vs ICD-10-specific patterns.

---

## Output Table 2: `concept_events.csv`

One row per event/measurement. Columns:

| Column | Type | Description |
|--------|------|-------------|
| `PatientId` | int | `admissions.hadm_id` |
| `ConceptName` | string | Exact raw-concept attribute name from `tak-repo-portable.json` |
| `StartDateTime` | datetime | Event timestamp (ISO 8601: `YYYY-MM-DD HH:MM:SS`) |
| `EndDateTime` | datetime | Always `StartDateTime + 1 second` |
| `Value` | string/float | Numeric string for measurements; `"True"` for events/indications/drug administrations |

### Rules
- Only events falling **within** `[admittime, dischtime or deathtime]` are included.
- `ConceptName` values must be a **strict subset** of raw-concept attribute names in `tak-repo-portable.json`.
- For measurement concepts, `Value` is the numeric reading.
- For event/drug/complication concepts, `Value = "True"`. `MEAL` has specific discrete values; `INSULIN_IV_DOSAGE`, `BASAL_DOSAGE`, and `BOLUS_DOSAGE` carry numeric doses.
- Duplicates (same `PatientId` + `ConceptName` + `StartDateTime`) should be deduplicated (keep first).

---

## Unit Normalization Policy

Every concept has **one canonical unit**. Downstream code cannot handle mixed units, so the pipeline must enforce a single unit per concept before emitting. Strategy:

1. **Convert** when the conversion is unambiguous and trivial (listed below).
2. Otherwise, **keep only the rows whose `valueuom` matches the canonical unit** (case-insensitive, ignoring whitespace and synonyms like `mEq/L` ≡ `mmol/L` for monovalent ions). Drop minority/foreign-unit rows — do **not** attempt a guess-based conversion.
3. If a concept ends up with zero rows after unit filtering, log a warning and leave the rows out. Do not invent values.
4. If you encounter an ambiguous unit case not listed below (e.g., multiple plausible canonical units, or two competing populations of comparable size), **stop and ask the user** rather than guessing.

### Canonical units per measurement concept

| Concept | Canonical unit | Conversion rule (if any) |
|---------|---------------|---------------------------|
| `GLUCOSE_MEASURE` | mg/dL | drop mmol/L |
| `BASE_GLUCOSE_MEASURE` | mg/dL | HbA1c% → eAG: `(HbA1c * 28.7) - 46.7`; otherwise fall back to first in-admission glucose (already in mg/dL) |
| `BICARBONATE_MEASURE` | mmol/L | mEq/L ≡ mmol/L (accept both) |
| `PH_MEASURE` | unitless (6.5–8.0 plausibility range) | drop out-of-range |
| `POTASSIUM_MEASURE` | mmol/L | mEq/L ≡ mmol/L (accept both) |
| `SODIUM_MEASURE` | mmol/L | mEq/L ≡ mmol/L (accept both) |
| `CREATININE_SERUM_MEASURE` | mg/dL | drop µmol/L |
| `ALBUMIN_SERUM_MEASURE` | g/dL | drop g/L |
| `ALANINE-AMINOTRANSFERASE_MEASURE` | U/L | drop IU/L only if different scale (treat IU/L ≡ U/L) |
| `ASPARATE-AMINOTRANSFERASE_MEASURE` | U/L | same as above |
| `HEMATOCRIT_MEASURE` | % | drop fractional (0–1) reports if any |
| `HEMOGLOBIN_MEASURE` | g/dL | drop g/L |
| `PLT_MEASURE` | K/uL (≡ 10³/µL) | accept `K/uL`, `10*3/uL`, `10^3/uL` |
| `INFECTION_WBC_MEASURE` | K/uL | same as above |
| `NEUTROPHILS_MEASURE` | K/uL (absolute count, labs `52075`, `53159`) | drop `%` rows such as lab `51256` (relative neutrophils) |
| `TROPONIN_MEASURE` | ng/L | convert ng/mL → ng/L (`*1000`) |
| `UREA_MEASURE` | mg/dL | drop mmol/L |
| `KETONES_SERUM_MEASURE` | mg/dL | no serum ketone item exists in MIMIC-IV v3.1 `d_labitems`; emit zero rows unless a future local dictionary adds one |
| `KETONES_URINE_MEASURE` | mg/dL (qualitative scale) | Negative=0, Trace=5, Small=15, Moderate=40, Large=80 |
| `CREATINE-KINASE_MEASURE` | U/L | drop other |
| `HEART_RATE_MEASURE` | bpm | drop rows outside (20, 250) |
| `BLOOD_PRESSURE_SYSTOLIC_MEASURE` | mmHg | drop rows outside (40, 280) |
| `BLOOD_PRESSURE_DIASTOLIC_MEASURE` | mmHg | drop rows outside (20, 200) |
| `BODY_TEMPERATURE` | °C | convert °F → °C: `(F-32)*5/9`; drop rows outside (25, 45) °C |
| `WEIGHT_MEASURE` | kg | convert lbs → kg (`*0.453592`); drop rows outside (20, 400) kg |
| `BMI_MEASURE` | kg/m² | drop rows outside (10, 80) |
| `E-GFR_MEASURE` | mL/min/1.73m² | drop other |

### Canonical units per drug-dose concept (only `INSULIN_IV_DOSAGE`, `BASAL_DOSAGE`, `BOLUS_DOSAGE` carry numeric values)

| Concept | Canonical unit | Conversion rule |
|---------|---------------|------------------|
| `INSULIN_IV_DOSAGE` | U (units) | `inputevents.amountuom` must be `units`; keep 0.01-100 only. `prescriptions` is **not** consulted for dose. |
| `BASAL_DOSAGE` | U | `inputevents.amountuom` must be `units`; keep 1-300 only. |
| `BOLUS_DOSAGE` | U | `inputevents.amountuom` must be `units`; keep 1-150 only. |

All other `*_BITZUA` concepts carry `Value="True"` (no numeric dose) — no unit normalization needed.

---

## Raw Concept → MIMIC-IV Mapping

### Category: Events

| ConceptName | Source | Logic |
|-------------|--------|-------|
| `ADMISSION` | `admissions.admittime` | One row per admission; Value=`"True"` |
| `RELEASE` | `admissions.dischtime` | Emit only if the admission terminus is *not* recast to DEATH (see DEATH rule below). Value=`"True"` |
| `DEATH` | See rule below | Value=`"True"` |

**DEATH / RELEASE terminus rule.** Decide per admission in this order:
1. If `hospital_expire_flag = 1` → emit `DEATH` at `deathtime` (or at `dischtime` if `deathtime` is null). No `RELEASE`.
2. Else if `patients.dod` is non-null and `dod` falls within `[dischtime, dischtime + 30 days]` → emit `DEATH` at `dod`. No `RELEASE`. (Within-30-day mortality after discharge is treated as the outcome of this admission.) Note: `dod` may fall outside `[admittime, dischtime]`, so the admission-window filter in Step 4 must allow this exception.
3. Else → emit `RELEASE` at `dischtime`. No `DEATH`.
| `MEAL` | `inputevents` itemid `226452` (PO Intake) and `226377` (PACU PO Intake) | See meal-subtype rule below |

**MEAL subtype derivation.** MIMIC-IV does not label PO Intake events with meal name. Derive `Value` from the `starttime` hour-of-day:
- `Breakfast` → 05:00–11:30
- `Lunch` → 11:30–16:30
- `Dinner` → 16:30–21:00
- `Night-Snack` → 21:00–05:00
put a script that if 2 concecutive meals have the same value - delete the latest.

Deduplicate `(PatientId, ConceptName, hour-rounded StartDateTime)` to avoid double-counting back-to-back PO Intake entries within the same meal window. also pull all dates to dates pandas can handle (not too big / too small)

---

### Category: Measurements

Use `labevents.valuenum` / `chartevents.valuenum` where `hadm_id` is not null and `valuenum` is not null and within admission window.

| ConceptName | itemid(s) | Source | Notes |
|-------------|-----------|--------|-------|
| `GLUCOSE_MEASURE` | Lab `50931`, `50809`, `52027`, `52569`; Chart `220621`, `226537`, `225664`, `228388` | labevents + chartevents | mg/dL. Chart fingerstick rows (`225664`) often have blank `valueuom`; keep them by itemid and plausibility range. |
| `BICARBONATE_MEASURE` | Lab `50882` | labevents | mEq/L |
| `PH_MEASURE` | Lab `50820` | labevents | unitless |
| `POTASSIUM_MEASURE` | Lab `50971`, `50822` | labevents | mEq/L |
| `SODIUM_MEASURE` | Lab `50983`, `50824` | labevents | mEq/L |
| `CREATININE_SERUM_MEASURE` | Lab `50912` | labevents | mg/dL |
| `ALBUMIN_SERUM_MEASURE` | Lab `50862` | labevents | g/dL |
| `ALANINE-AMINOTRANSFERASE_MEASURE` | Lab `50861` | labevents | U/L |
| `ASPARATE-AMINOTRANSFERASE_MEASURE` | Lab `50878` | labevents | U/L |
| `HEMATOCRIT_MEASURE` | Lab `51221` | labevents | % |
| `HEMOGLOBIN_MEASURE` | Lab `51222` | labevents | g/dL |
| `PLT_MEASURE` | Lab `51265` | labevents | K/uL |
| `INFECTION_WBC_MEASURE` | Lab `51301`, `51300` | labevents | K/uL |
| `NEUTROPHILS_MEASURE` | Lab `52075`, `53159` | labevents | K/uL absolute neutrophil count. Do not use `51256`; it is a percent differential. |
| `TROPONIN_MEASURE` | Lab `51003` (Troponin T), `52642` (Troponin I) | labevents | ng/mL → multiply by 1000 for ng/L |
| `UREA_MEASURE` | Lab `51006` | labevents | mg/dL (BUN) |
| `KETONES_SERUM_MEASURE` | none in MIMIC-IV v3.1 | labevents | `d_labitems` has urine ketone rows but no serum ketone row; emit zero rows rather than reusing glucose itemids. |
| `KETONES_URINE_MEASURE` | Lab `51484`, `51984` | labevents | qualitative → Negative=0, Trace=5, Small=15, Moderate=40, Large=80 |
| `CREATINE-KINASE_MEASURE` | Lab `50910` | labevents | U/L |
| `HEART_RATE_MEASURE` | Chart `220045` | chartevents | BPM |
| `BLOOD_PRESSURE_SYSTOLIC_MEASURE` | Chart `220179` (NIBP sys), `220050` (ABP sys) | chartevents | mmHg |
| `BLOOD_PRESSURE_DIASTOLIC_MEASURE` | Chart `220180` (NIBP dia), `220051` (ABP dia) | chartevents | mmHg |
| `BODY_TEMPERATURE` | Chart `223761` (°F), `223762` (°C) | chartevents | Convert °F→°C: `(F-32)*5/9` |
| `WEIGHT_MEASURE` | Chart `226512` (admit wt, kg), `224639` (daily wt, kg), `226531` (wt, lbs) | chartevents | Convert lbs→kg (`*0.453592`) where needed |
| `BMI_MEASURE` | `omr.csv.gz` `result_name` containing `BMI`, else compute from height + weight | omr / chartevents | kg/m² |
| `E-GFR_MEASURE` | Native lab itemids `50920`, `51770`, `52026` when present; otherwise CKD-EPI 2021 derived from timestamped serum creatinine + age + sex | labevents / derived | mL/min/1.73m². Timestamp is the native eGFR row time or the creatinine draw time |
| `BASE_GLUCOSE_MEASURE` | Two paths (see rule below) | labevents | mg/dL |

**`BASE_GLUCOSE_MEASURE` rule.** This concept represents the patient's baseline glycemic state. Emit it via either path; if both exist, prefer the HbA1c-derived value (more representative of long-term baseline):
1. **HbA1c-derived** — lab `50852` (Hemoglobin A1c, %): convert HbA1c% → estimated average glucose (eAG, mg/dL) with `eAG = (HbA1c * 28.7) - 46.7`. Timestamp = the HbA1c `charttime`.
2. **First-glucose fallback** — if no HbA1c row exists in the admission window, emit the earliest `GLUCOSE_MEASURE` value of the admission as `BASE_GLUCOSE_MEASURE` at the same timestamp (this row is in addition to the regular `GLUCOSE_MEASURE` row, not a replacement).

> **Tip**: when in doubt about an itemid, look it up in `d_items.csv.gz` (chart/input) or `d_labitems.csv.gz` (labs) by `label` substring match.

---

### Category: Drug Administration

#### Sources & Priority — administration-only

Only use sources that record **actual administration events**, never prescribed/ordered doses:

1. **Primary — `icu/inputevents.csv.gz`** (Metavision only): the only source with both administration timestamp and verified administered dose. Used for IV infusions, IV pushes, and documented SC/IM injections. Timestamp: `starttime`. Dose: `amount` (when `amountuom` is canonical, see Unit Normalization Policy).
2. **Secondary — `hosp/emar.csv.gz`** (Electronic Medication Administration Record): the only source for oral / SC drugs that don't flow through ICU inputevents (e.g., metformin tablets, oral antibiotics). Use only rows where `event_txt` ∈ {`Administered`, `Confirmed`, `Administered in Other Location`, `Partial Administered`, `Restarted`}. **EMAR has no dose value** — every EMAR-sourced row is `Value="True"`, regardless of drug class. Insulin amounts therefore come from `inputevents` only; SC home-style insulin given on the floor via EMAR is intentionally **not** picked up as a dosed row.

`hosp/prescriptions.csv.gz` is **not used** for `concept_events`. It holds prescribed orders, not administrations — including its doses would corrupt the dosing signal.

`hosp/pharmacy.csv.gz` is used **only as a route lookup** (`pharmacy_id → route`) so that EMAR rows can be split into IV vs PO vs SC concepts (steroids, antibiotics, heparin). Its `medication` and dose columns are **not** consumed. The route join works because EMAR carries `pharmacy_id` linking back to the parent pharmacy order.

#### Value Rules
- `INSULIN_IV_DOSAGE`, `BASAL_DOSAGE`, `BOLUS_DOSAGE`: from `inputevents` only. `Value` = `amount` (canonical unit: insulin Units; see Unit Normalization Policy - drop rows where `amountuom` is not Units or outside the allowed range).
- All other drug concepts: `Value = "True"`.

#### Insulin (`inputevents` only) — itemids verified against `d_items.csv.gz`

| ConceptName | itemid(s) | Label |
|-------------|-----------|-------|
| `INSULIN_IV_DOSAGE` | `223258` | Insulin - Regular. Exclude `229619` Insulin - U500 because it is concentrated regular insulin and should not be treated as default IV regular insulin dosage. |
| `BASAL_DOSAGE`      | `223259`, `223260` | Insulin - NPH, Insulin - Glargine |
| `BOLUS_DOSAGE`      | `223262`, `229299`, `223261`, `223257` | Insulin - Humalog, Insulin - Novolog, Insulin - Humalog 75/25, Insulin - 70/30 |

#### Antidiabetics (non-insulin) — `emar` only

| ConceptName | `medication` patterns (case-insensitive substring on EMAR `medication`) |
|-------------|------------------------------------------------------------------------|
| `METFORMIN_HOSPITAL_BITZUA` | `metformin` |
| `ANTIDIABETIC_HIGH_HYPO_HOSPITAL_BITZUA` | `glipizide`, `glyburide`, `glimepiride`, `sitagliptin`, `saxagliptin`, `alogliptin`, `linagliptin`, `exenatide`, `liraglutide`, `dulaglutide`, `semaglutide`, `pioglitazone` |
| `SGLT2_HOSPITAL_BITZUA` | `dapagliflozin`, `canagliflozin`, `empagliflozin`, `ertugliflozin`, `farxiga`, `invokana`, `jardiance`, `steglatro`, `synjardy`, `xigduo`, `glyxambi`, `sotagliflozin`, `bexagliflozin` |

#### Antibiotics

| ConceptName | `inputevents` itemid(s) | `emar` (`medication` patterns) |
|-------------|--------------------------|--------------------------------|
| `ANTIBIOTIC_IV_BITZUA` | all `d_items.category == "Antibiotics"` inputevents itemids: `225798`, `225837`, `225838`, `225840`, `225842`, `225843`, `225844`, `225845`, `225847`, `225848`, `225850`, `225851`, `225853`, `225855`, `225857`, `225859`, `225860`, `225862`, `225863`, `225865`, `225866`, `225868`, `225869`, `225871`, `225873`, `225875`, `225876`, `225877`, `225879`, `225881`, `225882`, `225883`, `225884`, `225885`, `225886`, `225888`, `225889`, `225890`, `225892`, `225893`, `225895`, `225896`, `225897`, `225898`, `225899`, `225900`, `225902`, `225903`, `225905`, `227691`, `228003`, `229059`, `229061`, `229064`, `229587` | n/a |
| `ANTIBIOTIC_PO_BITZUA` | n/a | antibiotic name pattern + EMAR `route` ∈ PO routes (via `pharmacy.route` join) |

Antibiotic name patterns (case-insensitive partial match): include the common antibacterial names above plus the other MIMIC-IV ICU antibiotic-category labels (`acyclovir`, `ambisome`, `amikacin`, `atovaquone`, `aztreonam`, `caspofungin`, `ceftazidime`, `chloroquine`, `colistin`, `erythromycin`, `ethambutol`, `fluconazole`, `foscarnet`, `ganciclovir`/`gancyclovir`, `gentamicin`, `isoniazid`, `mefloquine`, `micafungin`, `moxifloxacin`, `nafcillin`, `oxacillin`, `penicillin`, `pyrazinamide`, `quinine`, `ribavirin`, `rifampin`, `bactrim`, `tobramycin`, `valganciclovir`/`valgancyclovir`, `voriconazole`, `keflex`, `tamiflu`, `chloramphenicol`, `ertapenem`, `tigecycline`, `ceftaroline`).

#### Other Drugs

| ConceptName | `inputevents` itemid(s) | `emar` (`medication` patterns) |
|-------------|--------------------------|--------------------------------|
| `HEPARIN_IV_BITZUA` | `225152` Heparin Sodium, `225975` Heparin Prophylaxis, `229597` Impella, `230044` CRRT | n/a (all heparin in `inputevents` is IV) |
| `HEPARIN_SC_BITZUA` | n/a | `heparin` + EMAR `route` ∈ SC routes (via `pharmacy.route` join) |
| `DEXTROSE_BITZUA` | `220949` D5%, `220950` D10%, `220952` D50%, `228140/141/142` D20/30/40%, `225947` PN | n/a |
| `BICARBONATE_BITZUA` | `220995` 8.4%, `225165` Bicarbonate Base, `227533` 8.4% Amp, `221211` 1.4% | n/a |
| `CALCIUM-GLUCONATE_BITZUA` | `221456`, `227525` (CRRT), `229640` (Bolus) | n/a |
| `HYPERTONIC_SALINE_BITZUA` | `225161` NaCl 3%, `228341` NaCl 23.4% | n/a |
| `K_BINDER_BITZUA` | n/a | `kayexalate`, `sodium polystyrene`, `polystyrene sulfonate`, `patiromer`, `veltassa`, `lokelma`, `sodium zirconium`, `zirconium cyclosilicate` (any route). Low support in the current generated dataset, but keep in the pipeline because local EMAR contains additional names missed by the original pattern. |
| `STEROIDS_IV_BITZUA` | n/a — **no steroid itemids exist in MIMIC-IV `d_items`** | steroid name pattern + EMAR `route` ∈ IV routes (via `pharmacy.route` join) |
| `STEROIDS_PO_BITZUA` | n/a | steroid name pattern + EMAR `route` ∈ PO routes (via `pharmacy.route` join) |

SGLT2 name pattern: `dapagliflozin`, `canagliflozin`, `empagliflozin`, `ertugliflozin`, `farxiga`, `invokana`, `jardiance`, `steglatro`, `synjardy`, `xigduo`, `glyxambi`, `sotagliflozin`, `bexagliflozin`. Low support in the current generated dataset, but keep in the pipeline because local EMAR contains additional names missed by the original pattern.

Steroid name pattern: `methylprednisolone`, `hydrocortisone`, `dexamethasone`, `prednisone`, `prednisolone`.

> When joining EMAR rows to the admission window, use EMAR's `charttime` and filter to `[admittime, dischtime/deathtime]` per the global rule.

---

### Category: Diagnoses & Complications (from `diagnoses_icd.csv.gz`)

Use the **first occurrence** of a matching code per admission. Timestamp = `admittime` (diagnoses are not timestamped in MIMIC; use admission time as proxy). `Value = "True"`.

These map to `concept_events`, **not** `context_data`. The active diagnosis-derived event concepts and ICD patterns are defined in the table below and implemented directly in `mimic_pipeline.py`.

> **Always filter by `icd_version`** (9 or 10) before applying a pattern. Most MIMIC-IV admissions use ICD-10.

| ConceptName | ICD-9 codes (`icd_version=9`) | ICD-10 codes (`icd_version=10`) |
|-------------|-------------------------------|----------------------------------|
| `DIABETES_DIAGNOSIS` | `250*`, `249*` | `E08*`, `E09*`, `E10*`, `E11*`, `E13*` |
| `HYPERGLYCEMIA` | `79029`, `25000`, `25002` | `R739`, `E0865`, `E0965`, `E1065`, `E1165`, `E1365` |
| `HYPOGLYCEMIA` | `2510`, `2511`, `2512`, `25080`, `25082` | `E0864`, `E0964`, `E1064`, `E1164`, `E1364`, `E162` |
| `KETOACIDOSIS` | gated rule, see below | gated rule, see below |
| `DIABETIC_COMA` | `2502*`, `2503*` | coma sub-codes ending in `.01` or `.11`: `E0801`, `E0811`, `E0901`, `E0911`, `E1001`, `E1011`, `E1101`, `E1111`, `E1301`, `E1311` |
| `ACIDOSIS` | `2762` | `E872` |
| `HYPEROSMOLALITY` | gated rule, see below | gated rule, see below |
| `ATHEROSCLEROSIS` | `440*` | `I70*` |
| `CARDIOVASCULAR_DISORDER` | gated rule, see below | gated rule, see below |
| `KIDNEY_COMPLICATION` | `2504*`, `585*`, `5849` | `N17*`, `N18*`, `N19`, `E0822`, `E0922`, `E1022`, `E1122`, `E1322` |
| `RETINOPATHY` | `2505*`, `3620`, `36201`–`36215` | diabetic ophthalmic prefixes `E0831*`–`E0839*`, `E0931*`–`E0939*`, `E1031*`–`E1039*`, `E1131*`–`E1139*`, `E1331*`–`E1339*`, plus `H35*`, `H36*` |
| `NEUROVASCULAR_COMPLICATION` | `2507*`, `44320`–`44329` | diabetic peripheral circulatory prefixes `E0851`, `E0852`, `E0951`, `E0952`, `E1051`, `E1052`, `E1151`, `E1152`, `E1351`, `E1352`, plus `I7320`–`I7329` |
| `NERVOUS_SYSTEM_DISORDER` | `3572`, `2506*` | diabetic neurologic prefixes `E0840*`–`E0849*`, `E0940*`–`E0949*`, `E1040*`–`E1049*`, `E1140*`–`E1149*`, `E1340*`–`E1349*`, plus `G632` |
| `SKIN_ULCER` | `2508*`, `707*` | `L97*`, `L984*`, `E0862*`, `E0962*`, `E1062*`, `E1162*`, `E1362*` |
| `ACUTE_RESPIRATORY_DISORDER` | `51881`, `51882`, `51884` | `J80`, `J9600`–`J9602`, `J9690`–`J9692` |
| `OTHER_COMPLICATION` | `2509*` | `E1069`, `E1169`, `E1369` |

Use `str.startswith()` for prefix matching where a trailing `*` is shown.

**`CARDIOVASCULAR_DISORDER` gated rule.** This event is emitted iff *both* the diagnosis/procedure gate and the troponin signal fire on the admission. One event per admission, timestamped at the first qualifying troponin draw.

- `cv_diagnosis_gate` = any of:
  - broad CV diagnosis (`diagnoses_icd`): ICD-9 `410`–`414*`, `427*`, `428*`; ICD-10 `I20`–`I25*`, `I48*`, `I49*`, `I50*`
  - ischemic heart disease / MI (`diagnoses_icd`): ICD-9 `410`–`414*`; ICD-10 `I20`–`I25*`
  - coronary intervention / revascularization (`procedures_icd`):
    - PCI — ICD-9 `0066`, `360*`; ICD-10-PCS `0270*`–`0273*`
    - CABG — ICD-9 `361*`; ICD-10-PCS `0210*`–`0213*`
    - Cath — ICD-9 `3721`, `3722`, `3723`; ICD-10-PCS `B210*`, `B211*`
- `troponin_signal` = first `TROPONIN_MEASURE` ≥ 600 ng/L in the admission window (unit handling per the measurement table).
- `emit CARDIOVASCULAR_DISORDER = cv_diagnosis_gate AND troponin_signal`. Without the diagnosis/procedure gate, a bare troponin ≥ 600 can fire on non-cardiac myocardial injury (severe sepsis, PE, CKD, demand ischemia without coronary disease). The gate restricts the event to patients whose elevated troponin is contextually cardiovascular.

**`KETOACIDOSIS` gated rule.** One event per qualifying ketone draw, timestamped at that draw's `charttime`. All three axes must fire (concurrency window ±6 h between the ketone draw and the supporting glucose / pH / bicarbonate rows, since DM is untimed and is treated as always-on for the admission):

- `diabetes_or_glucose_high` =
  - `GLUCOSE_MEASURE` ≥ 180 mg/dL within ±6 h of the ketone draw, **OR**
  - this admission has a `DIABETES_DIAGNOSIS` (ICD-9 `250*`/`249*`; ICD-10 `E08*`/`E09*`/`E10*`/`E11*`/`E13*`)
- `ketones_high` (row presence implies the criterion is met) =
  - `KETONES_SERUM_MEASURE` ≥ 31.2 mg/dL (≈ β-hydroxybutyrate 3 mmol/L, the standard DKA cutoff; zero rows in MIMIC-IV v3.1 because no serum ketone itemid exists), **OR**
  - urine ketones ≥ Small (= 15 on the Negative=0 / Trace=5 / Small=15 / Moderate=40 / Large=80 ladder)
- `metabolic_acidosis` =
  - `PH_MEASURE` < 7.30 within ±6 h of the ketone draw, **OR**
  - `BICARBONATE_MEASURE` < 18 mmol/L within ±6 h of the ketone draw
- `emit KETOACIDOSIS = diabetes_or_glucose_high AND ketones_high AND metabolic_acidosis`.

Threshold rationale: glucose ≥ 180 (vs the older > 250) catches euglycemic-leaning DKA on SGLT2s and partially-treated presentations; the diabetes-diagnosis substitute covers DKA cases where glucose has already been corrected by the time the labs reach the ketone draw.

**`HYPEROSMOLALITY` gated rule (HHS-like).** This concept is the clinical HHS syndrome, not the loose "any hyperosmolar state" marker. One event per qualifying chemistry draw, timestamped at the draw's `charttime`.

- `effective_osmolality = 2 * SODIUM_MEASURE + GLUCOSE_MEASURE / 18` (no BUN term — effective, not total osmolality). Sodium and glucose must come from the **same** `charttime` (same blood draw, not interpolated).
- `HYPEROSMOLALITY` (HHS-like) fires iff:
  - `GLUCOSE_MEASURE` ≥ 600 mg/dL at the draw, **AND**
  - `effective_osmolality` ≥ 320 mOsm/kg at the draw, **AND**
  - `NOT strong_ketotic_acidosis` within ±6 h of the draw
- `strong_ketotic_acidosis` =
  - (`KETONES_SERUM_MEASURE` ≥ 31.2 mg/dL OR urine ketones ≥ Small) **AND**
  - (`PH_MEASURE` < 7.30 OR `BICARBONATE_MEASURE` < 18 mmol/L), all within ±6 h of each other.

The not-DKA gate distinguishes HHS from DKA: both can present with severe hyperglycemia and hyperosmolality, but HHS is by definition without significant ketoacidosis. Without this gate the event would fire on DKA cases that also happen to be hyperosmolar, conflating two clinically distinct complications. The loose "any measured/calculated osm > 300" path was removed — it fires on plain dehydration and is not the clinical target.

---

## Coverage Goal

The tak-repo currently contains **65 concepts** including `BASE_GLUCOSE_MEASURE`. The pipeline should attempt to emit raw rows for **every** concept that has any signal in MIMIC-IV — do not silently skip a concept just because the mapping is approximate. `KETONES_SERUM_MEASURE` is expected to emit zero rows in MIMIC-IV v3.1 because no serum ketone dictionary item exists. `E-GFR_MEASURE` is derived from timestamped creatinine if native eGFR lab rows are unavailable.

### Pipeline extension: `BASE_GLUCOSE_MEASURE`
`BASE_GLUCOSE_MEASURE` is required by this pipeline (see the measurement-mapping table for the derivation rule) and should exist as a raw-concept attribute in `tak-repo-portable.json`.

Time-of-day note: MIMIC-IV de-identification shifts each patient's calendar dates by a random per-patient offset but **preserves time-of-day, day-of-week, season, and within-patient intervals**. So the `MEAL` time-of-day heuristic, ICU shift patterns, and night-vs-day labs are all meaningful.

---

## Implementation Notes

### Script Structure (suggested)

```
mimic_pipeline.py
├── Step 0: Load all CSV.GZ files with pandas (compression="gzip")
├── Step 1: Build admission frame (admissions × patients join, compute age via anchor)
├── Step 2: Build context_data table
├── Step 3: Build concept_events table
│   ├── 3a: ADMISSION / RELEASE / DEATH events
│   ├── 3b: Lab measurements (labevents)
│   ├── 3c: Chart measurements (chartevents — Metavision itemids only)
│   ├── 3d: Drug administrations (inputevents primary, EMAR secondary; never prescriptions)
│   └── 3e: Diagnosis-based events (diagnoses_icd, both icd_version 9 and 10)
├── Step 4: Filter to admission window
├── Step 5: Validate concept names against tak-repo
└── Step 6: Export context_data.csv and concept_events.csv
```

### Key Constraints
1. **ConceptName validation**: the tak-repo JSON is the output contract. If a tak-repo concept has zero emitted rows, print a warning. If the pipeline emits rows for a concept absent from tak-repo, print a warning and auto-omit those rows before writing `concept_events.csv`. Do not omit valid concepts solely because they have low patient support; downstream models decide how to handle sparse concepts.
2. **Admission window filtering**: drop any event where `StartDateTime < admittime` or `StartDateTime > max(dischtime, deathtime)`.
3. **EndDateTime**: always `StartDateTime + pd.Timedelta(seconds=1)`.
4. **ICD code format**: stored as string without decimal, in both ICD-9 and ICD-10. Always check `icd_version` before applying a pattern.
5. **LABEVENTS with null hadm_id**: link by `subject_id` + `charttime` within `[admittime, dischtime]`, or skip if ambiguous.
6. **Units consistency**: convert all temperatures to °C, all weights to kg before emitting.
7. **Dataset size**: MIMIC-IV v3.1 has ~364k admissions; `chartevents.csv.gz` and `labevents.csv.gz` are multi-GB. Read in chunks or filter by `itemid` whitelist while loading (`usecols`, `dtype`, `chunksize`) to keep memory bounded.
8. **Itemid era**: MIMIC-IV is Metavision-only — do not use the MIMIC-III CareVue itemids (e.g., `211`, `51`, `442`, `30105`); they will not exist.

### Output File Format
- `context_data.csv`: comma-separated, header row, one row per admission
- `concept_events.csv`: comma-separated, header row, datetime format `YYYY-MM-DD HH:MM:SS`

---

## What This Pipeline Does NOT Need to Do
- Achieve perfect clinical accuracy — approximate mappings are acceptable for testing
- Cover every possible MIMIC-IV item ID — focus on the most common/obvious ones per concept
- Handle missing patients gracefully beyond median imputation and filtering per the inclusion criteria
- Be real-time or perfectly performant — a chunked pandas script is fine

---

## Files

```
mimic-dataset/
├── mimic-iv/physionet.org/files/mimiciv/3.1/    ← input CSVs (gzipped)
│   ├── hosp/
│   │   ├── admissions.csv.gz
│   │   ├── patients.csv.gz
│   │   ├── labevents.csv.gz
│   │   ├── prescriptions.csv.gz
│   │   ├── diagnoses_icd.csv.gz
│   │   ├── d_labitems.csv.gz
│   │   ├── emar.csv.gz
│   │   └── ...
│   └── icu/
│       ├── chartevents.csv.gz
│       ├── inputevents.csv.gz
│       ├── d_items.csv.gz
│       ├── icustays.csv.gz
│       └── ...
├── tak-repo-portable.json               ← portable TAK repo; raw-concept attributes define allowed ConceptName values
├── CLAUDE.md                            ← this file
├── mimic_pipeline.py                    ← to be ported MIMIC-III → MIMIC-IV
└── output/
    ├── context_data.csv                 ← output table 1
    └── concept_events.csv               ← output table 2
```
