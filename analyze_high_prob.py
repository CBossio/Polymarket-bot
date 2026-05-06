import json
import os

trades_file = "logs/sim_trades_data.jsonl"
trades = []
if os.path.exists(trades_file):
    with open(trades_file, 'r') as f:
        for line in f:
            try: trades.append(json.loads(line.strip()))
            except: pass

high_prob = [t for t in trades if t.get('price', 0) >= 0.8]
for t in high_prob:
    print(f"Price: {t.get('price')}, Result: {t.get('result')}, Size: {t.get('size_usdc')}, PnL: {t.get('pnl')}")

