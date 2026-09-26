import ast
import sys
import requests
import pandas as pd
from pathlib import Path

ENGINE_FILE = Path(__file__).with_name('smc_engine_step8_full.py')
SYMBOL = 'LTC_USDT'
TIMEFRAME = 'Min60'
BASE_URL = 'https://api.mexc.com/api/v1/contract/kline'
FETCH_LIMIT = 500
WARMUP_CANDLES = 200
MAX_REPLAY_SNAPSHOTS = 250
STEP7_STRENGTH = 2
POST_DISPLACEMENT_LOOKAHEAD = 12


def load_engine_functions():
    source = ENGINE_FILE.read_text(encoding='utf-8')
    marker = '\n# MAIN\n'
    if marker not in source:
        raise RuntimeError('Could not find MAIN marker in engine file.')
    prefix = source.split(marker, 1)[0]
    tree = ast.parse(prefix, filename=str(ENGINE_FILE))
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            keep.append(node)
    module = ast.Module(body=keep, type_ignores=[])
    code = compile(module, str(ENGINE_FILE), 'exec')
    namespace = {'__name__': 'replay_engine'}
    exec(code, namespace)
    return namespace


def fetch_candles():
    r = requests.get(
        f'{BASE_URL}/{SYMBOL}',
        params={'interval': TIMEFRAME},
        timeout=20,
    )
    r.raise_for_status()
    payload = r.json()
    if not payload.get('success'):
        raise RuntimeError(f'MEXC API error: {payload}')
    data = payload['data']
    df = pd.DataFrame({
        'time': data['time'],
        'open': data['open'],
        'high': data['high'],
        'low': data['low'],
        'close': data['close'],
        'volume': data['vol'],
    })
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df.dropna().drop_duplicates('time').sort_values('time').reset_index(drop=True).tail(FETCH_LIMIT)


def run_snapshot(df, ns):
    strength = ns['SWING_STRENGTH']
    swing_highs, swing_lows = ns['find_swings'](df, strength)
    events = ns['detect_historical_events'](df, swing_highs, swing_lows)
    structure = ns['get_current_structure'](swing_highs, swing_lows, events, df=df)
    liquidity = ns['analyze_liquidity'](df, swing_highs, swing_lows)
    premium_discount = ns['analyze_premium_discount'](df, swing_highs, swing_lows, structure['bias'])
    fvgs = ns['detect_fvgs'](df)
    order_blocks = ns['detect_order_blocks'](df, events)
    supply_demand = ns['detect_supply_demand'](df, events)
    preferred_pois, _ = ns['select_relevant_pois'](
        fvgs, order_blocks, supply_demand, liquidity['current_price'],
        structure['bias'], premium_discount=premium_discount, events=events, df=df,
    )
    all_pois = (
        [{**x, 'category': 'FVG'} for x in fvgs]
        + [{**x, 'category': 'ORDER BLOCK'} for x in order_blocks]
        + [{**x, 'category': 'SUPPLY/DEMAND'} for x in supply_demand]
    )
    sweeps = ns['detect_liquidity_sweeps'](
        df, swing_highs, swing_lows, preferred_pois=preferred_pois, all_pois=all_pois,
    )
    step5 = ns['get_step5_status'](sweeps, structure['bias'], preferred_pois=preferred_pois, events=events)
    step6 = ns['get_step6_status'](df, step5, structure['bias'])
    step7 = ns['get_step7_status'](df, step5, step6)
    step8 = ns['get_step8_status'](df, step7)
    return step5, step6, step7, step8


