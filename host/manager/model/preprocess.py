import numpy as np
import math


def preprocess_for_prediction_model(
    cpu_task_number: int,
    cpu_limit: float,
    hdd_limit: float,
    mem_limit: float,
    allocating_hdd: float,
    allocating_mem: float,
) -> np.ndarray:
    """
    Preprocess raw resource metrics into a feature vector for the prediction model.

    Args:
        cpu_task_number : number of concurrent CPU tasks
        cpu_limit       : CPU quota percentage divided by 100 (e.g. 60% → 0.6)
        hdd_limit       : HDD capacity limit in MB
        mem_limit       : memory limit in MB
        allocating_hdd  : currently allocated HDD in MB
        allocating_mem  : currently allocated memory in MB

    Returns:
        np.ndarray of shape (1, 8) containing the preprocessed features in the order expected by the model.
    """
    # Guard against None (e.g. mount point missing) — use NaN so the model
    # still receives a well-typed float array instead of crashing.
    hdd_limit_f = float(hdd_limit) if hdd_limit is not None else float('nan')
    mem_limit_f  = float(mem_limit)  if mem_limit  is not None else float('nan')

    hdd_rate = allocating_hdd / hdd_limit_f if (not math.isnan(hdd_limit_f) and hdd_limit_f != 0) else float('nan')
    mem_rate  = allocating_mem  / mem_limit_f  if (not math.isnan(mem_limit_f)  and mem_limit_f  != 0) else float('nan')

    feature_vector = np.array([[
        cpu_task_number,
        cpu_limit,
        hdd_limit_f,
        mem_limit_f,
        allocating_hdd,
        allocating_mem,
        hdd_rate,
        mem_rate,
    ]], dtype=np.float32)

    return feature_vector