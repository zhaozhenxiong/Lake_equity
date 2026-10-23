"""Fault injection proves model and completed-run indexes publish together."""
import copy
import json
from pathlib import Path
import sqlite3

import pytest

from lake_equity.learning import dataset as dataset_module
from lake_equity.learning import registry as registry_module
from lake_equity.learning.registry import ModelRegistry
from lake_equity.learning.service import LearningService
from test_learning_service import synthetic_dataset, request


def setup_study(root, monkeypatch):
    data = synthetic_dataset()
    monkeypatch.setattr(dataset_module, 'build_dataset', lambda *args, **kwargs: copy.deepcopy(data))
    spec = request(data)
    spec['models'] = ['ridge']
    return LearningService(root), ModelRegistry(root), spec


def test_failed_model_copy_never_publishes_run_or_any_model_index(tmp_path, monkeypatch):
    service, registry, spec = setup_study(tmp_path, monkeypatch)
    actual_copy = registry_module.shutil.copyfile
    copies = []

    def copy_with_failure(source, destination, *args, **kwargs):
        if Path(destination).is_relative_to(registry.directory):
            copies.append(destination)
            if len(copies) == 3:  # first bundle is already prepared
                raise OSError('injected model-copy failure')
        return actual_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(registry_module.shutil, 'copyfile', copy_with_failure)
    events = []
    with pytest.raises(OSError, match='injected model-copy failure'):
        service.run(spec, progress=events.append)
    assert service.list_runs() == {'runs': []}
    assert registry.list_models() == {'models': [], 'series': []}
    assert not any(event.get('phase') == 'completed' for event in events)
    pending = list(service.runs_root.glob('learn-*/report.json'))
    assert len(pending) == 1  # diagnostic bytes remain, but no public index
    report = json.loads(pending[0].read_text())
    with pytest.raises(KeyError):
        service.read_run(report['run_id'])
    for folder in registry.directory.glob('model-*'):
        with pytest.raises(KeyError):
            registry.read_model(folder.name)
    # A retry may reuse verified immutable prepared bytes without training.
    monkeypatch.setattr(registry_module.shutil, 'copyfile', actual_copy)
    published = registry.publish_run(report)
    assert len(published['models']) == len(report['models']) * len(report['folds'])
    assert service.read_run(report['run_id']) == report
    assert len(service.list_runs()['runs']) == 1


@pytest.mark.parametrize('abort_table', ['series', 'runs'])
def test_sqlite_abort_rolls_back_all_new_models_series_and_experiment_rows(tmp_path, monkeypatch, abort_table):
    service, registry, spec = setup_study(tmp_path, monkeypatch)
    parent = service.run(spec)
    previous_models = registry.list_models()
    previous_runs = service.list_runs()
    database = registry.index if abort_table == 'series' else service.index
    with sqlite3.connect(database) as connection:
        connection.execute(f"CREATE TRIGGER refuse_publication BEFORE INSERT ON {abort_table} "
                           "BEGIN SELECT RAISE(ABORT, 'injected publication rollback'); END")
        connection.commit()
    events = []
    with pytest.raises(sqlite3.IntegrityError, match='injected publication rollback'):
        service.run(spec, progress=events.append)
    assert registry.list_models() == previous_models
    assert service.list_runs() == previous_runs
    assert service.read_run(parent['run_id']) == parent
    assert not any(event.get('phase') == 'completed' for event in events)
    unpublished = [path for path in service.runs_root.glob('learn-*/report.json')
                   if path.parent.name != parent['run_id']]
    assert len(unpublished) == 1
    report = json.loads(unpublished[0].read_text())
    with pytest.raises(KeyError):
        service.read_run(report['run_id'])
    with sqlite3.connect(registry.index) as connection:
        assert connection.execute('SELECT COUNT(*) FROM models').fetchone()[0] == len(previous_models['models'])
        assert connection.execute('SELECT COUNT(*) FROM series').fetchone()[0] == len(previous_models['series'])
    with sqlite3.connect(service.index) as connection:
        assert connection.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == len(previous_runs['runs'])
    # The interrupted transaction released locks; a deliberate retry commits
    # both indexes once the injected failure is removed.
    with sqlite3.connect(database) as connection:
        connection.execute('DROP TRIGGER refuse_publication')
        connection.commit()
    registry.publish_run(report)
    assert service.read_run(report['run_id']) == report
    assert len(service.list_runs()['runs']) == len(previous_runs['runs']) + 1
    assert len(registry.list_models()['models']) == len(previous_models['models']) * 2
