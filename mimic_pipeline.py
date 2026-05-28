"""
MIMIC-IV v3.1 → context_data.csv + concept_events.csv

Implements the spec in CLAUDE.md:
- ICU-only cohort (admissions with >=1 icustay), 48-336h window, adults 18+
- Inclusion gated on >=2 GLUCOSE_MEASURE in first 48h OR a diabetes diagnosis
- Excludes gestational diabetes
- DEATH/RELEASE terminus rule (incl. 30-day post-discharge mortality)
- MEAL from PO Intake with time-of-day → Breakfast/Lunch/Dinner/Night-Snack
- Drug administration: inputevents (with dose) + EMAR for PO/SC (Value="True")
- Unit normalization per canonical unit; plausibility filtering for vitals
- BASE_GLUCOSE_MEASURE (HbA1c-derived eAG, else first glucose)
"""
import os
import json
import gzip
import warnings
import numpy as np
import pandas as pd
from tqdm import tqdm

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
ROOT     = "c:/Users/shaha/Work/Personal/mimic-dataset"
MIMIC    = f"{ROOT}/mimic-iv/physionet.org/files/mimiciv/3.1"
HOSP     = f"{MIMIC}/hosp"
ICU      = f"{MIMIC}/icu"
TAK_REPO = f"{ROOT}/rawconcept-tak-repo-portable.json"
OUT_DIR  = f"{ROOT}/output"
os.makedirs(OUT_DIR, exist_ok=True)

CHUNK_LAB   = 2_000_000   # labevents.csv.gz is ~2.6GB
CHUNK_CHART = 5_000_000   # chartevents.csv.gz is ~3.5GB
MIN_CONCEPT_PATIENT_SUPPORT_PCT = 1.0

# ─────────────────────────────────────────────────────────────────────────────
# Valid concepts (from tak-repo + extension)
# ─────────────────────────────────────────────────────────────────────────────
with open(TAK_REPO, encoding="utf-8") as f:
    tak = json.load(f)
TAK_KEYS = set(tak["taks"].keys())
VALID_CONCEPTS = TAK_KEYS

# ─────────────────────────────────────────────────────────────────────────────
# Itemid maps — all verified against d_items.csv.gz / d_labitems.csv.gz
# ─────────────────────────────────────────────────────────────────────────────
# Lab items
LAB_GLUCOSE    = {50931, 50809, 52027, 52569}          # Glucose (Chem + BG + Whole-Blood)
LAB_BICARB     = {50882, 50803, 52039}                 # Bicarbonate + Calc Bicarb
LAB_PH         = {50820}
LAB_POTASSIUM  = {50971, 50822, 52452, 52610}
LAB_SODIUM     = {50983, 50824, 52455, 52623}
LAB_CREAT      = {50912, 52024, 52546}
LAB_ALBUMIN    = {50862, 52022, 53085}
LAB_ALT        = {50861, 53084}
LAB_AST        = {50878, 53088}                        # MIMIC types "Asparate" (sic)
LAB_HCT        = {51221, 50810, 51638, 51639, 52028}   # Hematocrit + Calculated
LAB_HGB        = {51222, 50811, 50855, 51640, 51645}   # Hemoglobin (not A1c)
LAB_PLT        = {51265, 53189}                        # Platelet Count
LAB_WBC        = {51301, 51300, 51755, 51756}          # WBC variants
LAB_NEUT_ABS   = {52075, 53159}                        # Absolute Neutrophil Count (51256 is %)
LAB_TROPONIN   = {51003, 51002, 52642}                 # Trop T + Trop I
LAB_UREA       = {51006, 52647}
LAB_KETONE_UR  = {51484, 51984}                        # Urine ketone (qualitative)
LAB_KETONE_SER = set()                                 # No serum ketone item exists in MIMIC-IV d_labitems
LAB_CK         = {50910}                               # Creatine Kinase (CK) — exclude isoenzymes
LAB_HBA1C      = {50852, 51631, 50854}                 # %A1c, Glycated Hgb, Absolute A1c
LAB_EGFR       = {50920, 51770, 52026}                 # MDRD only (CKD-EPI variants excluded)
LAB_OSM        = {50964}                               # Osmolality, measured (mOsm/kg) — used for HYPEROSMOLALITY
# NOTE: Serum ketone lab does NOT exist in MIMIC-IV d_labitems → KETONES_SERUM_MEASURE = 0 rows.

LAB_ITEMIDS_ALL = (
    LAB_GLUCOSE | LAB_BICARB | LAB_PH | LAB_POTASSIUM | LAB_SODIUM
    | LAB_CREAT | LAB_ALBUMIN | LAB_ALT | LAB_AST | LAB_HCT | LAB_HGB
    | LAB_PLT | LAB_WBC | LAB_NEUT_ABS | LAB_TROPONIN | LAB_UREA
    | LAB_KETONE_UR | LAB_CK | LAB_HBA1C | LAB_EGFR | LAB_OSM
)

# Chart items (Metavision)
CHART_GLUCOSE   = {220621, 226537, 225664, 228388}     # serum/whole-blood/fingerstick/soft
CHART_HR        = {220045}
CHART_SBP       = {220179, 220050}                     # NIBP sys, ABP sys
CHART_DBP       = {220180, 220051}                     # NIBP dia, ABP dia
CHART_TEMP_F    = {223761}
CHART_TEMP_C    = {223762, 226329, 229236}             # Celsius + blood-temp CCO + cerebral
CHART_WEIGHT_KG = {224639, 226512, 226846}             # Daily / Admit / Feeding (all kg)
CHART_WEIGHT_LB = {226531}
CHART_HEIGHT_CM = {226730}
CHART_HEIGHT_IN = {226707}

CHART_ITEMIDS_ALL = (
    CHART_GLUCOSE | CHART_HR | CHART_SBP | CHART_DBP
    | CHART_TEMP_F | CHART_TEMP_C
    | CHART_WEIGHT_KG | CHART_WEIGHT_LB
    | CHART_HEIGHT_CM | CHART_HEIGHT_IN
)

# Input items (Metavision, all linksto='inputevents')
INS_REGULAR   = {223258}                               # Regular insulin; exclude U500 from IV dosage
INS_BASAL     = {223259, 223260}                       # NPH, Glargine
INS_BOLUS     = {223262, 229299, 223261, 223257}       # Humalog, Novolog, Humalog 75/25, 70/30
HEPARIN_IV    = {225152, 225975, 229597, 230044}       # generic, prophylaxis, Impella, CRRT
DEXTROSE      = {220949, 220950, 220952, 228140, 228141, 228142, 225947}  # 5/10/20/30/40/50% + PN
BICARB_DRUG   = {220995, 225165, 227533, 221211}       # 8.4%/base/8.4%Amp/1.4%
CA_GLUCONATE  = {221456, 227525, 229640}               # generic, CRRT, bolus
HTS_SALINE    = {225161, 228341}                       # NaCl 3%, NaCl 23.4%
PO_INTAKE     = {226452, 226377}                       # PO Intake, PACU PO Intake
# Steroids do NOT exist as inputevents itemids in MIMIC-IV → fall back to EMAR + pharmacy.route.
STEROIDS_IV   = set()

# Antibiotic IV itemids — verified by d_items category='Antibiotics'
ABX_IV = {
    225798, 225837, 225838, 225840, 225842, 225843, 225844, 225845,
    225847, 225848, 225850, 225851, 225853, 225855, 225857, 225859,
    225860, 225862, 225863, 225865, 225866, 225868, 225869, 225871,
    225873, 225875, 225876, 225877, 225879, 225881, 225882, 225883,
    225884, 225885, 225886, 225888, 225889, 225890, 225892, 225893,
    225895, 225896, 225897, 225898, 225899, 225900, 225902, 225903,
    225905, 227691, 228003, 229059, 229061, 229064, 229587,
}

# Drug name patterns for EMAR (case-insensitive substring)
ABX_PATTERNS = (
    "ceftriaxone|cefazolin|cefepime|vancomycin|piperacillin|meropenem|imipenem|"
    "ciprofloxacin|levofloxacin|metronidazole|azithromycin|amoxicillin|ampicillin|"
    "linezolid|daptomycin|clindamycin|trimethoprim|sulfamethoxazole|nitrofurantoin|"
    "tetracycline|doxycycline|acyclovir|ambisome|amikacin|atovaquone|aztreonam|"
    "caspofungin|ceftazidime|chloroquine|colistin|erythromycin|ethambutol|"
    "fluconazole|foscarnet|ganciclovir|gancyclovir|gentamicin|isoniazid|"
    "mefloquine|micafungin|moxifloxacin|nafcillin|oxacillin|penicillin|"
    "pyrazinamide|quinine|ribavirin|rifampin|bactrim|tobramycin|valganciclovir|"
    "valgancyclovir|voriconazole|keflex|tamiflu|chloramphenicol|ertapenem|"
    "tigecycline|ceftaroline"
)
METFORMIN_PATTERN    = r"metformin"
ANTIDIAB_PATTERN     = (
    "glipizide|glyburide|glimepiride|sitagliptin|saxagliptin|alogliptin|linagliptin|"
    "exenatide|liraglutide|dulaglutide|semaglutide|pioglitazone"
)
SGLT2_PATTERN        = (
    "dapagliflozin|canagliflozin|empagliflozin|ertugliflozin|farxiga|invokana|"
    "jardiance|steglatro|synjardy|xigduo|glyxambi|sotagliflozin|bexagliflozin"
)
KBINDER_PATTERN      = (
    "kayexalate|sodium polystyrene|polystyrene sulfonate|patiromer|veltassa|"
    "lokelma|sodium zirconium|zirconium cyclosilicate"
)
STEROIDS_PO_PATTERN  = "methylprednisolone|hydrocortisone|dexamethasone|prednisone|prednisolone"
HEPARIN_PATTERN      = "heparin"

