"""CUDA-first compute device resolution for local research jobs.

The project runs MLP training, Kronos sampling and other torch work on one
workstation. Prefer the GPU when it is usable, but never pretend: an explicit
``cuda`` request fails loudly when no device is available, and every consumer
records the actual device it ran on.
"""
from __future__ import annotations

VALID = ('cpu', 'cuda')


def cuda_available() -> bool:
    """True only when torch imports and reports a usable CUDA device."""
    try:
        import torch
    except Exception:
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def report() -> dict:
    """Read-only device facts for reports and the dashboard."""
    result = {'requested': None, 'resolved': None, 'cuda_available': cuda_available()}
    try:
        import torch
    except Exception:
        result['torch'] = None
        result['resolved'] = 'cpu'
        return result
    result['torch'] = torch.__version__
    if result['cuda_available']:
        try:
            props = torch.cuda.get_device_properties(0)
            result.update(gpu=props.name, gpu_memory_gib=round(props.total_memory / 2 ** 30, 1),
                          capability=f'{props.major}.{props.minor}')
        except Exception:
            pass
    result['resolved'] = 'cuda' if result['cuda_available'] else 'cpu'
    return result


def resolve(requested=None) -> str:
    """Resolve a device request; ``None`` means the CUDA-first project default."""
    if requested in (None, '', 'auto'):
        return 'cuda' if cuda_available() else 'cpu'
    if requested not in VALID:
        raise ValueError('device must be one of cpu, cuda, or omitted for the CUDA-first default')
    if requested == 'cuda' and not cuda_available():
        raise ValueError('CUDA device requested but torch.cuda.is_available() is false')
    return requested
