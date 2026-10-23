import pytest

from cnequity.orchestrator.scheduler_audit import run_ids_from_output


def test_group_results_include_every_run_once():
    assert run_ids_from_output('{"groups":[{"run_id":"a"},{"run_id":"b"},{"run_id":"a"}]}') == [
        "a",
        "b",
    ]


def test_stale_result_allows_cli_preface():
    assert run_ids_from_output('nothing stale\n{"status":"nothing_stale"}') == []


def test_weekly_group_skip_has_no_run_to_audit():
    assert run_ids_from_output('{"run_id":null,"status":"skipped_not_scheduled"}') == []


@pytest.mark.parametrize("output", ['{"run_id":"../outside"}', '{"run_id":null}', "invalid"])
def test_invalid_run_output_is_rejected(output):
    with pytest.raises(ValueError):
        run_ids_from_output(output)