# EMAR administered statuses
EMAR_ADMIN_STATUSES = {
    "Administered", "Confirmed", "Administered in Other Location",
    "Partial Administered", "Restarted",
}

# Plausibility ranges (drop rows outside)
RANGES = {
    "GLUCOSE_MEASURE":           (10, 1500),
    "BICARBONATE_MEASURE":       (3, 50),
    "PH_MEASURE":                (6.5, 8.0),
    "POTASSIUM_MEASURE":         (1.0, 10.0),
    "SODIUM_MEASURE":            (90, 200),
    "CREATININE_SERUM_MEASURE":  (0.1, 30),
    "ALBUMIN_SERUM_MEASURE":     (0.5, 7.0),
    "ALANINE-AMINOTRANSFERASE_MEASURE": (1, 10_000),
    "ASPARATE-AMINOTRANSFERASE_MEASURE": (1, 10_000),
    "HEMATOCRIT_MEASURE":        (5, 75),
    "HEMOGLOBIN_MEASURE":        (2, 25),
    "PLT_MEASURE":               (1, 2000),
    "INFECTION_WBC_MEASURE":     (0.1, 500),
    "NEUTROPHILS_MEASURE":       (0.0, 500),
    "TROPONIN_MEASURE":          (0, 200_000),
    "UREA_MEASURE":              (1, 300),
    "CREATINE-KINASE_MEASURE":   (1, 100_000),
    "HEART_RATE_MEASURE":        (20, 250),
    "BLOOD_PRESSURE_SYSTOLIC_MEASURE":  (40, 280),
    "BLOOD_PRESSURE_DIASTOLIC_MEASURE": (20, 200),
    "BODY_TEMPERATURE":          (25, 45),
    "WEIGHT_MEASURE":            (20, 400),
    "BMI_MEASURE":               (10, 80),
    "BASE_GLUCOSE_MEASURE":      (10, 1500),
    "E-GFR_MEASURE":             (0, 300),
}

DOSE_RANGES = {
    "INSULIN_IV_DOSAGE": (0.01, 100),
    "BASAL_DOSAGE": (1, 300),
    "BOLUS_DOSAGE": (1, 150),
}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def load_gz(path, parse_dates=None, **kw):
    """Load a gzipped CSV with lowercase column names and optional date parsing."""
    df = pd.read_csv(path, compression="gzip", **kw)
    df.columns = [c.lower() for c in df.columns]
    if parse_dates:
        for c in parse_dates:
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def make_event_df(patient_ids, concept, times, values):
    """Build a concept_events fragment in canonical schema."""
    df = pd.DataFrame({
        "PatientId":     patient_ids,
        "ConceptName":   concept,
        "StartDateTime": pd.to_datetime(times),
        "Value":         values,
    })
    df = df.dropna(subset=["PatientId", "StartDateTime"])
    df["EndDateTime"] = df["StartDateTime"] + pd.Timedelta(seconds=1)
    return df[["PatientId", "ConceptName", "StartDateTime", "EndDateTime", "Value"]]


def plausible(df, concept):
    """Drop rows whose numeric Value is outside the plausibility range for this concept."""
    lo, hi = RANGES.get(concept, (None, None))
    if lo is None:
        return df
    v = pd.to_numeric(df["Value"], errors="coerce")
    return df[(v >= lo) & (v <= hi)].copy()


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Build admission cohort
# ─────────────────────────────────────────────────────────────────────────────
print("[1/9] Loading core admission / patient / icustay tables...")
adm = load_gz(f"{HOSP}/admissions.csv.gz",
              parse_dates=["admittime", "dischtime", "deathtime"])
pat = load_gz(f"{HOSP}/patients.csv.gz", parse_dates=["dod"])
icu = load_gz(f"{ICU}/icustays.csv.gz", parse_dates=["intime", "outtime"])

# Compute age via anchor convention
adm = adm.merge(pat[["subject_id", "gender", "anchor_age", "anchor_year", "dod"]],
                on="subject_id", how="left")
adm["age_at_admission"] = adm["anchor_age"] + (adm["admittime"].dt.year - adm["anchor_year"])
adm.loc[adm["age_at_admission"] > 120, "age_at_admission"] = 91

# Window length
adm["end_time_raw"] = np.where(
    adm["hospital_expire_flag"] == 1,
    adm["deathtime"].combine_first(adm["dischtime"]),
    adm["dischtime"]
)
adm["end_time_raw"] = pd.to_datetime(adm["end_time_raw"])
adm["window_h"] = (adm["end_time_raw"] - adm["admittime"]).dt.total_seconds() / 3600

# Adults only
adm = adm[adm["age_at_admission"].fillna(0) >= 18].copy()
# 48–336h window, valid timestamps
adm = adm[adm["admittime"].notna() & adm["end_time_raw"].notna()].copy()
adm = adm[(adm["window_h"] >= 48) & (adm["window_h"] <= 336)].copy()

# Must have an ICU stay
icu_hadm = set(icu["hadm_id"].dropna().astype(int).tolist())
adm = adm[adm["hadm_id"].isin(icu_hadm)].copy()

print(f"  after window+age+ICU filters: {len(adm)} admissions")

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Diagnoses-based filters (gestational DM exclusion, DM-or-glucose gate)
# ─────────────────────────────────────────────────────────────────────────────
print("[2/9] Loading diagnoses for cohort filtering...")
diag = load_gz(f"{HOSP}/diagnoses_icd.csv.gz", dtype={"icd_code": str})
diag["icd_code"] = diag["icd_code"].astype(str).str.strip()
diag["icd_version"] = diag["icd_version"].astype(int)

def icd_hadm_set(icd9_prefixes=(), icd10_prefixes=()):
    """Return set of hadm_ids with any ICD9 (icd_version=9) or ICD10 prefix match."""
    m9  = (diag["icd_version"] == 9)  & diag["icd_code"].str.startswith(tuple(icd9_prefixes))  if icd9_prefixes  else pd.Series(False, index=diag.index)
    m10 = (diag["icd_version"] == 10) & diag["icd_code"].str.startswith(tuple(icd10_prefixes)) if icd10_prefixes else pd.Series(False, index=diag.index)
    return set(diag.loc[m9 | m10, "hadm_id"].dropna().astype(int).tolist())

# Gestational diabetes exclusion
gest_hadm = icd_hadm_set(icd9_prefixes=("6488",), icd10_prefixes=("O24",))
adm = adm[~adm["hadm_id"].isin(gest_hadm)].copy()

# Diabetes diagnosis (any type) — used to gate inclusion alongside glucose count
dm_hadm = icd_hadm_set(
    icd9_prefixes=("250", "249"),
    icd10_prefixes=("E08", "E09", "E10", "E11", "E13"),
)

print(f"  after gestational-DM exclusion: {len(adm)} admissions")

valid_hadm_set = set(adm["hadm_id"].astype(int).tolist())

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Load LABEVENTS in chunks (filter by hadm_id + itemid whitelist)
# ─────────────────────────────────────────────────────────────────────────────
print("[3/9] Streaming labevents.csv.gz (filter by hadm_id+itemid)...")
LAB_USECOLS = ["labevent_id", "subject_id", "hadm_id", "itemid", "charttime", "value", "valuenum", "valueuom"]
LAB_DTYPE   = {"labevent_id": "Int64", "subject_id": "Int64", "hadm_id": "Int64", "itemid": "Int64",
               "value": "string", "valuenum": "float64", "valueuom": "string"}

lab_parts = []
adm_lab_windows = adm[["hadm_id", "subject_id", "admittime", "end_time_raw"]].copy()
adm_subjects = set(adm_lab_windows["subject_id"].dropna().astype(int).tolist())
reader = pd.read_csv(f"{HOSP}/labevents.csv.gz", compression="gzip",
                     usecols=LAB_USECOLS, dtype=LAB_DTYPE,
                     parse_dates=["charttime"], chunksize=CHUNK_LAB)
