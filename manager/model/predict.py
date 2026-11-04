import pandas as pd
from typing import Any

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
    input_df: pd.DataFrame,
    model: Any,
) -> dict:
    """
    Predict task execution times from raw resource metrics.

    Args:
        input_df : pd.DataFrame containing the following columns:
            - cpu_task_number : number of concurrent CPU tasks
            - cpu_limit       : CPU quota percentage divided by 100 (e.g. 60% → 0.6)
            - hdd_limit       : HDD capacity limit in MB
            - mem_limit       : memory limit in MB
            - allocating_hdd  : currently allocated HDD in MB
            - allocating_mem  : currently allocated memory in MB
            - hdd_rate         : allocating_hdd / hdd_limit  (derived)
            - mem_rate         : allocating_mem / mem_limit   (derived)

    Returns:
        dict with keys cpu_processing_time, mem_processing_time, hdd_processing_time (seconds)
    """

    predictions = model.predict(input_df)[0]  # shape (3,)
    return dict(zip(TARGET_COLS, predictions))
