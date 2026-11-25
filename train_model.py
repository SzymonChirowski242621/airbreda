"""
Train the NO2 regression on training_data.csv and save model.pkl + model_metrics.json.

Evaluation uses a time-ordered split (first 75% of hours to train, last 25% to test),
because a random split would let the model see hours that come after the ones it is
tested on. The model that is saved and served is then refit on all rows, since every
hour of data matters at this size. The metrics file records both.

Run inside the same image that serves the model, so library versions match:
    docker run --rm -v "$PWD:/work" -w /work airbreda-dashboard python train_model.py
"""

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score

FEATURES = ["total_intensity_veh_per_hr", "hour_of_day"]
TARGET = "no2_ug_m3"


def evaluate(df: pd.DataFrame, test_fraction: float = 0.25) -> dict:
    n_test = max(1, int(round(len(df) * test_fraction)))
    train, test = df.iloc[:-n_test], df.iloc[-n_test:]
    model = LinearRegression().fit(train[FEATURES], train[TARGET])
    pred = model.predict(test[FEATURES])
    baseline = np.full(len(test), train[TARGET].mean())  # "always predict the training mean"
    return {
        "train_rows": len(train),
        "test_rows": len(test),
        "test_from": str(test["hour_start"].iloc[0]),
        "r2_test": round(float(r2_score(test[TARGET], pred)), 3),
        "mae_test": round(float(mean_absolute_error(test[TARGET], pred)), 2),
        "mae_test_mean_baseline": round(float(mean_absolute_error(test[TARGET], baseline)), 2),
    }


def main() -> None:
    df = pd.read_csv("training_data.csv").sort_values("hour_start").reset_index(drop=True)
    metrics = {"rows": len(df), "features": FEATURES, "holdout": evaluate(df)}

    model = LinearRegression().fit(df[FEATURES], df[TARGET])
    fitted = model.predict(df[FEATURES])
    metrics["final_model"] = {
        "r2_train": round(float(r2_score(df[TARGET], fitted)), 3),
        "mae_train": round(float(mean_absolute_error(df[TARGET], fitted)), 2),
        "intercept": round(float(model.intercept_), 3),
        "coefficients": {f: round(float(c), 5) for f, c in zip(FEATURES, model.coef_)},
        "correlation_no2_vs_total_intensity": round(
            float(df[TARGET].corr(df["total_intensity_veh_per_hr"])), 3),
        "no2_range": [float(df[TARGET].min()), float(df[TARGET].max())],
        "intensity_range": [float(df["total_intensity_veh_per_hr"].min()),
                            float(df["total_intensity_veh_per_hr"].max())],
    }

    joblib.dump(model, "model.pkl")
    Path("model_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