for chunk in tqdm(reader, desc="  labevents", unit="chunk"):
    chunk = chunk[chunk["itemid"].isin(LAB_ITEMIDS_ALL)]
    with_hadm = chunk[chunk["hadm_id"].isin(valid_hadm_set)].copy()

    # MIMIC-IV labevents may have null hadm_id. Recover unambiguous in-admission
    # rows by joining on subject_id and charttime within the admission window.
    missing_hadm = chunk[
        chunk["hadm_id"].isna() & chunk["subject_id"].isin(adm_subjects)
    ].copy()
    if not missing_hadm.empty:
        linked = missing_hadm.merge(
            adm_lab_windows,
            on="subject_id",
            how="inner",
            suffixes=("", "_linked"),
        )
        linked = linked[
            (linked["charttime"] >= linked["admittime"])
            & (linked["charttime"] <= linked["end_time_raw"])
        ].copy()
        linked = linked.drop_duplicates("labevent_id", keep=False)
        if not linked.empty:
            linked["hadm_id"] = linked["hadm_id_linked"].astype("Int64")
            with_hadm = pd.concat([with_hadm, linked[LAB_USECOLS]], ignore_index=True)

    if not with_hadm.empty:
        lab_parts.append(with_hadm)
lab = pd.concat(lab_parts, ignore_index=True) if lab_parts else pd.DataFrame(columns=LAB_USECOLS)
del lab_parts
print(f"  labevents kept: {len(lab):,} rows")

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Glucose-OR-diabetes inclusion gate (within first 48h)
# ─────────────────────────────────────────────────────────────────────────────
print("[4/9] Applying glucose/diabetes inclusion gate...")
adm_48h = adm[["hadm_id", "admittime"]].copy()
adm_48h["w48"] = adm_48h["admittime"] + pd.Timedelta(hours=48)

gluc_lab = lab[lab["itemid"].isin(LAB_GLUCOSE) & lab["valuenum"].notna()].merge(adm_48h, on="hadm_id")
gluc_lab = gluc_lab[(gluc_lab["charttime"] >= gluc_lab["admittime"]) & (gluc_lab["charttime"] <= gluc_lab["w48"])]
gluc_counts = gluc_lab.groupby("hadm_id").size()
hadm_with_2glucose = set(gluc_counts[gluc_counts >= 2].index.astype(int).tolist())

include_hadm = hadm_with_2glucose | (dm_hadm & valid_hadm_set)
adm = adm[adm["hadm_id"].isin(include_hadm)].copy()
valid_hadm_set = set(adm["hadm_id"].astype(int).tolist())
lab = lab[lab["hadm_id"].isin(valid_hadm_set)].copy()
print(f"  cohort after inclusion gate: {len(adm)} admissions")

# ─────────────────────────────────────────────────────────────────────────────
# Step 5: Build admission lookup, DEATH/RELEASE terminus
# ─────────────────────────────────────────────────────────────────────────────
print("[5/9] Computing admission windows and terminus events...")

# DEATH terminus rule
def compute_terminus(row):
    """
    Returns (terminus_type, terminus_time). Per CLAUDE.md:
      1) hospital_expire_flag=1 → DEATH at deathtime (or dischtime if null)
      2) dod within [dischtime, dischtime+30d] → DEATH at dod
      3) else RELEASE at dischtime
    """
    if row["hospital_expire_flag"] == 1:
        t = row["deathtime"] if pd.notna(row["deathtime"]) else row["dischtime"]
        return ("DEATH", t)
    dod = row["dod"]
    disch = row["dischtime"]
    if pd.notna(dod) and pd.notna(disch) and disch <= dod <= disch + pd.Timedelta(days=30):
        return ("DEATH", dod)
    return ("RELEASE", disch)

term = adm.apply(compute_terminus, axis=1, result_type="expand")
term.columns = ["terminus_type", "terminus_time"]
adm = pd.concat([adm.reset_index(drop=True), term.reset_index(drop=True)], axis=1)

# end_time for window filtering: stretch to terminus_time so 30d-post-discharge DEATH fits
adm["end_time"] = adm[["end_time_raw", "terminus_time"]].max(axis=1)
adm["end_time"] = pd.to_datetime(adm["end_time"])

adm_lookup = adm.set_index("hadm_id")[["admittime", "end_time", "subject_id"]].to_dict("index")

def filter_window(df, time_col="StartDateTime"):
    """Restrict events to [admittime, end_time] of their admission."""
    if df.empty:
        return df
    df = df[df["PatientId"].isin(valid_hadm_set)].copy()
    df["_a"] = df["PatientId"].map(lambda h: adm_lookup.get(int(h), {}).get("admittime"))
    df["_e"] = df["PatientId"].map(lambda h: adm_lookup.get(int(h), {}).get("end_time"))
    df["_a"] = pd.to_datetime(df["_a"]); df["_e"] = pd.to_datetime(df["_e"])
    df = df[(df[time_col] >= df["_a"]) & (df[time_col] <= df["_e"])].drop(columns=["_a", "_e"])
    return df

all_events = []

# ADMISSION
all_events.append(make_event_df(adm["hadm_id"], "ADMISSION", adm["admittime"], "True"))
# DEATH / RELEASE (mutually exclusive per row, per the terminus rule)
for term_type in ("DEATH", "RELEASE"):
    sub = adm[adm["terminus_type"] == term_type]
    if not sub.empty:
        all_events.append(make_event_df(sub["hadm_id"], term_type, sub["terminus_time"], "True"))

# ─────────────────────────────────────────────────────────────────────────────
# Step 6: Diagnosis-based concept_events (timestamped at admittime)
# ─────────────────────────────────────────────────────────────────────────────
print("[6/9] Building diagnosis/complication events...")
diag_in_cohort = diag[diag["hadm_id"].isin(valid_hadm_set)].copy()
diag_in_cohort = diag_in_cohort.merge(adm[["hadm_id", "admittime"]], on="hadm_id", how="left")

def emit_icd(concept, icd9_prefixes=(), icd10_prefixes=()):
    m9  = (diag_in_cohort["icd_version"] == 9)  & diag_in_cohort["icd_code"].str.startswith(tuple(icd9_prefixes))  if icd9_prefixes else pd.Series(False, index=diag_in_cohort.index)
    m10 = (diag_in_cohort["icd_version"] == 10) & diag_in_cohort["icd_code"].str.startswith(tuple(icd10_prefixes)) if icd10_prefixes else pd.Series(False, index=diag_in_cohort.index)
    sub = diag_in_cohort[m9 | m10].drop_duplicates("hadm_id")
    if sub.empty:
        return
    all_events.append(make_event_df(sub["hadm_id"], concept, sub["admittime"], "True"))

emit_icd("DIABETES_DIAGNOSIS",        icd9_prefixes=("250", "249"),                 icd10_prefixes=("E08", "E09", "E10", "E11", "E13"))

# ICD-derived complication/infection events disabled.
# Rationale: diagnoses_icd has no per-row timestamp — codes are billing diagnoses attached
# to the admission as a whole. Stamping them at admittime made every "outcome" land on
# t=0 of the stay, eliminating any in-hospital onset signal and conflating community-
# acquired vs hospital-acquired disease. ACIDOSIS and HYPEROSMOLALITY are now derived
# from time-stamped labs below (Step 7b). The chronic conditions (atherosclerosis,
# cardiovascular, kidney, retinopathy, neuro-* , skin ulcer, etc.) cannot be reliably
# time-resolved from MIMIC-IV and are intentionally dropped from concept_events.
# emit_icd("HYPERGLYCEMIA",             icd9_prefixes=("79029", "25000", "25002"),    icd10_prefixes=("R739", "E0865", "E0965", "E1065", "E1165", "E1365"))
# emit_icd("HYPOGLYCEMIA",              icd9_prefixes=("2510", "2511", "2512", "25080", "25082"), icd10_prefixes=("E0864", "E0964", "E1064", "E1164", "E1364", "E162"))
# emit_icd("KETOACIDOSIS",              icd9_prefixes=("2501",),                      icd10_prefixes=("E0810", "E0811", "E0910", "E0911", "E1010", "E1011", "E1110", "E1111", "E1310", "E1311"))
# emit_icd("DIABETIC_COMA",             icd9_prefixes=("2502", "2503"),               icd10_prefixes=("E0801", "E0811", "E0901", "E0911", "E1001", "E1011", "E1101", "E1111", "E1301", "E1311"))
# emit_icd("ACIDOSIS",                  icd9_prefixes=("2762",),                      icd10_prefixes=("E872",))            # replaced by lab-derived pH<7.35
# emit_icd("HYPEROSMOLALITY",           icd9_prefixes=("2760",),                      icd10_prefixes=("E870",))            # replaced by lab-derived osmolality>295
# emit_icd("ATHEROSCLEROSIS",           icd9_prefixes=("440",),                       icd10_prefixes=("I70",))
# emit_icd("CARDIOVASCULAR_DISORDER",   icd9_prefixes=("410", "411", "412", "413", "414", "427", "428"),
#                                       icd10_prefixes=("I20", "I21", "I22", "I23", "I24", "I25", "I48", "I49", "I50"))
# emit_icd("KIDNEY_COMPLICATION",       icd9_prefixes=("2504", "585", "5849"),        icd10_prefixes=("N17", "N18", "N19", "E0822", "E0922", "E1022", "E1122", "E1322"))
# emit_icd("RETINOPATHY",               ...)
# emit_icd("NEUROVASCULAR_COMPLICATION", ...)
# emit_icd("NERVOUS_SYSTEM_DISORDER",   ...)
# emit_icd("SKIN_ULCER",                ...)
# emit_icd("ACUTE_RESPIRATORY_DISORDER", ...)
# emit_icd("INFECTION",                 ...)
# emit_icd("OTHER_COMPLICATION",        ...)

