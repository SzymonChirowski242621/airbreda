"""
Turn the trained regression into a prediction and a 0-1 exceedance risk.

Threshold: 40 ug/m3, the EU annual limit value for NO2 (Directive 2008/50/EC). It is an
annual mean and we apply it to hourly values, so "exceedance" here means "this hour is above
the level the yearly average may not exceed", not a legal breach. The WHO 2021 guidelines
are stricter (25 ug/m3 for 24 hours, 10 ug/m3 annual), but the station's own hourly values
are above 25 most days, so a 25 threshold would put risk near 1 almost all the time and
tell the reader nothing. The revised EU directive lowers the annual limit to 20 ug/m3 by
2030; that would be the threshold to switch to for a forward-looking dashboard.
"""

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

MODEL_PATH = Path(__file__).with_name("model.pkl")
EXCEEDANCE_THRESHOLD_UG_M3 = 40.0
STEEPNESS = 0.2  # risk goes from ~0.12 at threshold-10 to ~0.88 at threshold+10


@lru_cache(maxsize=1)
def load_model():
    import joblib

    return joblib.load(MODEL_PATH)


def exceedance_risk(predicted_no2: float, threshold: float = EXCEEDANCE_THRESHOLD_UG_M3,
                    steepness: float = STEEPNESS) -> float:
    return float(1 / (1 + np.exp(-steepness * (predicted_no2 - threshold))))


def predict(total_intensity_veh_per_hr: float, hour_of_day: int) -> dict:
    """Predicted NO2 for one hour at the interchange, from total traffic over all four sites."""
    X = pd.DataFrame([[total_intensity_veh_per_hr, hour_of_day]],
                     columns=["total_intensity_veh_per_hr", "hour_of_day"])
    no2 = max(0.0, float(load_model().predict(X)[0]))  # a concentration cannot be negative
    return {"no2_ug_m3_predicted": round(no2, 2), "no2_exceedance_risk": round(exceedance_risk(no2), 3)}
