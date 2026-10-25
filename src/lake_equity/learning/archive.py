"""Plan and execute reversible archival of frozen model bundles.

Archival never deletes bytes: a bundle directory is moved from
``var/learning/models`` to ``var/learning/archive/models`` and the move is
recorded in a separate ``archived_models`` table in ``models.db``. The
registry keeps reading archived bundles, while the default model list hides
them unless ``include_archived=True`` is requested.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3

from .registry import canonical, digest

MODEL_ID = re.compile(r'model-[0-9a-f]{32}')
PLAN_ID = re.compile(r'[0-9a-f]{64}')
ARCHIVE_REASON = 'not_single_default_method'

KEEP_PRIORITY = (
    'active_release',
    'release_reference',
    'study_reference',
    'workflow_reference',
    'run_report_reference',
    'recent_refit',
)


def _root(root) -> Path:
    return Path(root).resolve()


def _read_payloads(index: Path, table: str) -> list[dict]:
    if not index.is_file():
        return []
    with closing(sqlite3.connect(index.as_uri() + '?mode=ro', uri=True)) as conn:
        exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return []
        return [json.loads(row[0]) for row in conn.execute(f'SELECT summary FROM {table}')]


def _release_state(root: Path) -> tuple[list[dict], dict | None]:
    index = root / 'var/learning/releases.db'
    if not index.is_file():
        return [], None
    with closing(sqlite3.connect(index.as_uri() + '?mode=ro', uri=True)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        releases = [json.loads(row[0]) for row in conn.execute('SELECT payload FROM releases')] if 'releases' in tables else []
        active_id = None
        if 'activations' in tables:
            row = conn.execute('SELECT release_id FROM activations ORDER BY sequence DESC LIMIT 1').fetchone()
            active_id = row[0] if row else None
    payloads = {row.get('release_id'): row for row in releases}
    return releases, payloads.get(active_id)


def _model_directories(root: Path) -> dict[str, Path]:
    directory = root / 'var/learning/models'
    if not directory.is_dir() or directory.is_symlink():
        return {}
    return {path.name: path for path in directory.iterdir()
            if path.is_dir() and not path.is_symlink() and MODEL_ID.fullmatch(path.name)}


def _model_rows(root: Path) -> list[dict]:
    return _read_payloads(root / 'var/learning/models.db', 'models')


def _archived_ids(root: Path) -> set[str]:
    index = root / 'var/learning/models.db'
    if not index.is_file():
        return set()
    with closing(sqlite3.connect(index.as_uri() + '?mode=ro', uri=True)) as conn:
        exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='archived_models'").fetchone()
        if not exists:
            return set()
        return {row[0] for row in conn.execute('SELECT id FROM archived_models')}


def _collect_model_ids(value, found: set[str]) -> None:
    if isinstance(value, str):
        if MODEL_ID.fullmatch(value):
            found.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_model_ids(item, found)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _collect_model_ids(item, found)


def _files(root: Path) -> int:
    return sum(1 for path in root.rglob('*') if path.is_file())


def plan(root, *, keep_recent=2) -> dict:
    """Return a deterministic archive plan without moving anything."""
    if isinstance(keep_recent, bool) or not isinstance(keep_recent, int) or keep_recent < 0:
        raise ValueError('keep_recent must be a non-negative integer')
    root = _root(root)
    rows = _model_rows(root)
    directories = _model_directories(root)
    indexes = {row['model_id']: row for row in rows if row.get('model_id')}
    all_ids = set(indexes) | set(directories)
    archived = _archived_ids(root) & all_ids

    keep: dict[str, str] = {}

    def retain(model_id, reason: str) -> None:
        if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id):
            return
        if model_id not in all_ids:
            return
        current = keep.get(model_id)
        if current is None or KEEP_PRIORITY.index(reason) < KEEP_PRIORITY.index(current):
            keep[model_id] = reason

    releases, active_release = _release_state(root)
    if active_release:
        retain(active_release.get('model_id'), 'active_release')
    for release in releases:
        retain(release.get('model_id'), 'release_reference')

    for study in _read_payloads(root / 'var/learning/studies.db', 'studies'):
        retain(study.get('best_model_id'), 'study_reference')

    workflow_ids: set[str] = set()
    workflow_root = root / 'var/learning/workflows'
    workflow_summaries = _read_payloads(workflow_root / 'workflows.db', 'runs')
    if workflow_summaries:
        # The index already records the frozen spec (model_id/series_id); never
        # parse the full reports here — scan/backtest reports can be hundreds of
        # megabytes each and block the UI for minutes.
        for summary in workflow_summaries:
            _collect_model_ids(summary, workflow_ids)
    elif workflow_root.is_dir():
        for path in sorted(workflow_root.glob('*/report.json')):
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > 50 * 1024 * 1024:
                continue
            report = json.loads(path.read_text(encoding='utf-8'))
            _collect_model_ids(report.get('model_id'), workflow_ids)
            _collect_model_ids(report.get('model_ids'), workflow_ids)
            for member in report.get('cross_fold_members') or []:
                _collect_model_ids(member, workflow_ids)
    for model_id in sorted(workflow_ids):
        retain(model_id, 'workflow_reference')

    run_ids: set[str] = set()
    runs_index = root / 'var/learning/runs.db'
    for summary in _read_payloads(runs_index, 'runs'):
        _collect_model_ids(summary, run_ids)
    runs_root = root / 'var/learning/runs'
    if not runs_index.is_file() and runs_root.is_dir():
        # Isolated workspaces without the run index keep the slower fallback,
        # still guarded by a size cap so one oversized report cannot hang plan().
        for path in sorted(runs_root.glob('*/report.json')):
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > 50 * 1024 * 1024:
                continue
            _collect_model_ids(json.loads(path.read_text(encoding='utf-8')), run_ids)
    for model_id in sorted(run_ids):
        retain(model_id, 'run_report_reference')

    refits = [row for row in rows
              if row.get('usage') == 'frozen_inference_only' and row.get('model_id') in all_ids]
    refits.sort(key=lambda row: str(row.get('created_at') or row.get('frozen_at') or ''), reverse=True)
    remaining = keep_recent
    for row in refits:
        model_id = row['model_id']
        if model_id in keep:
            continue
        if remaining <= 0:
            break
        keep[model_id] = 'recent_refit'
        remaining -= 1

    archive = sorted(model_id for model_id in all_ids if model_id not in keep and model_id not in archived)
    plan_id = hashlib.sha256(canonical(archive).encode()).hexdigest()
    keep_rows = [dict(model_id=model_id, reason=keep[model_id]) for model_id in sorted(keep)]
    estimated_paths = sum(_files(directories[model_id]) for model_id in archive if model_id in directories)
    value = dict(schema='model_archive_plan_v1', plan_id=plan_id,
                 created_at=datetime.now().astimezone().isoformat(), total=len(all_ids),
                 keep=keep_rows, archive=archive, already_archived=len(archived),
                 estimated_paths=estimated_paths)
    target = root / 'var/learning/archive/plans' / (plan_id + '.json')
    if target.parent.is_symlink():
        raise ValueError('Archive plan directory cannot be a symlink')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical(value), encoding='utf-8')
    return value


def _connect(root: Path):
    path = root / 'var/learning/models.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute('CREATE TABLE IF NOT EXISTS archived_models ('
                 'id TEXT PRIMARY KEY, archived_at TEXT, archive_path TEXT, reason TEXT, plan_id TEXT)')
    return conn


def _load_plan(root: Path, plan_id: str) -> dict:
    if not isinstance(plan_id, str) or not PLAN_ID.fullmatch(plan_id):
        raise ValueError('Invalid archive plan identity')
    path = root / 'var/learning/archive/plans' / (plan_id + '.json')
    if path.is_symlink() or not path.is_file():
        raise KeyError(plan_id)
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema') != 'model_archive_plan_v1' or value.get('plan_id') != plan_id:
        raise ValueError('Archive plan identity mismatch')
    if not isinstance(value.get('archive'), list):
        raise ValueError('Archive plan archive list is invalid')
    return value


def _active_model_id(root: Path) -> str | None:
    _, active = _release_state(root)
    return active.get('model_id') if active else None


def _same_tree(left: Path, right: Path) -> bool:
    left_files = {path.relative_to(left).as_posix(): path for path in left.rglob('*') if path.is_file()}
    right_files = {path.relative_to(right).as_posix(): path for path in right.rglob('*') if path.is_file()}
    if set(left_files) != set(right_files):
        return False
    return all(digest(left_files[name]) == digest(right_files[name]) for name in left_files)


def execute(root, plan_id, model_ids=None, *, actor='local user') -> dict:
    """Move planned model bundles into the archive and index the move."""
    root = _root(root)
    plan_value = _load_plan(root, plan_id)
    planned = set(plan_value['archive'])
    if model_ids is None:
        selected = list(plan_value['archive'])
    else:
        if not isinstance(model_ids, (list, tuple)) or any(not isinstance(item, str) for item in model_ids):
            raise ValueError('model_ids must be a list of model identities')
        selected = []
        for model_id in model_ids:
            if model_id not in planned:
                raise ValueError('Model identity is not a member of this archive plan: ' + str(model_id))
            if model_id not in selected:
                selected.append(model_id)
    active_model = _active_model_id(root)
    if active_model and active_model in selected:
        raise ValueError('Cannot archive a model referenced by the active strategy release')

    archived = _archived_ids(root)
    models_dir = root / 'var/learning/models'
    archive_dir = root / 'var/learning/archive/models'
    moved: list[str] = []
    now = datetime.now().astimezone().isoformat()
    conn = _connect(root)
    try:
        for model_id in selected:
            if model_id in archived:
                continue
            source = models_dir / model_id
            destination = archive_dir / model_id
            if not source.is_dir():
                if destination.is_dir():
                    archived.add(model_id)
                    conn.execute('INSERT OR IGNORE INTO archived_models VALUES (?,?,?,?,?)',
                                 (model_id, now, 'var/learning/archive/models/' + model_id, ARCHIVE_REASON, plan_id))
                    continue
                raise KeyError('Model bundle not found: ' + model_id)
            if destination.exists():
                if _same_tree(source, destination):
                    archived.add(model_id)
                    conn.execute('INSERT OR IGNORE INTO archived_models VALUES (?,?,?,?,?)',
                                 (model_id, now, 'var/learning/archive/models/' + model_id, ARCHIVE_REASON, plan_id))
                    continue
                raise ValueError('Archive destination already exists with different content: ' + model_id)
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            moved.append(model_id)
            archived.add(model_id)
            conn.execute('INSERT OR IGNORE INTO archived_models VALUES (?,?,?,?,?)',
                         (model_id, now, 'var/learning/archive/models/' + model_id, ARCHIVE_REASON, plan_id))
        conn.commit()
    finally:
        conn.close()
    return dict(plan_id=plan_id, archived=len(moved), model_ids=moved,
                path=str(root / 'var/learning/archive'))


def archived_ids(root) -> set[str]:
    return _archived_ids(_root(root))


def archived_rows(root) -> list[dict]:
    index = _root(root) / 'var/learning/models.db'
    if not index.is_file():
        return []
    with closing(sqlite3.connect(index.as_uri() + '?mode=ro', uri=True)) as conn:
        exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='archived_models'").fetchone()
        if not exists:
            return []
        columns = [row[1] for row in conn.execute('PRAGMA table_info(archived_models)')]
        return [dict(zip(columns, row)) for row in conn.execute(
            'SELECT id, archived_at, archive_path, reason, plan_id FROM archived_models ORDER BY archived_at, id')]