# ─────────────────────────────────────────────────────────────────────────────
# Step 7: Lab-based measurement events (unit normalization + plausibility)
# ─────────────────────────────────────────────────────────────────────────────
print("[7/9] Emitting lab measurements...")
lab["uom_l"] = lab["valueuom"].fillna("").str.lower().str.strip()

def lab_measure(concept, itemids, canonical_uoms=None, conversion=None):
    """
    Emit lab measurement events for a concept.
    canonical_uoms: iterable of acceptable lowercased uom strings (after stripping).
                    If None, accept any.
    conversion: callable (row -> new_value) applied AFTER unit filtering.
    """
    sub = lab[lab["itemid"].isin(itemids) & lab["valuenum"].notna()].copy()
    if canonical_uoms is not None:
        keep = sub["uom_l"].apply(lambda u: any(k in u for k in canonical_uoms))
        sub = sub[keep].copy()
    if conversion is not None:
        sub["valuenum"] = sub.apply(conversion, axis=1)
    if sub.empty:
        return
    df = make_event_df(sub["hadm_id"], concept, sub["charttime"], sub["valuenum"].astype(float))
    df = plausible(df, concept)
    all_events.append(filter_window(df))

lab_measure("GLUCOSE_MEASURE",                LAB_GLUCOSE,    canonical_uoms={"mg/dl"})
lab_measure("BICARBONATE_MEASURE",            LAB_BICARB,     canonical_uoms={"meq/l", "mmol/l"})
lab_measure("PH_MEASURE",                     LAB_PH)                                    # unitless
lab_measure("POTASSIUM_MEASURE",              LAB_POTASSIUM,  canonical_uoms={"meq/l", "mmol/l"})
lab_measure("SODIUM_MEASURE",                 LAB_SODIUM,     canonical_uoms={"meq/l", "mmol/l"})
lab_measure("CREATININE_SERUM_MEASURE",       LAB_CREAT,      canonical_uoms={"mg/dl"})
lab_measure("ALBUMIN_SERUM_MEASURE",          LAB_ALBUMIN,    canonical_uoms={"g/dl"})
lab_measure("ALANINE-AMINOTRANSFERASE_MEASURE", LAB_ALT,      canonical_uoms={"u/l", "iu/l"})
lab_measure("ASPARATE-AMINOTRANSFERASE_MEASURE", LAB_AST,     canonical_uoms={"u/l", "iu/l"})
lab_measure("HEMATOCRIT_MEASURE",             LAB_HCT,        canonical_uoms={"%"})
lab_measure("HEMOGLOBIN_MEASURE",             LAB_HGB,        canonical_uoms={"g/dl"})
lab_measure("PLT_MEASURE",                    LAB_PLT,        canonical_uoms={"k/ul", "10*3/ul", "10^3/ul", "10e3/ul"})
lab_measure("INFECTION_WBC_MEASURE",          LAB_WBC,        canonical_uoms={"k/ul", "10*3/ul", "10^3/ul", "10e3/ul"})
lab_measure("NEUTROPHILS_MEASURE",            LAB_NEUT_ABS,   canonical_uoms={"k/ul", "10*3/ul", "10^3/ul", "10e3/ul"})

# Troponin: convert ng/mL → ng/L (*1000); accept ng/L too
def trop_conv(r):
    v = r["valuenum"]
    if "ng/ml" in r["uom_l"]:
        return v * 1000.0
    return v
lab_measure("TROPONIN_MEASURE", LAB_TROPONIN, canonical_uoms={"ng/ml", "ng/l"}, conversion=trop_conv)

lab_measure("UREA_MEASURE",                   LAB_UREA,       canonical_uoms={"mg/dl"})
lab_measure("KETONES_SERUM_MEASURE",          LAB_KETONE_SER, canonical_uoms={"mg/dl"})

# KETONES_URINE: MIMIC-IV stores these mostly as numeric mg/dL strings ("10","15","40",
# "80","150") with a minority of qualitative abbreviations ("NEG","TR","TRACE"). The
# canonical 5-tier ladder (Neg/Trace/Small/Moderate/Large) is a fallback for the
# qualitative cases; numeric strings are parsed directly. "___" and other junk → dropped.
KU_MAP = {
    "negative": 0, "neg": 0, "n": 0,
    "trace": 5, "tr": 5, "tra": 5, "t": 5,
    "small": 15, "moderate": 40, "large": 80,
}
def _parse_ku(v):
    """Return numeric mg/dL from a raw urine-ketone string, else NaN."""
    if v is None:
        return float("nan")
    s = str(v).strip().lower()
    if not s or s == "___":
        return float("nan")
    if s in KU_MAP:
        return float(KU_MAP[s])
    # Strip leading >, >=, <, <= for capped readings ("> 80", ">=160")
    s2 = s.lstrip(">=<").strip()
    try:
        return float(s2)
    except ValueError:
        return float("nan")

ku = lab[lab["itemid"].isin(LAB_KETONE_UR)].copy()
if not ku.empty:
    ku["ku_val"] = ku["value"].map(_parse_ku)
    ku = ku[ku["ku_val"].notna()]
    if not ku.empty:
        df = make_event_df(ku["hadm_id"], "KETONES_URINE_MEASURE", ku["charttime"], ku["ku_val"].astype(float))
        all_events.append(filter_window(df))

lab_measure("CREATINE-KINASE_MEASURE",        LAB_CK,         canonical_uoms={"u/l", "iu/l"})

# BASE_GLUCOSE_MEASURE: HbA1c → eAG (preferred), else first GLUCOSE_MEASURE per admission
hba1c = lab[lab["itemid"].isin(LAB_HBA1C) & lab["valuenum"].notna()].copy()
if not hba1c.empty:
    hba1c["eag"] = hba1c["valuenum"] * 28.7 - 46.7
    df = make_event_df(hba1c["hadm_id"], "BASE_GLUCOSE_MEASURE", hba1c["charttime"], hba1c["eag"].astype(float))
    df = plausible(df, "BASE_GLUCOSE_MEASURE")
    df = filter_window(df)
    all_events.append(df)
    hba1c_hadm = set(df["PatientId"].astype(int).tolist())
else:
    hba1c_hadm = set()

gluc_only = lab[lab["itemid"].isin(LAB_GLUCOSE) & lab["valuenum"].notna() & (lab["uom_l"].str.contains("mg/dl"))].copy()
gluc_only = gluc_only[~gluc_only["hadm_id"].isin(hba1c_hadm)]
first_g = gluc_only.sort_values(["hadm_id", "charttime"]).drop_duplicates("hadm_id", keep="first")
if not first_g.empty:
    df = make_event_df(first_g["hadm_id"], "BASE_GLUCOSE_MEASURE", first_g["charttime"], first_g["valuenum"].astype(float))
    df = plausible(df, "BASE_GLUCOSE_MEASURE")
    all_events.append(filter_window(df))

# E-GFR_MEASURE (MDRD-only itemids 50920/51770/52026 — see LAB_EGFR)
egfr = lab[lab["itemid"].isin(LAB_EGFR) & lab["valuenum"].notna()].copy()
if not egfr.empty:
    df = make_event_df(egfr["hadm_id"], "E-GFR_MEASURE", egfr["charttime"], egfr["valuenum"].astype(float))
    df = plausible(df, "E-GFR_MEASURE")
    all_events.append(filter_window(df))

