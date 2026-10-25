"""Small presentation views over immutable learning reports."""
from copy import deepcopy
from urllib.parse import quote


_LEARNING_FAMILIES = {
    'cross_sectional_return_v1', 'ten_year_model_study_v1', 'frozen_factor_refit_v1',
}
_ACCOUNT_ARRAYS = {'daily', 'trades', 'orders', 'decisions', 'ledger', 'rejections'}


def learning_dashboard_view(report: dict) -> dict:
    """Copy a learning report without account row arrays; leave other families alone.

    Omitted arrays are counted before copying, so a dashboard response does not
    allocate a second copy of the potentially large account history. The complete
    immutable report remains available from the explicit archive URL.
    """
    family = report.get('family')
    if family not in _LEARNING_FAMILIES:
        return report
    is_study = family == 'ten_year_model_study_v1'
    identity = report.get('study_id') if is_study else report.get('run_id')
    identity = identity or report.get('run_id') or report.get('lake_run_id')
    route = 'studies' if is_study else 'runs'
    full_url = f'/api/v1/learning/{route}/{quote(str(identity), safe="")}' if identity else None

    def copy_view(value, *, account=False):
        if isinstance(value, dict):
            result, omitted = {}, {}
            for key, child in value.items():
                if account and key in _ACCOUNT_ARRAYS and isinstance(child, list):
                    omitted[key] = len(child)
                else:
                    result[key] = copy_view(child, account=key in {'portfolio', 'benchmark'})
            if omitted:
                result['dashboard_archive'] = dict(full_report_url=full_url, omitted_arrays=omitted)
            return result
        if isinstance(value, list):
            return [copy_view(child) for child in value]
        return deepcopy(value)

    result = copy_view(report)
    result['dashboard_view'] = dict(kind='dashboard_summary', full_report_url=full_url)
    return result
