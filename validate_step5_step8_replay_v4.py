import ast
import requests
import pandas as pd
from pathlib import Path

ENGINE_FILE = Path(__file__).with_name('smc_engine_step8_full.py')
SYMBOL = 'LTC_USDT'
TIMEFRAME = 'Min60'
BASE_URL = 'https://api.mexc.com/api/v1/contract/kline'
FETCH_LIMIT = 1000
WARMUP_CANDLES = 200
MAX_REPLAY_SNAPSHOTS = 700
STEP7_STRENGTH = 2
WIDE_LOOKAHEADS = (6, 12, 20, 40)


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
            price = float(closed.loc[i, 'high'])
            left = closed.loc[i-strength:i-1, 'high']
            if price <= float(left.max()):
                continue
            right_end = min(i + strength, len(closed) - 1)
            out.append({'type':'SWING HIGH','direction':direction,'price':price,'index':i,'time':closed.loc[i,'time'], 'confirmation_index':i+strength, 'right_needed_end':right_end})
        else:
            price = float(closed.loc[i, 'low'])
            left = closed.loc[i-strength:i-1, 'low']
            if price >= float(left.min()):
                continue
            right_end = min(i + strength, len(closed) - 1)
            out.append({'type':'SWING LOW','direction':direction,'price':price,'index':i,'time':closed.loc[i,'time'], 'confirmation_index':i+strength, 'right_needed_end':right_end})
    return out


def candidate_confirmed(closed, cand, strength=2):
    i = int(cand['index'])
    ci = i + strength
    if ci >= len(closed):
        return False
    price = float(cand['price'])
    if cand['direction'] == 'BULLISH':
        right = closed.loc[i+1:i+strength, 'high']
        return len(right) == strength and price > float(right.max())
    right = closed.loc[i+1:i+strength, 'low']
    return len(right) == strength and price < float(right.min())


def find_bos(closed, level_price, start_index, direction, lookahead):
    end = min(len(closed), start_index + lookahead + 1)
    for j in range(start_index, end):
        close = float(closed.loc[j, 'close'])
        broken = (direction == 'BULLISH' and close > level_price) or (direction == 'BEARISH' and close < level_price)
        if broken:
            return {'index':j,'time':closed.loc[j,'time'],'direction':direction,'broken_level':level_price,'close':close,'high':float(closed.loc[j,'high']),'low':float(closed.loc[j,'low'])}
    return None


def find_retest(closed, bos, direction, lookahead):
    if bos is None:
        return None
    level = float(bos['broken_level'])
    start = bos['index'] + 1
    end = min(len(closed), start + lookahead + 1)
    for j in range(start, end):
        row = closed.loc[j]
        touched = float(row['low']) <= level <= float(row['high'])
        if not touched:
            continue
        close = float(row['close'])
        valid = (direction == 'BULLISH' and close > level) or (direction == 'BEARISH' and close < level)
        if valid:
            return {'index':j,'time':row['time'],'level':level,'close':close}
    return None


def trace_case(df, case):
    full = df.reset_index(drop=True)
    disp = case['displacement_index']
    sweep = case['sweep_index']
    direction = case['direction']
    pre = full.iloc[:disp+1].reset_index(drop=True)
    candidates = latent_candidates(pre, sweep, disp, direction, STEP7_STRENGTH)

    confirmed = []
    for cand in candidates:
        ci = cand['confirmation_index']
        if ci >= len(full):
            continue
        closed_at_ci = full.iloc[:ci+1].reset_index(drop=True)
        if candidate_confirmed(closed_at_ci, cand, STEP7_STRENGTH):
            confirmed.append((cand, ci))

    # Evaluate each candidate against progressively wider post-confirmation windows.
    candidate_rows = []
    for cand, ci in confirmed:
        for lookahead in WIDE_LOOKAHEADS:
            bos = find_bos(full, float(cand['price']), max(disp+1, ci+1), direction, lookahead)
            retest = find_retest(full, bos, direction, lookahead) if bos else None
            candidate_rows.append({
                **case,
                'candidate_type':cand['type'],
                'candidate_time':str(cand['time']),
                'candidate_index':cand['index'],
                'candidate_price':cand['price'],
                'candidate_confirmation_time':str(full.loc[ci,'time']),
                'candidate_confirmation_index':ci,
                'lookahead':lookahead,
                'bos':int(bos is not None),
                'bos_time':str(bos['time']) if bos else '',
                'bos_close':bos['close'] if bos else '',
                'retest':int(retest is not None),
                'retest_time':str(retest['time']) if retest else '',
            })
    return candidates, confirmed, candidate_rows


