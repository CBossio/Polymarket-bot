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
wins = [t for t in resolved if t.get('result') == 'WON']
losses = [t for t in resolved if t.get('result') == 'LOST']

print(f"Total resolved: {len(resolved)}")
print(f"Wins: {len(wins)}, Losses: {len(losses)}")
print(f"True Win Rate: {len(wins)/len(resolved):.2%}")

buckets = {
    "0.60-0.70": {"res": 0, "wins": 0},
    "0.70-0.80": {"res": 0, "wins": 0},
    "0.80-0.90": {"res": 0, "wins": 0},
    "0.90-1.00": {"res": 0, "wins": 0},
}

for t in resolved:
    p = t.get('price', 0)
    if 0.6 <= p < 0.7: b = "0.60-0.70"
    elif 0.7 <= p < 0.8: b = "0.70-0.80"
    elif 0.8 <= p < 0.9: b = "0.80-0.90"
    elif 0.9 <= p <= 1.0: b = "0.90-1.00"
    else: continue
    
    buckets[b]["res"] += 1
    if t.get("result") == "WON":
        buckets[b]["wins"] += 1

print("\nWin rates by bucket:")
for k, v in buckets.items():
    if v["res"] > 0:
        print(f"{k}: {v['wins']}/{v['res']} ({v['wins']/v['res']:.2%})")