# ─────────────────────────────────────────────────────────────────────────────
# Step 7b: Lab-derived acute events (timestamped at the lab draw)
#
# These give events a real in-hospital onset time instead of being collapsed to admittime.
# ACIDOSIS is intentionally NOT emitted here: the downstream stage computes it with a
# multi-signal rule (pH < 7.3 + bicarbonate < 10 + insulin-IV within 2h). PH_MEASURE
# and BICARBONATE_MEASURE are already emitted by Step 7, which is the only input that
# stage needs from this pipeline.
#
# HYPEROSMOLALITY criterion:
#   Serum osmolality > 300 mOsm/kg (clinical hyperosmolar alarm point). Two paths,
#   preferring directly measured:
#     1) Measured osmolality (lab itemid 50964) > 300 mOsm/kg.
#     2) Calculated osmolality > 300 from a single chemistry draw (same charttime)
#        using the standard formula:  2*Na + glucose/18 + BUN/2.8
#        (Rasouli, Clin Chem Lab Med 2011; routine reference range 275–295; >300 is
#        the typical clinical alarm threshold for true hyperosmolar state.)
#        Requires Na (mmol/L), glucose (mg/dL), and BUN (mg/dL) all present at the
#        same charttime — same blood draw, not interpolated.
#   Every qualifying timepoint is emitted (not just the first) so the downstream
#   model sees persistence/recurrence. Same-timestamp dupes (measured+calculated
#   from one draw) are collapsed by the global dedup step.
#
# CARDIOVASCULAR_DISORDER criterion (also lab-derived, below `del lab`):
#   See block under troponin handling.
#
# Emissions are Value="True" event rows, timestamped at the lab's charttime, and
# filter_window'd to the admission window.
# ─────────────────────────────────────────────────────────────────────────────
print("[7b/9] Emitting lab-derived HYPEROSMOLALITY / CARDIOVASCULAR_DISORDER...")

# ACIDOSIS-rule candidates — finished after Step 9 once insulin-IV times are loaded.
# Rule: pH <= 7.3 AND bicarbonate <= 10 (within ±1h) AND insulin-IV within ±2h of pH.
ph_acid_cand = lab[lab["itemid"].isin(LAB_PH) & lab["valuenum"].notna()].copy()
ph_acid_cand = ph_acid_cand[(ph_acid_cand["valuenum"] >= 6.5) & (ph_acid_cand["valuenum"] <= 8.0)]
ph_acid_cand = ph_acid_cand[ph_acid_cand["valuenum"] <= 7.3][["hadm_id", "charttime"]].rename(columns={"charttime": "ph_time"})

hco3_low = lab[lab["itemid"].isin(LAB_BICARB) & lab["valuenum"].notna()].copy()
hco3_low = hco3_low[hco3_low["uom_l"].str.contains("mmol|meq")]
hco3_low = hco3_low[hco3_low["valuenum"] <= 10][["hadm_id", "charttime"]].rename(columns={"charttime": "hco3_time"})

# HYPEROSMOLALITY — every measured osmolality > 300, plus every calculated > 300 from same-draw Na+glucose+BUN.
# 300 is the clinical alarm point for hyperosmolar state; 295 is the upper edge of normal and over-fires.
OSM_HI_THRESHOLD = 300.0
osm_meas = lab[lab["itemid"].isin(LAB_OSM) & lab["valuenum"].notna()].copy()
osm_meas = osm_meas[osm_meas["valuenum"] > OSM_HI_THRESHOLD]
if not osm_meas.empty:
    df = make_event_df(osm_meas["hadm_id"], "HYPEROSMOLALITY", osm_meas["charttime"], "True")
    all_events.append(filter_window(df))

# Calculated path — emit at every chemistry draw where osm_calc > 300. Dedup of same-timestamp
# rows (e.g. a draw that ALSO has a measured osmolality) is handled by the global dedup step.
na   = lab[lab["itemid"].isin(LAB_SODIUM)   & lab["valuenum"].notna() & lab["uom_l"].str.contains("mmol|meq")][["hadm_id", "charttime", "valuenum"]].rename(columns={"valuenum": "na"})
glu  = lab[lab["itemid"].isin(LAB_GLUCOSE)  & lab["valuenum"].notna() & lab["uom_l"].str.contains("mg/dl")][["hadm_id", "charttime", "valuenum"]].rename(columns={"valuenum": "glu"})
bun  = lab[lab["itemid"].isin(LAB_UREA)     & lab["valuenum"].notna() & lab["uom_l"].str.contains("mg/dl")][["hadm_id", "charttime", "valuenum"]].rename(columns={"valuenum": "bun"})
draw = na.merge(glu, on=["hadm_id", "charttime"]).merge(bun, on=["hadm_id", "charttime"])
if not draw.empty:
    draw["osm_calc"] = 2 * draw["na"] + draw["glu"] / 18.0 + draw["bun"] / 2.8
    draw_hi = draw[draw["osm_calc"] > OSM_HI_THRESHOLD]
    if not draw_hi.empty:
        df = make_event_df(draw_hi["hadm_id"], "HYPEROSMOLALITY", draw_hi["charttime"], "True")
        all_events.append(filter_window(df))

# CARDIOVASCULAR_DISORDER — every troponin >= 600 ng/L per admission
#
# Criterion: high-sensitivity troponin (T or I) >= 600 ng/L on any lab draw.
#   - URL (99th percentile) for hs-cTn is ~14-40 ng/L (assay-dependent).
#   - >=600 ng/L is well above MI/ACS diagnostic cutoffs and captures clinically
#     significant myocardial injury (large MI, severe demand ischemia, myocarditis,
#     cardiogenic shock), not subtle troponin leak.
#   - We mirror the unit handling of TROPONIN_MEASURE: ng/mL rows are converted to
#     ng/L (*1000); ng/L rows are kept as-is. Other units are dropped.
# Caveat (for downstream interpretation): troponin is order-driven in MIMIC — only
# ~26% of admissions ever get one drawn — so this concept's absence does not imply
# absence of cardiovascular disease, only absence of clinical suspicion + measurement.
trop = lab[lab["itemid"].isin(LAB_TROPONIN) & lab["valuenum"].notna()].copy()
if not trop.empty:
    keep = trop["uom_l"].apply(lambda u: ("ng/ml" in u) or ("ng/l" in u))
    trop = trop[keep].copy()
    trop["v_ngL"] = trop.apply(lambda r: r["valuenum"] * 1000.0 if "ng/ml" in r["uom_l"] else r["valuenum"], axis=1)
    trop_hi = trop[trop["v_ngL"] >= 600]
    if not trop_hi.empty:
        df = make_event_df(trop_hi["hadm_id"], "CARDIOVASCULAR_DISORDER", trop_hi["charttime"], "True")
        all_events.append(filter_window(df))

# KIDNEY_COMPLICATION — lab-derived, time-stamped at the qualifying draw.
#
# Criteria (any one, evaluated per lab row):
#   - Serum creatinine >= 2.0 mg/dL  (probable AKI / advanced CKD; KDIGO AKI stage 2+)
#   - eGFR < 30 mL/min/1.73m^2       (CKD stage 4 or worse)
# KDIGO rise-based criteria (>=0.3 mg/dL absolute, >=1.5x relative) are NOT emitted here:
# TAK computes them downstream as parameterized concepts from CREATININE_SERUM_MEASURE,
# which is already emitted in Step 7. Emitting Value="True" rows here covers the A1
# attribute of the KIDNEY_COMPLICATION_EVENT rule so the OR clause can fire when the
# raw creatinine is itself the trigger.
creat_hi = lab[lab["itemid"].isin(LAB_CREAT) & lab["valuenum"].notna()].copy()
creat_hi = creat_hi[creat_hi["uom_l"].str.contains("mg/dl")]
creat_hi = creat_hi[(creat_hi["valuenum"] >= 2.0) & (creat_hi["valuenum"] <= 30)]
if not creat_hi.empty:
    df = make_event_df(creat_hi["hadm_id"], "KIDNEY_COMPLICATION", creat_hi["charttime"], "True")
    all_events.append(filter_window(df))

egfr_lo = lab[lab["itemid"].isin(LAB_EGFR) & lab["valuenum"].notna()].copy()
egfr_lo = egfr_lo[(egfr_lo["valuenum"] >= 0) & (egfr_lo["valuenum"] < 30)]
if not egfr_lo.empty:
    df = make_event_df(egfr_lo["hadm_id"], "KIDNEY_COMPLICATION", egfr_lo["charttime"], "True")
    all_events.append(filter_window(df))