def main():
    print('=== LTCUSDT.P STEP 5 → STEP 8 REPLAY V4 — WIDE WINDOW DIAGNOSTIC ===')
    print(f'Symbol: {SYMBOL} | Timeframe: 1H | Fetch: {FETCH_LIMIT} candles | Lookaheads: {WIDE_LOOKAHEADS}')
    df = fetch_candles()
    if len(df) < WARMUP_CANDLES + 20:
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
        cases[disp_time] = {
            'displacement_time':disp_time,'direction':direction,
            'sweep_time':str(sweep.get('time')),'sweep_index':sweep_idx,'displacement_index':disp_idx
        }

    case_rows = []
    candidate_rows = []
    for case in cases.values():
        full = df.reset_index(drop=True)
        strict = ns['_find_post_sweep_internal_levels'](full.iloc[:case['displacement_index']+1], case['sweep_index'], case['displacement_index'], case['direction'], STEP7_STRENGTH)
        candidates, confirmed, details = trace_case(full, case)
        candidate_rows.extend(details)
        row = {
            **case,
            'bars_sweep_to_displacement':case['displacement_index']-case['sweep_index'],
            'strict_internal_levels_at_displacement':len(strict),
            'latent_candidate_count':len(candidates),
            'late_confirmed_candidate_count':len(confirmed),
            'current_step7_confirmed':0,
        }
        for lookahead in WIDE_LOOKAHEADS:
            subset = [r for r in details if r['lookahead'] == lookahead]
            row[f'bos_within_{lookahead}'] = int(any(r['bos'] for r in subset))
            row[f'retest_within_{lookahead}'] = int(any(r['retest'] for r in subset))
        row['diagnostic_result'] = (
            'RETEST_FOUND' if any(r['retest'] for r in details) else
            'BOS_FOUND_NO_RETEST' if any(r['bos'] for r in details) else
            'LATE_SWING_CONFIRMED_NO_BOS' if confirmed else
            'LATENT_ONLY' if candidates else
            'NO_INTERNAL_CANDIDATE'
        )
        case_rows.append(row)

    cases_df = pd.DataFrame(case_rows).sort_values('displacement_time') if case_rows else pd.DataFrame()
    candidates_df = pd.DataFrame(candidate_rows).sort_values(['displacement_time','candidate_time','lookahead']) if candidate_rows else pd.DataFrame()
    cases_df.to_csv('step5_step8_replay_v4_cases.csv', index=False)
    candidates_df.to_csv('step5_step8_replay_v4_candidates.csv', index=False)

    print('\n=== V4 RESULT ===')
    print(f'Unique Step 6 displacement cases: {len(cases_df)}')
    if cases_df.empty:
        print('No Step 6 cases found.')
        return
    print(f'Cases with latent candidates: {(cases_df.latent_candidate_count > 0).sum()}')
    print(f'Cases with late-confirmed candidates: {(cases_df.late_confirmed_candidate_count > 0).sum()}')
    for n in WIDE_LOOKAHEADS:
        print(f'Cases with BOS within {n} candles: {(cases_df[f"bos_within_{n}"] > 0).sum()}')
        print(f'Cases with retest within {n} candles: {(cases_df[f"retest_within_{n}"] > 0).sum()}')
    print('\n--- Cases ---')
    for _, r in cases_df.iterrows():
        print(f"{r['displacement_time']} | {r['direction']} | sweep={r['sweep_time']} | latent={int(r['latent_candidate_count'])} | confirmed={int(r['late_confirmed_candidate_count'])} | BOS6={int(r['bos_within_6'])} BOS12={int(r['bos_within_12'])} BOS20={int(r['bos_within_20'])} BOS40={int(r['bos_within_40'])} | RETEST40={int(r['retest_within_40'])} | {r['diagnostic_result']}")
    print('\nReports: step5_step8_replay_v4_cases.csv, step5_step8_replay_v4_candidates.csv')
    print('Diagnostic only. No production Step 7/8 logic is changed.')


if __name__ == '__main__':
    main()
