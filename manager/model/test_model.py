# load model

import joblib
from pathlib import Path
import pandas as pd

model_path = Path(__file__).parent / "v1" / "xgb_multioutput_model.joblib"
if model_path.exists():
    xgb_model = joblib.load(model_path)
else:
    print(f"Model file not found at {model_path}")
    exit(1)

print("Model loaded successfully")

# Feature columns (must match training order)
FEATURE_COLS = [
    'cpu_task_number',  # int   - current number of CPU tasks
    'cpu_limit',        # float - CPU quota (raw value / 100, e.g. 60 → 0.6)
    'hdd_limit',        # float - HDD capacity limit (MB)
    'mem_limit',        # float - Memory limit (MB)
    'allocating_hdd',   # float - Allocated HDD usage (MB)
    'allocating_mem',   # float - Allocated memory (MB)
    'hdd_rate',         # float - allocating_hdd / hdd_limit  (derived)
    'mem_rate',         # float - allocating_mem / mem_limit   (derived)
]

# Output columns
TARGET_COLS = ['cpu_processing_time', 'mem_processing_time', 'hdd_processing_time']


def predict_execution_time(
    cpu_task_number: int,
    cpu_limit: float,
    hdd_limit: float,
    mem_limit: float,
    allocating_hdd: float,
    allocating_mem: float,
) -> dict:
    """
    Predict task execution times from raw resource metrics.

    Args:
        cpu_task_number : number of concurrent CPU tasks
        cpu_limit       : CPU quota percentage divided by 100 (e.g. 60% → 0.6)
        hdd_limit       : HDD capacity limit in MB
        mem_limit       : memory limit in MB
        allocating_hdd  : currently allocated HDD in MB
        allocating_mem  : currently allocated memory in MB

    Returns:
        dict with keys cpu_processing_time, mem_processing_time, hdd_processing_time (seconds)
    """
    hdd_rate = allocating_hdd / hdd_limit
    mem_rate  = allocating_mem  / mem_limit

    input_df = pd.DataFrame([[
        cpu_task_number,
        cpu_limit,
        hdd_limit,
        mem_limit,
        allocating_hdd,
        allocating_mem,
        hdd_rate,
        mem_rate,
    ]], columns=FEATURE_COLS)

    predictions = xgb_model.predict(input_df)[0]  # shape (3,)
    return dict(zip(TARGET_COLS, predictions))


if __name__ == "__main__":
    # ── Basic type inputs ────────────────────────────────────────────────────
    cpu_task_number = 121832     # int
    cpu_limit       = 0.6        # float  (60% → 0.6)
    hdd_limit       = 163840.0   # float  MB
    mem_limit       = 819.2      # float  MB
    allocating_hdd  = 20230.0    # float  MB
    allocating_mem  = 1744.0     # float  MB

    print("=" * 45)
    print("Input:")
    print(f"  cpu_task_number : {cpu_task_number}  (int)")
    print(f"  cpu_limit       : {cpu_limit}       (float, already /100)")
    print(f"  hdd_limit       : {hdd_limit}  (float, MB)")
    print(f"  mem_limit       : {mem_limit}      (float, MB)")
    print(f"  allocating_hdd  : {allocating_hdd}   (float, MB)")
    print(f"  allocating_mem  : {allocating_mem}    (float, MB)")
    print("  [hdd_rate / mem_rate are derived internally]")

    result = predict_execution_time(
        cpu_task_number=cpu_task_number,
        cpu_limit=cpu_limit,
        hdd_limit=hdd_limit,
        mem_limit=mem_limit,
        allocating_hdd=allocating_hdd,
        allocating_mem=allocating_mem,
    )

    print("=" * 45)
    print("Output (predicted processing times in seconds):")
    for key, val in result.items():
        print(f"  {key:<25}: {val:.6f} s")
    print("=" * 45)