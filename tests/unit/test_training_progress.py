"""Training progress parsing handles both tqdm rate units; every accepted
dataset type has a LoRA name and registry type."""
import pytest

import training


@pytest.mark.parametrize("t", training.VALID_TYPES)
def test_every_valid_type_is_named(t):
    runner = training.TrainingRunner.__new__(training.TrainingRunner)
    name = runner._out_name(t, {"dataset": "set1"})
    assert name.endswith("_set1") and training._LORA_NAMING[t][1]


@pytest.mark.parametrize("content,expected", [
    ("[]", {}),                                   # how the store once shipped
    ("not json", {}),
    ('{"r1": {"status": "done"}}', {"r1": {"status": "done"}}),
])
def test_runs_store_reads_only_a_mapping(tmp_path, monkeypatch, content, expected):
    db = tmp_path / "training_runs.json"
    db.write_text(content, encoding="utf-8")
    monkeypatch.setattr(training, "RUNS_DB", db)
    assert training._load_runs() == expected


@pytest.mark.parametrize("rate,unit,s_per_it", [("3.48", "s/it", 3.48),
                                                ("4.00", "it/s", 0.25)])
def test_tqdm_rate_units(tmp_path, monkeypatch, rate, unit, s_per_it):
    monkeypatch.setattr(training, "_save_runs", lambda runs: None)
    log = tmp_path / "train.log"
    log.write_text(f"run:  75%|######  | 15/20 [01:07<00:17, {rate}{unit}, "
                   f"lr: 1.0e-04 loss: 2.369e-01]\n", encoding="utf-8")
    runner = training.TrainingRunner.__new__(training.TrainingRunner)
    runner._log_pos = 0
    r = {"log": str(log)}
    runner._update_from_log("rid", r, {})
    assert r["step"] == 15 and r["total_steps"] == 20
    assert r["it_s"] == pytest.approx(1.0 / s_per_it)
    assert r["eta_s"] == pytest.approx(5 * s_per_it)
