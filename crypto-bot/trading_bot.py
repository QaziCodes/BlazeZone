import time
import requests
import pandas as pd
from datetime import datetime, timezone
import ccxt
import logging
import json
import sys
from typing import Dict, List, Optional, Tuple
import numpy as np
import ccxt.async_support as ccxt_async
import asyncio

# Configure logging for debugging and tracking
logging.basicConfig(
    filename='trading_bot.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filemode='a'
)

# API credentials (replace with your own)
API_KEY = "684084ab6025980001ef6ac3"
API_SECRET = "0cbb014b-223d-4c82-833a-f1555cceb51c"
API_PASSPHRASE = "Muhammad12099021"
CRYPTO_PANIC_API_KEY = "5176231a99efb4a1c385a47f9039ff36d1a50f78"

# Trading parameters
COINS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "AVAX/USDT",
    "XRP/USDT", "ADA/USDT", "TRX/USDT", "LTC/USDT", "DOT/USDT",
    "LINK/USDT", "ATOM/USDT", "ARB/USDT", "OP/USDT", "INJ/USDT"
]
INTERVAL = "5m"
LIMIT = 50  # Reduced from 100 to avoid data issues
REPEAT_DELAY = 30  # Seconds between scans
STOP_LOSS_PERCENT = 0.007  # 0.7% stop-loss
TRADING_FEE_PERCENT = 0.001
MIN_TRADE_AMOUNT = {
    "BTC/USDT": 0.0001, "ETH/USDT": 0.001, "BNB/USDT": 0.01, "SOL/USDT": 0.1,
    "AVAX/USDT": 0.1, "XRP/USDT": 1, "ADA/USDT": 10, "TRX/USDT": 10,
    "LTC/USDT": 0.01, "DOT/USDT": 0.1, "LINK/USDT": 0.1, "ATOM/USDT": 0.1,
    "ARB/USDT": 1, "OP/USDT": 1, "INJ/USDT": 0.1
}
TRAILING_STOP_PERCENT = {
    "BTC/USDT": 0.01, "ETH/USDT": 0.01,  # Low volatility
    "SOL/USDT": 0.015, "DOT/USDT": 0.015, "LINK/USDT": 0.015, "ADA/USDT": 0.015,  # Medium
    "AVAX/USDT": 0.02, "XRP/USDT": 0.02, "LTC/USDT": 0.02,  # Medium-High
    "TRX/USDT": 0.025, "ATOM/USDT": 0.025, "ARB/USDT": 0.025, "OP/USDT": 0.025, "INJ/USDT": 0.025  # High
}
COOLDOWN_MINUTES = 30  # 30-minute cooldown after a buy+sell
OVERTRADING_DELAY = 180  # 3-minute wait after a trade
MIN_CANDLES = 14  # Minimum candles required for indicators

# Global state
positions: Dict[str, Dict] = {}
trade_history: List[Dict] = []
cooldowns: Dict[str, datetime] = {}
last_trade_time: Optional[datetime] = None

# Initialize exchange and validate credentials
try:
    exchange = ccxt.kucoin({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'password': API_PASSPHRASE,
        'enableRateLimit': True,
        'timeout': 60000  # Increased timeout
    })
    exchange.load_markets()
    # Verify API credentials by fetching balance
    exchange.fetch_balance()
    logging.info("KuCoin exchange initialized and credentials validated successfully")
    print("Exchange initialized successfully")
except ccxt.AuthenticationError as e:
    logging.error(f"Authentication error: Invalid API credentials: {e}")
    print(f"Error: Invalid API credentials. Please check API_KEY, API_SECRET, and API_PASSPHRASE.")
    sys.exit(1)
except Exception as e:
    logging.error(f"Failed to initialize KuCoin exchange: {e}")
    print(f"Error initializing exchange: {e}")
    sys.exit(1)

# Validate trading pairs
valid_coins = []
for coin in COINS:
    if coin in exchange.markets and exchange.markets[coin].get('active', False):
        valid_coins.append(coin)
    else:
        logging.warning(f"{coin} is not an active trading pair, skipping.")
        print(f"Warning: {coin} is not an active trading pair, skipping.")
COINS[:] = valid_coins  # Update COINS list with only valid pairs
if not COINS:
    logging.error("No valid trading pairs available. Exiting.")
    print("Error: No valid trading pairs available. Exiting.")
    sys.exit(1)

def send_notification(message: str) -> None:
    """Log and print messages for debugging."""
    logging.info(message)
    print(message)

async def fetch_data(symbol: str) -> Optional[pd.DataFrame]:
    """Fetch OHLCV data with enhanced retries and ticker fallback."""
    async_exchange = ccxt_async.kucoin({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'password': API_PASSPHRASE,
        'enableRateLimit': True,
        'timeout': 60000
    })
    try:
        for attempt in range(5):
            try:
                ohlcv = await async_exchange.fetch_ohlcv(symbol, INTERVAL, limit=LIMIT)
                if not ohlcv:
                    logging.warning(f"No OHLCV data returned for {symbol} on attempt {attempt+1}")
                    await asyncio.sleep(2 ** attempt * 1.5)
                    continue
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df = df.astype(float).iloc[::-1].reset_index(drop=True)
                if len(df) < MIN_CANDLES:
                    logging.warning(f"Data length {len(df)} for {symbol} too short, padding with last value")
                    last_row = df.iloc[-1:].copy()
                    df = pd.concat([pd.DataFrame([last_row.iloc[0]] * (MIN_CANDLES - len(df)), columns=df.columns), df], ignore_index=True)
                logging.info(f"Fetched {len(df)} candles for {symbol}")
                return df
            except ccxt.RateLimitExceeded as e:
                delay = 2 ** attempt * 1.5
                logging.warning(f"Rate limit hit for {symbol}: {e}. Retrying after {delay}s")
                await asyncio.sleep(delay)
            except ccxt.NetworkError as e:
                logging.warning(f"Network error for {symbol}: {e}. Retrying attempt {attempt+1}/5")
                await asyncio.sleep(2)
            except Exception as e:
                logging.error(f"Attempt {attempt+1} failed to fetch OHLCV for {symbol}: {e}")
                if attempt < 4:
                    await asyncio.sleep(2)
                    continue
                # Fallback to ticker data
                try:
                    ticker = await async_exchange.fetch_ticker(symbol)
                    price = ticker.get('last', None)
                    if not price:
                        logging.error(f"No valid price in ticker for {symbol}")
                        return None
                    df = pd.DataFrame({
                        'timestamp': [int(time.time() * 1000)] * MIN_CANDLES,
                        'open': [price] * MIN_CANDLES,
                        'high': [price] * MIN_CANDLES,
                        'low': [price] * MIN_CANDLES,
                        'close': [price] * MIN_CANDLES,
                        'volume': [ticker.get('baseVolume', 0)] * MIN_CANDLES
                    })
                    logging.warning(f"Fallback to ticker data for {symbol} with price {price}")
                    return df
                except Exception as e:
                    logging.error(f"Fallback ticker fetch failed for {symbol}: {e}")
                    return None
    finally:
        await async_exchange.close()
    return None