def find_latent_internal_levels(closed, sweep_index, displacement_index, direction, strength=2):
    """Diagnostic only.

    Finds pre-displacement local extrema that have enough LEFT context to be
    candidates, but whose RIGHT-side confirmation may only become available
    after the displacement candle. This does NOT change production Step 7.
    """
    levels = []
    start = max(strength, sweep_index + 1)
    end = displacement_index  # strictly before displacement
    if end <= start:
        return levels

    for i in range(start, end):
        if direction == 'BULLISH':
            left = closed.loc[i-strength:i-1, 'high']
            current = float(closed.loc[i, 'high'])
            if current <= float(left.max()):
                continue
            right_end = min(len(closed) - 1, i + strength)
            right = closed.loc[i+1:right_end, 'high']
            right_confirmed = len(right) == strength and current > float(right.max())
            levels.append({
                'type': 'SWING HIGH', 'direction': direction, 'price': current,
                'index': i, 'time': closed.loc[i, 'time'],
                'right_confirmation_available': right_confirmed,
                'confirmation_index': i + strength,
                'confirmation_time': closed.loc[min(i + strength, len(closed)-1), 'time'],
            })
        else:
            left = closed.loc[i-strength:i-1, 'low']
            current = float(closed.loc[i, 'low'])
            if current >= float(left.min()):
                continue
            right_end = min(len(closed) - 1, i + strength)
            right = closed.loc[i+1:right_end, 'low']
            right_confirmed = len(right) == strength and current < float(right.min())
            levels.append({
                'type': 'SWING LOW', 'direction': direction, 'price': current,
                'index': i, 'time': closed.loc[i, 'time'],
                'right_confirmation_available': right_confirmed,
                'confirmation_index': i + strength,
                'confirmation_time': closed.loc[min(i + strength, len(closed)-1), 'time'],
            })
    return levels


def diagnose_post_displacement_confirmation(closed, displacement_index, direction, candidates):
    """Diagnostic: can a pre-displacement candidate become confirmed after displacement,
    and is there a later close that breaks it? No production signal is emitted here.
    """
    confirmed_after = []
    break_events = []
    for level in candidates:
        confirm_idx = level['confirmation_index']
        if confirm_idx <= displacement_index:
            continue
        if confirm_idx >= len(closed):
            continue
        # Re-check the right-side confirmation using the candles that are actually
        # available at the confirmation time.
        i = level['index']
        if i + STEP7_STRENGTH >= len(closed):
            continue
        if direction == 'BULLISH':
            right = closed.loc[i+1:i+STEP7_STRENGTH, 'high']
            confirmed = float(level['price']) > float(right.max())
        else:
            right = closed.loc[i+1:i+STEP7_STRENGTH, 'low']
            confirmed = float(level['price']) < float(right.min())
        if not confirmed:
            continue
        confirmed_after.append(level)
        for j in range(max(displacement_index + 1, confirm_idx + 1), min(len(closed), confirm_idx + 1 + POST_DISPLACEMENT_LOOKAHEAD)):
            close = float(closed.loc[j, 'close'])
            broken = (direction == 'BULLISH' and close > float(level['price'])) or (direction == 'BEARISH' and close < float(level['price']))
            if broken:
                break_events.append({
                    'level': level,
                    'break_index': j,
                    'break_time': closed.loc[j, 'time'],
                    'break_close': close,
                })
                break
    return confirmed_after, break_events


