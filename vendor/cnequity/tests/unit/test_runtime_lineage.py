from cnequity.config import Config
from cnequity.storage.provenance import config_fingerprint, runtime_lineage


def test_config_fingerprint_is_stable_and_omits_secret_values(tmp_path):
    first = Config(
        data_root=tmp_path / "one",
        tushare_token="secret-one",
        eastmoney_proxy="https://user:password@example.test",
    )
    second = Config(
        data_root=tmp_path / "two",
        tushare_token="secret-two",
        eastmoney_proxy="https://different:secret@example.test",
    )

    assert config_fingerprint(first) == config_fingerprint(second)
    assert "secret-one" not in config_fingerprint(first)


def test_config_fingerprint_changes_for_data_affecting_option(tmp_path):
    first = Config(data_root=tmp_path / "data", adj_factors_types=["hfq"])
    second = Config(data_root=tmp_path / "data", adj_factors_types=["hfq", "qfq"])
    assert config_fingerprint(first) != config_fingerprint(second)


def test_runtime_lineage_contains_code_and_config_identity(tmp_path):
    lineage = runtime_lineage(Config(data_root=tmp_path / "data"))
    assert lineage["package_version"]
    assert len(lineage["config_fingerprint"]) == 64
    assert lineage["git_commit"] is None or len(lineage["git_commit"]) == 40
    assert lineage["git_dirty"] in {True, False, None}
