import json
import os

trades_file = "logs/sim_trades_data.jsonl"
decisions_file = "logs/decisions.jsonl"

trades = []
if os.path.exists(trades_file):
    with open(trades_file, 'r') as f:
        for line in f:
            try:
                trades.append(json.loads(line.strip()))
            except: pass

if trades:
    total_trades = len(trades)
    won_trades = [t for t in trades if t.get('result') == 'WON']
    lost_trades = [t for t in trades if t.get('result') == 'LOST']
    
    win_rate = len(won_trades) / total_trades if total_trades > 0 else 0
    total_pnl = sum((t.get('pnl') or 0.0) for t in trades)
    
    print(f"Total trades: {total_trades}")
    print(f"Win rate: {win_rate:.2%}")
    print(f"Total PnL: {total_pnl:.2f}\n")
    
    # Calculate returns by price bucket
    buckets = {
        "0.50-0.60": {"trades": 0, "wins": 0, "pnl": 0},
        "0.60-0.70": {"trades": 0, "wins": 0, "pnl": 0},
        "0.70-0.80": {"trades": 0, "wins": 0, "pnl": 0},
        "0.80-0.90": {"trades": 0, "wins": 0, "pnl": 0},
        "0.90-1.00": {"trades": 0, "wins": 0, "pnl": 0},
    }
    
    for t in trades:
        price = t.get('price', 0)
        bucket_key = None
        if 0.5 <= price < 0.6: bucket_key = "0.50-0.60"
        elif 0.6 <= price < 0.7: bucket_key = "0.60-0.70"
        elif 0.7 <= price < 0.8: bucket_key = "0.70-0.80"
        elif 0.8 <= price < 0.9: bucket_key = "0.80-0.90"
        elif 0.9 <= price <= 1.0: bucket_key = "0.90-1.00"
        
        if bucket_key:
            buckets[bucket_key]["trades"] += 1
            if t.get("result") == "WON":
                buckets[bucket_key]["wins"] += 1
            buckets[bucket_key]["pnl"] += (t.get("pnl") or 0.0)
            
    print("Performance by Price Bucket:")
    for k, v in buckets.items():
        if v["trades"] > 0:
            w_rate = v["wins"] / v["trades"]
            print(f"Price {k}: {v['trades']} trades, Win rate: {w_rate:.2%}, PnL: {v['pnl']:.2f}")
            
    print("\nLosses Analysis:")
    if lost_trades:
        avg_loss_price = sum(t.get('price', 0) for t in lost_trades) / len(lost_trades)
        avg_loss_size = sum(t.get('size_usdc', 0) for t in lost_trades) / len(lost_trades)
        print(f"Average loss entry price: {avg_loss_price:.3f}")
        print(f"Average loss size (USDC): {avg_loss_size:.2f}")
else:
    print("No trades found.")

# Let's also look at decisions to see what's happening
decisions = []
if os.path.exists(decisions_file):
    with open(decisions_file, 'r') as f:
        for line in f:
            try:
                decisions.append(json.loads(line.strip()))
            except: pass

print("\n--- DECISIONS ANALYSIS ---")
print(f"Total decisions evaluated: {len(decisions)}")
buys = [d for d in decisions if d.get("decision") == "BUY"]
skips = [d for d in decisions if d.get("decision") == "SKIP"]
print(f"Total BUYs: {len(buys)}")
print(f"Total SKIPs: {len(skips)}")
