import ast
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
POST_DISPLACEMENT_LOOKAHEAD = 24


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
    r = requests.get(f'{BASE_URL}/{SYMBOL}', params={'interval': TIMEFRAME}, timeout=20)
    r.raise_for_status()
    payload = r.json()
    if not payload.get('success'):
        raise RuntimeError(f'MEXC API error: {payload}')
    data = payload['data']
    df = pd.DataFrame({
        'time': data['time'], 'open': data['open'], 'high': data['high'],
        'low': data['low'], 'close': data['close'], 'volume': data['vol']
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
        fvgs, order_blocks, supply_demand, liquidity['current_price'], structure['bias'],
        premium_discount=premium_discount, events=events, df=df,
    )
    all_pois = (
        [{**x, 'category': 'FVG'} for x in fvgs]
        + [{**x, 'category': 'ORDER BLOCK'} for x in order_blocks]
        + [{**x, 'category': 'SUPPLY/DEMAND'} for x in supply_demand]
    )
    sweeps = ns['detect_liquidity_sweeps'](df, swing_highs, swing_lows, preferred_pois=preferred_pois, all_pois=all_pois)
    step5 = ns['get_step5_status'](sweeps, structure['bias'], preferred_pois=preferred_pois, events=events)
    step6 = ns['get_step6_status'](df, step5, structure['bias'])
    step7 = ns['get_step7_status'](df, step5, step6)
    step8 = ns['get_step8_status'](df, step7)
    return step5, step6, step7, step8


def latent_candidates(closed, sweep_index, displacement_index, direction, strength=2):
    out = []
    start = max(strength, sweep_index + 1)
    end = displacement_index
    if end <= start:
        return out
    for i in range(start, end):
        if direction == 'BULLISH':
            current = float(closed.loc[i, 'high'])
            left = closed.loc[i-strength:i-1, 'high']
            if current <= float(left.max()):
                continue
            out.append({'type': 'SWING HIGH', 'direction': direction, 'price': current, 'index': i, 'time': closed.loc[i, 'time'], 'confirmation_index': i + strength})
        else:
            current = float(closed.loc[i, 'low'])
            left = closed.loc[i-strength:i-1, 'low']
            if current >= float(left.min()):
                continue
            out.append({'type': 'SWING LOW', 'direction': direction, 'price': current, 'index': i, 'time': closed.loc[i, 'time'], 'confirmation_index': i + strength})
    return out


def confirm_candidate_when_available(closed, candidate, direction):
    ci = int(candidate['confirmation_index'])
    if ci >= len(closed):
        return False
    i = int(candidate['index'])
    if direction == 'BULLISH':
        right = closed.loc[i+1:i+STEP7_STRENGTH, 'high']
        return len(right) == STEP7_STRENGTH and float(candidate['price']) > float(right.max())
    right = closed.loc[i+1:i+STEP7_STRENGTH, 'low']
    return len(right) == STEP7_STRENGTH and float(candidate['price']) < float(right.min())


def find_bos_after_confirmation(closed, candidate, displacement_index, direction):
    ci = int(candidate['confirmation_index'])
    start = max(displacement_index + 1, ci + 1)
    end = min(len(closed), start + POST_DISPLACEMENT_LOOKAHEAD)
    for j in range(start, end):
        close = float(closed.loc[j, 'close'])
        broken = (direction == 'BULLISH' and close > float(candidate['price'])) or (direction == 'BEARISH' and close < float(candidate['price']))
        if broken:
            return {
                'index': j, 'time': closed.loc[j, 'time'], 'direction': direction,
                'event': 'BOS', 'level_type': candidate['type'], 'broken_level': float(candidate['price']),
                'open': float(closed.loc[j, 'open']), 'high': float(closed.loc[j, 'high']),
                'low': float(closed.loc[j, 'low']), 'close': close,
                'break_method': 'CLOSE ABOVE LEVEL' if direction == 'BULLISH' else 'CLOSE BELOW LEVEL'
            }
    return None


def find_retest_after_bos(closed, bos, direction):
    if not bos:
        return None
    start = int(bos['index']) + 1
    end = min(len(closed), start + POST_DISPLACEMENT_LOOKAHEAD)
    level = float(bos['broken_level'])
    for j in range(start, end):
        row = closed.loc[j]
        touched = float(row['low']) <= level <= float(row['high'])
        if not touched:
            continue
        close = float(row['close'])
        valid = (direction == 'BULLISH' and close > level) or (direction == 'BEARISH' and close < level)
        if valid:
            return {'index': j, 'time': row['time'], 'close': close, 'level': level, 'type': 'VALID RETEST'}
    return None


def evaluate_case(df, ns, case):
    disp_idx = case['displacement_index']
    sweep_idx = case['sweep_index']
    direction = case['direction']
    # Evaluate every closed snapshot after displacement so confirmation and BOS
    # happen only when the necessary candles have actually closed.
    candidates = latent_candidates(df.iloc[:disp_idx+1].reset_index(drop=True), sweep_idx, disp_idx, direction, STEP7_STRENGTH)
    results = []
    for cand in candidates:
        confirm_idx = int(cand['confirmation_index'])
        if confirm_idx >= len(df):
            continue
        closed_at_confirmation = df.iloc[:confirm_idx+1].reset_index(drop=True)
        if not confirm_candidate_when_available(closed_at_confirmation, cand, direction):
            continue
        closed_after = df.reset_index(drop=True)
        bos = find_bos_after_confirmation(closed_after, cand, disp_idx, direction)
        retest = find_retest_after_bos(closed_after, bos, direction)
        results.append((cand, bos, retest))
    # Prefer the earliest candidate that actually reaches BOS; otherwise earliest confirmed candidate.
    with_bos = [x for x in results if x[1] is not None]
    chosen = sorted(with_bos, key=lambda x: x[1]['index'])[0] if with_bos else (sorted(results, key=lambda x: x[0]['index'])[0] if results else None)
    return results, chosen


def main():
    print('=== LTCUSDT.P STEP 5 → STEP 8 REPLAY V3 — FULL SEQUENCE TRACE ===')
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
        matches = closed.index[closed['time'] == candle.get('time')].tolist()
        if not matches:
            continue
        disp_idx = int(matches[-1])
        sweep_idx = int(sweep.get('candle_index', -1))
        if sweep_idx < 0 or sweep_idx >= disp_idx:
            continue
        direction = disp.get('direction')
        case = {'displacement_time': disp_time, 'direction': direction, 'sweep_time': str(sweep.get('time')), 'sweep_index': sweep_idx, 'displacement_index': disp_idx}
        cases[disp_time] = case

    rows = []
    for case in cases.values():
        subset = df.reset_index(drop=True)
        results, chosen = evaluate_case(subset, ns, case)
        strict = ns['_find_post_sweep_internal_levels'](subset.iloc[:case['displacement_index']+1], case['sweep_index'], case['displacement_index'], case['direction'], STEP7_STRENGTH)
        row = {
            **case,
            'bars_sweep_to_displacement': case['displacement_index'] - case['sweep_index'],
            'strict_internal_levels_at_displacement': len(strict),
            'latent_candidate_count': len(latent_candidates(subset.iloc[:case['displacement_index']+1], case['sweep_index'], case['displacement_index'], case['direction'], STEP7_STRENGTH)),
            'late_confirmed_candidate_count': len(results),
            'bos_after_late_confirmation': int(any(x[1] is not None for x in results)),
            'retest_after_late_bos': int(any(x[2] is not None for x in results if x[1] is not None)),
            'chosen_candidate': '', 'chosen_candidate_time': '', 'candidate_price': '',
            'candidate_confirmation_time': '', 'bos_time': '', 'bos_level': '', 'bos_close': '',
            'retest_time': '', 'retest_close': '', 'retest_level': '',
        }
        if chosen:
            cand, bos, retest = chosen
            row['chosen_candidate'] = cand['type']
            row['chosen_candidate_time'] = str(cand['time'])
            row['candidate_price'] = cand['price']
            row['candidate_confirmation_time'] = str(subset.loc[cand['confirmation_index'], 'time']) if cand['confirmation_index'] < len(subset) else ''
            if bos:
                row['bos_time'] = str(bos['time']); row['bos_level'] = bos['broken_level']; row['bos_close'] = bos['close']
            if retest:
                row['retest_time'] = str(retest['time']); row['retest_close'] = retest['close']; row['retest_level'] = retest['level']
        row['sequence_result'] = (
            'RETEST_CONFIRMED' if row['retest_after_late_bos'] else
            'BOS_CONFIRMED_NO_RETEST' if row['bos_after_late_confirmation'] else
            'LATE_SWING_CONFIRMED_NO_BOS' if row['late_confirmed_candidate_count'] else
            'NO_LATE_SWING_CONFIRMATION'
        )
        rows.append(row)

    report = pd.DataFrame(rows).sort_values('displacement_time') if rows else pd.DataFrame()
    report.to_csv('step5_step8_replay_v3_full_sequence.csv', index=False)

    print('\n=== V3 RESULT ===')
    print(f'Unique Step 6 displacement cases: {len(report)}')
    if report.empty:
        print('No Step 6 cases found.'); return
    print(f'Cases with latent candidates: {(report.latent_candidate_count > 0).sum()}')
    print(f'Cases with late-confirmed candidates: {(report.late_confirmed_candidate_count > 0).sum()}')
    print(f'Cases reaching BOS after late confirmation: {(report.bos_after_late_confirmation > 0).sum()}')
    print(f'Cases reaching valid retest after that BOS: {(report.retest_after_late_bos > 0).sum()}')
    print('\n--- Sequence Cases ---')
    for _, r in report.iterrows():
        print(f"{r['displacement_time']} | {r['direction']} | sweep {r['sweep_time']} | latent={int(r['latent_candidate_count'])} | late_confirm={int(r['late_confirmed_candidate_count'])} | BOS={int(r['bos_after_late_confirmation'])} | RETEST={int(r['retest_after_late_bos'])} | {r['sequence_result']}")
    print('\nReport: step5_step8_replay_v3_full_sequence.csv')
    print('Diagnostic only. No production Step 7/8 logic is changed by this replay.')


if __name__ == '__main__':
    main()
