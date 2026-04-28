import json
from collections import Counter
import os

trades_file = "logs/sim_trades_data.jsonl"
decisions_file = "logs/decisions.jsonl"

print("--- ANÁLISIS DE PÉRDIDAS ---")
if os.path.exists(trades_file):
    with open(trades_file, "r") as f:
        for line in f:
            try:
                trade = json.loads(line.strip())
                if trade.get("result") == "LOST":
                    print(f"Mercado: {trade.get('market')}")
                    print(f"  Precio de entrada: {trade.get('price')}")
                    print(f"  Tamaño (USDC): {trade.get('size_usdc')}")
                    print(f"  PnL: {trade.get('pnl')}")
                    print("---")
            except:
                pass

print("\n--- TOP 5 MOTIVOS DE SKIP ---")
skip_reasons = []
if os.path.exists(decisions_file):
    with open(decisions_file, "r") as f:
        for line in f:
            try:
                dec = json.loads(line.strip())
                if dec.get("decision") == "SKIP":
                    reason = dec.get("reason", "")
                    if "Wide spread" in reason: reason = "Wide spread"
                    elif "Price barely moved" in reason: reason = "Price barely moved"
                    elif "Focus ratio" in reason: reason = "Focus ratio too high"
                    elif "Initial consensus too low" in reason: reason = "Initial consensus too low"
                    elif "Consensus dropped" in reason: reason = "Consensus dropped"
                    elif "Already resolved" in reason: reason = "Already resolved"
                    elif "No smart money conviction" in reason: reason = "No smart money conviction"
                    elif "Starts in" in reason: reason = "Event starts too soon"
                    skip_reasons.append(reason)
            except:
                pass
    
    counts = Counter(skip_reasons)
    for reason, count in counts.most_common(10):
        print(f"{count} veces: {reason}")