def validate_symbol(symbol: str) -> bool:
    """Validate trading pair availability."""
    for attempt in range(3):
        try:
            return symbol in exchange.markets and exchange.markets[symbol].get('active', False)
        except ccxt.NetworkError as e:
            logging.warning(f"Network error validating {symbol}: {e}. Retrying...")
            time.sleep(2 ** attempt)
        except Exception as e:
            logging.error(f"Error validating symbol {symbol}: {e}")
            return False
    return False

def check_balance(symbol: str, signal: str) -> float:
    """Check available balance for trading."""
    for attempt in range(3):
        try:
            balance = exchange.fetch_balance()
            if signal == "BUY":
                usdt_balance = balance.get('USDT', {}).get('free', 0)
                logging.info(f"USDT balance: {usdt_balance:.2f} for {symbol} BUY check")
                return usdt_balance
            elif signal == "SELL":
                coin = symbol.split('/')[0]
                coin_balance = balance.get(coin, {}).get('free', 0)
                logging.info(f"{coin} balance: {coin_balance:.6f} for {symbol} SELL check")
                return coin_balance
            return 0
        except ccxt.NetworkError as e:
            logging.warning(f"Network error checking balance for {symbol}: {e}. Retrying...")
            time.sleep(2 ** attempt)
        except Exception as e:
            logging.error(f"Error checking balance for {symbol}: {e}")
            return 0
    return 0

def calculate_ema(data: pd.Series, period: int) -> float:
    """Calculate EMA for the last value."""
    if len(data) < period:
        return data.iloc[-1]
    return data.ewm(span=period, adjust=False).mean().iloc[-1]

def calculate_rsi(data: pd.Series, period: int = 14) -> float:
    """Calculate RSI."""
    if len(data) < period + 1:
        return 50.0
    delta = data.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=period).mean()
    rs = gain / loss.replace(0, 0.0001)
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 50.0