def main():
    print('=== LTCUSDT.P STEP 5 → STEP 8 REPLAY V2 — STEP 7 DEBUG ===')
    print(f'Symbol: {SYMBOL} | Timeframe: 1H | Fetch: {FETCH_LIMIT} candles')
    df = fetch_candles()
    if len(df) < WARMUP_CANDLES + 10:
        raise RuntimeError(f'Not enough candles: {len(df)}')
    ns = load_engine_functions()

    start = WARMUP_CANDLES
    end = len(df) - 1
    if MAX_REPLAY_SNAPSHOTS:
        start = max(start, end - MAX_REPLAY_SNAPSHOTS + 1)

    cases = {}
    for closed_count in range(start, end + 1):
        closed = df.iloc[:closed_count].copy().reset_index(drop=True)
        replay_df = pd.concat([closed, closed.tail(1)], ignore_index=True)
        step5, step6, step7, step8 = run_snapshot(replay_df, ns)
        disp = step6.get('displacement') if step6 else None
        if not disp or not disp.get('confirmed'):
            continue
        candle = disp.get('candle') or {}
        disp_time = str(candle.get('time'))
        if disp_time in cases:
            continue
        sweep = step5.get('latest') or {}
        sweep_time = str(sweep.get('time'))
        matches = closed.index[closed['time'] == candle.get('time')].tolist()
        if not matches:
            continue
        disp_idx = int(matches[-1])
        sweep_idx = int(sweep.get('candle_index', -1))
        direction = disp.get('direction')
        candidates = find_latent_internal_levels(closed, sweep_idx, disp_idx, direction, STEP7_STRENGTH)
        strict_levels = ns['_find_post_sweep_internal_levels'](closed, sweep_idx, disp_idx, direction, STEP7_STRENGTH)
        confirmed_after, break_events = diagnose_post_displacement_confirmation(closed, disp_idx, direction, candidates)
        cases[disp_time] = {
            'displacement_time': disp_time,
            'direction': direction,
            'sweep_time': sweep_time,
            'sweep_index': sweep_idx,
            'displacement_index': disp_idx,
            'bars_sweep_to_displacement': disp_idx - sweep_idx,
            'strict_internal_level_count': len(strict_levels),
            'latent_candidate_count': len(candidates),
            'latent_candidates_confirmed_after_displacement': len(confirmed_after),
            'diagnostic_break_after_late_confirmation': len(break_events),
            'strict_step7_status': step7.get('status'),
            'strict_step7_reason': (step7.get('structure_break') or {}).get('reason'),
            'candidate_details': '|'.join(
                f"{x['time']} @ {x['price']:.4f} -> confirm {x['confirmation_time']}" for x in candidates
            ),
            'late_confirmed_details': '|'.join(
                f"{x['time']} @ {x['price']:.4f}" for x in confirmed_after
            ),
            'diagnostic_break_details': '|'.join(
                f"{x['break_time']} close {x['break_close']:.4f} >/< level {x['level']['price']:.4f}" for x in break_events
            ),
        }

    report = pd.DataFrame(cases.values())
    report = report.sort_values('displacement_time') if not report.empty else report
    report.to_csv('step5_step8_replay_v2_debug.csv', index=False)

    print('\n=== V2 DEBUG RESULT ===')
    print(f'Unique Step 6 displacement cases: {len(report)}')
    if report.empty:
        print('No Step 6 cases found.')
        return
    print(f'Cases with strict internal levels: {(report.strict_internal_level_count > 0).sum()}')
    print(f'Cases with latent pre-displacement candidates: {(report.latent_candidate_count > 0).sum()}')
    print(f'Cases where a candidate becomes confirmed only AFTER displacement: {(report.latent_candidates_confirmed_after_displacement > 0).sum()}')
    print(f'Cases with a diagnostic break after late confirmation: {(report.diagnostic_break_after_late_confirmation > 0).sum()}')
    print('\n--- Cases ---')
    for _, r in report.iterrows():
        print(
            f"{r['displacement_time']} | {r['direction']} | sweep {r['sweep_time']} | "
            f"bars {int(r['bars_sweep_to_displacement'])} | strict={int(r['strict_internal_level_count'])} | "
            f"latent={int(r['latent_candidate_count'])} | late_confirm={int(r['latent_candidates_confirmed_after_displacement'])} | "
            f"late_break={int(r['diagnostic_break_after_late_confirmation'])} | {r['strict_step7_status']}"
        )
    print('\nReport: step5_step8_replay_v2_debug.csv')
    print('Diagnostic-only: late-confirmed levels are NOT used as production Step 7 signals.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'REPLAY V2 FAILED: {exc}')
        sys.exit(1)