# KETOACIDOSIS — lab-derived, time-stamped at the qualifying ketone draw.
#
# ADA diagnostic criteria for DKA (all concurrent):
#   1. Hyperglycemia:   glucose > 250 mg/dL
#   2. Acidosis:        arterial pH < 7.30  OR  serum bicarbonate < 18 mmol/L
#   3. Ketonemia:       ketones present (urine ketones >= Small (15 on our ladder);
#                       serum ketones unavailable in MIMIC-IV v3.1)
# Concurrency window: ±6 hours between the ketone draw and the supporting glucose /
# pH / bicarbonate rows — same DKA episode, not interpolated across unrelated days.
# Emit at the ketone charttime; same-time dupes are collapsed by the global dedup.
ket_pos = lab[lab["itemid"].isin(LAB_KETONE_UR)].copy()
if not ket_pos.empty:
    ket_pos["ku_val"] = ket_pos["value"].map(_parse_ku)
    ket_pos = ket_pos[ket_pos["ku_val"].notna() & (ket_pos["ku_val"] >= 15)][["hadm_id", "charttime"]]
    ket_pos = ket_pos.rename(columns={"charttime": "ket_time"})

    glu_hi = lab[lab["itemid"].isin(LAB_GLUCOSE) & lab["valuenum"].notna() & lab["uom_l"].str.contains("mg/dl")]
    glu_hi = glu_hi[glu_hi["valuenum"] > 250][["hadm_id", "charttime"]].rename(columns={"charttime": "glu_time"})

    ph_lo = lab[lab["itemid"].isin(LAB_PH) & lab["valuenum"].notna()]
    ph_lo = ph_lo[(ph_lo["valuenum"] >= 6.5) & (ph_lo["valuenum"] < 7.30)][["hadm_id", "charttime"]].rename(columns={"charttime": "ph_time"})

    hco3_lo = lab[lab["itemid"].isin(LAB_BICARB) & lab["valuenum"].notna() & lab["uom_l"].str.contains("mmol|meq")]
    hco3_lo = hco3_lo[hco3_lo["valuenum"] < 18][["hadm_id", "charttime"]].rename(columns={"charttime": "hco3_time"})

    WIN = pd.Timedelta(hours=6)

    # ketone + glucose concurrency
    kg = ket_pos.merge(glu_hi, on="hadm_id")
    kg = kg[(kg["glu_time"] - kg["ket_time"]).abs() <= WIN][["hadm_id", "ket_time"]].drop_duplicates()

    # ketone + (pH < 7.30 OR bicarb < 18) concurrency
    kp = ket_pos.merge(ph_lo, on="hadm_id")
    kp = kp[(kp["ph_time"] - kp["ket_time"]).abs() <= WIN][["hadm_id", "ket_time"]]
    kb = ket_pos.merge(hco3_lo, on="hadm_id")
    kb = kb[(kb["hco3_time"] - kb["ket_time"]).abs() <= WIN][["hadm_id", "ket_time"]]
    kac = pd.concat([kp, kb], ignore_index=True).drop_duplicates()

    # intersection: ketone draws meeting ALL three axes
    dka = kg.merge(kac, on=["hadm_id", "ket_time"]).drop_duplicates()
    if not dka.empty:
        df = make_event_df(dka["hadm_id"], "KETOACIDOSIS", dka["ket_time"], "True")
        all_events.append(filter_window(df))

del lab  # free memory

# ─────────────────────────────────────────────────────────────────────────────
# Step 8: Stream chartevents (HR, BP, temp, weight, height, glucose, …)
# ─────────────────────────────────────────────────────────────────────────────
print("[8/9] Streaming chartevents.csv.gz...")
CHART_USECOLS = ["hadm_id", "itemid", "charttime", "value", "valuenum", "valueuom"]
CHART_DTYPE   = {"hadm_id": "Int64", "itemid": "Int64",
                 "value": "string", "valuenum": "float64", "valueuom": "string"}

chart_parts = []
reader = pd.read_csv(f"{ICU}/chartevents.csv.gz", compression="gzip",
                     usecols=CHART_USECOLS, dtype=CHART_DTYPE,
                     parse_dates=["charttime"], chunksize=CHUNK_CHART)
for chunk in tqdm(reader, desc="  chartevents", unit="chunk"):
    chunk = chunk[chunk["hadm_id"].isin(valid_hadm_set) & chunk["itemid"].isin(CHART_ITEMIDS_ALL)]
    if not chunk.empty:
        chart_parts.append(chunk)
chart = pd.concat(chart_parts, ignore_index=True) if chart_parts else pd.DataFrame(columns=CHART_USECOLS)
del chart_parts
print(f"  chartevents kept: {len(chart):,} rows")

chart["uom_l"] = chart["valueuom"].fillna("").str.lower().str.strip()

def chart_measure(concept, itemids, canonical_uoms=None, transform=None):
    """Emit chart events for a concept; same semantics as lab_measure."""
    sub = chart[chart["itemid"].isin(itemids) & chart["valuenum"].notna()].copy()
    if canonical_uoms is not None:
        keep = sub["uom_l"].apply(lambda u: any(k in u for k in canonical_uoms))
        sub = sub[keep].copy()
    if transform is not None:
        sub["valuenum"] = sub.apply(transform, axis=1)
    if sub.empty:
        return
    df = make_event_df(sub["hadm_id"], concept, sub["charttime"], sub["valuenum"].astype(float))
    df = plausible(df, concept)
    all_events.append(filter_window(df))

# Bedside glucose itemids are mg/dL by definition; fingerstick rows often have blank valueuom.
chart_measure("GLUCOSE_MEASURE", CHART_GLUCOSE)
chart_measure("HEART_RATE_MEASURE", CHART_HR, canonical_uoms={"bpm"})
chart_measure("BLOOD_PRESSURE_SYSTOLIC_MEASURE",  CHART_SBP, canonical_uoms={"mmhg"})
chart_measure("BLOOD_PRESSURE_DIASTOLIC_MEASURE", CHART_DBP, canonical_uoms={"mmhg"})

# Body temperature: 223761=F (convert), 223762=C
temp_c = chart[chart["itemid"].isin(CHART_TEMP_C) & chart["valuenum"].notna()].copy()
temp_f = chart[chart["itemid"].isin(CHART_TEMP_F) & chart["valuenum"].notna()].copy()
temp_f["valuenum"] = (temp_f["valuenum"] - 32) * 5.0 / 9.0
temp_all = pd.concat([temp_c, temp_f], ignore_index=True)
if not temp_all.empty:
    df = make_event_df(temp_all["hadm_id"], "BODY_TEMPERATURE", temp_all["charttime"], temp_all["valuenum"].astype(float))
    df = plausible(df, "BODY_TEMPERATURE")
    all_events.append(filter_window(df))

# Weight: kg directly + lb→kg
wt_kg = chart[chart["itemid"].isin(CHART_WEIGHT_KG) & chart["valuenum"].notna()].copy()
wt_lb = chart[chart["itemid"].isin(CHART_WEIGHT_LB) & chart["valuenum"].notna()].copy()
wt_lb["valuenum"] = wt_lb["valuenum"] * 0.453592
wt_all = pd.concat([wt_kg, wt_lb], ignore_index=True)
if not wt_all.empty:
    df = make_event_df(wt_all["hadm_id"], "WEIGHT_MEASURE", wt_all["charttime"], wt_all["valuenum"].astype(float))
    df = plausible(df, "WEIGHT_MEASURE")
    all_events.append(filter_window(df))

# BMI: prefer omr.csv.gz `result_name` contains BMI, else compute from height+weight per admission
print("  BMI from omr / chart…")
omr = load_gz(f"{HOSP}/omr.csv.gz", parse_dates=["chartdate"])
omr_bmi = omr[omr["result_name"].str.contains("BMI", case=False, na=False)].copy()
omr_bmi_hadm = set()
if not omr_bmi.empty:
    # Map subject_id to hadm_id via overlap window
    omr_bmi["result_value"] = pd.to_numeric(omr_bmi["result_value"], errors="coerce")
    omr_bmi = omr_bmi.dropna(subset=["result_value"])
    # Join: each omr_bmi row → all admissions of that subject whose window covers chartdate
    adm_for_join = adm[["hadm_id", "subject_id", "admittime", "end_time"]]
    j = omr_bmi.merge(adm_for_join, on="subject_id", how="inner")
    j = j[(j["chartdate"] >= j["admittime"]) & (j["chartdate"] <= j["end_time"])]
    if not j.empty:
        df = make_event_df(j["hadm_id"], "BMI_MEASURE", j["chartdate"], j["result_value"].astype(float))
        df = plausible(df, "BMI_MEASURE")
        df = filter_window(df)
        all_events.append(df)
        omr_bmi_hadm = set(df["PatientId"].astype(int).tolist())

# BMI fallback from charted height and weight for admissions without an OMR BMI.
height_cm = chart[chart["itemid"].isin(CHART_HEIGHT_CM) & chart["valuenum"].notna()].copy()
height_in = chart[chart["itemid"].isin(CHART_HEIGHT_IN) & chart["valuenum"].notna()].copy()
height_in["valuenum"] = height_in["valuenum"] * 2.54
height_all = pd.concat([height_cm, height_in], ignore_index=True)
height_all = height_all[(height_all["valuenum"] >= 100) & (height_all["valuenum"] <= 250)]
if not height_all.empty and not wt_all.empty:
    height_by_hadm = (
        height_all.sort_values(["hadm_id", "charttime"])
                  .groupby("hadm_id", as_index=False)["valuenum"]
                  .median()
                  .rename(columns={"valuenum": "height_cm"})
    )
    bmi_src = wt_all[~wt_all["hadm_id"].isin(omr_bmi_hadm)].merge(height_by_hadm, on="hadm_id", how="inner")
    if not bmi_src.empty:
        bmi_src["bmi"] = bmi_src["valuenum"] / ((bmi_src["height_cm"] / 100.0) ** 2)
        df = make_event_df(bmi_src["hadm_id"], "BMI_MEASURE", bmi_src["charttime"], bmi_src["bmi"].astype(float))
        df = plausible(df, "BMI_MEASURE")
        all_events.append(filter_window(df))