def calculate_macd_hist(data: pd.Series) -> float:
    """Calculate MACD histogram."""
    if len(data) < 26:
        return 0.0
    ema12 = data.ewm(span=12, adjust=False).mean()
    ema26 = data.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return (macd_line - signal_line).iloc[-1]

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Calculate ATR."""
    if len(df) < period:
        return 0.0001
    tr1 = df['high'] - df['low']
    tr2 = abs(df['high'] - df['close'].shift())
    tr3 = abs(df['low'] - df['close'].shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr.iloc[-1] if not pd.isna(atr.iloc[-1]) else 0.0001

def calculate_ichimoku_base(df: pd.DataFrame) -> float:
    """Calculate Ichimoku Base Line."""
    if len(df) < 26:
        return df['close'].iloc[-1]
    high_26 = df['high'].rolling(window=26).max()
    low_26 = df['low'].rolling(window=26).min()
    return (high_26 + low_26).iloc[-1] / 2

def calculate_stochastic_rsi(data: pd.Series, period: int = 14) -> float:
    """Calculate Stochastic RSI."""
    if len(data) < period + 1:
        return 0.5
    rsi_series = pd.Series(data).rolling(window=period + 1).apply(
        lambda x: calculate_rsi(pd.Series(x)), raw=False
    )
    stoch_rsi = rsi_series.rolling(window=period).apply(
        lambda x: (x[-1] - x.min()) / (x.max() - x.min()) if x.max() != x.min() else 0.0, raw=True
    )
    return stoch_rsi.iloc[-1] if not pd.isna(stoch_rsi.iloc[-1]) else 0.5

def signal_a(df: pd.DataFrame) -> Tuple[str, int]:
    """Signal A: Trend and candlestick patterns (100 points max)."""
    try:
        close = df['close']
        open_ = df['open']
        high = df['high']
        low = df['low']
        ema50 = calculate_ema(close, 50)
        ema200 = calculate_ema(close, 200)
        trend_score = 0
        if ema50 > ema200 and close.iloc[-1] > ema50:
            trend_score = 50  # Uptrend
        elif ema50 < ema200 and close.iloc[-1] < ema50:
            trend_score = -50  # Downtrend
        candle_score = 0
        last_candle = df.iloc[-1]
        prev_candle = df.iloc[-2]
        # Bullish patterns
        if (last_candle['close'] > last_candle['open'] and
                last_candle['close'] > prev_candle['high'] and
                last_candle['open'] < prev_candle['close']):
            candle_score = 50  # Bullish Engulfing
        elif (last_candle['close'] > last_candle['open'] and
              abs(last_candle['close'] - last_candle['open']) > 0.8 * (last_candle['high'] - last_candle['low'])):
            candle_score = 50  # Marubozu
        elif (last_candle['low'] < min(prev_candle['low'], prev_candle['close']) and
              last_candle['close'] > last_candle['open'] and
              (last_candle['high'] - last_candle['close']) < 0.2 * (last_candle['close'] - last_candle['open'])):
            candle_score = 50  # Hammer
        # Bearish patterns
        elif (last_candle['close'] < last_candle['open'] and
              last_candle['close'] < prev_candle['low'] and
              last_candle['open'] > prev_candle['close']):
            candle_score = -50  # Bearish Engulfing
        elif (high.iloc[-1] < high.iloc[-2] and high.iloc[-2] < high.iloc[-3]):
            candle_score = -50  # Lower highs
        total_score = trend_score + candle_score
        if total_score >= 50:
            return "BUY", min(total_score, 100)
        elif total_score <= -50:
            return "SELL", min(abs(total_score), 100)
        return "WAIT", 0
    except Exception as e:
        logging.error(f"Error in Signal A: {e}")
        return "WAIT", 0

def signal_b(df: pd.DataFrame) -> Tuple[str, int]:
    """Signal B: Indicators (70 points max, 10 per indicator)."""
    try:
        close = df['close']
        volume = df['volume']
        indicators = {
            'rsi': calculate_rsi(close),
            'ema': calculate_ema(close, 9) > calculate_ema(close, 21),
            'macd_hist': calculate_macd_hist(close),
            'volume': volume.iloc[-1] > volume.rolling(10).mean().iloc[-1] * 1.5,
            'atr': calculate_atr(df) > 0,
            'ichimoku_base': calculate_ichimoku_base(df) < close.iloc[-1],
            'stoch_rsi': calculate_stochastic_rsi(close)
        }
        buy = sell = 0
        if indicators['rsi'] < 30:
            buy += 10
        elif indicators['rsi'] > 70:
            sell += 10
        if indicators['ema']:
            buy += 10
        else:
            sell += 10
        if indicators['macd_hist'] > 0:
            buy += 10
        else:
            sell += 10
        if indicators['volume']:
            buy += 10
        else:
            sell += 10
        if indicators['atr']:
            buy += 10
        if indicators['ichimoku_base']:
            buy += 10
        else:
            sell += 10
        if indicators['stoch_rsi'] < 0.2:
            buy += 10
        elif indicators['stoch_rsi'] > 0.8:
            sell += 10
        total_score = buy - sell
        signal = "WAIT"
        accuracy = 0
        if buy >= 40:
            signal = "BUY"
            accuracy = {40: 40, 50: 50, 60: 60, 70: 70}.get(buy, 40)
        elif sell >= 40:
            signal = "SELL"
            accuracy = {40: 40, 50: 50, 60: 60, 70: 70}.get(sell, 40)
        logging.info(f"Signal B: {json.dumps(indicators, default=str)}")
        return signal, accuracy
    except Exception as e:
        logging.error(f"Error in Signal B: {e}")
        return "WAIT", 0

def signal_c(symbol: str) -> Tuple[str, int]:
    """Signal C: Order book analysis (100 points max)."""
    try:
        order_book = exchange.fetch_order_book(symbol, limit=20)
        bids = pd.Series([x[1] for x in order_book['bids']])
        asks = pd.Series([x[1] for x in order_book['asks']])
        bid_volume = bids.sum()
        ask_volume = asks.sum()
        total_volume = bid_volume + ask_volume
        if total_volume == 0:
            return "WAIT", 0
        bid_ratio = bid_volume / total_volume
        score = int((bid_ratio - 0.5) * 200)
        if score >= 50:
            return "BUY", min(score, 100)
        elif score <= -50:
            return "SELL", min(abs(score), 100)
        return "WAIT", 0
    except Exception as e:
        logging.error(f"Error in Signal C for {symbol}: {e}")
        return "WAIT", 0

def signal_d(symbol: str) -> Tuple[bool, str, int]:
    """Signal D: News check (100 points max)."""
    try:
        coin = symbol.split('/')[0]
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTO_PANIC_API_KEY}&filter=important&currencies={coin}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        news = response.json().get('results', [])
        for article in news:
            created_at = datetime.strptime(article['created_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - created_at).total_seconds() / 60 < 60:
                title = article['title'].lower()
                if any(keyword in title for keyword in ['crash', 'ban', 'hack', 'dump']):
                    return False, f"Negative news: {title}", 0
                if any(keyword in title for keyword in ['pump', 'etf', 'adoption', 'rally']):
                    return True, f"Positive news: {title}", 100
        return True, "No significant news", 50
    except Exception as e:
        logging.error(f"News fetch error for {symbol}: {e}")
        return True, "News check failed, proceeding", 50

async def signal_e(symbol: str) -> Tuple[str, int]:
    """Signal E: On-chain data analysis (100 points max)."""
    async_exchange = ccxt_async.kucoin({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'password': API_PASSPHRASE,
        'enableRateLimit': True
    })
    try:
        trades = await async_exchange.fetch_trades(symbol, limit=50)
        large_trades = [t for t in trades if t['amount'] * t['price'] > 10000]
        buy_volume = sum(t['amount'] for t in large_trades if t['side'] == 'buy')
        sell_volume = sum(t['amount'] for t in large_trades if t['side'] == 'sell')
        total_volume = buy_volume + sell_volume
        if total_volume == 0:
            return "WAIT", 0
        score = int(((buy_volume / total_volume) - 0.5) * 200)
        if score >= 50:
            return "BUY", min(score, 100)
        elif score <= -50:
            return "SELL", min(abs(score), 100)
        return "WAIT", 0
    except Exception as e:
        logging.error(f"Error in Signal E for {symbol}: {e}")
        return "WAIT", 0
    finally:
        await async_exchange.close()

def place_trade(symbol: str, signal: str, price: float) -> Optional[Dict]:
    """Place a market order with cooldown and overtrading checks."""
    try:
        if symbol in cooldowns and (datetime.now(timezone.utc) - cooldowns[symbol]).total_seconds() < COOLDOWN_MINUTES * 60:
            logging.info(f"{symbol} in cooldown, skipping trade")
            return None
        global last_trade_time
        if last_trade_time and (datetime.now(timezone.utc) - last_trade_time).total_seconds() < OVERTRADING_DELAY:
            logging.info(f"Overtrading delay active, skipping trade")
            return None
        news_ok, news_msg, _ = signal_d(symbol)
        if not news_ok:
            msg = f"Skipped {symbol} {signal}: {news_msg}"
            send_notification(msg)
            return None
        if signal == "BUY":
            usdt_balance = check_balance(symbol, "BUY")
            if usdt_balance <= 0:
                logging.warning(f"Insufficient USDT balance for {symbol} BUY")
                return None
            min_amount = MIN_TRADE_AMOUNT.get(symbol, 0)
            amount = usdt_balance / price
            trade_usdt = usdt_balance
            if amount < min_amount:
                amount = min_amount
                trade_usdt = min_amount * price
                if trade_usdt > usdt_balance:
                    logging.warning(f"Adjusted trade amount {trade_usdt:.6f} USDT for {symbol} exceeds balance {usdt_balance:.6f}")
                    return None
            buy_fee = trade_usdt * TRADING_FEE_PERCENT
            order = exchange.create_market_buy_order(symbol, amount)
            positions[symbol] = {
                'amount': amount,
                'entry_price': price,
                'peak_price': price,
                'buy_fee': buy_fee,
                'open_time': datetime.now(timezone.utc)
            }
            trade_history.append({
                'symbol': symbol,
                'type': 'BUY',
                'amount': amount,
                'price': price,
                'fee': buy_fee,
                'time': datetime.now(timezone.utc)
            })
            msg = f"Placed BUY order for {symbol}: {amount:.6f} at {price:.6f} (Fee: {buy_fee:.6f} USDT)"
            send_notification(msg)
            with open("trades.txt", "a", encoding='utf-8') as f:
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"{now} - {msg}\n")
            last_trade_time = datetime.now(timezone.utc)
            cooldowns[symbol] = datetime.now(timezone.utc)
            return order
        elif signal == "SELL":
            coin = symbol.split('/')[0]
            pos = positions.get(symbol, {})
            amount = pos.get('amount', 0)
            if amount < MIN_TRADE_AMOUNT.get(symbol, 0):
                logging.warning(f"Sell amount {amount:.6f} for {symbol} below minimum")
                return None
            if amount > 0:
                sell_value = amount * price
                sell_fee = sell_value * TRADING_FEE_PERCENT
                order = exchange.create_market_sell_order(symbol, amount)
                buy_fee = pos.get('buy_fee', 0)
                entry_price = pos.get('entry_price', price)
                profit = sell_value - (amount * entry_price) - buy_fee - sell_fee
                trade_history.append({
                    'symbol': symbol,
                    'type': 'SELL',
                    'amount': amount,
                    'price': price,
                    'fee': sell_fee,
                    'profit': profit,
                    'time': datetime.now(timezone.utc)
                })
                msg = f"Closed SELL order for {symbol}: {amount:.6f} at {price:.6f} (Profit/Loss: {profit:.6f} USDT)"
                send_notification(msg)
                with open("trades.txt", "a", encoding='utf-8') as f:
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"{now} - {msg}\n")
                positions.pop(symbol, None)
                last_trade_time = datetime.now(timezone.utc)
                cooldowns[symbol] = datetime.now(timezone.utc)
                return order
            return None
    except ccxt.NetworkError as e:
        logging.error(f"Network error placing {signal} order for {symbol}: {e}")
        return None
    except Exception as e:
        logging.error(f"Error placing {signal} order for {symbol}: {e}")
        return None

def manage_positions(current_prices: Dict[str, float]) -> None:
    """Manage positions with stop-loss and trailing stop."""
    for symbol, pos in list(positions.items()):
        try:
            current_price = current_prices.get(symbol, pos['entry_price'])
            entry_price = pos['entry_price']
            peak_price = pos['peak_price']
            amount = pos['amount']
            buy_fee = pos['buy_fee']
            if current_price > peak_price:
                positions[symbol]['peak_price'] = current_price
                peak_price = current_price
            if current_price <= entry_price * (1 - STOP_LOSS_PERCENT):
                sell_value = amount * current_price
                sell_fee = sell_value * TRADING_FEE_PERCENT
                profit = sell_value - (amount * entry_price) - buy_fee - sell_fee
                order = exchange.create_market_sell_order(symbol, amount)
                trade_history.append({
                    'symbol': symbol,
                    'type': 'SELL',
                    'amount': amount,
                    'price': current_price,
                    'fee': sell_fee,
                    'profit': profit,
                    'time': datetime.now(timezone.utc)
                })
                msg = f"Stop-loss triggered for {symbol}: Sold {amount:.6f} at {current_price:.6f} (Loss: {profit:.6f} USDT)"
                send_notification(msg)
                with open("trades.txt", "a", encoding='utf-8') as f:
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"{now} - {msg}\n")
                positions.pop(symbol, None)
                cooldowns[symbol] = datetime.now(timezone.utc)
                continue
            trail_percent = TRAILING_STOP_PERCENT.get(symbol, 0.02)
            if current_price <= peak_price * (1 - trail_percent):
                sell_value = amount * current_price
                sell_fee = sell_value * TRADING_FEE_PERCENT
                profit = sell_value - (amount * entry_price) - buy_fee - sell_fee
                order = exchange.create_market_sell_order(symbol, amount)
                trade_history.append({
                    'symbol': symbol,
                    'type': 'SELL',
                    'amount': amount,
                    'price': current_price,
                    'fee': sell_fee,
                    'profit': profit,
                    'time': datetime.now(timezone.utc)
                })
                msg = f"Trailing stop triggered for {symbol}: Sold {amount:.6f} at {current_price:.6f} (Profit/Loss: {profit:.6f} USDT)"
                send_notification(msg)
                with open("trades.txt", "a", encoding='utf-8') as f:
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"{now} - {msg}\n")
                positions.pop(symbol, None)
                cooldowns[symbol] = datetime.now(timezone.utc)
        except Exception as e:
            logging.error(f"Error managing position for {symbol}: {e}")

async def main():
    """Main loop running every 30 seconds."""
    while True:
        try:
            print("=" * 60)
            print(f"Scan Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
            print("-" * 60)
            results = []
            current_prices = {}
            for coin in COINS:
                if not validate_symbol(coin):
                    send_notification(f"[{coin}] Invalid trading pair, skipping.")
                    continue
                if coin in cooldowns and (datetime.now(timezone.utc) - cooldowns[coin]).total_seconds() < COOLDOWN_MINUTES * 60:
                    send_notification(f"[{coin}] In cooldown, skipping.")
                    continue
                df = await fetch_data(coin)
                if df is None or df.empty or len(df) < MIN_CANDLES:
                    send_notification(f"[{coin}] Failed to fetch sufficient data, skipping.")
                    continue
                signal_a_result, signal_a_score = signal_a(df)
                if signal_a_result == "WAIT":
                    send_notification(f"[{coin}] Signal A: WAIT (Score: {signal_a_score})")
                    continue
                signal_b_result, signal_b_score = signal_b(df)
                signal_c_result, signal_c_score = signal_c(coin)
                signal_d_ok, signal_d_msg, signal_d_score = signal_d(coin)
                signal_e_result, signal_e_score = await signal_e(coin)
                total_score = signal_a_score + signal_b_score + signal_c_score + signal_d_score + signal_e_score
                current_price = df['close'].iloc[-1]
                current_prices[coin] = current_price
                final_signal = "WAIT"
                if signal_a_result == "BUY" and signal_b_result == "BUY" and signal_d_ok:
                    final_signal = "BUY"
                elif signal_a_result == "SELL" and signal_b_result == "SELL" and signal_d_ok and coin in positions:
                    final_signal = "SELL"
                results.append({
                    'coin': coin,
                    'signal': final_signal,
                    'accuracy': total_score,
                    'price': current_price,
                    'signals': {
                        'A': (signal_a_result, signal_a_score),
                        'B': (signal_b_result, signal_b_score),
                        'C': (signal_c_result, signal_c_score),
                        'D': (signal_d_ok, signal_d_score, signal_d_msg),
                        'E': (signal_e_result, signal_e_score)
                    }
                })
                with open("signals.txt", "a", encoding='utf-8') as f:
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"{now} - {coin} - Signal: {final_signal}, Accuracy: {total_score}/470, "
                           f"A: {signal_a_result} ({signal_a_score}), B: {signal_b_result} ({signal_b_score}), "
                           f"C: {signal_c_result} ({signal_c_score}), D: {signal_d_msg} ({signal_d_score}), "
                           f"E: {signal_e_result} ({signal_e_score})\n")
                send_notification(f"[{coin}] Signal: {final_signal} | Accuracy: {total_score}/470")
            king_signal = max(results, key=lambda x: x['accuracy'], default=None)
            if king_signal and king_signal['signal'] != "WAIT":
                if king_signal['signal'] == "BUY" and king_signal['coin'] not in positions:
                    if check_balance(king_signal['coin'], "BUY") > 0:
                        place_trade(king_signal['coin'], "BUY", king_signal['price'])
                elif king_signal['signal'] == "SELL" and king_signal['coin'] in positions:
                    if check_balance(king_signal['coin'], "SELL") >= MIN_TRADE_AMOUNT.get(king_signal['coin'], 0):
                        place_trade(king_signal['coin'], "SELL", king_signal['price'])
            manage_positions(current_prices)
            if not results:
                send_notification("No valid signals this cycle.")
            await asyncio.sleep(REPEAT_DELAY)
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
# import time
# import requests
# import pandas as pd
# from datetime import datetime, timezone
# import ccxt
# import logging
# import json
# import sys
# from typing import Dict, List, Optional, Tuple
# import numpy as np
# from googleapiclient.discovery import build
# import ccxt.async_support as ccxt_async
# import asyncio

# # Configure logging for debugging and tracking
# logging.basicConfig(
#     filename='trading_bot.log',
#     level=logging.INFO,
#     format='%(asctime)s - %(levelname)s - %(message)s',
#     filemode='a'
# )

# # API credentials (replace with your own)
# API_KEY = "684084ab6025980001ef6ac3"
# API_SECRET = "0cbb014b-223d-4c82-833a-f1555cceb51c"
# API_PASSPHRASE = "Muhammad12099021"
# CRYPTO_PANIC_API_KEY = "5176231a99efb4a1c385a47f9039ff36d1a50f78"
# YOUTUBE_API_KEY = "AIzaSyA-gjSVY5IbYDUrCwEhIsTyr3LCYImJGSI"  # Replace with your YouTube Data API key

# # Trading parameters
# COINS = [
#     "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "AVAX/USDT",
#     "XRP/USDT", "ADA/USDT", "TRX/USDT", "LTC/USDT", "DOT/USDT",
#     "LINK/USDT", "ATOM/USDT", "ARB/USDT", "OP/USDT", "INJ/USDT"
# ]
# INTERVAL = "5m"
# LIMIT = 100
# REPEAT_DELAY = 30  # Seconds between scans
# STOP_LOSS_PERCENT = 0.007  # 0.7% stop-loss
# TRADING_FEE_PERCENT = 0.001
# MIN_TRADE_AMOUNT = {
#     "BTC/USDT": 0.0001, "ETH/USDT": 0.001, "BNB/USDT": 0.01, "SOL/USDT": 0.1,
#     "AVAX/USDT": 0.1, "XRP/USDT": 1, "ADA/USDT": 10, "TRX/USDT": 10,
#     "LTC/USDT": 0.01, "DOT/USDT": 0.1, "LINK/USDT": 0.1, "ATOM/USDT": 0.1,
#     "ARB/USDT": 1, "OP/USDT": 1, "INJ/USDT": 0.1
# }
# TRAILING_STOP_PERCENT = {
#     "BTC/USDT": 0.01, "ETH/USDT": 0.01,  # Low volatility
#     "SOL/USDT": 0.015, "DOT/USDT": 0.015, "LINK/USDT": 0.015, "ADA/USDT": 0.015,  # Medium
#     "AVAX/USDT": 0.02, "XRP/USDT": 0.02, "LTC/USDT": 0.02,  # Medium-High
#     "TRX/USDT": 0.025, "ATOM/USDT": 0.025, "ARB/USDT": 0.025, "OP/USDT": 0.025, "INJ/USDT": 0.025  # High
# }
# COOLDOWN_MINUTES = 30  # 30-minute cooldown after a buy+sell
# OVERTRADING_DELAY = 180  # 3-minute wait after a trade

# # Global state
# positions: Dict[str, Dict] = {}
# trade_history: List[Dict] = []
# cooldowns: Dict[str, datetime] = {}
# last_trade_time = None

# # Initialize exchange
# try:
#     exchange = ccxt.kucoin({
#         'apiKey': API_KEY,
#         'secret': API_SECRET,
#         'password': API_PASSPHRASE,
#         'enableRateLimit': True,
#         'timeout': 30000
#     })
#     exchange.load_markets()
#     logging.info("KuCoin exchange initialized successfully")
#     print("Exchange initialized successfully")
# except Exception as e:
#     logging.error(f"Failed to initialize KuCoin exchange: {e}")
#     print(f"Error initializing exchange: {e}")
#     sys.exit(1)

# def send_notification(message: str) -> None:
#     """Log and print messages for debugging."""
#     logging.info(message)
#     print(message)

# async def fetch_data(symbol: str) -> Optional[pd.DataFrame]:
#     """Fetch OHLCV data with enhanced retries and ticker fallback."""
#     async_exchange = ccxt_async.kucoin({
#         'apiKey': API_KEY,
#         'secret': API_SECRET,
#         'password': API_PASSPHRASE,
#         'enableRateLimit': True,
#         'timeout': 30000
#     })
#     try:
#         for attempt in range(12):
#             try:
#                 ohlcv = await async_exchange.fetch_ohlcv(symbol, INTERVAL, limit=LIMIT)
#                 if not ohlcv or len(ohlcv) < LIMIT:
#                     logging.warning(f"Insufficient data for {symbol}, got {len(ohlcv)} candles")
#                     await asyncio.sleep(2 ** attempt)
#                     continue
#                 df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
#                 df = df.astype(float).iloc[::-1].reset_index(drop=True)
#                 if len(df) < 26:
#                     logging.warning(f"Data length {len(df)} for {symbol} too short, padding with last value")
#                     last_row = df.iloc[-1:].copy()
#                     df = pd.concat([pd.DataFrame([last_row.iloc[0]] * (26 - len(df)), columns=df.columns), df], ignore_index=True)
#                 await async_exchange.close()
#                 return df
#             except ccxt.RateLimitExceeded:
#                 delay = 2 ** attempt
#                 logging.warning(f"Rate limit hit for {symbol}. Retrying after {delay}s")
#                 await asyncio.sleep(delay)
#             except Exception as e:
#                 logging.error(f"Attempt {attempt+1} failed to fetch data for {symbol}: {e}")
#                 if attempt < 11:
#                     await asyncio.sleep(2)
#                     continue
#                     continue
#                 # Fallback to ticker data
#                 try:
#                     ticker = await async_exchange.fetch_ticker(symbol)
#                     price = ticker['last']
#                     df = pd.DataFrame({
#                         'timestamp': [int(time.time() * 1000)] * 26,
#                         'open': [price] * 26,
#                         'high': [price] * 26,
#                         'low': [price] * 26,
#                         'close': [price] * 26,
#                         'volume': [ticker['baseVolume'] or 0] * 26
#                     })
#                     logging.warning(f"Fallback to ticker data for {symbol}")
#                     await async_exchange.close()
#                     return df
#                 except Exception as e:
#                     logging.error(f"Fallback ticker fetch failed for {symbol}: {e}")
#                     await async_exchange.close()
#                     return None
#     finally:
#         await async_exchange.close()
#     return None

# def validate_symbol(symbol: str) -> bool:
#     """Validate trading pair availability."""
#     for attempt in range(3):
#         try:
#             return symbol in exchange.markets and exchange.markets[symbol]['active']
#         except ccxt.NetworkError as e:
#             logging.warning(f"Network error validating {symbol}: {e}. Retrying...")
#             time.sleep(2 ** attempt)
#         except Exception as e:
#             logging.error(f"Error validating symbol {symbol}: {e}")
#             return False
#     return False

# def check_balance(symbol: str, signal: str) -> float:
#     """Check available balance for trading."""
#     for attempt in range(3):
#         try:
#             balance = exchange.fetch_balance()
#             if signal == "BUY":
#                 usdt_balance = balance.get('USDT', {}).get('free', 0)
#                 logging.info(f"USDT balance: {usdt_balance:.2f} for {symbol} BUY check")
#                 return usdt_balance
#             elif signal == "SELL":
#                 coin = symbol.split('/')[0]
#                 coin_balance = balance.get(coin, {}).get('free', 0)
#                 logging.info(f"{coin} balance: {coin_balance:.6f} for {symbol} SELL check")
#                 return coin_balance
#             return 0
#         except ccxt.NetworkError as e:
#             logging.warning(f"Network error checking balance for {symbol}: {e}. Retrying...")
#             time.sleep(2 ** attempt)
#         except Exception as e:
#             logging.error(f"Error checking balance for {symbol}: {e}")
#             return 0
#     return 0

# def calculate_ema(data: pd.Series, period: int) -> float:
#     """Calculate EMA for the last value."""
#     if len(data) < period:
#         return data.iloc[-1]
#     return data.ewm(span=period, adjust=False).mean().iloc[-1]

# def calculate_rsi(data: pd.Series, period: int = 14) -> float:
#     """Calculate RSI."""
#     if len(data) < period + 1:
#         return 50.0
#     delta = data.diff()
#     gain = delta.where(delta > 0, 0).rolling(window=period).mean()
#     loss = -delta.where(delta < 0, 0).rolling(window=period).mean()
#     rs = gain / loss.replace(0, 0.0001)
#     rsi = 100 - (100 / (1 + rs))
#     return rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 50.0

# def calculate_macd_hist(data: pd.Series) -> float:
#     """Calculate MACD histogram."""
#     if len(data) < 26:
#         return 0.0
#     ema12 = data.ewm(span=12, adjust=False).mean()
#     ema26 = data.ewm(span=26, adjust=False).mean()
#     macd_line = ema12 - ema26
#     signal_line = macd_line.ewm(span=9, adjust=False).mean()
#     return (macd_line - signal_line).iloc[-1]

# def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
#     """Calculate ATR."""
#     if len(df) < period:
#         return 0.0001
#     tr1 = df['high'] - df['low']
#     tr2 = abs(df['high'] - df['close'].shift())
#     tr3 = abs(df['low'] - df['close'].shift())
#     tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
#     atr = tr.rolling(window=period).mean()
#     return atr.iloc[-1] if not pd.isna(atr.iloc[-1]) else 0.0001

# def calculate_ichimoku_base(df: pd.DataFrame) -> float:
#     """Calculate Ichimoku Base Line."""
#     if len(df) < 26:
#         return df['close'].iloc[-1]
#     high_26 = df['high'].rolling(window=26).max()
#     low_26 = df['low'].rolling(window=26).min()
#     return (high_26 + low_26).iloc[-1] / 2

# def calculate_stochastic_rsi(data: pd.Series, period: int = 14) -> float:
#     """Calculate Stochastic RSI."""
#     if len(data) < period + 1:
#         return 0.5
#     rsi_series = pd.Series(data).rolling(window=period + 1).apply(
#         lambda x: calculate_rsi(pd.Series(x)), raw=False
#     )
#     stoch_rsi = rsi_series.rolling(window=period).apply(
#         lambda x: (x[-1] - x.min()) / (x.max() - x.min()) if x.max() != x.min() else 0.0, raw=True
#     )
#     return stoch_rsi.iloc[-1] if not pd.isna(stoch_rsi.iloc[-1]) else 0.5

# def signal_a(df: pd.DataFrame) -> Tuple[str, int]:
#     """Signal A: Trend and candlestick patterns (100 points max)."""
#     try:
#         close = df['close']
#         open_ = df['open']
#         high = df['high']
#         low = df['low']
#         ema50 = calculate_ema(close, 50)
#         ema200 = calculate_ema(close, 200)
#         trend_score = 0
#         if ema50 > ema200 and close.iloc[-1] > ema50:
#             trend_score = 50  # Uptrend
#         elif ema50 < ema200 and close.iloc[-1] < ema50:
#             trend_score = -50  # Downtrend
#         candle_score = 0
#         last_candle = df.iloc[-1]
#         prev_candle = df.iloc[-2]
#         # Bullish patterns
#         if (last_candle['close'] > last_candle['open'] and
#                 last_candle['close'] > prev_candle['high'] and
#                 last_candle['open'] < prev_candle['close']):
#             candle_score = 50  # Bullish Engulfing
#         elif (last_candle['close'] > last_candle['open'] and
#               abs(last_candle['close'] - last_candle['open']) > 0.8 * (last_candle['high'] - last_candle['low'])):
#             candle_score = 50  # Marubozu
#         elif (last_candle['low'] < min(prev_candle['low'], prev_candle['close']) and
#               last_candle['close'] > last_candle['open'] and
#               (last_candle['high'] - last_candle['close']) < 0.2 * (last_candle['close'] - last_candle['open'])):
#             candle_score = 50  # Hammer
#         # Bearish patterns
#         elif (last_candle['close'] < last_candle['open'] and
#               last_candle['close'] < prev_candle['low'] and
#               last_candle['open'] > prev_candle['close']):
#             candle_score = -50  # Bearish Engulfing
#         elif (high.iloc[-1] < high.iloc[-2] and high.iloc[-2] < high.iloc[-3]):
#             candle_score = -50  # Lower highs
#         total_score = trend_score + candle_score
#         if total_score >= 50:
#             return "BUY", min(total_score, 100)
#         elif total_score <= -50:
#             return "SELL", min(abs(total_score), 100)
#         return "WAIT", 0
#     except Exception as e:
#         logging.error(f"Error in Signal A: {e}")
#         return "WAIT", 0

# def signal_b(df: pd.DataFrame) -> Tuple[str, int]:
#     """Signal B: Indicators (70 points max, 10 per indicator)."""
#     try:
#         close = df['close']
#         volume = df['volume']
#         indicators = {
#             'rsi': calculate_rsi(close),
#             'ema': calculate_ema(close, 9) > calculate_ema(close, 21),
#             'macd_hist': calculate_macd_hist(close),
#             'volume': volume.iloc[-1] > volume.rolling(10).mean().iloc[-1] * 1.5,
#             'atr': calculate_atr(df) > 0,
#             'ichimoku_base': calculate_ichimoku_base(df) < close.iloc[-1],
#             'stoch_rsi': calculate_stochastic_rsi(close)
#         }
#         buy = sell = 0
#         if indicators['rsi'] < 30:
#             buy += 10
#         elif indicators['rsi'] > 70:
#             sell += 10
#         if indicators['ema']:
#             buy += 10
#         else:
#             sell += 10
#         if indicators['macd_hist'] > 0:
#             buy += 10
#         else:
#             sell += 10
#         if indicators['volume']:
#             buy += 10
#         else:
#             sell += 10
#         if indicators['atr']:
#             buy += 10
#         if indicators['ichimoku_base']:
#             buy += 10
#         else:
#             sell += 10
#         if indicators['stoch_rsi'] < 0.2:
#             buy += 10
#         elif indicators['stoch_rsi'] > 0.8:
#             sell += 10
#         total_score = buy - sell
#         signal = "WAIT"
#         accuracy = 0
#         if buy >= 40:
#             signal = "BUY"
#             accuracy = {40: 40, 50: 50, 60: 60, 70: 70}.get(buy, 40)
#         elif sell >= 40:
#             signal = "SELL"
#             accuracy = {40: 40, 50: 50, 60: 60, 70: 70}.get(sell, 40)
#         logging.info(f"Signal B: {json.dumps(indicators, default=str)}")
#         return signal, accuracy
#     except Exception as e:
#         logging.error(f"Error in Signal B: {e}")
#         return "WAIT", 0

# def signal_c(symbol: str) -> Tuple[str, int]:
#     """Signal C: Order book analysis (100 points max)."""
#     try:
#         order_book = exchange.fetch_order_book(symbol, limit=20)
#         bids = pd.Series([x[1] for x in order_book['bids']])
#         asks = pd.Series([x[1] for x in order_book['asks']])
#         bid_volume = bids.sum()
#         ask_volume = asks.sum()
#         total_volume = bid_volume + ask_volume
#         if total_volume == 0:
#             return "WAIT", 0
#         bid_ratio = bid_volume / total_volume
#         score = int((bid_ratio - 0.5) * 200)
#         if score >= 50:
#             return "BUY", min(score, 100)
#         elif score <= -50:
#             return "SELL", min(abs(score), 100)
#         return "WAIT", 0
#     except Exception as e:
#         logging.error(f"Error in Signal C for {symbol}: {e}")
#         return "WAIT", 0

# def signal_d(symbol: str) -> Tuple[bool, str, int]:
#     """Signal D: News check (100 points max)."""
#     try:
#         coin = symbol.split('/')[0]
#         url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTO_PANIC_API_KEY}&filter=important&currencies={coin}"
#         response = requests.get(url, timeout=10)
#         response.raise_for_status()
#         news = response.json().get('results', [])
#         for article in news:
#             created_at = datetime.strptime(article['created_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
#             if (datetime.now(timezone.utc) - created_at).total_seconds() / 60 < 60:
#                 title = article['title'].lower()
#                 if any(keyword in title for keyword in ['crash', 'ban', 'hack', 'dump']):
#                     return False, f"Negative news: {title}", 0
#                 if any(keyword in title for keyword in ['pump', 'etf', 'adoption', 'rally']):
#                     return True, f"Positive news: {title}", 100
#         return True, "No significant news", 50
#     except Exception as e:
#         logging.error(f"News fetch error for {symbol}: {e}")
#         return True, "News check failed, proceeding", 50

# async def signal_e(symbol: str) -> Tuple[str, int]:
#     """Signal E: On-chain data analysis (100 points max)."""
#     async_exchange = ccxt_async.kucoin({
#         'apiKey': API_KEY,
#         'secret': API_SECRET,
#         'password': API_PASSPHRASE,
#         'enableRateLimit': True
#     })
#     try:
#         trades = await async_exchange.fetch_trades(symbol, limit=50)
#         large_trades = [t for t in trades if t['amount'] * t['price'] > 10000]
#         buy_volume = sum(t['amount'] for t in large_trades if t['side'] == 'buy')
#         sell_volume = sum(t['amount'] for t in large_trades if t['side'] == 'sell')
#         total_volume = buy_volume + sell_volume
#         if total_volume == 0:
#             return "WAIT", 0
#         score = int(((buy_volume / total_volume) - 0.5) * 200)
#         if score >= 50:
#             return "BUY", min(score, 100)
#         elif score <= -50:
#             return "SELL", min(abs(score), 100)
#         return "WAIT", 0
#     except Exception as e:
#         logging.error(f"Error in Signal E for {symbol}: {e}")
#         return "WAIT", 0
#     finally:
#         await async_exchange.close()

# async def signal_f(symbol: str) -> Tuple[str, int]:
#     """Signal F: Sentiment analysis from X and YouTube (100 points max)."""
#     try:
#         coin = symbol.split('/')[0].lower()
#         sentiment_score = 0
#         # X sentiment (replace with your X API implementation)
#         headers = {"Authorization": f"Bearer {X_API_KEY}"}
#         url = f"https://api.twitter.com/2/tweets/search/recent?query={coin}%20crypto"
#         response = requests.get(url, headers=headers, timeout=10)
#         if response.status_code == 200:
#             tweets = response.json().get('data', [])
#             positive_words = ['bullish', 'pump', 'moon', 'buy', 'rally']
#             negative_words = ['bearish', 'dump', 'crash', 'sell']
#             for tweet in tweets:
#                 text = tweet['text'].lower()
#                 sentiment_score += sum(10 for word in positive_words if word in text)
#                 sentiment_score -= sum(10 for word in negative_words if text)
#         # YouTube sentiment
#         youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
#         request = youtube.search().list(
#             part='snippet',
#             q=f"{coin} crypto",
#             maxResults=10,
#             order='date',
#             type='video'
#         )
#         response = request.execute()
#         for item in response.get('items', []):
#             title = item['snippet']['title'].lower()
#             sentiment_score += sum(10 for word in positive_words if word in title)
#             sentiment_score -= sum(10 for word in negative_words if word in title)
#         score = min(max(sentiment_score, -100), 100)
#         if score >= 50:
#             return "BUY", score
#         elif score <= -50:
#             return "SELL", abs(score)
#         return "WAIT", 0
#     except Exception as e:
#         logging.error(f"Error in Signal F for {symbol}: {e}")
#         return "WAIT", 0

# def place_trade(symbol: str, signal: str, price: float) -> Optional[Dict]:
#     """Place a market order with cooldown and overtrading checks."""
#     try:
#         if symbol in cooldowns and (datetime.now(timezone.utc) - cooldowns[symbol]).total_seconds() < COOLDOWN_MINUTES * 60:
#             logging.info(f"{symbol} in cooldown, skipping trade")
#             return None
#         global last_trade_time
#         if last_trade_time and (datetime.now(timezone.utc) - last_trade_time).total_seconds() < OVERTRADING_DELAY:
#             logging.info(f"Overtrading delay active, skipping trade")
#             return None
#         news_ok, news_msg, _ = signal_d(symbol)
#         if not news_ok:
#             msg = f"Skipped {symbol} {signal}: {news_msg}"
#             send_notification(msg)
#             return None
#         if signal == "BUY":
#             usdt_balance = check_balance(symbol, "BUY")
#             if usdt_balance <= 0:
#                 logging.warning(f"Insufficient USDT balance for {symbol} BUY")
#                 return None
#             min_amount = MIN_TRADE_AMOUNT.get(symbol, 0)
#             amount = usdt_balance / price
#             trade_usdt = usdt_balance
#             if amount < min_amount:
#                 amount = min_amount
#                 trade_usdt = min_amount * price
#                 if trade_usdt > usdt_balance:
#                     logging.warning(f"Adjusted trade amount {trade_usdt:.6f} USDT for {symbol} exceeds balance {usdt_balance:.6f}")
#                     return None
#             buy_fee = trade_usdt * TRADING_FEE_PERCENT
#             order = exchange.create_market_buy_order(symbol, amount)
#             positions[symbol] = {
#                 'amount': amount,
#                 'entry_price': price,
#                 'peak_price': price,
#                 'buy_fee': buy_fee,
#                 'open_time': datetime.now(timezone.utc)
#             }
#             trade_history.append({
#                 'symbol': symbol,
#                 'type': 'BUY',
#                 'amount': amount,
#                 'price': price,
#                 'fee': buy_fee,
#                 'time': datetime.now(timezone.utc)
#             })
#             msg = f"Placed BUY order for {symbol}: {amount:.6f} at {price:.6f} (Fee: {buy_fee:.6f} USDT)"
#             send_notification(msg)
#             with open("trades.txt", "a", encoding='utf-8') as f:
#                 now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
#                 f.write(f"{now} - {msg}\n")
#             last_trade_time = datetime.now(timezone.utc)
#             cooldowns[symbol] = datetime.now(timezone.utc)
#             return order
#         elif signal == "SELL":
#             coin = symbol.split('/')[0]
#             pos = positions.get(symbol, {})
#             amount = pos.get('amount', 0)
#             if amount < MIN_TRADE_AMOUNT.get(symbol, 0):
#                 logging.warning(f"Sell amount {amount:.6f} for {symbol} below minimum")
#                 return None
#             if amount > 0:
#                 sell_value = amount * price
#                 sell_fee = sell_value * TRADING_FEE_PERCENT
#                 order = exchange.create_market_sell_order(symbol, amount)
#                 buy_fee = pos.get('buy_fee', 0)
#                 entry_price = pos.get('entry_price', price)
#                 profit = sell_value - (amount * entry_price) - buy_fee - sell_fee
#                 trade_history.append({
#                     'symbol': symbol,
#                     'type': 'SELL',
#                     'amount': amount,
#                     'price': price,
#                     'fee': sell_fee,
#                     'profit': profit,
#                     'time': datetime.now(timezone.utc)
#                 })
#                 msg = f"Closed SELL order for {symbol}: {amount:.6f} at {price:.6f} (Profit/Loss: {profit:.6f} USDT)"
#                 send_notification(msg)
#                 with open("trades.txt", "a", encoding='utf-8') as f:
#                     now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
#                     f.write(f"{now} - {msg}\n")
#                 positions.pop(symbol, None)
#                 last_trade_time = datetime.now(timezone.utc)
#                 cooldowns[symbol] = datetime.now(timezone.utc)
#                 return order
#             return None
#     except ccxt.NetworkError as e:
#         logging.error(f"Network error placing {signal} order for {symbol}: {e}")
#         return None
#     except Exception as e:
#         logging.error(f"Error placing {signal} order for {symbol}: {e}")
#         return None

# def manage_positions(current_prices: Dict[str, float]) -> None:
#     """Manage positions with stop-loss and trailing stop."""
#     for symbol, pos in list(positions.items()):
#         try:
#             current_price = current_prices.get(symbol, pos['entry_price'])
#             entry_price = pos['entry_price']
#             peak_price = pos['peak_price']
#             amount = pos['amount']
#             buy_fee = pos['buy_fee']
#             if current_price > peak_price:
#                 positions[symbol]['peak_price'] = current_price
#                 peak_price = current_price
#             if current_price <= entry_price * (1 - STOP_LOSS_PERCENT):
#                 sell_value = amount * current_price
#                 sell_fee = sell_value * TRADING_FEE_PERCENT
#                 profit = sell_value - (amount * entry_price) - buy_fee - sell_fee
#                 order = exchange.create_market_sell_order(symbol, amount)
#                 trade_history.append({
#                     'symbol': symbol,
#                     'type': 'SELL',
#                     'amount': amount,
#                     'price': current_price,
#                     'fee': sell_fee,
#                     'profit': profit,
#                     'time': datetime.now(timezone.utc)
#                 })
#                 msg = f"Stop-loss triggered for {symbol}: Sold {amount:.6f} at {current_price:.6f} (Loss: {profit:.6f} USDT)"
#                 send_notification(msg)
#                 with open("trades.txt", "a", encoding='utf-8') as f:
#                     now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
#                     f.write(f"{now} - {msg}\n")
#                 positions.pop(symbol, None)
#                 cooldowns[symbol] = datetime.now(timezone.utc)
#                 continue
#             trail_percent = TRAILING_STOP_PERCENT.get(symbol, 0.02)
#             if current_price <= peak_price * (1 - trail_percent):
#                 sell_value = amount * current_price
#                 sell_fee = sell_value * TRADING_FEE_PERCENT
#                 profit = sell_value - (amount * entry_price) - buy_fee - sell_fee
#                 order = exchange.create_market_sell_order(symbol, amount)
#                 trade_history.append({
#                     'symbol': symbol,
#                     'type': 'SELL',
#                     'amount': amount,
#                     'price': current_price,
#                     'fee': sell_fee,
#                     'profit': profit,
#                     'time': datetime.now(timezone.utc)
#                 })
#                 msg = f"Trailing stop triggered for {symbol}: Sold {amount:.6f} at {current_price:.6f} (Profit/Loss: {profit:.6f} USDT)"
#                 send_notification(msg)
#                 with open("trades.txt", "a", encoding='utf-8') as f:
#                     now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
#                     f.write(f"{now} - {msg}\n")
#                 positions.pop(symbol, None)
#                 cooldowns[symbol] = datetime.now(timezone.utc)
#         except Exception as e:
#             logging.error(f"Error managing position for {symbol}: {e}")

# async def main():
#     """Main loop running every 30 seconds."""
#     while True:
#         try:
#             print("=" * 60)
#             print(f"Scan Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
#             print("-" * 60)
#             results = []
#             current_prices = {}
#             for coin in COINS:
#                 if not validate_symbol(coin):
#                     send_notification(f"[{coin}] Invalid trading pair, skipping.")
#                     continue
#                 if coin in cooldowns and (datetime.now(timezone.utc) - cooldowns[coin]).total_seconds() < COOLDOWN_MINUTES * 60:
#                     send_notification(f"[{coin}] In cooldown, skipping.")
#                     continue
#                 df = await fetch_data(coin)
#                 if df is None or df.empty or len(df) < 26:
#                     send_notification(f"[{coin}] Failed to fetch sufficient data, skipping.")
#                     continue
#                 signal_a_result, signal_a_score = signal_a(df)
#                 if signal_a_result == "WAIT":
#                     send_notification(f"[{coin}] Signal A: WAIT (Score: {signal_a_score})")
#                     continue
#                 signal_b_result, signal_b_score = signal_b(df)
#                 signal_c_result, signal_c_score = signal_c(coin)
#                 signal_d_ok, signal_d_msg, signal_d_score = signal_d(coin)
#                 signal_e_result, signal_e_score = await signal_e(coin)
#                 signal_f_result, signal_f_score = await signal_f(coin)
#                 total_score = signal_a_score + signal_b_score + signal_c_score + signal_d_score + signal_e_score + signal_f_score
#                 current_price = df['close'].iloc[-1]
#                 current_prices[coin] = current_price
#                 final_signal = "WAIT"
#                 if signal_a_result == "BUY" and signal_b_result == "BUY" and signal_d_ok:
#                     final_signal = "BUY"
#                 elif signal_a_result == "SELL" and signal_b_result == "SELL" and signal_d_ok and coin in positions:
#                     final_signal = "SELL"
#                 results.append({
#                     'coin': coin,
#                     'signal': final_signal,
#                     'accuracy': total_score,
#                     'price': current_price,
#                     'signals': {
#                         'A': (signal_a_result, signal_a_score),
#                         'B': (signal_b_result, signal_b_score),
#                         'C': (signal_c_result, signal_c_score),
#                         'D': (signal_d_ok, signal_d_score, signal_d_msg),
#                         'E': (signal_e_result, signal_e_score),
#                         'F': (signal_f_result, signal_f_score)
#                     }
#                 })
#                 with open("signals.txt", "a", encoding='utf-8') as f:
#                     now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
#                     f.write(f"{now} - {coin} - Signal: {final_signal}, Accuracy: {total_score}/570, "
#                            f"A: {signal_a_result} ({signal_a_score}), B: {signal_b_result} ({signal_b_score}), "
#                            f"C: {signal_c_result} ({signal_c_score}), D: {signal_d_msg} ({signal_d_score}), "
#                            f"E: {signal_e_result} ({signal_e_score}), F: {signal_f_result} ({signal_f_score})\n")
#                 send_notification(f"[{coin}] Signal: {final_signal} | Accuracy: {total_score}/570")
#             king_signal = max(results, key=lambda x: x['accuracy'], default=None)
#             if king_signal and king_signal['signal'] != "WAIT":
#                 if king_signal['signal'] == "BUY" and king_signal['coin'] not in positions:
#                     if check_balance(king_signal['coin'], "BUY") > 0:
#                         place_trade(king_signal['coin'], "BUY", king_signal['price'])
#                 elif king_signal['signal'] == "SELL" and king_signal['coin'] in positions:
#                     if check_balance(king_signal['coin'], "SELL") >= MIN_TRADE_AMOUNT.get(king_signal['coin'], 0):
#                         place_trade(king_signal['coin'], "SELL", king_signal['price'])
#             manage_positions(current_prices)
#             if not results:
#                 send_notification("No valid signals this cycle.")
#             await asyncio.sleep(REPEAT_DELAY)
#         except Exception as e:
#             logging.error(f"Main loop error: {e}")
#             await asyncio.sleep(60)

# if __name__ == "__main__":
#     asyncio.run(main())