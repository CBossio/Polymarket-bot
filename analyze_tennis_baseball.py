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

def guess_category(market):
    q = market.get("market_question", "").lower()
    url = market.get("market_url", "").lower()
    if "mlb" in url or "baseball" in q or "innings" in q or "runs" in q or "hits" in q: return "Baseball"
    elif "atp" in url or "wta" in url or "tennis" in q or "set handicap" in q or "set winner" in q: return "Tennis"
    else: return "Other"

print("TENNIS LOSSES:")
for t in resolved:
    if guess_category(t) == "Tennis" and t.get('result') == 'LOST':
        print(f"Price: {t.get('price')}, Size: {t.get('size_usdc')}, Market: {t.get('market_question')}")

print("\nBASEBALL LOSSES:")
for t in resolved:
    if guess_category(t) == "Baseball" and t.get('result') == 'LOST':
        print(f"Price: {t.get('price')}, Size: {t.get('size_usdc')}, Market: {t.get('market_question')}")