del chart

# ─────────────────────────────────────────────────────────────────────────────
# Step 9: Drug administration — inputevents (dose) + emar (PO confirmations)
# ─────────────────────────────────────────────────────────────────────────────
print("[9/9] Loading inputevents (drug administrations)...")
INP_USECOLS = ["hadm_id", "stay_id", "starttime", "itemid", "amount", "amountuom",
               "ordercategoryname"]
INP_DTYPE = {"hadm_id": "Int64", "stay_id": "Int64", "itemid": "Int64",
             "amount": "float64", "amountuom": "string", "ordercategoryname": "string"}

inp_parts = []
reader = pd.read_csv(f"{ICU}/inputevents.csv.gz", compression="gzip",
                     usecols=INP_USECOLS, dtype=INP_DTYPE,
                     parse_dates=["starttime"], chunksize=CHUNK_LAB)
INP_ITEMIDS_ALL = (
    INS_REGULAR | INS_BASAL | INS_BOLUS | HEPARIN_IV | DEXTROSE
    | BICARB_DRUG | CA_GLUCONATE | HTS_SALINE | PO_INTAKE
    | ABX_IV   # STEROIDS_IV is empty in MIMIC-IV; handled via EMAR + pharmacy.route below
)
for chunk in tqdm(reader, desc="  inputevents", unit="chunk"):
    chunk = chunk[chunk["hadm_id"].isin(valid_hadm_set) & chunk["itemid"].isin(INP_ITEMIDS_ALL)]
    if not chunk.empty:
        inp_parts.append(chunk)
inp = pd.concat(inp_parts, ignore_index=True) if inp_parts else pd.DataFrame(columns=INP_USECOLS)
del inp_parts
print(f"  inputevents kept: {len(inp):,} rows")

inp["amountuom_l"] = inp["amountuom"].fillna("").str.lower().str.strip()

def emit_input_true(concept, itemids):
    sub = inp[inp["itemid"].isin(itemids)]
    if sub.empty:
        return
    df = make_event_df(sub["hadm_id"], concept, sub["starttime"], "True")
    all_events.append(filter_window(df))

def emit_input_dose_units(concept, itemids):
    """Emit dose in Units (insulin). Drop rows whose amountuom is not 'units' (case-insensitive)."""
    sub = inp[inp["itemid"].isin(itemids) & inp["amount"].notna() & (inp["amountuom_l"] == "units")].copy()
    lo, hi = DOSE_RANGES[concept]
    sub = sub[(sub["amount"] >= lo) & (sub["amount"] <= hi)].copy()
    if sub.empty:
        return
    df = make_event_df(sub["hadm_id"], concept, sub["starttime"], sub["amount"].astype(float))
    all_events.append(filter_window(df))

# Insulin (dose in Units)
emit_input_dose_units("INSULIN_IV_DOSAGE", INS_REGULAR)
emit_input_dose_units("BASAL_DOSAGE",      INS_BASAL)
emit_input_dose_units("BOLUS_DOSAGE",      INS_BOLUS)

# Other IV drugs (Value="True")
emit_input_true("HEPARIN_IV_BITZUA",        HEPARIN_IV)
emit_input_true("DEXTROSE_BITZUA",          DEXTROSE)
emit_input_true("BICARBONATE_BITZUA",       BICARB_DRUG)
emit_input_true("CALCIUM-GLUCONATE_BITZUA", CA_GLUCONATE)
emit_input_true("HYPERTONIC_SALINE_BITZUA", HTS_SALINE)
emit_input_true("ANTIBIOTIC_IV_BITZUA",     ABX_IV)
# STEROIDS_IV_BITZUA: not in inputevents — emitted below from EMAR via pharmacy.route.

# MEAL (PO Intake), with time-of-day → Breakfast/Lunch/Dinner/Night-Snack
print("  MEAL events…")
po = inp[inp["itemid"].isin(PO_INTAKE)].copy()
if not po.empty:
    po["h"] = po["starttime"].dt.hour + po["starttime"].dt.minute / 60.0

    def meal_label(h):
        """Map hour-of-day to Breakfast / Lunch / Dinner / Night-Snack."""
        if 5.0   <= h < 11.5:  return "Breakfast"
        if 11.5  <= h < 16.5:  return "Lunch"
        if 16.5  <= h < 21.0:  return "Dinner"
        if (h >= 21.0) or (h < 5.0): return "Night-Snack"
        return None

    po["meal"] = po["h"].map(meal_label)
    po = po[po["meal"].notna()].copy()
    # hour-rounded dedup within (PatientId, ConceptName)
    po["hour_round"] = po["starttime"].dt.floor("H")
    po = po.sort_values(["hadm_id", "starttime"]).drop_duplicates(subset=["hadm_id", "meal", "hour_round"], keep="first")
    # collapse consecutive same-meal rows per admission (keep first only)
    po = po.sort_values(["hadm_id", "starttime"])
    po["prev_meal"] = po.groupby("hadm_id")["meal"].shift(1)
    po = po[po["meal"] != po["prev_meal"]].copy()

    df = make_event_df(po["hadm_id"], "MEAL", po["starttime"], po["meal"])
    all_events.append(filter_window(df))

# ─────────────────────────────────────────────────────────────────────────────
# ACIDOSIS — derived rule (matches downstream ETL definition)
#   pH <= 7.3 AND HCO3 <= 10 (within ±1h of pH) AND insulin-IV within ±2h of pH.
#   Emitted at the pH charttime, Value="True", every qualifying timepoint.
# ─────────────────────────────────────────────────────────────────────────────
print("  ACIDOSIS rule join (pH+HCO3+insulin-IV)…")
ins_iv_times = inp[
    inp["itemid"].isin(INS_REGULAR) & inp["amount"].notna() & (inp["amountuom_l"] == "units")
][["hadm_id", "starttime"]].rename(columns={"starttime": "ins_time"})

if not ph_acid_cand.empty and not hco3_low.empty and not ins_iv_times.empty:
    m = ph_acid_cand.merge(hco3_low, on="hadm_id")
    m["dt_hco3"] = (m["ph_time"] - m["hco3_time"]).abs()
    m = m[m["dt_hco3"] <= pd.Timedelta(hours=1)].drop_duplicates(subset=["hadm_id", "ph_time"])

    m = m.merge(ins_iv_times, on="hadm_id")
    m["dt_ins"] = (m["ph_time"] - m["ins_time"]).abs()
    m = m[m["dt_ins"] <= pd.Timedelta(hours=2)].drop_duplicates(subset=["hadm_id", "ph_time"])

    if not m.empty:
        df = make_event_df(m["hadm_id"], "ACIDOSIS", m["ph_time"], "True")
        all_events.append(filter_window(df))

del inp

# EMAR: confirmations of administered PO/SC drugs (with pharmacy.route join for IV/PO/SC split)
print("  EMAR PO/SC drug confirmations…")
EMAR_USECOLS = ["hadm_id", "charttime", "medication", "event_txt", "pharmacy_id"]
EMAR_DTYPE   = {"hadm_id": "Int64", "medication": "string", "event_txt": "string",
                "pharmacy_id": "Int64"}

emar_parts = []
reader = pd.read_csv(f"{HOSP}/emar.csv.gz", compression="gzip",
                     usecols=EMAR_USECOLS, dtype=EMAR_DTYPE,
                     parse_dates=["charttime"], chunksize=CHUNK_LAB)
for chunk in tqdm(reader, desc="  emar", unit="chunk"):
    chunk = chunk[
        chunk["hadm_id"].isin(valid_hadm_set) &
        chunk["event_txt"].isin(EMAR_ADMIN_STATUSES) &
        chunk["medication"].notna()
    ]
    if not chunk.empty:
        emar_parts.append(chunk)
emar = pd.concat(emar_parts, ignore_index=True) if emar_parts else pd.DataFrame(columns=EMAR_USECOLS)
del emar_parts
print(f"  emar kept: {len(emar):,} rows")

# Pull route from pharmacy and merge in (route is metadata, not a dose source)
print("  pharmacy.route lookup (for EMAR route disambiguation)…")
PH_USECOLS = ["pharmacy_id", "route"]
PH_DTYPE   = {"pharmacy_id": "Int64", "route": "string"}
ph_parts = []
reader = pd.read_csv(f"{HOSP}/pharmacy.csv.gz", compression="gzip",
                     usecols=PH_USECOLS, dtype=PH_DTYPE, chunksize=CHUNK_LAB)
