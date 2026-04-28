import os
from dotenv import load_dotenv

load_dotenv()

# --- Polymarket API Endpoints ---
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# --- Target Market Tag IDs (Polymarket Gamma API) ---
# 1  = Sports (NBA, NFL, Soccer, etc.)
# 2  = Crypto (BTC prices, ETF approvals, airdrops)
# 3  = Politics (Elections, debates, breaking news)
# 4  = Pop Culture (Oscars, Box Office)
# 6  = Business / Economy (Fed rates, CPI, inflation)
# 9  = Science / Technology (SpaceX, OpenAI releases)
# 64 = Esports (CS2, LoL, Valorant)
SPORTS_TAG_IDS = [1, 2, 3, 4, 6, 9, 64]

# --- Strategy Parameters ---
MIN_LIQUIDITY_USD = 200             # Minimum liquidity to START observing (filters out ghost markets early)
MIN_TRADE_LIQUIDITY_USD = 300       # Minimum liquidity to actually PLACE an order
CONSENSUS_THRESHOLD = 0.65          # Probability must exceed 65% to trigger buy
MAX_CONSENSUS_THRESHOLD = 0.85      # Probability must NOT exceed 85% (terrible risk/reward ratio)
OBSERVATION_WINDOW_SECS = 1200      # 20-minute observation window
MIN_PRICE_MOVE = 0.02               # Required mid-price move during window (when starting below threshold)
MIN_HOURS_TO_EVENT = 1              # Skip markets starting in < 1h (avoids live/in-play slippage)
MAX_HOURS_TO_EVENT = 168            # Skip markets where game starts > 168h (1 week) from now
CORR_BOOST_THRESHOLD = 0.60         # Min consensus for a correlated market to count as confirmation
FOCUS_RATIO_NOISE_THRESHOLD = 500   # FR above this = algorithmic noise, skip
SCANNER_INTERVAL_SECS = 30          # Poll Gamma API every 30 seconds

# --- Risk Management ---
MAX_POSITION_PCT = 0.10             # Max 10% of bankroll per single trade
MAX_POSITION_USDC = 10.0            # Hard cap of $10 USD per trade
DAILY_DRAWDOWN_KILL_PCT = 0.05      # Kill switch at 5% daily loss
MAX_SLIPPAGE_PCT = 0.02             # Abort order if slippage > 2%

# --- Position Sizing ---
MIN_POSITION_USDC = 2.0             # Don't place orders below this size
MAX_SPREAD = 0.25                   # Skip if bid-ask spread > 25% (market too uncertain)
MAX_MARKET_AGE_HOURS = 72           # Ignore markets created more than N hours ago
TAKE_PROFIT_MULTIPLIER = 2.0        # Auto-sell when position value reaches this × entry price
STOP_LOSS_THRESHOLD = 0.55          # Auto-sell (cut losses) if win probability drops below 55%
PROFIT_CHECK_INTERVAL_SECS = 60     # How often to check open positions for take-profit

# --- Blockchain / Polymarket Auth ---
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE")
SAFE_ADDRESS = os.getenv("SAFE_ADDRESS")   # Gnosis Safe address (funder)

# --- Redis ---
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# --- Bot Capital (can also be fetched from Safe balance dynamically) ---
BANKROLL_USDC = float(os.getenv("BANKROLL_USDC", 500.0))
