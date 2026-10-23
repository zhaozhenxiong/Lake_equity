"""Explicit, versioned native state; no pickle or executable deserialization.

The frozen CZSC runtime only exports retained raw bars. Restoration is admitted
only when reconstructing those bars reproduces its complete exposed geometry.
An unsupported retained state fails closed so callers can replay one stock.
"""
from __future__ import annotations

from datetime import datetime

from my_strategy.adapters.czsc_adapter import native_runtime, engine_contract
from my_strategy.core.run_context import stable_hash
from my_strategy.services.czsc_trend_support import copy_json

VERSION = 'czsc_explicit_state_v1'


class StateNotRestorable(ValueError):
    pass


def bar_record(bar):
    return dict(symbol=str(bar.symbol), dt=bar.dt.isoformat(), freq=str(bar.freq), id=int(bar.id),
                open=float(bar.open), high=float(bar.high), low=float(bar.low), close=float(bar.close),
                vol=float(bar.vol), amount=float(bar.amount))


def restore_bar(value):
    if set(value) != {'symbol','dt','freq','id','open','high','low','close','vol','amount'}:
        raise StateNotRestorable('native bar state schema mismatch')
    czsc=native_runtime()
    freq={'日线':czsc.Freq.D,'周线':czsc.Freq.W,'月线':czsc.Freq.M}.get(value['freq'])
    if freq is None: raise StateNotRestorable('unsupported native state frequency')
    return czsc.RawBar(**{**value,'dt':datetime.fromisoformat(value['dt']),'freq':freq})


def geometry(analysis):
    """Include retained inputs, forming bars, every pen and its dependencies."""
    def pen(item):
        return {'sdt':item.sdt.isoformat(),'edt':item.edt.isoformat(),'direction':str(item.direction),
                'high':float(item.high),'low':float(item.low),
                'raw':[bar_record(bar) for bar in item.raw_bars]}
    return {'raw':[bar_record(bar) for bar in analysis.bars_raw],
            'pens':[pen(item) for item in analysis.bi_list],
            'finished':[pen(item) for item in analysis.finished_bis],
            'ubi':[[bar_record(raw) for raw in bar.raw_bars] for bar in analysis.bars_ubi],
            'zones':[[item.sdt.isoformat(),item.edt.isoformat(),float(item.zd),float(item.zg)] for item in analysis.zs_list]}


def dump_session(session):
    if session.collect_confirmations or session.position is not None:
        raise StateNotRestorable('state export is restricted to causal evidence sessions')
    value={'format':VERSION,'engine':engine_contract(),'symbol':session.symbol,
           'config':copy_json(session.config),
           'bg':{freq:[bar_record(bar) for bar in bars] for freq,bars in session.bg.bars.items()},
           'kas':{freq:{'raw':[bar_record(bar) for bar in item.bars_raw],
                        'geometry_hash':stable_hash(geometry(item))} for freq,item in session.kas.items()},
           # advance_structure consumes only the last two closed weekly prices.
           'closed':{freq:[bar_record(bar) for bar in bars[-2:]] for freq,bars in session.closed.items()},
           'pending':{freq:bar_record(bar) for freq,bar in session.pending.items()}}
    value['sha256']=stable_hash(value)
    return value


def restore_session(value, *, cancel_check=None):
    from my_strategy.services.czsc_analysis import CzscSession
    if (value.get('format')!=VERSION or value.get('engine')!=engine_contract() or
        value.get('sha256')!=stable_hash({k:v for k,v in value.items() if k!='sha256'})):
        raise StateNotRestorable('native state schema, engine or SHA mismatch')
    session=CzscSession(value['symbol'],value['config'],0,collect_confirmations=False,create_position=False)
    # Only invoke the known frozen class with validated data; never call an
    # externally supplied constructor or pickle reduction.
    session.bg.__setstate__({'market':'A股','freq_bars':{
        freq:[restore_bar(bar) for bar in bars] for freq,bars in value['bg'].items()}})
    for freq,item in value['kas'].items():
        if cancel_check:
            if hasattr(cancel_check,'check'):cancel_check.check()
            elif cancel_check.is_set() if hasattr(cancel_check,'is_set') else cancel_check():
                raise InterruptedError('原生状态恢复已取消')
        analysis=session.czsc.CZSC([restore_bar(bar) for bar in item['raw']],
            min_bi_len=int(session.config['min_bi_len']),max_bi_num=int(session.config['max_bi_num']))
        if stable_hash(geometry(analysis))!=item['geometry_hash']:
            raise StateNotRestorable('retained native geometry cannot be restored exactly')
        session.kas[freq]=analysis
    session.closed={freq:[restore_bar(bar) for bar in bars] for freq,bars in value['closed'].items()}
    session.pending={freq:restore_bar(bar) for freq,bar in value['pending'].items()}
    return session


