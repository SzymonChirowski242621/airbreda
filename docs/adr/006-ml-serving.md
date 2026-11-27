# ADR-006: ML serving architecture

Status: Accepted
Date: 2026-11-25, retrained 2026-11-27

## Context

The dashboard has to show a predicted NO₂ value and a 0–1 `no2_exceedance_risk` for the A27/Breda interchange, from a model trained on data our own pipeline collected.

The first model, on 25 November, was trained on 46 hours. It was retrained on 27 November, when the join gave **93 training rows**: every hour from 23 Nov 11:00 to 27 Nov 07:00, with no gaps. Traffic only starts on 23 November, so the join cannot go further back, even though the database has older NO₂ values. Only 5 of the 93 hours are above 40 µg/m³.

Two details of the data shape the design:

- Luchtmeetnet labels each NO₂ value with the end of its averaging hour (12:00 means the mean over 11:00–12:00). The traffic job stores NDW's latest one-minute snapshot just before the hour turns (for example 11:55). Traffic hour H is therefore joined to the NO₂ value labelled H+1.
- The traffic value is a one-minute snapshot expressed as vehicles per hour, while NO₂ is a one-hour average. The feature is much noisier than the target it explains.

## Decision

### Model

A scikit-learn `LinearRegression` on two features, `total_intensity_veh_per_hr` (sum of the four sites) and `hour_of_day`, trained by `train_model.py` on `training_data.csv`. Flagged NO₂ rows (null or stale) are excluded from training.

Evaluation uses a time-ordered split: the first 70 hours train, the last 23 (26 Nov 09:00 to 27 Nov 07:00) test. A random split would let the model learn from hours that come after the ones it is tested on.

| Metric | 25 Nov, 46 rows | 27 Nov, 93 rows (served) |
|---|---|---|
| Test hours | 12 | 23 |
| Test MAE | 8.82 µg/m³ | 6.32 µg/m³ |
| Test MAE of always predicting the training mean | 13.02 µg/m³ | 4.89 µg/m³ |
| Test R² | −16.6 | −0.68 |
| Train R² (final model, all rows) | 0.35 | 0.15 |
| Traffic coefficient (µg/m³ per veh/h) | +0.00211 | +0.00157, about +1.6 per 1,000 vehicles/hour |
| `hour_of_day` coefficient (µg/m³ per hour) | +0.54 | +0.28 |
| Correlation of NO₂ with total intensity | 0.43 | 0.32 |

With 46 rows the model beat the naive baseline. With 93 rows it does not: on the new test window its absolute error is 1.4 µg/m³ worse than always predicting the training mean. The first result was mostly luck in the test window. Those 12 hours were overnight, NO₂ barely moved, and the training mean happened to be far from them.

The new test window shows what the model cannot see. On the morning of 26 November traffic was heavy (3,400 to 4,500 vehicles/hour) and NO₂ was low, around 15 µg/m³, so the model over-predicted by 8 to 12. On the night of 26 to 27 November traffic fell below 500 vehicles/hour, but NO₂ stayed between 21 and 26 and then climbed to 33 by 07:00, and the model under-predicted by 6 to 12. In both cases NO₂ moved against the traffic, which points to weather: wind and mixing decide how much exhaust stays near the station. The model has no weather feature.

The served model is refit on all 93 rows. Twice the hours give a steadier estimate of the traffic effect than 46 did, even though that effect turned out smaller, and the 46-row model's apparent advantage over the baseline did not hold up. Serving the old model because its numbers looked better would be choosing the test window, not the model.

The traffic coefficient has the expected sign: more traffic, more NO₂. The `hour_of_day` coefficient should not be read as "NO₂ rises through the day". Treating hour 0–23 as one straight line says that 23:00 is far from 00:00, which is wrong; it is in the model because the course specifies the feature, and it soaks up some of the daily pattern.

A linear regression is the right amount of model for 93 rows. A random forest or gradient boosting would fit those rows more closely and generalise worse, and with one test set of 23 hours we could not even tell. The retrain also shows that one test window is not enough to judge a model at all: the same method went from beating the baseline to losing to it when the window moved by two days. What would justify more: several months of data, so that weekdays, weekends and seasons all appear many times, and a test set large enough to compare models with confidence. Before that, the next improvements are features rather than algorithms. Wind speed and direction come first, since the retrain's errors look like weather. After that: an hourly mean of traffic instead of one snapshot, the hour encoded as a cycle (sine and cosine), and weekday versus weekend.

### Exceedance risk

