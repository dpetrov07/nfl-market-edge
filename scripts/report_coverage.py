"""Small local coverage audit; no feature engineering or modeling."""
from pathlib import Path
import argparse
import json
import re
import numpy as np
import pandas as pd


def name_key(name):
    name = re.sub(r'\b(jr|sr|ii|iii|iv|v)\b', '', str(name).lower())
    key = re.sub('[^a-z]', '', name)
    return {'hollywoodbrown': 'marquisebrown', 'cameronward': 'camward'}.get(key, key)


def number(value):
    return None if pd.isna(value) else float(value)


def summarize(out):
    stats = pd.read_parquet(out / 'nflverse_player_stats_2025.parquet')
    stats['name_key'] = stats.player_display_name.map(name_key)
    lookup = {}
    for r in stats.to_dict('records'):
        lookup.setdefault((r['game_id'], r['name_key']), []).append(r)
    summary = {}
    for platform in ['kalshi']:
        markets = pd.read_parquet(out / f'{platform}_prop_markets.parquet')
        markets['name_key'] = markets.player.map(name_key)
        player_ids, actuals, match_status = [], [], []
        for m in markets.to_dict('records'):
            candidates = lookup.get((m['game_id'], m['name_key']), [])
            player_ids.append(candidates[0]['player_id'] if len(candidates) == 1 else None)
            field = m['prop_type']
            actuals.append(candidates[0][field] if len(candidates) == 1 else None)
            match_status.append('exact_normalized_name_and_game' if len(candidates)==1 else 'ambiguous' if candidates else 'unmatched')
        markets['player_id'] = player_ids
        markets['actual_stat'] = actuals
        markets['player_match_status'] = match_status
        markets['threshold_review_flag'] = markets.threshold.gt(150)
        settled_over = markets.result.map({'yes': 1., 'no': 0.})
        markets['settlement_over_value'] = settled_over
        comparable = markets.actual_stat.notna() & settled_over.isin([0.,1.])
        markets['settlement_stat_disagreement'] = comparable & markets.actual_stat.gt(markets.threshold).ne(settled_over.eq(1.))
        markets.to_parquet(out / f'{platform}_market_player_matches.parquet', index=False)
        markets[markets.threshold_review_flag | markets.settlement_stat_disagreement].to_csv(out / f'{platform}_market_quality_flags.csv', index=False)
        details = []
        for m in markets.to_dict('records'):
            path = out / f'{platform}_price_parts' / (m['market_id']+'.parquet')
            auditpath = path.with_suffix('.json')
            audit = json.loads(auditpath.read_text()) if auditpath.exists() else {}
            r = {k: m[k] for k in ['market_id', 'player', 'player_id', 'prop_type', 'threshold', 'game_id', 'kickoff', 'open_time', 'close_time']}
            r.update(status=audit.get('status', 'not_attempted'), observations=0, usable_observations=0,
                     pregame_observations=0, distinct_pregame_prices=0, reference_observations=0,
                     trade_observations=0, pregame_trade_observations=0, narrow_pregame_quotes=0,
                     bid_ask_observations=0, volume_observations=0, open_interest_observations=0)
            if path.exists():
                p = pd.read_parquet(path)
                r['observations'] = len(p)
                if len(p):
                    p = p.sort_values('timestamp')
                    bid, ask = p.yes_bid_close, p.yes_ask_close
                    valid = bid.between(0,1)&ask.between(0,1)&bid.le(ask)
                    p['reference_price'] = ((bid+ask)/2).where(valid)
                    # Trade candles can be useful even if no valid quote exists.
                    trade = p.get('trade_close', pd.Series(np.nan, index=p.index))
                    r['usable_observations'] = int((valid|trade.between(0,1)).sum())
                    r['trade_observations'] = int(trade.notna().sum())
                    r['pregame_trade_observations'] = int((trade.notna()&p.timestamp.lt(m['kickoff'])).sum())
                    r['bid_ask_observations'] = int(valid.sum())
                    r['volume_observations'] = int(p.volume.notna().sum())
                    r['open_interest_observations'] = int(p.open_interest.notna().sum())
                    prevalid = valid & p.timestamp.lt(m['kickoff'])
                    r['median_pregame_spread'] = number((ask-bid)[prevalid].median())
                    r['narrow_pregame_quotes'] = int((prevalid & (ask-bid).le(.10)).sum())
                    p = p[p.reference_price.notna()]
                    r['reference_observations'] = len(p)
                    if len(p):
                        r['median_spacing_seconds'] = number(p.timestamp.diff().dt.total_seconds().median())
                        r['first_observation'] = p.timestamp.iloc[0]
                        r['last_observation'] = p.timestamp.iloc[-1]
                        r['first_price'] = number(p.reference_price.iloc[0])
                        r['first_hours_before_kickoff'] = (m['kickoff']-p.timestamp.iloc[0]).total_seconds()/3600
                        r['latest_hours_after_kickoff'] = (p.timestamp.iloc[-1]-m['kickoff']).total_seconds()/3600
                        pre = p[p.timestamp.lt(m['kickoff'])]
                        r['pregame_observations'] = len(pre)
                        if len(pre):
                            r['pregame_span_hours'] = (pre.timestamp.iloc[-1]-pre.timestamp.iloc[0]).total_seconds()/3600
                            r['last_pregame_price'] = number(pre.reference_price.iloc[-1])
                            r['min_pregame_price'] = number(pre.reference_price.min())
                            r['max_pregame_price'] = number(pre.reference_price.max())
                            r['last_pregame_lag_minutes'] = (m['kickoff']-pre.timestamp.iloc[-1]).total_seconds()/60
                            r['distinct_pregame_prices'] = int(pre.reference_price.nunique())
                            r['pregame_changes'] = int(pre.reference_price.diff().fillna(0).ne(0).sum())
            details.append(r)
        d = pd.DataFrame(details)
        d.to_parquet(out / f'{platform}_market_history_coverage.parquet', index=False)
        d.to_csv(out / f'{platform}_market_history_coverage.csv', index=False)
        attempted = d[d.status.ne('not_attempted')]
        usable = d[d.usable_observations.gt(0)]
        pre = d[d.pregame_observations.gt(0)]
        def median(col, frame=pre):
            return number(frame[col].median()) if col in frame else None
        s = {
            'markets_by_prop': markets.prop_type.value_counts().to_dict(),
            'total_markets': len(markets), 'unique_players': int(markets.name_key.nunique()),
            'unique_games': int(markets.game_id.nunique()),
            'unique_player_game_prop': len(markets.drop_duplicates(['name_key','game_id','prop_type'])),
            'unique_player_game_prop_threshold': len(markets.drop_duplicates(['name_key','game_id','prop_type','threshold'])),
            'kickoff_min': str(markets.kickoff.min()), 'kickoff_max': str(markets.kickoff.max()),
            'history_status': d.status.value_counts().to_dict(), 'markets_attempted': len(attempted),
            'markets_with_usable_history': len(usable), 'markets_with_pregame_reference_history': len(pre),
            'markets_with_changing_pregame_prices': int(d.distinct_pregame_prices.gt(1).sum()),
            'markets_with_at_least_10_pregame_prices': int(d.distinct_pregame_prices.ge(10).sum()),
            'total_observations': int(d.observations.sum()),
            'usable_observations': int(d.usable_observations.sum()),
            'reference_observations': int(d.reference_observations.sum()),
            'pregame_reference_observations': int(d.pregame_observations.sum()),
            'median_observations_per_attempted_market': median('observations', attempted),
            'median_observations_per_usable_market': median('observations', usable),
            'median_reference_observations_per_usable_market': median('reference_observations', usable),
            'median_pregame_reference_observations': median('pregame_observations'),
            'median_spacing_seconds': median('median_spacing_seconds', usable),
            'median_pregame_span_hours': median('pregame_span_hours'),
            'median_first_hours_before_kickoff': median('first_hours_before_kickoff'),
            'first_hours_before_kickoff_p10': number(pre.first_hours_before_kickoff.quantile(.1)) if len(pre) else None,
            'first_hours_before_kickoff_p90': number(pre.first_hours_before_kickoff.quantile(.9)) if len(pre) else None,
            'median_last_pregame_lag_minutes': median('last_pregame_lag_minutes'),
            'median_latest_hours_after_kickoff': median('latest_hours_after_kickoff', usable),
            'median_pregame_spread': median('median_pregame_spread'),
            'markets_with_narrow_pregame_quotes': int(d.narrow_pregame_quotes.gt(0).sum()),
            'markets_with_pregame_trades': int(d.pregame_trade_observations.gt(0).sum()),
            'trade_observations': int(d.trade_observations.sum()),
            'pregame_trade_observations': int(d.pregame_trade_observations.sum()),
            'bid_ask_observations': int(d.bid_ask_observations.sum()),
            'volume_observations': int(d.volume_observations.sum()),
            'open_interest_observations': int(d.open_interest_observations.sum()),
            'threshold_review_markets': int(markets.threshold_review_flag.sum()),
            'settlement_stat_disagreements': int(markets.settlement_stat_disagreement.sum()),
            'matched_market_rows': int(markets.player_id.notna().sum()),
            'unmatched_players': sorted(markets.loc[markets.player_id.isna(), 'player'].unique().tolist()),
            'history_unique_player_game_props': len(markets[markets.market_id.isin(usable.market_id)].drop_duplicates(['name_key','game_id','prop_type'])),
            'history_unique_games': int(markets[markets.market_id.isin(usable.market_id)].game_id.nunique()),
            'history_unique_players': int(markets[markets.market_id.isin(usable.market_id)].name_key.nunique()),
        }
        examples = []
        for prop in ['receiving_yards', 'rushing_yards']:
            candidates = pre[pre.prop_type.eq(prop) & pre.distinct_pregame_prices.ge(3)].sort_values('kickoff')
            if not len(candidates): continue
            for idx in sorted(set([0, len(candidates)//2, len(candidates)-1])):
                examples.append(candidates.iloc[idx].to_dict())
        selected_path = out / f'{platform}_history_selection.json'
        selected_ids = set(json.loads(selected_path.read_text())) if selected_path.exists() else set()
        selected = d[d.market_id.isin(selected_ids)]
        s['selected_sample'] = {'markets':len(selected), 'usable':int(selected.usable_observations.gt(0).sum()),
            'observations':int(selected.observations.sum()),
            'median_spacing_seconds':median('median_spacing_seconds', selected),
            'median_first_hours_before_kickoff':median('first_hours_before_kickoff', selected),
            'median_observations':median('observations', selected)}
        markets['has_usable_history'] = markets.market_id.isin(usable.market_id)
        markets['has_pregame_history'] = markets.market_id.isin(pre.market_id)
        s['examples'] = json.loads(pd.DataFrame(examples).to_json(orient="records", date_format="iso"))
        summary[platform] = s
        print(platform, {k:v for k,v in s.items() if k != 'examples'}, flush=True)
    summary['nflverse'] = {}
    for path in sorted(out.glob('nflverse_*.parquet')):
        d = pd.read_parquet(path)
        summary['nflverse'][path.stem] = {'rows':len(d), 'columns':len(d.columns),
             'week_min':number(d.week.min()) if 'week' in d else None,
             'week_max':number(d.week.max()) if 'week' in d else None}
    (out / 'coverage_summary.json').write_text(json.dumps(summary, indent=2, default=str, allow_nan=False))
    manifest_path = out / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest['coverage_report_utc'] = pd.Timestamp.now(tz='UTC').isoformat()
    manifest['observed_coverage'] = {platform: {key: summary[platform][key] for key in
        ['total_markets', 'markets_attempted', 'history_status', 'total_observations',
         'markets_with_usable_history']} for platform in ['kalshi']}
    manifest['nflverse'] = summary['nflverse']
    manifest_path.write_text(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=Path('data/raw'))
    summarize(parser.parse_args().out)