needed_pids = set(emar["pharmacy_id"].dropna().astype(int).tolist())
for chunk in tqdm(reader, desc="  pharmacy", unit="chunk"):
    chunk = chunk[chunk["pharmacy_id"].isin(needed_pids)]
    if not chunk.empty:
        ph_parts.append(chunk)
pharm_route = pd.concat(ph_parts, ignore_index=True) if ph_parts else pd.DataFrame(columns=PH_USECOLS)
pharm_route = pharm_route.drop_duplicates(subset=["pharmacy_id"], keep="first")
del ph_parts

emar = emar.merge(pharm_route, on="pharmacy_id", how="left")
emar["med_l"]   = emar["medication"].str.lower()
emar["route_u"] = emar["route"].fillna("").str.upper().str.strip()

IV_ROUTES = {"IV", "IV DRIP", "IV BOLUS", "IVPB", "IV PUSH", "IVP", "INTRAVENOUS", "IM", "INTRAMUSCULAR"}
PO_ROUTES = {"PO", "PO/NG", "NG", "GT", "PO OR NG", "PO/NG/TUBE", "PO/GT", "PO/TUBE", "ORAL"}
SC_ROUTES = {"SC", "SQ", "SUBQ", "SUBCUTANEOUS", "SUBCUT"}

def emit_emar(concept, pattern, route_set=None):
    """Emit Value='True' EMAR rows where medication matches pattern (and optionally route in route_set)."""
    sub = emar[emar["med_l"].str.contains(pattern, regex=True, na=False)]
    if route_set is not None:
        sub = sub[sub["route_u"].isin(route_set)]
    if sub.empty:
        return
    df = make_event_df(sub["hadm_id"], concept, sub["charttime"], "True")
    all_events.append(filter_window(df))

# Routeless: emit on medication match alone (concept is the route)
emit_emar("METFORMIN_HOSPITAL_BITZUA",    METFORMIN_PATTERN)
emit_emar("ANTIDIABETIC_HIGH_HYPO_HOSPITAL_BITZUA", ANTIDIAB_PATTERN)
emit_emar("SGLT2_HOSPITAL_BITZUA",        SGLT2_PATTERN)
emit_emar("K_BINDER_BITZUA",              KBINDER_PATTERN)

# Route-disambiguated
emit_emar("STEROIDS_IV_BITZUA",  STEROIDS_PO_PATTERN, IV_ROUTES)
emit_emar("STEROIDS_PO_BITZUA",  STEROIDS_PO_PATTERN, PO_ROUTES)
emit_emar("ANTIBIOTIC_PO_BITZUA", ABX_PATTERNS,        PO_ROUTES)
emit_emar("HEPARIN_SC_BITZUA",    HEPARIN_PATTERN,     SC_ROUTES)

del emar, pharm_route

# ─────────────────────────────────────────────────────────────────────────────
# Combine, validate, dedup, write
# ─────────────────────────────────────────────────────────────────────────────
print("Combining and validating concept_events...")
concept_events = pd.concat([e for e in all_events if e is not None and not e.empty], ignore_index=True)

unknown = sorted(set(concept_events["ConceptName"].unique()) - VALID_CONCEPTS)
if unknown:
    omitted = concept_events[concept_events["ConceptName"].isin(unknown)].groupby("ConceptName").size()
    print("  WARNING: rows emitted for concepts absent from tak-repo; auto-omitting:")
    for name, cnt in omitted.sort_values(ascending=False).items():
        print(f"    {name}: {cnt:,} rows")
    concept_events = concept_events[concept_events["ConceptName"].isin(VALID_CONCEPTS)].copy()

concept_events = (
    concept_events.sort_values(["PatientId", "ConceptName", "StartDateTime"])
                  .drop_duplicates(subset=["PatientId", "ConceptName", "StartDateTime"], keep="first")
)
concept_events = filter_window(concept_events)
concept_events["PatientId"] = concept_events["PatientId"].astype(int)

support = concept_events.groupby("ConceptName")["PatientId"].nunique()
support_pct = 100.0 * support / len(valid_hadm_set)
low_support = sorted(support_pct[support_pct < MIN_CONCEPT_PATIENT_SUPPORT_PCT].index.tolist())
if low_support:
    print(
        f"  WARNING: auto-omitting concepts with <{MIN_CONCEPT_PATIENT_SUPPORT_PCT:g}% "
        "patient support:"
    )
    low_counts = concept_events[concept_events["ConceptName"].isin(low_support)].groupby("ConceptName").size()
    for name in low_support:
        print(f"    {name}: {support[name]:,} patients, {low_counts[name]:,} rows")
    concept_events = concept_events[~concept_events["ConceptName"].isin(low_support)].copy()

print(f"  concept_events rows: {len(concept_events):,}")

# ─────────────────────────────────────────────────────────────────────────────
# Build context_data.csv
# ─────────────────────────────────────────────────────────────────────────────
print("Building context_data.csv…")
context = adm[["hadm_id", "age_at_admission", "gender", "admission_type"]].copy()
context = context.rename(columns={"hadm_id": "PatientId"})

context["gender"] = context["gender"].map({"F": 0, "M": 1}).fillna(-1).astype(int)

def encode_admission_type(s):
    """Encode admission_type per CLAUDE.md: EMER*→0, ELECTIVE→1, URGENT→2, else→3."""
    if not isinstance(s, str): return 3
    su = s.upper()
    if "EMER" in su:        return 0
    if "ELECTIVE" in su:    return 1
    if "URGENT" in su:      return 2
    return 3
context["admission_type"] = context["admission_type"].map(encode_admission_type).astype(int)

# Chronic diagnosis flags
t1_hadm = icd_hadm_set(
    icd9_prefixes=(),  # done below via per-position check
    icd10_prefixes=("E10",),
)
# ICD-9 type-1: 250 with 5th-char in {1,3}
diag9 = diag[(diag["icd_version"] == 9) & diag["icd_code"].str.startswith("250") & (diag["icd_code"].str.len() >= 5)]
t1_hadm |= set(diag9[diag9["icd_code"].str[4].isin(["1", "3"])]["hadm_id"].dropna().astype(int).tolist())

t2_hadm = icd_hadm_set(
    icd9_prefixes=("249",),
    icd10_prefixes=("E08", "E09", "E11", "E13"),
)
t2_hadm |= set(diag9[diag9["icd_code"].str[4].isin(["0", "2"])]["hadm_id"].dropna().astype(int).tolist())

htn_hadm = icd_hadm_set(
    icd9_prefixes=("401", "402", "403", "404", "405"),
    icd10_prefixes=("I10", "I11", "I12", "I13", "I14", "I15"),
)
ob_hadm  = icd_hadm_set(
    icd9_prefixes=("2780",),
    icd10_prefixes=("E66",),
)

context["has_diabetes_type1"] = context["PatientId"].isin(t1_hadm).astype(int)
context["has_diabetes_type2"] = context["PatientId"].isin(t2_hadm).astype(int)
context["has_hypertension"]   = context["PatientId"].isin(htn_hadm).astype(int)
context["has_obesity"]        = context["PatientId"].isin(ob_hadm).astype(int)

# Median-impute age (others are already 0/1)
context["age_at_admission"] = context["age_at_admission"].fillna(context["age_at_admission"].median()).round(1)

context_out = context[[
    "PatientId", "age_at_admission", "gender", "admission_type",
    "has_diabetes_type1", "has_diabetes_type2", "has_hypertension", "has_obesity",
]]

# ─────────────────────────────────────────────────────────────────────────────
# Write outputs
# ─────────────────────────────────────────────────────────────────────────────
context_out.to_csv(f"{OUT_DIR}/context_data.csv", index=False)
concept_events["StartDateTime"] = concept_events["StartDateTime"].dt.strftime("%Y-%m-%d %H:%M:%S")
concept_events["EndDateTime"]   = pd.to_datetime(concept_events["EndDateTime"]).dt.strftime("%Y-%m-%d %H:%M:%S")
concept_events.to_csv(f"{OUT_DIR}/concept_events.csv", index=False)

# Summary
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
print(f"Admissions (PatientIds):     {len(context_out):,}")
print(f"concept_events total rows:   {len(concept_events):,}")
print("\nRows per concept:")
for name, cnt in concept_events.groupby("ConceptName").size().sort_values(ascending=False).items():
    print(f"  {name:42s} {cnt:>10,}")
missing = sorted(TAK_KEYS - set(concept_events["ConceptName"].unique()))
if missing:
    print(f"\nWARNING: tak-repo concepts with ZERO rows ({len(missing)}):")
    for m in missing:
        print(f"  {m}")
print(f"\ncontext_data.csv   -> {OUT_DIR}/context_data.csv")
print(f"concept_events.csv -> {OUT_DIR}/concept_events.csv")
print("Done.")