CHART_VERSION='czsc_chart_state_v1'


def dump_chart_session(session):
    """Explicit chart-session state: engine truth plus confirmation bookkeeping.

    Chart payloads need the full closed-bar history (evidence dumps retain only
    two), the per-kind confirmation dicts and divergence records. Geometry is
    re-verified from the retained bars at restore; the bookkeeping is protected
    by the whole-state SHA and the caller's content-addressed identity.
    """
    if not session.collect_confirmations or session.position is not None:
        raise StateNotRestorable('chart state export requires a confirmation session without position')
    if any(session.signals.values()) or session.events or session.decisions:
        raise StateNotRestorable('chart state export requires an advance-only session')
    value={'format':CHART_VERSION,'engine':engine_contract(),'symbol':session.symbol,
           'config':copy_json(session.config),'rows':len(session.closed['日线']),
           'bg':{freq:[bar_record(bar) for bar in bars] for freq,bars in session.bg.bars.items()},
           'closed':{freq:[bar_record(bar) for bar in bars] for freq,bars in session.closed.items()},
           'closed_available':{freq:list(items) for freq,items in session.closed_available.items()},
           'pending':{freq:bar_record(bar) for freq,bar in session.pending.items()},
           'geometry':{freq:stable_hash(geometry(analysis)) for freq,analysis in session.kas.items()},
           'confirmations':{freq:{kind:[[list(key),available] for key,available in mapping.items()]
                                  for kind,mapping in confirmed.items()}
                            for freq,confirmed in session.confirmations.items()},
           'divergence_seen':{freq:[list(key) for key in seen] for freq,seen in session.divergence_seen.items()},
           'divergences':copy_json(session.divergences)}
    value['sha256']=stable_hash(value)
    return value


def restore_chart_session(value, *, cancel_check=None):
    from my_strategy.services.czsc_analysis import CzscSession
    if (value.get('format')!=CHART_VERSION or value.get('engine')!=engine_contract() or
        value.get('sha256')!=stable_hash({k:v for k,v in value.items() if k!='sha256'})):
        raise StateNotRestorable('chart state schema, engine or SHA mismatch')
    closed={freq:[restore_bar(bar) for bar in bars] for freq,bars in value['closed'].items()}
    session=CzscSession(value['symbol'],value['config'],len(closed.get('日线',[])),
                        collect_confirmations=True,create_position=False)
    # Same known-class-only construction and geometry re-verification as the
    # causal evidence restore above.
    session.bg.__setstate__({'market':'A股','freq_bars':{
        freq:[restore_bar(bar) for bar in bars] for freq,bars in value['bg'].items()}})
    for freq,bars in closed.items():
        if not bars:
            continue
        if cancel_check:
            if hasattr(cancel_check,'check'):cancel_check.check()
            elif cancel_check.is_set() if hasattr(cancel_check,'is_set') else cancel_check():
                raise InterruptedError('图表状态恢复已取消')
        analysis=session.czsc.CZSC(list(bars),
            min_bi_len=int(session.config['min_bi_len']),max_bi_num=int(session.config['max_bi_num']))
        if stable_hash(geometry(analysis))!=value['geometry'].get(freq):
            raise StateNotRestorable('retained chart geometry cannot be restored exactly')
        session.kas[freq]=analysis
    session.closed=closed
    session.closed_available={freq:list(items) for freq,items in value['closed_available'].items()}
    session.pending={freq:restore_bar(bar) for freq,bar in value['pending'].items()}
    session.confirmations={freq:{kind:{tuple(key):available for key,available in pairs}
                                 for kind,pairs in confirmed.items()}
                           for freq,confirmed in value['confirmations'].items()}
    session.divergence_seen={freq:{tuple(key) for key in keys} for freq,keys in value['divergence_seen'].items()}
    session.divergences=copy_json(value['divergences'])
    return session
