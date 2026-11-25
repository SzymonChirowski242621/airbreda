import predict


def test_model_pkl_loads():
    model = predict.load_model()
    assert hasattr(model, "predict")


def test_predict_returns_plausible_values():
    out = predict.predict(total_intensity_veh_per_hr=4000, hour_of_day=8)
    assert isinstance(out["no2_ug_m3_predicted"], float)
    assert 0 <= out["no2_ug_m3_predicted"] <= 200
    assert 0 <= out["no2_exceedance_risk"] <= 1


def test_risk_is_one_half_at_threshold_and_increases():
    t = predict.EXCEEDANCE_THRESHOLD_UG_M3
    assert predict.exceedance_risk(t) == 0.5
    assert predict.exceedance_risk(t - 10) < 0.5 < predict.exceedance_risk(t + 10)