`predict.py` converts the predicted concentration into a risk with a sigmoid centred on a threshold of **40 µg/m³** and steepness 0.2: risk is 0.5 at 40, about 0.12 at 30 and about 0.88 at 50.

40 µg/m³ is the EU annual limit value for NO₂. It is an annual mean and we apply it to hourly values, so the risk means "this hour is above the level the yearly average may not exceed", not a legal breach. The WHO 2021 guideline of 25 µg/m³ for 24 hours was considered and rejected as the threshold: 37 of the 93 hours are above 25, and the model's predictions (16 to 29 µg/m³ over the training hours) sit around 25, so the risk would hover near 0.5 and tell a reader nothing.

The alternative, a `LogisticRegression` trained directly on "did this hour exceed 40", was rejected for now. With 5 positive examples out of 93 it would learn almost nothing. The sigmoid keeps the risk tied to the regression, so the predicted value and the risk on the dashboard always agree.

One consequence of the retrain: the model never predicts above 29 µg/m³ for any traffic level seen so far, so the risk never goes above about 0.10. The 5 hours that did exceed 40 were not predicted. A low risk on the dashboard means "traffic alone does not suggest an exceedance", not "the air is fine".

### Serving

`model.pkl` is baked into the dashboard image (`Dockerfile.dashboard`), and `predict()` is called inside the dashboard process. There is no separate model server: the model is 873 bytes and predicts in microseconds.

The model is trained inside that same image (`docker run ... airbreda-dashboard python train_model.py`), so the scikit-learn, NumPy and pandas versions that write the pickle are exactly the ones that read it. All versions are pinned in `requirements.txt`.

## Training-serving skew

Training-serving skew is when the model sees features at serving time that are computed differently from the ones it was trained on. In this design it could appear in four places:

1. **Which intensity.** The model is trained on the total of all four sites. Feeding it one site's intensity in `/site/{id}` would put a value of about 300 into a model that learned on values of 3,000–5,000. The dashboard therefore always predicts from the four-site total, and all four sites get the same prediction. That is correct: there is one NO₂ station and one prediction for the interchange; what differs per site is the traffic.
2. **Which hour.** Training takes `hour_of_day` from the UTC hour of the traffic snapshot. Serving does the same. Using local Dutch time in one place and UTC in the other would shift every prediction by one or two hours.
3. **Which traffic value.** Both use the stored one-minute snapshot from S3, read from the same CSV format.
4. **Which library versions.** Baking the pickle into the image it was trained in removes this one completely. Loading a pickle written by a different scikit-learn version can fail or, worse, load and behave differently.

The price of baking the model in is that every retrain needs an image rebuild and a container restart. Loading the model from S3 at startup would allow retraining without a rebuild, but then the pickle and the image's library versions could drift apart, which is exactly cause 4.

## One station for four sites

All four NDW sites sit at hectometre 63 of the A27, at the same interchange that station NL10240 (Breda-Tilburgseweg) monitors. One real measurement legitimately describes the air at all four, so no site needs a stand-in reading borrowed from somewhere else.

A second interchange would need its own air-quality station. Using NL10240's value for traffic several kilometres away would train the model on air that the traffic did not produce and would show residents a measurement from somewhere else. If no Luchtmeetnet station exists near a new interchange, that interchange gets traffic data only, and no NO₂ value or prediction, until there is one.

## When `predict()` fails

`/site/{id}` still returns HTTP 200 with the real `no2_ug_m3`, `intensity_veh_per_hr` and timestamps. It sets `no2_ug_m3_predicted` and `no2_exceedance_risk` to `null`, adds a `prediction_error` field, and logs `prediction_failed`. The page then shows "n/a" for the prediction and the real numbers as usual.

The alternative, failing the whole request with a 500, was rejected. The measurements are the most trustworthy part of the response and do not depend on the model. Hiding them because the least reliable component failed would make the API less useful exactly when something is wrong. This behaviour is covered by a test (`test_prediction_failure_degrades_to_real_values`).

## Consequences

The prediction is weak and the dashboard should say so. With 93 hours it does not beat predicting the average on the latest test window, so it is a working pipeline end to end, not a forecast to rely on.

Retraining is manual: rebuild `training_data.csv` on the VM, run `train_model.py` in the dashboard image, rebuild and restart the dashboard. Every day of ingestion adds 24 rows. The retrain on 27 November took about ten minutes and doubled the data from 46 to 93 rows.

The dashboard has no record of which model version it serves beyond the image. `model_metrics.json` is committed next to `model.pkl`, so the repository shows which data and metrics belong to the deployed model.
