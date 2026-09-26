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
    SWING_STRENGTH = ns['SWING_STRENGTH']
    swing_highs, swing_lows = ns['find_swings'](df, SWING_STRENGTH)
    events = ns['detect_historical_events'](df, swing_highs, swing_lows)
    structure = ns['get_current_structure'](swing_highs, swing_lows, events, df=df)
    liquidity = ns['analyze_liquidity'](df, swing_highs, swing_lows)
    premium_discount = ns['analyze_premium_discount'](df, swing_highs, swing_lows, structure['bias'])
    fvgs = ns['detect_fvgs'](df)
    order_blocks = ns['detect_order_blocks'](df, events)
    supply_demand = ns['detect_supply_demand'](df, events)
    preferred_pois, _ = ns['select_relevant_pois'](
        fvgs,
        order_blocks,
        supply_demand,
        liquidity['current_price'],
        structure['bias'],
        premium_discount=premium_discount,
        events=events,
        df=df,
    )
    all_pois = (
        [{**x, 'category': 'FVG'} for x in fvgs]
        + [{**x, 'category': 'ORDER BLOCK'} for x in order_blocks]
        + [{**x, 'category': 'SUPPLY/DEMAND'} for x in supply_demand]
    )
    sweeps = ns['detect_liquidity_sweeps'](
        df, swing_highs, swing_lows,
        preferred_pois=preferred_pois,
        all_pois=all_pois,
    )
    step5 = ns['get_step5_status'](sweeps, structure['bias'], preferred_pois=preferred_pois, events=events)
    step6 = ns['get_step6_status'](df, step5, structure['bias'])
    step7 = ns['get_step7_status'](df, step5, step6)
    step8 = ns['get_step8_status'](df, step7)
    return step5, step6, step7, step8


def main():
    print('=== LTCUSDT.P STEP 5 → STEP 8 HISTORICAL REPLAY ===')
    print(f'Symbol: {SYMBOL} | Timeframe: 1H | Fetch: {FETCH_LIMIT} candles')
    print('Downloading MEXC Futures candles...')
    df = fetch_candles()
    if len(df) < WARMUP_CANDLES + 10:
        raise RuntimeError(f'Not enough candles: {len(df)}')
    print(f'Loaded: {len(df)} candles')

    ns = load_engine_functions()
    start = WARMUP_CANDLES
    end = len(df) - 1  # exclude current/forming candle from replay target
    if MAX_REPLAY_SNAPSHOTS:
        start = max(start, end - MAX_REPLAY_SNAPSHOTS + 1)

    rows = []
    unique_step8 = {}
    unique_step7 = {}
    unique_step6 = {}
    unique_step5 = {}

    for closed_count in range(start, end + 1):
        closed = df.iloc[:closed_count].copy().reset_index(drop=True)
        # Engine convention: final row is the still-forming candle.
        replay_df = pd.concat([closed, closed.tail(1)], ignore_index=True)
        step5, step6, step7, step8 = run_snapshot(replay_df, ns)
        asof = closed.iloc[-1]['time']

        sweep = step5.get('latest')
        disp = step6.get('displacement') if step6 else None
        sb = step7.get('structure_break') if step7 else None
        rt = step8.get('retest') if step8 else None

        if sweep:
            key = (str(sweep.get('time')), str(sweep.get('direction')), round(float(sweep.get('level', 0)), 6))
            unique_step5[key] = sweep
        if disp and disp.get('confirmed'):
            c = disp.get('candle') or {}
            key = str(c.get('time'))
            unique_step6[key] = disp
        if step7.get('confirmed') and sb:
            key = (str(sb.get('break_time')), round(float(sb.get('broken_level', 0)), 6))
            unique_step7[key] = sb
        if step8.get('confirmed') and rt:
            key = (str(rt.get('break_time')), str(rt.get('time')), round(float(rt.get('level', 0)), 6))
            unique_step8[key] = rt

        rows.append({
            'asof': asof,
            'step5': step5.get('status'),
            'step6': step6.get('status'),
            'step7': step7.get('status'),
            'step8': step8.get('status'),
            'step6_confirmed': bool(disp and disp.get('confirmed')),
            'step7_confirmed': bool(step7.get('confirmed')),
            'step8_confirmed': bool(step8.get('confirmed')),
        })

    report = pd.DataFrame(rows)
    report.to_csv('step5_step8_replay_report.csv', index=False)

    print('\n=== REPLAY RESULT ===')
    print(f'Snapshots analyzed: {len(report)}')
    print(f'Unique Step 5 active sweeps observed: {len(unique_step5)}')
    print(f'Unique Step 6 displacement confirmations: {len(unique_step6)}')
    print(f'Unique Step 7 structure breaks: {len(unique_step7)}')
    print(f'Unique Step 8 confirmed retests: {len(unique_step8)}')

    if unique_step8:
        print('\nConfirmed full-chain Step 8 events:')
        for rt in unique_step8.values():
            print(
                f"  {rt['direction']} | BOS: {rt['break_time']} | "
                f"Retest: {rt['time']} | Level: {rt['level']:.4f} | "
                f"Close: {rt['close']:.4f}"
            )
    else:
        print('\nNo full Step 5 → Step 8 confirmation occurred in the replay window.')

    print('\nImportant: this is a mechanical replay, not a profitability test.')
    print('It measures whether the current rules generate sequential confirmations without look-ahead.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'REPLAY FAILED: {exc}')
        sys.exit(1)
