import json
import os

trades_file = "logs/sim_trades_data.jsonl"
trades = []
if os.path.exists(trades_file):
    with open(trades_file, 'r') as f:
        for line in f:
            try: trades.append(json.loads(line.strip()))
            except: pass

resolved = [t for t in trades if t.get('result') in ('WON', 'LOST')]

# Helper to guess category from title/URL
def guess_category(market):
    q = market.get("market_question", "").lower()
    url = market.get("market_url", "").lower()
    
    if "cs2" in url or "counter-strike" in q or "esports" in url or "lol" in url or "valorant" in url:
        return "Esports"
    elif "mlb" in url or "baseball" in q or "innings" in q or "runs" in q or "hits" in q:
        return "Baseball"
    elif "nba" in url or "basketball" in q or "points" in q or "rebounds" in q:
        return "Basketball"
    elif "nhl" in url or "hockey" in q:
        return "Hockey"
    elif "soccer" in q or "epl" in url or "champions league" in q or "premier league" in q or "la liga" in q:
        return "Soccer"
    elif "atp" in url or "wta" in url or "tennis" in q or "set handicap" in q or "set winner" in q:
        return "Tennis"
    else:
        return "Other Sports/Events"

categories = {}
for t in resolved:
    cat = guess_category(t)
    if cat not in categories:
        categories[cat] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    
    categories[cat]["trades"] += 1
    if t.get("result") == "WON":
        categories[cat]["wins"] += 1
    else:
        categories[cat]["losses"] += 1
    
    categories[cat]["pnl"] += (t.get("pnl") or 0.0)

print(f"{'Category':<20} | {'Trades':<6} | {'Win%':<7} | {'PnL'}")
print("-" * 45)
for cat, stats in sorted(categories.items(), key=lambda x: x[1]['pnl'], reverse=True):
    win_rate = stats['wins'] / stats['trades'] if stats['trades'] > 0 else 0
    print(f"{cat:<20} | {stats['trades']:<6} | {win_rate:7.2%} | {stats['pnl']:.2f}")

