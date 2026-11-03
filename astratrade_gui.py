import base64, ctypes, json, math, os, queue, threading, time, uuid, urllib.parse, urllib.request, tkinter as tk
from urllib.error import HTTPError, URLError
from datetime import datetime, timedelta, timezone
from tkinter import ttk, messagebox
from zoneinfo import ZoneInfo

PAPER = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
EXCHANGES = {"NYSE", "NASDAQ", "AMEX"}
EXCLUDED_NAME_TERMS = (" etf", "fund", "warrant", "unit", "right", "preferred", "depositary", "depositary share", "notes", "bond")
POS = {"beats","beat","surge","record","upgrade","growth","profit","strong","buyback","raises","approval","approved","bullish","wins","revenue"}
NEG = {"miss","downgrade","plunge","loss","warning","warns","cuts","fraud","probe","investigation","lawsuit","recall","bankruptcy","bearish","weak","layoff"}
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AstraTradeAI")
os.makedirs(APP_DIR, exist_ok=True)
LOG_FILE = os.path.join(APP_DIR, "astratrade.log")
STATE_FILE = os.path.join(APP_DIR, "bot_state.json")
MAX_MANAGED_POSITIONS = 5

def read_app_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def write_app_state(**updates):
    data = read_app_state()
    data.update(updates)
    temp_file = STATE_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp_file, STATE_FILE)

def load_managed_symbols():
    values = read_app_state().get("managed_symbols", [])
    return {str(x).upper() for x in values if isinstance(x, str) and x.strip()}

def save_managed_symbols(symbols):
    write_app_state(managed_symbols=sorted(set(symbols)))

def load_managed_groups():
    data = read_app_state().get("managed_groups", {})
    if not isinstance(data, dict):
        return {}
    return {str(k).upper(): str(v) for k, v in data.items() if isinstance(k, str) and v}

def save_managed_groups(groups):
    write_app_state(managed_groups={str(k).upper(): str(v) for k, v in groups.items() if v})

def load_managed_protection():
    data = read_app_state().get("managed_protection", {})
    return data if isinstance(data, dict) else {}

def save_managed_protection(data):
    write_app_state(managed_protection=data)

CREDENTIAL_FILE = os.path.join(APP_DIR, "credentials.dat")

def _dpapi_transform(raw, protect=True):
    if os.name != "nt":
        raise RuntimeError("API anahtarlarını güvenli kaydetmek için Windows DPAPI gerekir.")
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_byte))]
    crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    input_buffer = ctypes.create_string_buffer(raw)
    input_blob = DATA_BLOB(len(raw), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = DATA_BLOB()
    if protect:
        crypt32.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(DATA_BLOB)]
        crypt32.CryptProtectData.restype = ctypes.c_bool
        ok = crypt32.CryptProtectData(ctypes.byref(input_blob), "AstraTrade AI Paper API", None, None, None, 1, ctypes.byref(output_blob))
    else:
        description = ctypes.c_wchar_p()
        crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(DATA_BLOB)]
        crypt32.CryptUnprotectData.restype = ctypes.c_bool
        ok = crypt32.CryptUnprotectData(ctypes.byref(input_blob), ctypes.byref(description), None, None, None, 1, ctypes.byref(output_blob))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))

def save_credentials(key, secret):
    payload = json.dumps({"key": key, "secret": secret}).encode("utf-8")
    encrypted = _dpapi_transform(payload, protect=True)
    temp_file = CREDENTIAL_FILE + ".tmp"
    with open(temp_file, "w", encoding="ascii") as f:
        f.write(base64.b64encode(encrypted).decode("ascii"))
    os.replace(temp_file, CREDENTIAL_FILE)

def load_credentials():
    try:
        with open(CREDENTIAL_FILE, "r", encoding="ascii") as f:
            encrypted = base64.b64decode(f.read().strip())
        payload = json.loads(_dpapi_transform(encrypted, protect=False).decode("utf-8"))
        return str(payload.get("key", "")), str(payload.get("secret", ""))
    except FileNotFoundError:
        return "", ""
    except Exception as exc:
        logline("Kayıtlı API bilgileri açılamadı: " + str(exc))
        return "", ""
def logline(msg):
    line = datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg)
    try:
        # Keep a bounded active log plus one rotated backup for long-running sessions.
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) >= 5 * 1024 * 1024:
            backup = LOG_FILE + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(LOG_FILE, backup)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    return line

class RetryableDataRequestError(RuntimeError):
    """A retryable market-data request exhausted its safe GET retries."""


class Alpaca:
    def __init__(self, key, secret):
        if not key or not secret: raise ValueError("Paper API Key ve Secret Key gerekli.")
        self.headers = {"APCA-API-KEY-ID":key, "APCA-API-SECRET-KEY":secret, "Content-Type":"application/json"}
    def call(self, base, path, params=None, method="GET", body=None):
        url = base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else {}
                detail = payload.get("message") or payload.get("detail") or payload.get("code") or raw
            except Exception:
                detail = raw or str(exc.reason)
            raise RuntimeError(f"Alpaca HTTP {exc.code} ({method} {path}): {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Alpaca bağlantı hatası ({path}): {exc.reason}") from exc
    def data_call_with_retry(self, path, params=None, description="piyasa verisi", progress_callback=None, attempts=3):
        """Retry only idempotent market-data GET requests; never retry order mutations."""
        retry_markers = (
            "alpaca bağlantı hatası", "timed out", "timeout",
            "connection reset", "connection aborted", "remote end closed",
            "temporarily unavailable", "temporary failure",
            "http 429", "http 500", "http 502", "http 503", "http 504"
        )
        for attempt in range(attempts):
            try:
                return self.call(DATA, path, params)
            except Exception as exc:
                message = str(exc).lower()
                retryable = isinstance(exc, (TimeoutError, OSError)) or any(
                    marker in message for marker in retry_markers
                )
                if not retryable:
                    raise
                if attempt + 1 >= attempts:
                    raise RetryableDataRequestError(
                        f"{description}: {attempts} denemede veri alınamadı: {exc}"
                    ) from exc
                delay = 1.0 * (2 ** attempt)
                notice = (
                    f"{description}: geçici bağlantı/zaman aşımı; "
                    f"{attempt + 1}/{attempts - 1} yeniden deneme {delay:.0f} sn sonra."
                )
                logline(notice)
                if progress_callback:
                    progress_callback(notice)
                time.sleep(delay)

    def _fetch_bars_chunk(self, symbols, params, max_pages, description, progress_callback=None):
        """Fetch one symbol group atomically; split slow groups down to five symbols."""
        chunk_data = {}
        page_params = dict(params)
        try:
            for page_number in range(max_pages):
                page = self.data_call_with_retry(
                    "/v2/stocks/bars", page_params, description, progress_callback
                )
                for symbol, rows in page.get("bars", {}).items():
                    chunk_data.setdefault(symbol, []).extend(rows)
                token = page.get("next_page_token")
                if not token:
                    break
                page_params = dict(params, page_token=token)
            else:
                raise RuntimeError(
                    f"{description}: sayfalama {max_pages} istekte tamamlanmadı."
                )
            return chunk_data, set()
        except RetryableDataRequestError as exc:
            if len(symbols) <= 5:
                failed = set(symbols)
                notice = (
                    f"{description}: {', '.join(symbols)} için veri alınamadı; "
                    "bu semboller yeni alım taramasının dışında bırakılıyor."
                )
                logline(notice)
                if progress_callback:
                    progress_callback(notice)
                return {}, failed
            midpoint = len(symbols) // 2
            notice = (
                f"{description}: {len(symbols)} sembollük grup zaman aşımına uğradı; "
                f"{midpoint} + {len(symbols) - midpoint} sembollük iki gruba ayrılıyor."
            )
            logline(notice)
            if progress_callback:
                progress_callback(notice)
            left_data, left_failed = self._fetch_bars_chunk(
                symbols[:midpoint], params, max_pages, description, progress_callback
            )
            right_data, right_failed = self._fetch_bars_chunk(
                symbols[midpoint:], params, max_pages, description, progress_callback
            )
            for symbol, rows in right_data.items():
                left_data.setdefault(symbol, []).extend(rows)
            return left_data, left_failed | right_failed

    def account(self): return self.call(PAPER, "/v2/account")
    def assets(self): return self.call(PAPER, "/v2/assets", {"status":"active","asset_class":"us_equity"})
    def positions(self): return self.call(PAPER, "/v2/positions")
    def clock(self): return self.call(PAPER, "/v2/clock")
    def open_orders(self):
        # Alpaca caps one order-list response at 500. Fetch all open orders so
        # position ownership and protective OCO legs are never hidden on page 2+.
        params = {"status": "open", "limit": 500, "direction": "asc", "nested": "false"}
        orders = []
        seen_ids = set()
        for page_number in range(21):
            # Probe once beyond the 10,000-order safety cap, so exactly 10,000
            # orders is accepted while larger books fail closed.
            if page_number == 20:
                params["limit"] = 1
            page = self.call(PAPER, "/v2/orders", params)
            if not isinstance(page, list):
                raise RuntimeError("Alpaca açık emir listesi beklenen liste biçiminde değil.")
            if page_number == 20 and page:
                raise RuntimeError("Açık emir sayısı 10.000 kayıt sınırını aştı; güvenlik için işlem taraması durduruldu.")
            for order in page:
                order_id = order.get("id")
                if order_id and order_id not in seen_ids:
                    seen_ids.add(order_id)
                    orders.append(order)
            if len(page) < params["limit"]:
                break
            last_id = page[-1].get("id")
            if not last_id:
                raise RuntimeError("Alpaca emir sayfalamasında son emir kimliği eksik.")
            params["after_order_id"] = last_id
        else:
            raise RuntimeError("Alpaca emir sayfalaması beklenmeyen şekilde sonlandı; güvenlik için işlem taraması durduruldu.")
        return orders
    def bars(self, symbols, progress_callback=None):
        """Fetch one year of daily IEX bars with safe retries and progress reporting."""
        collected = {}
        failed_symbols = set()
        total = len(symbols)
        completed = 0
        for offset in range(0, total, 20):
            chunk = symbols[offset:offset + 20]
            params = {
                "symbols": ",".join(chunk),
                "timeframe": "1Day",
                "limit": 10000,
                "feed": "iex",
                "adjustment": "split",
                "start": (datetime.now(timezone.utc) - timedelta(days=365)).isoformat(),
                "end": datetime.now(timezone.utc).isoformat(),
                "sort": "asc"
            }
            chunk_data, chunk_failed = self._fetch_bars_chunk(
                chunk, params, max_pages=5,
                description="Günlük hisse verisi", progress_callback=progress_callback
            )
            for symbol, rows in chunk_data.items():
                collected.setdefault(symbol, []).extend(rows)
            failed_symbols.update(chunk_failed)
            completed += len(chunk)
            if progress_callback and (completed % 100 == 0 or completed == total):
                progress_callback(
                    f"Günlük veri indirme: {completed}/{total} sembol tamamlandı; "
                    f"verisi alınamayan {len(failed_symbols)} sembol."
                )
        return {"bars": collected, "failed_symbols": sorted(failed_symbols)}

    def intraday_bars(self, symbols, days=5):
        """Fetch recent 5-minute IEX bars in safe, retryable 20-symbol chunks."""
        collected = {}
        failed_symbols = set()
        for offset in range(0, len(symbols), 20):
            chunk = symbols[offset:offset + 20]
            params = {
                "symbols": ",".join(chunk),
                "timeframe": "5Min",
                "limit": 10000,
                "feed": "iex",
                "adjustment": "raw",
                "start": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
                "end": datetime.now(timezone.utc).isoformat(),
                "sort": "asc"
            }
            chunk_data, chunk_failed = self._fetch_bars_chunk(
                chunk, params, max_pages=10, description="5 dakikalık hisse verisi"
            )
            for symbol, rows in chunk_data.items():
                collected.setdefault(symbol, []).extend(rows)
            failed_symbols.update(chunk_failed)
        return {"bars": collected, "failed_symbols": sorted(failed_symbols)}

    def history(self, symbol, timeframe="1Day", days=365, adjustment="raw"):
        params = {
            "timeframe": timeframe,
            "adjustment": adjustment,
            "start": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
            "end": datetime.now(timezone.utc).isoformat(),
            "limit": 10000,
            "feed": "iex",
            "sort": "asc"
        }
        rows = []
        for _ in range(5):
            page = self.data_call_with_retry("/v2/stocks/" + urllib.parse.quote(symbol.upper()) + "/bars", params, f"{symbol} tarihsel veri")
            rows.extend(page.get("bars", []))
            token = page.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return rows

    def portfolio_history(self):
        # Alpaca accepts market_hours / extended_hours / continuous.
        # Omitting intraday_reporting avoids the invalid "market" value (HTTP 422).
        return self.call(PAPER, "/v2/account/portfolio/history", {
            "period": "1M", "timeframe": "1D"
        })
    def order(self, symbol, qty, side, take_profit=None, stop_loss=None):
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": f"astra-{symbol.lower()}-{uuid.uuid4().hex[:20]}"
        }
        if side == "buy" and take_profit is not None and stop_loss is not None:
            body.update({
                "time_in_force": "gtc",
                "order_class": "bracket",
                "take_profit": {"limit_price": f"{take_profit:.2f}"},
                "stop_loss": {"stop_price": f"{stop_loss:.2f}"}
            })
        return self.call(PAPER, "/v2/orders", method="POST", body=body)

    def cancel_order(self, order_id):
        return self.call(PAPER, "/v2/orders/" + urllib.parse.quote(str(order_id)), method="DELETE")

    def close_position(self, symbol):
        return self.call(PAPER, "/v2/positions/" + urllib.parse.quote(symbol), method="DELETE")

    def restore_oco_protection(self, symbol, qty, take_profit, stop_loss):
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "limit",
            "time_in_force": "gtc",
            "order_class": "oco",
            "take_profit": {"limit_price": f"{take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{stop_loss:.2f}"},
            "client_order_id": f"astra-protect-{symbol.lower()}-{uuid.uuid4().hex[:16]}"
        }
        return self.call(PAPER, "/v2/orders", method="POST", body=body)

def ema(values, n):
    if not values:
        return 0.0
    k = 2 / (n + 1)
    value = float(values[0])
    for x in values[1:]:
        value = k * float(x) + (1 - k) * value
    return value

def ema_series(values, n):
    if not values:
        return []
    k = 2 / (n + 1)
    value = float(values[0])
    result = [value]
    for x in values[1:]:
        value = k * float(x) + (1 - k) * value
        result.append(value)
    return result

def prepare_daily_signal_rows(rows, market_is_open, market_day):
    """
    Return completed daily bars, the current session's latest price (if present),
    and a flag indicating whether that price is fresh enough for a live entry.
    """
    rows = rows or []
    if not rows:
        return [], None, False
    last_day = str(rows[-1].get("t", ""))[:10]
    if market_is_open:
        if market_day and last_day == market_day and len(rows) >= 2:
            try:
                live_price = float(rows[-1].get("c", 0) or 0)
            except (TypeError, ValueError):
                live_price = 0.0
            return rows[:-1], live_price if live_price > 0 else None, live_price > 0
        return rows, None, False
    try:
        close = float(rows[-1].get("c", 0) or 0)
    except (TypeError, ValueError):
        close = 0.0
    return rows, close if close > 0 else None, False

def confirm_live_entry(signal, current_price):
    """Avoid entries after the breakout has failed or price has become overextended."""
    try:
        price = float(current_price)
        signal_close = float(signal["price"])
        ema20_value = float(signal["ema20"])
        breakout = float(signal["prior_20_high"])
        atr = float(signal["atr"])
    except (KeyError, TypeError, ValueError):
        return False
    if min(price, signal_close, ema20_value, breakout, atr) <= 0:
        return False
    return price > ema20_value and price > breakout and price <= signal_close + 1.5*atr

def is_eligible_common_stock(asset):
    name = str(asset.get("name", "")).lower()
    exchange = str(asset.get("exchange", "")).upper()
    symbol = str(asset.get("symbol", "")).strip()
    is_common = "common stock" in name or "common shares" in name
    excluded = any(term in name for term in EXCLUDED_NAME_TERMS)
    return bool(
        symbol
        and asset.get("tradable")
        and str(asset.get("status", "")).lower() == "active"
        and exchange in EXCHANGES
        and is_common
        and not excluded
    )

def calculate_order_qty(
    equity, available_cash, price, stop_distance, managed_exposure,
    profit_target_usd=None, min_reward_risk=1.6
):
    """Size shares against risk, capital, exposure and optional USD-target reward/risk."""
    equity = max(0.0, float(equity))
    available_cash = max(0.0, float(available_cash))
    price = float(price)
    stop_distance = float(stop_distance)
    managed_exposure = max(0.0, float(managed_exposure))
    if equity <= 0 or price <= 0 or stop_distance <= 0:
        return 0
    risk_budget = equity * 0.0025
    exposure_room = max(0.0, equity * 0.30 - managed_exposure)
    qty_by_risk = int(risk_budget / stop_distance)
    qty_by_position = int(equity * 0.07 / price)
    qty_by_exposure = int(exposure_room / price)
    qty_by_cash = int(available_cash * 0.98 / price)
    limits = [qty_by_risk, qty_by_position, qty_by_exposure, qty_by_cash]
    if profit_target_usd is not None:
        profit_target_usd = float(profit_target_usd)
        min_reward_risk = float(min_reward_risk)
        if profit_target_usd <= 0 or min_reward_risk <= 0:
            return 0
        limits.append(int(profit_target_usd / (stop_distance * min_reward_risk)))
    return max(0, min(limits))


def dollar_profit_limit_price(entry_price, qty, profit_target_usd, buffer_rate=0.001):
    """Round a sell limit up so its estimated gross P/L includes a small fill-price cushion."""
    entry_price = float(entry_price)
    qty = int(qty)
    profit_target_usd = float(profit_target_usd)
    buffer_rate = max(0.0, float(buffer_rate))
    if entry_price <= 0 or qty <= 0 or profit_target_usd <= 0:
        return None
    buffer_usd = max(0.50, entry_price * qty * buffer_rate)
    target_usd_with_buffer = profit_target_usd + buffer_usd
    raw_target = entry_price + target_usd_with_buffer / qty
    target_price = math.ceil(raw_target * 100 - 1e-8) / 100
    target_price = round(target_price, 2)
    return {
        "price": target_price,
        "distance": target_price - entry_price,
        "estimated_gross_profit_usd": (target_price - entry_price) * qty,
        "buffer_usd": buffer_usd
    }


def profit_target_reached(position, target_usd):
    """True when Alpaca reports at least the configured unrealized USD profit on a position."""
    try:
        pnl = float(position.get("unrealized_pl", 0) or 0)
        target = float(target_usd)
    except (AttributeError, TypeError, ValueError):
        return False
    return target > 0 and pnl >= target

def analyze(rows):
    # Long-only daily swing strategy; all indicators use the same price series.
    if len(rows) < 220:
        return None
    c = [float(x["c"]) for x in rows]
    h = [float(x["h"]) for x in rows]
    l = [float(x["l"]) for x in rows]
    o = [float(x["o"]) for x in rows]
    v = [float(x["v"]) for x in rows]
    price = c[-1]
    e20 = ema(c[-100:], 20)
    e50 = ema(c[-150:], 50)
    sma200 = sum(c[-200:]) / 200
    macd_line = [a - b for a, b in zip(ema_series(c, 12), ema_series(c, 26))]
    macd_signal = ema(macd_line[-40:], 9)
    macd_hist = macd_line[-1] - macd_signal
    changes = [c[i] - c[i-1] for i in range(1, len(c))]
    initial = changes[:14]
    avg_gain = sum(max(0.0, x) for x in initial) / 14
    avg_loss = sum(max(0.0, -x) for x in initial) / 14
    for change in changes[14:]:
        avg_gain = (avg_gain * 13 + max(0.0, change)) / 14
        avg_loss = (avg_loss * 13 + max(0.0, -change)) / 14
    if avg_loss == 0:
        rsi = 100.0 if avg_gain else 50.0
    else:
        rsi = 100 - 100 / (1 + avg_gain / avg_loss)
    true_ranges = []
    for i in range(len(c)-14, len(c)):
        true_ranges.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    atr = sum(true_ranges) / len(true_ranges)
    avg_volume = sum(v[-21:-1]) / 20
    relative_volume = (v[-1] / avg_volume) if avg_volume > 0 else 0.0
    dollar_vol = sum(c[-20+i] * v[-20+i] for i in range(20)) / 20
    prior_20_high = max(h[-21:-1])
    conditions = {
        "uzun_trend": price > sma200,
        "trend_sirasi": e20 > e50,
        "macd_pozitif": macd_hist > 0,
        "rsi_uygun": 50 <= rsi <= 68,
        "20_gun_tepe": price > prior_20_high,
        "hacim_onayi": relative_volume >= 1.10
    }
    # Liquidity is a hard gate, not a second deduction in the technical score.
    score = sum(1 for value in conditions.values() if value) / len(conditions)
    trend_ok = conditions["uzun_trend"] and conditions["trend_sirasi"] and conditions["macd_pozitif"]
    liquid_enough = dollar_vol >= 10_000_000
    action = "BUY" if trend_ok and score >= 5/6 and liquid_enough else ("SELL" if price < e20 and macd_hist < 0 and rsi < 45 else "HOLD")
    return {
        "price": price, "open": o[-1], "ema20": e20, "ema50": e50,
        "sma200": sma200, "macd_hist": macd_hist, "rsi": rsi,
        "atr": atr, "dollar_vol": dollar_vol, "relative_volume": relative_volume,
        "prior_20_high": prior_20_high,
        "score": score, "action": action, "conditions": conditions
    }

def _parse_utc(value):
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip().replace("Z", "+00:00")
        if not raw:
            raise ValueError("Zaman damgası boş.")
        dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def prepare_intraday_rows(rows, now=None):
    """Keep completed regular-session 5-minute bars and report whether the last is fresh."""
    rows = rows or []
    now_utc = _parse_utc(now) if now is not None else datetime.now(timezone.utc)
    ny = ZoneInfo("America/New_York")
    completed = []
    for row in rows:
        try:
            stamp = _parse_utc(row.get("t"))
            local = stamp.astimezone(ny)
            minute_of_day = local.hour * 60 + local.minute
            if local.weekday() >= 5 or not (570 <= minute_of_day < 960):
                continue
            if stamp + timedelta(minutes=5) <= now_utc + timedelta(seconds=2):
                completed.append(row)
        except (TypeError, ValueError, OverflowError):
            continue
    completed.sort(key=lambda item: _parse_utc(item.get("t")))
    if not completed:
        return [], None, False
    try:
        price = float(completed[-1].get("c", 0) or 0)
        last_end = _parse_utc(completed[-1].get("t")) + timedelta(minutes=5)
        age_seconds = (now_utc - last_end).total_seconds()
        fresh = price > 0 and -60 <= age_seconds <= 600
    except (TypeError, ValueError):
        price, fresh = None, False
    return completed, price if price and price > 0 else None, fresh


def aggregate_15m(rows):
    grouped = {}
    for row in rows or []:
        stamp = _parse_utc(row.get("t"))
        bucket = int(stamp.timestamp() // 900) * 900
        grouped.setdefault(bucket, []).append(row)
    result = []
    for bucket in sorted(grouped):
        items = sorted(grouped[bucket], key=lambda item: _parse_utc(item.get("t")))
        # A 15-minute candle is usable only after its third 5-minute interval has closed.
        bucket_end = datetime.fromtimestamp(bucket + 900, timezone.utc)
        last_five_minute_end = _parse_utc(items[-1].get("t")) + timedelta(minutes=5)
        if len(items) < 2 or last_five_minute_end < bucket_end:
            continue
        result.append({
            "t": datetime.fromtimestamp(bucket, timezone.utc).isoformat(),
            "o": float(items[0]["o"]),
            "h": max(float(x["h"]) for x in items),
            "l": min(float(x["l"]) for x in items),
            "c": float(items[-1]["c"]),
            "v": sum(float(x.get("v", 0) or 0) for x in items)
        })
    return result


def wilder_rsi(values, period=14):
    if len(values) <= period:
        return 50.0
    changes = [float(values[i]) - float(values[i-1]) for i in range(1, len(values))]
    gains = [max(0.0, change) for change in changes]
    losses = [max(0.0, -change) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def analyze_intraday(rows, daily_signal):
    """Short-horizon long-only setup: 5m trigger, 15m confirmation and daily regime."""
    if len(rows or []) < 60 or not daily_signal:
        return None
    c = [float(x["c"]) for x in rows]
    h = [float(x["h"]) for x in rows]
    l = [float(x["l"]) for x in rows]
    v = [float(x.get("v", 0) or 0) for x in rows]
    price = c[-1]
    ema9 = ema(c[-100:], 9)
    ema21 = ema(c[-150:], 21)
    macd_line = [a - b for a, b in zip(ema_series(c, 12), ema_series(c, 26))]
    macd_hist = macd_line[-1] - ema(macd_line[-40:], 9)
    rsi = wilder_rsi(c, 14)
    true_ranges = [
        max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
        for i in range(len(c)-14, len(c))
    ]
    atr = sum(true_ranges) / len(true_ranges)
    previous_volume = sum(v[-21:-1]) / 20 if len(v) >= 21 else 0.0
    relative_volume = v[-1] / previous_volume if previous_volume > 0 else 0.0

    ny = ZoneInfo("America/New_York")
    last_date = _parse_utc(rows[-1]["t"]).astimezone(ny).date()
    session_rows = [
        row for row in rows
        if _parse_utc(row["t"]).astimezone(ny).date() == last_date
    ]
    volume_sum = sum(float(x.get("v", 0) or 0) for x in session_rows)
    vwap = (
        sum(
            ((float(x["h"]) + float(x["l"]) + float(x["c"])) / 3)
            * float(x.get("v", 0) or 0)
            for x in session_rows
        ) / volume_sum
        if volume_sum > 0 else price
    )
    previous_high = max(h[-7:-1])
    fifteen = aggregate_15m(rows)
    if len(fifteen) < 12:
        return None
    c15 = [float(x["c"]) for x in fifteen]
    ema9_15 = ema(c15[-80:], 9)
    ema21_15 = ema(c15[-120:], 21)

    conditions = {
        "5m_trend": ema9 > ema21,
        "15m_trend": ema9_15 > ema21_15,
        "macd_positive": macd_hist > 0,
        "rsi_window": 48 <= rsi <= 72,
        "above_vwap": price > vwap,
        "6_bar_breakout": price > previous_high,
        "relative_volume": relative_volume >= 1.00
    }
    score = sum(bool(value) for value in conditions.values()) / len(conditions)
    daily_uptrend = (
        float(daily_signal.get("price", 0)) > float(daily_signal.get("sma200", 0))
        and float(daily_signal.get("ema50", 0)) > float(daily_signal.get("sma200", 0))
    )
    liquid = float(daily_signal.get("dollar_vol", 0)) >= 25_000_000
    hard_gates = conditions["5m_trend"] and conditions["15m_trend"] and conditions["above_vwap"]
    action = "BUY" if daily_uptrend and liquid and hard_gates and score >= 4/7 else (
        "SELL" if price < ema21 and price < vwap and macd_hist < 0 else "HOLD"
    )
    return {
        "price": price, "ema9": ema9, "ema21": ema21, "ema9_15": ema9_15,
        "ema21_15": ema21_15, "macd_hist": macd_hist, "rsi": rsi,
        "atr": atr, "vwap": vwap, "relative_volume": relative_volume,
        "prior_6_high": previous_high, "score": score, "action": action,
        "conditions": conditions, "daily_uptrend": daily_uptrend,
        "dollar_vol": float(daily_signal.get("dollar_vol", 0))
    }


def intraday_exit_levels(price, atr):
    """Short-horizon volatility-aware bracket: 0.50%-1.00% stop, >=0.80% target, >=1.6R."""
    price = float(price)
    atr = float(atr)
    if price <= 0 or atr <= 0:
        return None
    stop_distance = max(1.0 * atr, price * 0.005)
    if stop_distance > price * 0.010:
        return None
    target_distance = max(price * 0.008, stop_distance * 1.6)
    if target_distance > price * 0.020:
        return None
    stop = round(price - stop_distance, 2)
    target = round(price + target_distance, 2)
    if stop <= 0 or target <= price:
        return None
    return {
        "stop": stop, "target": target,
        "stop_distance": price - stop,
        "target_distance": target - price
    }


def backtest_strategy(rows, spy_rows, starting_equity=10000.0):
    if len(rows) < 250 or len(spy_rows) < 250:
        raise ValueError("En az 250 günlük mum gerekli; veri sağlayıcısı bu sembole daha az geçmiş döndürdü.")
    market_regime = {}
    for j in range(219, len(spy_rows)):
        mf = analyze(spy_rows[:j+1])
        if mf:
            d = str(spy_rows[j].get("t", ""))[:10]
            market_regime[d] = mf["price"] > mf["sma200"] and mf["ema50"] > mf["sma200"]
    equity = float(starting_equity)
    peak = equity
    max_dd = 0.0
    trades = []
    position = None
    wins = []
    losses = []
    slippage = 0.001
    last_marked_equity = equity
    for i in range(220, len(rows)):
        signal = analyze(rows[:i])
        if not signal:
            continue
        day = rows[i]
        previous_day = str(rows[i-1].get("t", ""))[:10]
        market_ok = market_regime.get(previous_day, False)
        day_open = float(day["o"])
        day_high = float(day["h"])
        day_low = float(day["l"])
        day_close = float(day["c"])
        closed_this_bar = False

        if position:
            # If both barriers are touched in one daily candle, assume the stop was hit first.
            exit_price = None
            exit_reason = ""
            if day_low <= position["stop"]:
                exit_price = min(day_open, position["stop"]) * (1-slippage)
                exit_reason = "STOP"
            elif day_high >= position["target"]:
                exit_price = position["target"] * (1-slippage)
                exit_reason = "HEDEF"
            elif signal["action"] == "SELL":
                exit_price = day_open * (1-slippage)
                exit_reason = "TREND ÇIKIŞI"
            elif i == len(rows)-1:
                exit_price = day_close * (1-slippage)
                exit_reason = "TEST SONU"
            if exit_price is not None:
                pnl = (exit_price-position["entry"])*position["qty"]
                equity += pnl
                trades.append({"pnl":pnl,"entry":position["entry"],"exit":exit_price,"qty":position["qty"],"reason":exit_reason})
                (wins if pnl > 0 else losses).append(pnl)
                position = None
                closed_this_bar = True

        # Peak drawdown is persistent in live mode; do not start new trades after a 5% breach.
        drawdown_gate_open = peak <= 0 or last_marked_equity > peak*0.95
        if not position and not closed_this_bar and signal["action"] == "BUY" and market_ok and drawdown_gate_open and confirm_live_entry(signal, day_open):
            entry = day_open * (1+slippage)
            distance = max(2.0*signal["atr"], signal["price"]*0.015)
            if distance > 0 and distance < entry*0.25:
                qty = max(0, min(int(equity*0.0025/distance), int(equity*0.07/entry)))
                if qty > 0:
                    position = {"entry":entry,"qty":qty,"stop":entry-distance,"target":entry+3.0*distance}
                    # Evaluate stop/target on the entry day too; the earlier version skipped this.
                    if day_low <= position["stop"]:
                        exit_price = min(day_open, position["stop"]) * (1-slippage)
                        pnl = (exit_price-position["entry"])*position["qty"]
                        equity += pnl
                        trades.append({"pnl":pnl,"entry":position["entry"],"exit":exit_price,"qty":position["qty"],"reason":"GİRİŞ GÜNÜ STOP"})
                        (wins if pnl > 0 else losses).append(pnl)
                        position = None
                        closed_this_bar = True
                    elif day_high >= position["target"]:
                        exit_price = position["target"] * (1-slippage)
                        pnl = (exit_price-position["entry"])*position["qty"]
                        equity += pnl
                        trades.append({"pnl":pnl,"entry":position["entry"],"exit":exit_price,"qty":position["qty"],"reason":"GİRİŞ GÜNÜ HEDEF"})
                        (wins if pnl > 0 else losses).append(pnl)
                        position = None
                        closed_this_bar = True
                    elif i == len(rows)-1:
                        exit_price = day_close * (1-slippage)
                        pnl = (exit_price-position["entry"])*position["qty"]
                        equity += pnl
                        trades.append({"pnl":pnl,"entry":position["entry"],"exit":exit_price,"qty":position["qty"],"reason":"TEST SONU"})
                        (wins if pnl > 0 else losses).append(pnl)
                        position = None
                        closed_this_bar = True

        mark_to_market = equity
        if position:
            mark_to_market += (day_close-position["entry"])*position["qty"]
        peak = max(peak, mark_to_market)
        if peak > 0:
            max_dd = max(max_dd, (peak-mark_to_market)/peak)
        last_marked_equity = mark_to_market
    if position:
        final_exit = float(rows[-1]["c"])*(1-slippage)
        pnl = (final_exit-position["entry"])*position["qty"]
        equity += pnl
        trades.append({"pnl":pnl,"entry":position["entry"],"exit":final_exit,"qty":position["qty"],"reason":"TEST SONU"})
        (wins if pnl > 0 else losses).append(pnl)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak-equity)/peak)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit/gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    first = float(rows[220]["c"])
    last = float(rows[-1]["c"])
    benchmark = ((last/first)-1)*100 if first else 0.0
    spy_first = float(spy_rows[220]["c"])
    spy_last = float(spy_rows[-1]["c"])
    spy_benchmark = ((spy_last/spy_first)-1)*100 if spy_first else 0.0
    return {
        "initial":starting_equity,"final":equity,"return_pct":(equity/starting_equity-1)*100,
        "trades":len(trades),"win_rate":(len(wins)/len(trades)*100) if trades else 0.0,
        "profit_factor":profit_factor,"max_drawdown":max_dd*100,
        "benchmark":benchmark,"spy_benchmark":spy_benchmark,"avg_trade":(sum(x["pnl"] for x in trades)/len(trades)) if trades else 0.0,
        "trades_detail":trades
    }


def backtest_intraday_strategy(intraday_rows, daily_rows, spy_daily_rows, starting_equity=10000.0, slippage=0.001, now=None, profit_target_usd=20.0):
    """Conservative single-symbol 5m simulation; stop wins when both barriers touch."""
    intraday_rows = intraday_rows or []
    if not intraday_rows or len(daily_rows or []) < 220 or len(spy_daily_rows or []) < 220:
        raise ValueError("5 dakikalık test için intraday veri ve en az 220 günlük mum gerekli.")
    # Exclude the current incomplete 5-minute candle; use broker clock when supplied.
    rows, _, _ = prepare_intraday_rows(intraday_rows, now)
    if len(rows) < 80:
        raise ValueError("Test dönemi içinde en az 80 tamamlanmış normal seans 5 dakikalık mum bulunamadı.")

    ny = ZoneInfo("America/New_York")
    daily_cache = {}
    spy_cache = {}

    def asof_signal(source, cache, day):
        key = day.isoformat()
        if key not in cache:
            eligible = [
                item for item in source
                if _parse_utc(item.get("t")).astimezone(ny).date() < day
            ]
            cache[key] = analyze(eligible) if len(eligible) >= 220 else None
        return cache[key]

    equity = float(starting_equity)
    peak = equity
    max_dd = 0.0
    wins, losses, trades = [], [], []
    position = None
    for i in range(60, len(rows)):
        bar = rows[i]
        local = _parse_utc(bar["t"]).astimezone(ny)
        day = local.date()
        daily_signal = asof_signal(daily_rows, daily_cache, day)
        spy_signal = asof_signal(spy_daily_rows, spy_cache, day)
        market_ok = bool(
            spy_signal
            and spy_signal["price"] > spy_signal["sma200"]
            and spy_signal["ema50"] > spy_signal["sma200"]
        )
        signal = analyze_intraday(rows[:i], daily_signal)
        bar_open = float(bar["o"])
        bar_high = float(bar["h"])
        bar_low = float(bar["l"])
        bar_close = float(bar["c"])
        exited = False

        if position:
            exit_price = None
            reason = ""
            if bar_low <= position["stop"]:
                exit_price = min(bar_open, position["stop"]) * (1 - slippage)
                reason = "STOP"
            elif bar_high >= position.get("profit_trigger_price", position["target"]):
                exit_price = max(bar_open, position.get("profit_trigger_price", position["target"])) * (1 - slippage)
                reason = "USD KÂR HEDEFİ"
            elif i - position["entry_index"] >= 9 or (local.hour * 60 + local.minute) >= 945:
                exit_price = bar_close * (1 - slippage)
                reason = "ZAMAN/GÜN SONU"
            elif signal and signal["action"] == "SELL":
                exit_price = bar_close * (1 - slippage)
                reason = "TERS SİNYAL"
            if exit_price is not None:
                pnl = (exit_price - position["entry"]) * position["qty"]
                equity += pnl
                trades.append({"pnl": pnl, "entry": position["entry"], "exit": exit_price, "qty": position["qty"], "reason": reason})
                (wins if pnl > 0 else losses).append(pnl)
                position = None
                exited = True

        if (
            not position and not exited and signal
            and signal["action"] == "BUY" and market_ok
            and 585 <= (local.hour * 60 + local.minute) < 900
        ):
            entry = bar_open * (1 + slippage)
            levels = intraday_exit_levels(entry, signal["atr"])
            if levels:
                qty = calculate_order_qty(
                    equity, equity, entry, levels["stop_distance"], 0.0,
                    profit_target_usd=profit_target_usd
                )
                dollar_target = dollar_profit_limit_price(entry, qty, profit_target_usd) if qty else None
                if qty > 0 and dollar_target and dollar_target["distance"] <= entry * 0.02:
                    profit_trigger_price = entry + float(profit_target_usd) / qty
                    position = {
                        "entry": entry, "qty": qty, "stop": levels["stop"],
                        "target": dollar_target["price"],
                        "profit_trigger_price": profit_trigger_price,
                        "entry_index": i
                    }
                    if bar_low <= position["stop"]:
                        exit_price = min(bar_open, position["stop"]) * (1 - slippage)
                        pnl = (exit_price - entry) * qty
                        equity += pnl
                        trades.append({"pnl": pnl, "entry": entry, "exit": exit_price, "qty": qty, "reason": "GİRİŞ MUMU STOP"})
                        (wins if pnl > 0 else losses).append(pnl)
                        position = None
                    elif bar_high >= position.get("profit_trigger_price", position["target"]):
                        exit_price = max(bar_open, position.get("profit_trigger_price", position["target"])) * (1 - slippage)
                        pnl = (exit_price - entry) * qty
                        equity += pnl
                        trades.append({"pnl": pnl, "entry": entry, "exit": exit_price, "qty": qty, "reason": "GİRİŞ MUMU USD KÂR HEDEFİ"})
                        (wins if pnl > 0 else losses).append(pnl)
                        position = None

        marked = equity
        if position:
            marked += (bar_close - position["entry"]) * position["qty"]
        peak = max(peak, marked)
        if peak > 0:
            max_dd = max(max_dd, (peak - marked) / peak)

    if position:
        exit_price = float(rows[-1]["c"]) * (1 - slippage)
        pnl = (exit_price - position["entry"]) * position["qty"]
        equity += pnl
        trades.append({"pnl": pnl, "entry": position["entry"], "exit": exit_price, "qty": position["qty"], "reason": "TEST SONU"})
        (wins if pnl > 0 else losses).append(pnl)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    first, last = float(rows[0]["c"]), float(rows[-1]["c"])
    benchmark = ((last / first) - 1) * 100 if first else 0.0
    spy_first, spy_last = float(spy_daily_rows[-2]["c"]), float(spy_daily_rows[-1]["c"])
    spy_benchmark = ((spy_last / spy_first) - 1) * 100 if spy_first else 0.0
    return {
        "initial": starting_equity, "final": equity,
        "return_pct": (equity / starting_equity - 1) * 100,
        "trades": len(trades),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "profit_factor": profit_factor, "max_drawdown": max_dd * 100,
        "benchmark": benchmark, "spy_benchmark": spy_benchmark,
        "avg_trade": (sum(x["pnl"] for x in trades) / len(trades)) if trades else 0.0,
        "trades_detail": trades
    }


def news_score(items):
    scores={}
    for item in items:
        words={w.strip(".,:;!?()[]{}'\\\"").lower() for w in (str(item.get("headline",""))+" "+str(item.get("summary",""))).split()}
        s=max(-1,min(1,(len(words&POS)-len(words&NEG))/3))
        for sym in item.get("symbols",[]): scores.setdefault(sym,[]).append(s)
    return {sym:sum(vals)/len(vals) for sym,vals in scores.items()}

class Bot:
    def __init__(self, broker, events, stop, profit_target_usd=20.0):
        self.broker = broker
        self.events = events
        self.stop = stop
        self.profit_target_usd = float(profit_target_usd)
        self.managed_symbols = load_managed_symbols()
        self.managed_groups = load_managed_groups()
        self.managed_protection = load_managed_protection()

    def _save_management_state(self):
        save_managed_symbols(self.managed_symbols)
        save_managed_groups(self.managed_groups)
        save_managed_protection(self.managed_protection)

    def _close_managed_position(self, symbol, open_orders):
        """Cancel only this AstraTrade position's OCO legs, then close it at market."""
        position = next((p for p in self.broker.positions() if p.get("symbol") == symbol), None)
        if not position:
            self.managed_symbols.discard(symbol)
            self.managed_groups.pop(symbol, None)
            self.managed_protection.pop(symbol, None)
            self._save_management_state()
            self.events.put(("log", f"{symbol}: pozisyon zaten kapanmış; takip kaydı temizlendi."))
            return True

        sell_orders = [
            o for o in open_orders
            if o.get("symbol") == symbol and str(o.get("side", "")).lower() == "sell"
        ]
        group_id = self.managed_groups.get(symbol)
        candidate_groups = {
            str(o.get("group_id"))
            for o in sell_orders
            if o.get("group_id") and o.get("group_role") in ("take_profit", "stop_loss")
        }
        if not group_id and len(candidate_groups) == 1:
            group_id = next(iter(candidate_groups))
            self.managed_groups[symbol] = group_id
            self._save_management_state()
        if len(candidate_groups) > 1:
            self.events.put(("log", f"ÇIKIŞ GÜVENLİK ENGELİ {symbol}: birden fazla emir grubu var; otomatik satış yapılmadı."))
            return False
        if group_id and candidate_groups and group_id not in candidate_groups:
            self.events.put(("log", f"ÇIKIŞ GÜVENLİK ENGELİ {symbol}: kayıtlı emir grubu ile Alpaca emirleri uyuşmuyor."))
            return False

        protection = self.managed_protection.get(symbol, {})
        take_profit = float(protection.get("take_profit", 0) or 0)
        stop_loss = float(protection.get("stop_loss", 0) or 0)
        for order in sell_orders:
            if group_id and str(order.get("group_id") or "") == str(group_id):
                if order.get("group_role") == "take_profit":
                    take_profit = float(order.get("limit_price") or take_profit or 0)
                elif order.get("group_role") == "stop_loss":
                    stop_loss = float(order.get("stop_price") or stop_loss or 0)

        unrelated_sells = [
            o for o in sell_orders
            if not group_id or str(o.get("group_id") or "") != str(group_id)
        ]
        # A standalone/manual sell order is never canceled by AstraTrade.
        if unrelated_sells:
            self.events.put(("log", f"ÇIKIŞ GÜVENLİK ENGELİ {symbol}: başka/manuel satış emri bulundu; dokunulmadı."))
            return False

        group_legs = [
            o for o in sell_orders
            if group_id and str(o.get("group_id") or "") == str(group_id)
        ]
        for leg in group_legs:
            status = str(leg.get("status", "")).lower()
            if status in ("filled", "canceled", "expired", "rejected", "replaced", "pending_cancel"):
                continue
            order_id = leg.get("id")
            if not order_id:
                self.events.put(("log", f"ÇIKIŞ GÜVENLİK ENGELİ {symbol}: koruyucu emrin kimliği okunamadı."))
                return False
            try:
                self.broker.cancel_order(order_id)
            except Exception as exc:
                # An OCO sibling may have been canceled automatically or filled during cancellation.
                self.events.put(("log", f"{symbol} koruyucu emir iptal yanıtı: {exc}"))

        # Wait for both OCO legs to leave the open-order book before sending a market close.
        remaining = []
        for _ in range(12):
            time.sleep(0.25)
            fresh_orders = self.broker.open_orders()
            remaining = [
                o for o in fresh_orders
                if o.get("symbol") == symbol
                and str(o.get("side", "")).lower() == "sell"
                and group_id
                and str(o.get("group_id") or "") == str(group_id)
            ]
            if not remaining:
                open_orders = fresh_orders
                break
        if remaining:
            self.events.put(("log", f"ÇIKIŞ ERTELENDİ {symbol}: zarar-kes/kâr-al emirleri henüz kapanmadı; pozisyon korunuyor."))
            return False

        fresh_sells = [
            o for o in open_orders
            if o.get("symbol") == symbol and str(o.get("side", "")).lower() == "sell"
        ]
        if fresh_sells:
            self.events.put(("log", f"ÇIKIŞ GÜVENLİK ENGELİ {symbol}: iptal sonrasında başka satış emri kaldı; pozisyona dokunulmadı."))
            return False

        fresh_position = next((p for p in self.broker.positions() if p.get("symbol") == symbol), None)
        if not fresh_position:
            self.managed_symbols.discard(symbol)
            self.managed_groups.pop(symbol, None)
            self.managed_protection.pop(symbol, None)
            self._save_management_state()
            self.events.put(("log", f"{symbol}: koruyucu emir tetiklenmiş ve pozisyon kapanmış."))
            return True

        try:
            result = self.broker.close_position(symbol)
            self.events.put(("log", f"ASTRATRADE PAPER ÇIKIŞ {symbol} | tam pozisyon kapatma emri: {result.get('status', 'gönderildi')}."))
            return True
        except Exception as exc:
            # If the market-close request fails after canceling our OCO legs,
            # restore the exact saved protection rather than leaving the holding naked.
            fresh_position = next((p for p in self.broker.positions() if p.get("symbol") == symbol), None)
            if fresh_position and take_profit > 0 and stop_loss > 0:
                try:
                    qty = abs(float(fresh_position.get("qty", 0) or 0))
                    restored = self.broker.restore_oco_protection(symbol, qty, take_profit, stop_loss)
                    if restored.get("group_id"):
                        self.managed_groups[symbol] = str(restored["group_id"])
                        self._save_management_state()
                    self.events.put(("log", f"ÇIKIŞ BAŞARISIZ {symbol}: {exc}. Eski zarar-kes/kâr-al koruması yeniden kuruldu."))
                except Exception as protect_exc:
                    self.events.put(("log", f"KRİTİK KORUMA HATASI {symbol}: satış başarısız ({exc}); OCO koruması da kurulamadı ({protect_exc}). Bot güvenlik için durduruluyor."))
                    self.stop.set()
            else:
                self.events.put(("log", f"KRİTİK ÇIKIŞ HATASI {symbol}: satış emri gönderilemedi ({exc}); koruyucu fiyatlar bulunamadığından otomatik kapatma denemesi yapılmadı."))
                self.stop.set()
            return False

    def _run_legacy(self):
        self.broker.account()
        assets = self.broker.assets()
        symbols = sorted({str(a.get("symbol", "")).strip() for a in assets if is_eligible_common_stock(a)})
        if not symbols:
            raise RuntimeError("Alpaca'dan uygun ABD adi hissesi alınamadı.")
        self.events.put(("log", f"Tarama evreni: {len(symbols)} aktif NYSE/NASDAQ/AMEX adi hissesi."))
        batch_size = 200
        scan_interval = 300
        offset = 0
        self.events.put(("log", "Strateji: tamamlanmış günlük mumlarda SMA200 + EMA20/50 + MACD + Wilder RSI + 20 günlük kırılım + hacim teyidi."))
        self.events.put(("log", "Risk motoru: işlem başına %0,25 risk, pozisyon başına azami %7, toplam yönetilen maruziyet %30, günlük zarar sınırı %1,5, zirveden düşüş sınırı %5."))
        while not self.stop.is_set():
            batch = symbols[offset:offset+batch_size]
            offset = (offset+batch_size) % len(symbols)
            try:
                bars = self.broker.bars(batch).get("bars", {})
                bar_counts = {sym: len(bars.get(sym, [])) for sym in batch}
                sufficient_history = sum(1 for count in bar_counts.values() if count >= 220)
                total_daily_bars = sum(bar_counts.values())
                samples = ", ".join(f"{sym}:{bar_counts[sym]}" for sym in batch[:8])
                clock = self.broker.clock()
                market_day = str(clock.get("timestamp", ""))[:10]
                spy_rows = self.broker.bars(["SPY"]).get("bars", {}).get("SPY", [])
                spy_signal_rows, _, _ = prepare_daily_signal_rows(spy_rows, bool(clock.get("is_open")), market_day)
                spy = analyze(spy_signal_rows)
                market_ok = bool(spy and spy["price"] > spy["sma200"] and spy["ema50"] > spy["sma200"])

                positions = self.broker.positions()
                held = {p["symbol"]: p for p in positions}
                open_orders = self.broker.open_orders()
                pending = {o.get("symbol") for o in open_orders if o.get("symbol")}
                pending_buy_symbols = {
                    o.get("symbol") for o in open_orders
                    if o.get("symbol") and str(o.get("side", "")).lower() == "buy"
                }
                pending_market_sell_symbols = {
                    o.get("symbol") for o in open_orders
                    if o.get("symbol") and str(o.get("side", "")).lower() == "sell" and not o.get("group_role")
                }

                # Newer Alpaca order responses expose group_id/group_role on each OCO leg.
                # Recover those IDs when upgrading from an earlier AstraTrade build.
                for order in open_orders:
                    symbol = order.get("symbol")
                    if not symbol:
                        continue
                    # Recover an entry accepted by Alpaca just before a prior app shutdown.
                    if str(order.get("side", "")).lower() == "buy" and str(order.get("client_order_id", "")).startswith("astra-"):
                        self.managed_symbols.add(symbol)
                    if symbol in self.managed_symbols and order.get("group_id") and order.get("group_role") in ("take_profit", "stop_loss"):
                        self.managed_groups[symbol] = str(order["group_id"])
                        protection = self.managed_protection.setdefault(symbol, {})
                        if order.get("group_role") == "take_profit" and order.get("limit_price"):
                            protection["take_profit"] = float(order["limit_price"])
                        elif order.get("group_role") == "stop_loss" and order.get("stop_price"):
                            protection["stop_loss"] = float(order["stop_price"])

                active_symbols = set(held) | pending
                removed_symbols = self.managed_symbols - active_symbols
                self.managed_symbols.intersection_update(active_symbols)
                for removed in removed_symbols:
                    self.managed_groups.pop(removed, None)
                    self.managed_protection.pop(removed, None)
                self._save_management_state()

                managed_active = set(self.managed_symbols)
                reserved = set(held) | pending
                external_positions = set(held) - managed_active
                account = self.broker.account()
                equity = float(account.get("equity", 0) or 0)
                cash = float(account.get("cash", 0) or 0)
                last_equity = float(account.get("last_equity", equity) or equity)
                daily_return = (equity-last_equity)/last_equity if last_equity else 0.0
                state = read_app_state()
                peak_equity = float(state.get("peak_equity", equity) or equity)
                if "peak_equity" not in state or equity > peak_equity:
                    peak_equity = equity
                    write_app_state(peak_equity=peak_equity)
                peak_drawdown = (equity-peak_equity)/peak_equity if peak_equity else 0.0
                risk_allowed = daily_return > -0.015 and peak_drawdown > -0.05

                managed_exposure = sum(
                    abs(float(p.get("market_value", 0) or 0))
                    for symbol, p in held.items() if symbol in managed_active
                )
                pending_buy_unfilled = pending_buy_symbols - set(held)
                pending_managed_buys = pending_buy_unfilled & managed_active
                # Reserve one maximum-sized slot for every unfilled entry; this prevents
                # repeated scans/restarts from overspending the 30% portfolio exposure cap.
                managed_exposure += len(pending_managed_buys) * equity * 0.07
                reserved_cash = len(pending_buy_unfilled) * equity * 0.07
                available_cash = max(0.0, cash-reserved_cash)

                self.events.put(("log", f"Hesap: {len(held)} pozisyon | AstraTrade: {len(managed_active)}/5 | eski/harici: {len(external_positions)} | günlük değişim: {daily_return*100:.2f}% | zirveden düşüş: {peak_drawdown*100:.2f}%."))
                if not market_ok:
                    self.events.put(("log", "PİYASA FİLTRESİ: SPY uzun vadeli trendi olumlu değil veya veri yetersiz; yeni alışlar bekletiliyor."))
                if not risk_allowed:
                    self.events.put(("log", "RİSK KİLİDİ: Günlük %1,5 zarar veya zirveden %5 düşüş sınırı aşıldı; yeni alışlar kilitlendi."))

                scanned = 0
                best_score = 0.0
                buy_signal_count = 0
                blocked = {"trend": 0, "score": 0, "liquidity": 0, "other": 0}
                order_blocked = {
                    "piyasa_kapali": 0, "veri_eski": 0, "canli_fiyat": 0, "spy_filtresi": 0,
                    "gunluk_risk": 0, "bekleyen_emir": 0, "pozisyon_limiti": 0,
                    "dolar_hedefi_uzak": 0, "miktar_sifir": 0
                }
                stale_price_count = 0
                for sym in batch:
                    if self.stop.is_set():
                        break
                    raw_rows = bars.get(sym, [])
                    signal_rows, execution_price, fresh_price = prepare_daily_signal_rows(
                        raw_rows, bool(clock.get("is_open")), market_day
                    )
                    f = analyze(signal_rows)
                    if not f:
                        continue
                    live_price = execution_price if execution_price and execution_price > 0 else f["price"]
                    if live_price < 5:
                        continue
                    if clock.get("is_open") and not fresh_price:
                        stale_price_count += 1
                    action = f["action"]
                    best_score = max(best_score, f["score"])
                    if action == "BUY":
                        buy_signal_count += 1
                    else:
                        conditions = f["conditions"]
                        if not (conditions["uzun_trend"] and conditions["trend_sirasi"] and conditions["macd_pozitif"]):
                            blocked["trend"] += 1
                        elif f["score"] < 5/6:
                            blocked["score"] += 1
                        elif f["dollar_vol"] < 10_000_000:
                            blocked["liquidity"] += 1
                        else:
                            blocked["other"] += 1
                    scanned += 1
                    self.events.put(("decision", (sym, action, f["score"], live_price, f["rsi"])))

                    if action == "BUY":
                        if not clock.get("is_open"):
                            order_blocked["piyasa_kapali"] += 1
                        elif sym in pending_buy_symbols:
                            order_blocked["bekleyen_emir"] += 1
                        elif not fresh_price:
                            order_blocked["veri_eski"] += 1
                        elif not confirm_live_entry(f, live_price):
                            order_blocked["canli_fiyat"] += 1
                        elif not market_ok:
                            order_blocked["spy_filtresi"] += 1
                        elif not risk_allowed:
                            order_blocked["gunluk_risk"] += 1
                        elif sym in reserved:
                            order_blocked["bekleyen_emir"] += 1
                        elif len(managed_active) >= MAX_MANAGED_POSITIONS:
                            order_blocked["pozisyon_limiti"] += 1
                        else:
                            distance = max(2.0*f["atr"], live_price*0.015)
                            if distance <= 0 or distance >= live_price*0.25:
                                order_blocked["miktar_sifir"] += 1
                                continue
                            qty = calculate_order_qty(equity, available_cash, live_price, distance, managed_exposure)
                            if not qty:
                                order_blocked["miktar_sifir"] += 1
                                continue
                            stop_price = round(live_price-distance, 2)
                            take_profit_price = round(live_price+3.0*distance, 2)
                            try:
                                result = self.broker.order(sym, qty, "buy", take_profit=take_profit_price, stop_loss=stop_price)
                            except Exception as order_error:
                                self.events.put(("log", f"ALIŞ EMRİ REDDEDİLDİ {sym}: {order_error}"))
                                continue
                            self.managed_symbols.add(sym)
                            if result.get("group_id"):
                                self.managed_groups[sym] = str(result["group_id"])
                            self.managed_protection[sym] = {
                                "take_profit": take_profit_price,
                                "stop_loss": stop_price,
                                "qty": qty
                            }
                            self._save_management_state()
                            managed_active.add(sym)
                            reserved.add(sym)
                            pending.add(sym)
                            pending_buy_symbols.add(sym)
                            managed_exposure += qty*live_price
                            available_cash = max(0.0, available_cash-qty*live_price)
                            self.events.put(("log", f"ASTRATRADE PAPER ALIŞ {sym} x{qty} | skor {f['score']*100:.0f}/100 | planlanan risk USD {qty*distance:.2f} | SL {stop_price:.2f} | TP {take_profit_price:.2f} | {result.get('status')}"))
                    elif action == "SELL" and sym in held and sym in self.managed_symbols:
                        if not clock.get("is_open"):
                            continue
                        if sym in pending_market_sell_symbols:
                            continue
                        if sym in pending_buy_symbols:
                            continue
                        self._close_managed_position(sym, open_orders)
                self.events.put(("status", (len(symbols), scanned, equity, cash, len(held), bool(clock.get("is_open")), daily_return, len(managed_active))))
                data_symbols = sum(1 for count in bar_counts.values() if count > 0)
                self.events.put(("log", f"Tarama kalitesi: {scanned} analiz | en yüksek skor {best_score:.2f}/1,00 | BUY sinyali {buy_signal_count}."))
                self.events.put(("log", f"Teknik BUY engelleri (öncelikli sınıflama): trend {blocked['trend']}, skor {blocked['score']}, likidite {blocked['liquidity']}, diğer {blocked['other']}. Alış için 6 teknik koşuldan en az 5'i ve 10 milyon USD ortalama dolar hacmi zorunlu."))
                self.events.put(("log", "Emir engelleri: " + ", ".join(f"{key}={value}" for key, value in order_blocked.items()) + "."))
                self.events.put(("log", f"Canlı fiyatı güncel olmayan sembol: {stale_price_count}; bu sembollere yeni alış gönderilmedi."))
                self.events.put(("log", f"Veri: {data_symbols}/{len(batch)} sembol; {total_daily_bars} günlük bar; 220+ bar geçmişi olan: {sufficient_history}. SPY piyasa filtresi: {'OLUMLU' if market_ok else 'OLUMSUZ'}."))
                self.events.put(("log", f"Veri örneği: {samples}"))
                if data_symbols == 0:
                    self.events.put(("log", "VERİ UYARISI: IEX piyasa verisi bu grupta boş döndü."))
                self.events.put(("log", f"Tarama tamamlandı: {scanned} hisse analiz edildi. Piyasa: {'açık' if clock.get('is_open') else 'kapalı'}."))
            except Exception as exc:
                self.events.put(("log", "Tarama hatası: " + str(exc)))
            self.stop.wait(scan_interval)
        self.events.put(("log", "Bot durduruldu."))

    def _run_intraday(self):
        """Production strategy: top-liquid universe, 5m entries, 15m confirmation, intraday exits."""
        self.broker.account()
        assets = self.broker.assets()
        symbols = sorted({
            str(asset.get("symbol", "")).strip()
            for asset in assets if is_eligible_common_stock(asset)
        })
        if not symbols:
            raise RuntimeError("Alpaca'dan uygun ABD adi hissesi alınamadı.")

        startup_clock = self.broker.clock()
        market_day = str(startup_clock.get("timestamp", ""))[:10]
        self.events.put(("log", f"ABD adi hisse evreni: {len(symbols)} sembol; likidite sıralaması hazırlanıyor."))
        daily_result = self.broker.bars(
            symbols,
            progress_callback=lambda message: self.events.put(("log", message))
        )
        daily_data = daily_result.get("bars", {})
        failed_daily_symbols = daily_result.get("failed_symbols", [])
        if failed_daily_symbols:
            self.events.put(("log", f"Günlük verisi alınamayan {len(failed_daily_symbols)} sembol yeni alım adaylarından çıkarıldı."))
        if len(failed_daily_symbols) > max(25, int(len(symbols) * 0.02)):
            raise RuntimeError(
                f"Günlük veri kaybı yüksek ({len(failed_daily_symbols)}/{len(symbols)} sembol); "
                "eksik evrenle işlem yapılmaması için bot durduruldu."
            )
        daily_signals = {}
        for symbol in symbols:
            completed, _, _ = prepare_daily_signal_rows(
                daily_data.get(symbol, []), bool(startup_clock.get("is_open")), market_day
            )
            signal = analyze(completed)
            if signal and signal["price"] >= 10 and signal["dollar_vol"] >= 20_000_000:
                daily_signals[symbol] = signal
        ranked = sorted(
            daily_signals,
            key=lambda symbol: daily_signals[symbol]["dollar_vol"],
            reverse=True
        )
        scan_symbols = ranked[:300]
        if not scan_symbols:
            raise RuntimeError("20 milyon USD üzeri ortalama günlük dolar hacmine sahip yeterli aday bulunamadı.")
        del daily_data
        self.daily_signals = daily_signals
        self.scan_symbols = scan_symbols
        spy_rows = self.broker.bars(["SPY"]).get("bars", {}).get("SPY", [])
        spy_completed, _, _ = prepare_daily_signal_rows(
            spy_rows, bool(startup_clock.get("is_open")), market_day
        )
        spy_signal = analyze(spy_completed)
        market_ok = bool(
            spy_signal
            and spy_signal["price"] > spy_signal["sma200"]
            and spy_signal["ema50"] > spy_signal["sma200"]
        )
        self.events.put(("log", f"Kısa vadeli izleme listesi: {len(scan_symbols)} en likit hisse | günlük ortalama dolar hacmi en az USD 20 milyon."))
        self.events.put(("log", "Strateji: 5 dakikalık EMA9/21 + VWAP + MACD + Wilder RSI + 6 mumluk kırılım + hacim; 15 dakikalık EMA9/21 teyidi; günlük SMA200 rejim filtresi."))
        self.events.put(("log", f"İşlem planı: pozisyon başına USD {self.profit_target_usd:.2f} gerçekleşmemiş kâr hedefi; hedef/stop en az 1,6; stop %0,50-%1,00; 45 dk azami taşıma; 15:45 ET gün sonu kapatma."))
        self.events.put(("log", "Risk motoru: işlem başına azami %0,25 risk bütçesi, pozisyon başına %7, yönetilen toplam maruziyet %30, günlük zarar kilidi %1,5, zirveden düşüş kilidi %5."))

        while not self.stop.is_set():
            cycle_started = time.monotonic()
            try:
                clock = self.broker.clock()
                if not clock.get("is_open"):
                    self.events.put(("log", "Piyasa kapalı; yeni intraday işlem gönderilmiyor. Açılışta otomatik devam edilecek."))
                    self.stop.wait(300)
                    continue
                now_utc = _parse_utc(clock.get("timestamp"))
                ny_now = now_utc.astimezone(ZoneInfo("America/New_York"))
                minute_of_day = ny_now.hour * 60 + ny_now.minute
                can_enter_time = 585 <= minute_of_day < 900  # 09:45-15:00 ET
                force_flat_time = minute_of_day >= 945       # 15:45 ET

                positions = self.broker.positions()
                held = {p["symbol"]: p for p in positions if p.get("symbol")}
                open_orders = self.broker.open_orders()
                pending = {o.get("symbol") for o in open_orders if o.get("symbol")}
                pending_buy_symbols = {
                    o.get("symbol") for o in open_orders
                    if o.get("symbol") and str(o.get("side", "")).lower() == "buy"
                }
                pending_market_sell_symbols = {
                    o.get("symbol") for o in open_orders
                    if o.get("symbol") and str(o.get("side", "")).lower() == "sell" and not o.get("group_role")
                }

                for order in open_orders:
                    symbol = order.get("symbol")
                    if not symbol:
                        continue
                    if str(order.get("side", "")).lower() == "buy" and str(order.get("client_order_id", "")).startswith("astra-"):
                        self.managed_symbols.add(symbol)
                    if symbol in self.managed_symbols and order.get("group_id") and order.get("group_role") in ("take_profit", "stop_loss"):
                        self.managed_groups[symbol] = str(order["group_id"])
                        protection = self.managed_protection.setdefault(symbol, {})
                        if order.get("group_role") == "take_profit" and order.get("limit_price"):
                            protection["take_profit"] = float(order["limit_price"])
                        elif order.get("group_role") == "stop_loss" and order.get("stop_price"):
                            protection["stop_loss"] = float(order["stop_price"])

                active_symbols = set(held) | pending
                removed_symbols = self.managed_symbols - active_symbols
                self.managed_symbols.intersection_update(active_symbols)
                for removed in removed_symbols:
                    self.managed_groups.pop(removed, None)
                    self.managed_protection.pop(removed, None)
                self._save_management_state()

                # Time exits are evaluated every cycle for all AstraTrade intraday holdings,
                # not just when their ticker happens to appear in the scanning list.
                for symbol in sorted(set(held) & self.managed_symbols):
                    meta = self.managed_protection.get(symbol, {})
                    if meta.get("strategy") != "intraday_v1" or symbol in pending_market_sell_symbols:
                        continue
                    position = held.get(symbol, {})
                    position_target = float(meta.get("profit_target_usd", self.profit_target_usd) or self.profit_target_usd)
                    current_pnl = float(position.get("unrealized_pl", 0) or 0)
                    if profit_target_reached(position, position_target):
                        self.events.put((
                            "log",
                            f"DOLAR KÂR HEDEFİ {symbol}: gerçekleşmemiş K/Z USD {current_pnl:+.2f} >= USD {position_target:.2f}; pozisyon kapatılıyor."
                        ))
                        if self._close_managed_position(symbol, open_orders):
                            pending_market_sell_symbols.add(symbol)
                        continue
                    opened_at = meta.get("opened_at")
                    try:
                        elapsed_minutes = (now_utc - _parse_utc(opened_at)).total_seconds() / 60
                    except (TypeError, ValueError):
                        elapsed_minutes = 0
                    max_hold = float(meta.get("max_hold_minutes", 45) or 45)
                    if elapsed_minutes >= max_hold or force_flat_time:
                        self.events.put(("log", f"ZAMAN ÇIKIŞI {symbol}: taşıma {elapsed_minutes:.0f} dk; pozisyon kapatılıyor."))
                        if self._close_managed_position(symbol, open_orders):
                            pending_market_sell_symbols.add(symbol)

                intraday_symbols = sorted(set(scan_symbols) | {
                    symbol for symbol in held
                    if self.managed_protection.get(symbol, {}).get("strategy") == "intraday_v1"
                })
                intraday_data = self.broker.intraday_bars(intraday_symbols, days=5).get("bars", {})
                equity_account = self.broker.account()
                equity = float(equity_account.get("equity", 0) or 0)
                cash = float(equity_account.get("cash", 0) or 0)
                last_equity = float(equity_account.get("last_equity", equity) or equity)
                daily_return = (equity - last_equity) / last_equity if last_equity else 0.0
                state = read_app_state()
                peak_equity = float(state.get("peak_equity", equity) or equity)
                if "peak_equity" not in state or equity > peak_equity:
                    peak_equity = equity
                    write_app_state(peak_equity=peak_equity)
                peak_drawdown = (equity - peak_equity) / peak_equity if peak_equity else 0.0
                risk_allowed = daily_return > -0.015 and peak_drawdown > -0.05

                managed_active = set(self.managed_symbols)
                managed_exposure = sum(
                    abs(float(position.get("market_value", 0) or 0))
                    for symbol, position in held.items() if symbol in managed_active
                )
                pending_buy_unfilled = pending_buy_symbols - set(held)
                pending_managed_buys = pending_buy_unfilled & managed_active
                managed_exposure += len(pending_managed_buys) * equity * 0.07
                reserved_cash = len(pending_buy_unfilled) * equity * 0.07
                available_cash = max(0.0, cash - reserved_cash)
                reserved = set(held) | pending | pending_market_sell_symbols

                scanned = 0
                best_score = 0.0
                buy_signal_count = 0
                order_blocked = {
                    "piyasa_kapali": 0, "giris_saati": 0, "veri_eski": 0,
                    "gunluk_trend": 0, "spy_filtresi": 0, "gunluk_risk": 0,
                    "bekleyen_emir": 0, "pozisyon_limiti": 0, "stop_genis": 0,
                    "dolar_hedefi_uzak": 0, "miktar_sifir": 0
                }
                stale_count = 0
                for symbol in scan_symbols:
                    if self.stop.is_set():
                        break
                    completed, live_price, fresh = prepare_intraday_rows(
                        intraday_data.get(symbol, []), now_utc
                    )
                    daily_signal = daily_signals.get(symbol)
                    signal = analyze_intraday(completed, daily_signal)
                    if not signal:
                        stale_count += 1
                        continue
                    scanned += 1
                    best_score = max(best_score, signal["score"])
                    if signal["action"] == "BUY":
                        buy_signal_count += 1
                        if not can_enter_time:
                            order_blocked["giris_saati"] += 1
                        elif not fresh or live_price is None:
                            order_blocked["veri_eski"] += 1
                        elif not signal["daily_uptrend"]:
                            order_blocked["gunluk_trend"] += 1
                        elif not market_ok:
                            order_blocked["spy_filtresi"] += 1
                        elif not risk_allowed:
                            order_blocked["gunluk_risk"] += 1
                        elif symbol in pending_buy_symbols or symbol in reserved:
                            order_blocked["bekleyen_emir"] += 1
                        elif len(managed_active) >= MAX_MANAGED_POSITIONS:
                            order_blocked["pozisyon_limiti"] += 1
                        else:
                            levels = intraday_exit_levels(live_price, signal["atr"])
                            if not levels:
                                order_blocked["stop_genis"] += 1
                                continue
                            qty = calculate_order_qty(
                                equity, available_cash, live_price,
                                levels["stop_distance"], managed_exposure,
                                profit_target_usd=self.profit_target_usd
                            )
                            if not qty:
                                order_blocked["miktar_sifir"] += 1
                                continue
                            dollar_target = dollar_profit_limit_price(
                                live_price, qty, self.profit_target_usd
                            )
                            if not dollar_target or dollar_target["distance"] > live_price * 0.02:
                                order_blocked["dolar_hedefi_uzak"] += 1
                                continue
                            levels["target"] = dollar_target["price"]
                            # Persist ownership and intended short-term protection before POSTing.
                            # This preserves intraday management if the process restarts just after
                            # Alpaca accepts the entry. A rejected POST rolls the state back.
                            self.managed_symbols.add(symbol)
                            self.managed_protection[symbol] = {
                                "take_profit": levels["target"],
                                "stop_loss": levels["stop"],
                                "qty": qty,
                                "strategy": "intraday_v1",
                                "opened_at": now_utc.isoformat(),
                                "max_hold_minutes": 45,
                                "profit_target_usd": self.profit_target_usd,
                                "estimated_target_profit_usd": dollar_target["estimated_gross_profit_usd"],
                                "entry_price": live_price
                            }
                            self._save_management_state()
                            try:
                                result = self.broker.order(
                                    symbol, qty, "buy",
                                    take_profit=levels["target"], stop_loss=levels["stop"]
                                )
                            except Exception as order_error:
                                # A timeout can happen after Alpaca accepted the order.
                                # Keep the persisted owner/strategy metadata and reconcile it
                                # against positions/open orders on the next scan before any retry.
                                self.events.put(("log", f"ALIŞ EMRİ YANITI BELİRSİZ {symbol}: {order_error}. Yinelenen alıştan kaçınmak için Alpaca kaydı sonraki turda uzlaştırılacak."))
                                continue
                            if result.get("group_id"):
                                self.managed_groups[symbol] = str(result["group_id"])
                            self._save_management_state()
                            managed_active.add(symbol)
                            reserved.add(symbol)
                            pending.add(symbol)
                            pending_buy_symbols.add(symbol)
                            managed_exposure += qty * live_price
                            available_cash = max(0.0, available_cash - qty * live_price)
                            self.events.put((
                                "log",
                                f"ASTRATRADE PAPER KISA VADE ALIŞ {symbol} x{qty} | skor {signal['score']*100:.0f}/100 | giriş USD {live_price:.2f} | SL {levels['stop']:.2f} | TP {levels['target']:.2f} | brüt hedef yaklaşık USD {dollar_target['estimated_gross_profit_usd']:.2f} (tetik USD {self.profit_target_usd:.2f}) | 45 dk | {result.get('status')}"
                            ))
                    elif signal["action"] == "SELL" and symbol in held and symbol in self.managed_symbols:
                        meta = self.managed_protection.get(symbol, {})
                        if meta.get("strategy") == "intraday_v1" and symbol not in pending_market_sell_symbols:
                            self.events.put(("log", f"TERS SİNYAL ÇIKIŞI {symbol}: 5 dakikalık trend/VWAP zayıfladı."))
                            if self._close_managed_position(symbol, open_orders):
                                pending_market_sell_symbols.add(symbol)

                self.events.put(("status", (
                    len(symbols), scanned, equity, cash, len(held), True,
                    daily_return, len(managed_active)
                )))
                self.events.put(("log", f"Kısa vade taraması: {scanned}/{len(scan_symbols)} hisse analiz | en yüksek skor {best_score:.2f}/1,00 | BUY sinyali {buy_signal_count}."))
                self.events.put(("log", "Emir engelleri: " + ", ".join(f"{key}={value}" for key, value in order_blocked.items()) + "."))
                self.events.put(("log", f"Güncel olmayan/veri yetersiz intraday sembol: {stale_count}. Veri kaynağı: IEX 5 dakikalık mumlar."))
                self.events.put(("log", f"Piyasa: açık | SPY günlük rejim filtresi: {'OLUMLU' if market_ok else 'OLUMSUZ'} | aktif yönetilen pozisyon: {len(managed_active)}/5."))
            except Exception as exc:
                self.events.put(("log", "Kısa vade tarama hatası: " + str(exc)))
            elapsed = time.monotonic() - cycle_started
            self.stop.wait(max(1, 300 - elapsed))
        self.events.put(("log", "Bot durduruldu."))

    def run(self):
        if callable(getattr(self.broker, "intraday_bars", None)):
            return self._run_intraday()
        # Kept only for older isolated unit-test brokers; the Windows app always
        # uses Alpaca.intraday_bars and therefore the intraday production strategy.
        return self._run_legacy()

class App:
    def __init__(self, root):
        self.root=root; root.title("AstraTrade AI"); root.geometry("1180x820"); root.minsize(1000,700); root.configure(bg="#111827")
        self.events=queue.Queue(); self.stop_event=threading.Event(); self.thread=None; self.broker=None
        self.build()
        saved_key, saved_secret = load_credentials()
        if saved_key: self.key.insert(0, saved_key)
        if saved_secret: self.secret.insert(0, saved_secret)
        root.after(250,self.poll); root.protocol("WM_DELETE_WINDOW",self.close)
    def build(self):
        style=ttk.Style()
        try: style.theme_use("clam")
        except Exception: pass
        style.configure("TNotebook",background="#111827",borderwidth=0)
        style.configure("TNotebook.Tab",background="#1f2d42",foreground="#cbd5e1",padding=(16,9),font=("Segoe UI",10,"bold"))
        style.map("TNotebook.Tab",background=[("selected","#36c99a")],foreground=[("selected","#07131a")])
        style.configure("Treeview",background="#101a2a",foreground="#e5edf7",fieldbackground="#101a2a",rowheight=28,borderwidth=0)
        style.configure("Treeview.Heading",background="#25364d",foreground="#e5edf7",font=("Segoe UI",9,"bold"))
        bg="#111827"; panel="#1f2d42"; green="#36c99a"
        head=tk.Frame(self.root,bg=bg); head.pack(fill="x",padx=20,pady=18)
        tk.Label(head,text="ASTRATRADE AI",bg=bg,fg="white",font=("Segoe UI",22,"bold")).pack(side="left")
        tk.Label(head,text="US EQUITIES • PAPER ONLY • RULE-BASED",bg=bg,fg=green).pack(side="left",padx=15)
        self.connection=tk.Label(head,text="● BAĞLI DEĞİL",bg=bg,fg="#ffc36d"); self.connection.pack(side="right")
        tabs=ttk.Notebook(self.root); tabs.pack(fill="both",expand=True,padx=20,pady=(0,15)); self.tabs=tabs
        home=tk.Frame(tabs,bg=bg); settings=tk.Frame(tabs,bg=bg); history=tk.Frame(tabs,bg=bg); chart=tk.Frame(tabs,bg=bg); backtest=tk.Frame(tabs,bg=bg); portfolio=tk.Frame(tabs,bg=bg)
        tabs.add(home,text="Genel Bakış"); tabs.add(portfolio,text="Portföy"); tabs.add(chart,text="Piyasa Grafiği"); tabs.add(backtest,text="Strateji Testi"); tabs.add(history,text="Emirler"); tabs.add(settings,text="Bağlantı ve Risk"); self.settings=settings
        self.chart_page=chart
        self.metrics={}
        row=tk.Frame(home,bg=bg); row.pack(fill="x",pady=15)
        for key,title in [("equity","SANAL ÖZKAYNAK"),("cash","KULLANILABİLİR NAKİT"),("daily","GÜNLÜK K/Z"),("positions","ASTRATRADE POZİSYON")]:
            card=tk.Frame(row,bg=panel,padx=15,pady=12); card.pack(side="left",fill="x",expand=True,padx=5)
            tk.Label(card,text=title,bg=panel,fg="#9caec1").pack(anchor="w")
            value=tk.Label(card,text="—",bg=panel,fg="white",font=("Segoe UI",18,"bold")); value.pack(anchor="w"); self.metrics[key]=value
        bar=tk.Frame(home,bg=panel,padx=12,pady=12); bar.pack(fill="x",pady=10)
        tk.Label(bar,text="Otomatik sanal işlem",bg=panel,fg="white").pack(side="left")
        self.start_button=tk.Button(bar,text="BOTU BAŞLAT",command=self.start,bg=green,fg="#07131a",relief="flat",padx=18,pady=9); self.start_button.pack(side="right")
        self.stop_button=tk.Button(bar,text="DURDUR",command=self.stop_bot,bg="#a13e4a",fg="white",relief="flat",padx=18,pady=9,state="disabled"); self.stop_button.pack(side="right",padx=8)
        tk.Button(home,text="HESABI / GRAFİĞİ YENİLE",command=self.refresh_portfolio,bg="#26364d",fg="white",relief="flat").pack(anchor="e",pady=(0,5))
        self.market=tk.Label(home,text="Piyasa: — | Tarama evreni: —",bg=bg,fg="#9caec1"); self.market.pack(anchor="w")
        self.portfolio_canvas=tk.Canvas(home,bg="#0b1220",height=190,highlightthickness=0); self.portfolio_canvas.pack(fill="x",pady=8)
        tk.Label(home,text="BOT FAALİYETLERİ",bg=bg,fg="white").pack(anchor="w",pady=(4,3))
        self.log=tk.Text(home,bg="#090f1a",fg="#dbe5ef",font=("Consolas",9),relief="flat",height=8); self.log.pack(fill="both",expand=True)
        box=tk.Frame(settings,bg=panel,padx=22,pady=22); box.pack(fill="x",padx=18,pady=18)
        tk.Label(box,text="Alpaca Paper Trading",bg=panel,fg="white",font=("Segoe UI",15,"bold")).pack(anchor="w")
        tk.Label(box,text="YALNIZCA Paper API anahtarlarını gir. Gerçek hesap anahtarlarını kullanma.",bg=panel,fg="#ffc36d").pack(anchor="w",pady=12)
        tk.Label(box,text="Paper API Key",bg=panel,fg="white").pack(anchor="w"); self.key=tk.Entry(box,font=("Consolas",10)); self.key.pack(fill="x",pady=5)
        tk.Label(box,text="Paper Secret Key",bg=panel,fg="white").pack(anchor="w"); self.secret=tk.Entry(box,show="*",font=("Consolas",10)); self.secret.pack(fill="x",pady=5)
        tk.Label(box,text="Pozisyon başına otomatik kâr hedefi (USD)",bg=panel,fg="white").pack(anchor="w",pady=(8,0))
        self.profit_target_entry=tk.Entry(box,font=("Consolas",10),width=14); self.profit_target_entry.insert(0,"20"); self.profit_target_entry.pack(anchor="w",pady=5)
        tk.Label(box,text="Botun açtığı her yeni pozisyon, bu tutarda gerçekleşmemiş dolar kârına ulaştığında otomatik kapatılır. Emirde küçük bir fiyat-kayması payı bulunur.",bg=panel,fg="#9caec1",wraplength=850,justify="left").pack(anchor="w",pady=(0,6))
        self.test_button=tk.Button(box,text="BAĞLANTIYI TEST ET VE GÜVENLİ KAYDET",command=self.test_connection,bg=green,fg="#07131a",relief="flat",padx=15,pady=9); self.test_button.pack(anchor="w",pady=12)
        tk.Label(box,text="Başarılı bağlantıdan sonra anahtarlar Windows hesabına bağlı DPAPI şifrelemesiyle bu bilgisayarda saklanır ve sonraki açılışta otomatik yüklenir.",bg=panel,fg="#9caec1",wraplength=850,justify="left").pack(anchor="w",pady=4)
        self.result=tk.Label(settings,text="Henüz bağlantı test edilmedi.",bg=bg,fg="#9caec1"); self.result.pack(anchor="w",padx=20)
        tk.Label(settings,text="STRATEJİ: Kısa vadeli gün içi işlem. 5 dakikalık EMA9/21, VWAP, MACD, Wilder RSI, 6 mumluk (30 dk) kırılım ve hacim; 15 dakikalık EMA9/21 yön teyidi; günlük SMA200 ve SPY piyasa rejimi filtresi. İzleme listesi günlük ortalama en az 20 milyon USD dolar hacmine sahip en likit 300 adi hisseden oluşturulur. Sistem makine öğrenmesi değildir; kâr garantisi vermez.",bg=bg,fg="#9caec1",wraplength=850,justify="left").pack(anchor="w",padx=20,pady=(15,5))
        tk.Label(settings,text="Yeni pozisyonlar ayarlardaki dolar kâr hedefinde (varsayılan USD 20) kapatılır; hedef/stop oranı en az 1,6 korunacak şekilde adet hesaplanır. Hedef fiyatın girişten %2’den uzakta kalması gerekiyorsa işlem alınmaz. Volatiliteye göre stop %0,50-%1,00 aralığındadır. Azami pozisyon süresi 45 dakika, gün sonu kapatma saati 15:45 ET; yeni alış penceresi 09:45-15:00 ET. İşlem başına risk bütçesi %0,25, tek pozisyon %7, toplam yönetilen maruziyet %30. Günlük %1,5 zarar ve zirveden %5 düşüşte yeni alış kilitlenir. Yalnızca nakit kullanılır. Eski/manuel pozisyonlara ve önceden açılmış ADSK gibi eski strateji pozisyonlarına otomatik müdahale edilmez.",bg=bg,fg="#ffc36d",wraplength=850,justify="left").pack(anchor="w",padx=20,pady=5)
        tk.Label(settings,text="Piyasa verisi ücretsiz IEX akışıdır; tüm ABD borsalarının işlem hacmini kapsamaz. 2.518 sembollük evrenden günlük likiditeye göre en üst 300 hisse seçilir ve her 5 dakikada tamamı yeniden taranır. Veri indirme zaman aşımında güvenli GET tekrarları ve büyük grupları küçültme uygulanır; sınırlı sayıda verisi alınamayan sembol tarama dışına çıkarılır, veri kaybı yüksekse bot işlem yapmadan durur. Sadece aktif NYSE/NASDAQ/NYSE American adi hisseleri kullanılır; ETF ve diğer fonlar dışarıda bırakılır. Önceden açılmış pozisyonların koruma emirleri değiştirilmez. Gerçek para hesabı desteklenmez.",bg=bg,fg="#9caec1",wraplength=850,justify="left").pack(anchor="w",padx=20,pady=5)
        # Midas benzeri mum grafiği: fiyat, EMA20/50 ve hacim.
        chart_controls=tk.Frame(chart,bg=bg,padx=15,pady=12); chart_controls.pack(fill="x")
        tk.Label(chart_controls,text="Hisse kodu",bg=bg,fg="white").pack(side="left")
        self.chart_symbol=tk.Entry(chart_controls,width=12,font=("Segoe UI",11)); self.chart_symbol.insert(0,"AAPL"); self.chart_symbol.pack(side="left",padx=6)
        tk.Label(chart_controls,text="Zaman aralığı",bg=bg,fg="white").pack(side="left",padx=(12,4))
        self.chart_tf=ttk.Combobox(chart_controls,state="readonly",width=12,values=["Günlük","Saatlik","15 Dakika","5 Dakika"]); self.chart_tf.set("Günlük"); self.chart_tf.pack(side="left")
        tk.Button(chart_controls,text="GRAFİĞİ GETİR",command=self.load_chart,bg=green,fg="#07131a",relief="flat",padx=14,pady=7).pack(side="left",padx=10)
        self.chart_status=tk.Label(chart,text="Grafik için Alpaca Paper hesabına bağlan ve hisse kodunu getir.",bg=bg,fg="#9caec1"); self.chart_status.pack(anchor="w",padx=18)
        self.price_canvas=tk.Canvas(chart,bg="#0b1220",highlightthickness=0); self.price_canvas.pack(fill="both",expand=True,padx=12,pady=12)
        self.price_canvas.bind("<Configure>",lambda e:self.draw_price_chart())
        self.chart_rows=[]; self.chart_name=""; self.chart_timeframe="1Day"
        posbar=tk.Frame(portfolio,bg=bg,padx=15,pady=12); posbar.pack(fill="x")
        tk.Label(posbar,text="Hesaptaki bütün açık pozisyonlar • gerçekleşmemiş K/Z",bg=bg,fg="#9caec1").pack(side="left")
        tk.Button(posbar,text="POZİSYONLARI YENİLE",command=self.refresh_positions,bg=green,fg="#07131a",relief="flat",padx=14,pady=7).pack(side="right")
        self.position_tree=ttk.Treeview(portfolio,columns=("symbol","qty","avg","last","value","pnl","pnlpc"),show="headings")
        for col,title,width in [("symbol","HİSSE",90),("qty","ADET",90),("avg","ORT. MALİYET",130),("last","GÜNCEL FİYAT",130),("value","PİYASA DEĞERİ",150),("pnl","GERÇEKLEŞMEMİŞ K/Z",170),("pnlpc","K/Z %",100)]:
            self.position_tree.heading(col,text=title); self.position_tree.column(col,width=width,anchor="center")
        self.position_tree.tag_configure("profit",foreground="#36c99a")
        self.position_tree.tag_configure("loss",foreground="#ef6b73")
        self.position_tree.pack(fill="both",expand=True,padx=15,pady=15)
        btbar=tk.Frame(backtest,bg=bg,padx=18,pady=18); btbar.pack(fill="x")
        tk.Label(btbar,text="ABD hisse kodu",bg=bg,fg="white").pack(side="left")
        self.backtest_symbol=tk.Entry(btbar,width=12,font=("Segoe UI",11)); self.backtest_symbol.insert(0,"AAPL"); self.backtest_symbol.pack(side="left",padx=8)
        tk.Button(btbar,text="30 GÜNLÜK 5 DK TESTİ ÇALIŞTIR",command=self.run_backtest,bg=green,fg="#07131a",relief="flat",padx=16,pady=8).pack(side="left",padx=8)
        tk.Label(backtest,text="Bu test yaklaşık 30 günlük 5 dakikalık IEX mumları ve %0,10 giriş/çıkış kayması varsayımıyla çalışır. Tek hisse simülasyonudur; tüm evreni, emir kuyruğunu, spreadi ve gerçek kısmi dolumları tam modellemez. Geçmiş performans geleceği garanti etmez.",bg=bg,fg="#9caec1",wraplength=920,justify="left").pack(anchor="w",padx=18,pady=5)
        self.backtest_output=tk.Text(backtest,bg="#09111d",fg="#dbe5ef",font=("Consolas",11),relief="flat",height=18)
        self.backtest_output.pack(fill="both",expand=True,padx=18,pady=16)
        self.tree=ttk.Treeview(history,columns=("time","symbol","side","qty","status"),show="headings")
        for col,title,width in [("time","ZAMAN",180),("symbol","HİSSE",100),("side","YÖN",100),("qty","ADET",100),("status","DURUM",180)]:
            self.tree.heading(col,text=title); self.tree.column(col,width=width,anchor="center")
        self.tree.pack(fill="both",expand=True,padx=15,pady=15)
        tk.Button(history,text="İŞLEMLERİ YENİLE",command=self.refresh_history).pack(anchor="e",padx=15,pady=10)
    def refresh_positions(self):
        if not self.broker:
            messagebox.showinfo("Bağlantı","Önce Alpaca Paper bağlantısını test et.")
            return
        def work():
            try:
                self.events.put(("positions",self.broker.positions()))
            except Exception as exc:
                self.events.put(("log","Pozisyonlar alınamadı: "+str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def run_backtest(self):
        if not self.broker:
            messagebox.showinfo("Bağlantı","Önce Alpaca Paper bağlantısına bağlan.")
            return
        symbol=self.backtest_symbol.get().strip().upper()
        if not symbol or not symbol.isalnum():
            messagebox.showwarning("Hisse kodu","Geçerli bir hisse kodu gir.")
            return
        try:
            profit_target = float(self.profit_target_entry.get().strip().replace(",", "."))
            if not math.isfinite(profit_target) or not 1.0 <= profit_target <= 10000.0:
                raise ValueError
        except (TypeError, ValueError):
            messagebox.showwarning("Kâr hedefi", "Test için kâr hedefini 1 ile 10.000 USD arasında gir.")
            self.tabs.select(self.settings)
            return
        self.backtest_output.delete("1.0","end")
        self.backtest_output.insert("end",f"{symbol} için yaklaşık 30 günlük 5 dakikalık test verisi indiriliyor (hedef USD {profit_target:.2f})...\n")
        def work():
            try:
                intraday=self.broker.history(symbol,"5Min",35,adjustment="raw")
                daily=self.broker.history(symbol,"1Day",400,adjustment="split")
                spy_daily=self.broker.history("SPY","1Day",400,adjustment="split")
                clock=self.broker.clock()
                result=backtest_intraday_strategy(intraday,daily,spy_daily,now=clock.get("timestamp"),profit_target_usd=profit_target)
                self.events.put(("backtest",(symbol,result)))
            except Exception as exc:
                self.events.put(("backtest_error",str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def refresh_portfolio(self):
        if not self.broker:
            return
        def work():
            try:
                data=self.broker.portfolio_history()
                self.events.put(("portfolio",data))
            except Exception as exc:
                self.events.put(("log","Portföy grafiği alınamadı: "+str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def draw_portfolio_history(self,data):
        canvas=self.portfolio_canvas
        canvas.delete("all")
        values=[float(x) for x in data.get("equity",[]) if x not in (None,"NaN") and math.isfinite(float(x))]
        if len(values)<2:
            canvas.create_text(18,20,anchor="nw",fill="#9caec1",text="Portföy performans grafiği için henüz yeterli veri yok.")
            return
        w=max(canvas.winfo_width(),600); h=max(canvas.winfo_height(),170)
        left,right,top,bottom=72,w-24,18,h-28
        low,high=min(values),max(values)
        if high==low: high=low+1
        canvas.create_text(left,8,anchor="nw",fill="#9caec1",text="PORTFÖY ÖZKAYNAK GELİŞİMİ • SON 1 AY")
        for k in range(4):
            y=top+(bottom-top)*k/3
            val=high-(high-low)*k/3
            canvas.create_line(left,y,right,y,fill="#243247")
            canvas.create_text(left-8,y,anchor="e",fill="#8294aa",text=f"${val:,.0f}",font=("Segoe UI",8))
        pts=[]
        for i,val in enumerate(values):
            x=left+(right-left)*i/max(1,len(values)-1)
            y=bottom-(val-low)/(high-low)*(bottom-top)
            pts.extend([x,y])
        if len(pts)>=4: canvas.create_line(*pts,fill="#36c99a",width=2,smooth=True)
        canvas.create_text(right,bottom+6,anchor="ne",fill="#9caec1",text=f"Son: USD {values[-1]:,.2f}",font=("Segoe UI",9,"bold"))

    def load_chart(self):
        if not self.broker:
            messagebox.showinfo("Bağlantı","Önce Alpaca Paper bağlantısını test et.")
            return
        symbol=self.chart_symbol.get().strip().upper()
        if not symbol or not symbol.isalnum():
            messagebox.showwarning("Hisse kodu","Geçerli bir hisse kodu gir.")
            return
        choice=self.chart_tf.get()
        mapping={"Günlük":("1Day",365),"Saatlik":("1Hour",30),"15 Dakika":("15Min",7),"5 Dakika":("5Min",2)}
        timeframe,days=mapping.get(choice,("1Day",365))
        self.chart_status.configure(text=f"{symbol} verisi yükleniyor...")
        def work():
            try:
                rows=self.broker.history(symbol,timeframe,days,adjustment="split")
                if not rows: raise RuntimeError("Bu sembol için IEX akışında grafik verisi bulunamadı.")
                self.events.put(("chart",(symbol,rows,timeframe)))
            except Exception as exc:
                self.events.put(("log","Grafik hatası: "+str(exc)))
                self.events.put(("chart_error",str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def draw_price_chart(self):
        if not hasattr(self,"price_canvas"):
            return
        canvas=self.price_canvas
        canvas.delete("all")
        rows=self.chart_rows[-120:]
        if len(rows)<2:
            canvas.create_text(22,22,anchor="nw",fill="#9caec1",text="Grafik verisi bekleniyor. Hisse kodu girip GRAFİĞİ GETİR'e bas.")
            return
        w=max(canvas.winfo_width(),620); h=max(canvas.winfo_height(),360)
        left,right,top,bottom=68,w-22,42,h-62
        volume_top=bottom-(bottom-top)*0.20
        highs=[float(x["h"]) for x in rows]; lows=[float(x["l"]) for x in rows]
        opens=[float(x["o"]) for x in rows]; closes=[float(x["c"]) for x in rows]
        volumes=[float(x.get("v",0) or 0) for x in rows]
        upper=max(highs); lower=min(lows); pad=(upper-lower)*0.07 or max(upper*0.01,0.1)
        upper+=pad; lower-=pad
        def py(v): return top+(upper-v)/(upper-lower)*(volume_top-top)
        canvas.create_text(left,12,anchor="nw",fill="#e6edf7",text=f"{self.chart_name}  ${closes[-1]:,.2f}  |  Mum + EMA20 / EMA50 + Hacim",font=("Segoe UI",11,"bold"))
        for k in range(5):
            val=upper-(upper-lower)*k/4
            y=py(val)
            canvas.create_line(left,y,right,y,fill="#243247")
            canvas.create_text(left-8,y,anchor="e",fill="#8ea0b6",text=f"{val:.2f}",font=("Segoe UI",8))
        max_vol=max(volumes) or 1
        n=len(rows); step=(right-left)/n; candle=max(2,min(9,step*0.62))
        ema20=ema_series(closes,20); ema50=ema_series(closes,50)
        for i,row in enumerate(rows):
            x=left+(i+0.5)*step
            color="#31c48d" if closes[i]>=opens[i] else "#ef6b73"
            canvas.create_line(x,py(highs[i]),x,py(lows[i]),fill=color,width=1)
            y1=py(max(opens[i],closes[i])); y2=py(min(opens[i],closes[i]))
            if y2-y1<1: y2=y1+1
            canvas.create_rectangle(x-candle/2,y1,x+candle/2,y2,fill=color,outline=color)
            vy=bottom-(volumes[i]/max_vol)*(bottom-volume_top)
            canvas.create_rectangle(x-candle/2,vy,x+candle/2,bottom,fill="#263d55",outline="")
        for vals,color in ((ema20,"#f6c85f"),(ema50,"#7aa2f7")):
            points=[]
            for i,val in enumerate(vals):
                x=left+(i+0.5)*step
                points.extend([x,py(val)])
            if len(points)>=4: canvas.create_line(*points,fill=color,width=1.5)
        canvas.create_text(left,volume_top+4,anchor="nw",fill="#8ea0b6",text="HACİM",font=("Segoe UI",8))
        canvas.create_text(left,bottom+8,anchor="nw",fill="#8ea0b6",text=str(rows[0].get("t",""))[:10],font=("Segoe UI",8))
        canvas.create_text(right,bottom+8,anchor="ne",fill="#8ea0b6",text=str(rows[-1].get("t",""))[:10],font=("Segoe UI",8))
        canvas.create_text(right,top+3,anchor="ne",fill="#f6c85f",text="EMA20",font=("Segoe UI",8))
        canvas.create_text(right,top+17,anchor="ne",fill="#7aa2f7",text="EMA50",font=("Segoe UI",8))

    def add_log(self,msg):
        line = logline(msg)
        self.log.insert("end", line + "\n")
        try:
            line_count = int(self.log.index("end-1c").split(".")[0])
            if line_count > 3000:
                self.log.delete("1.0", f"{line_count-2500}.0")
        except Exception:
            pass
        self.log.see("end")
    def test_connection(self):
        key=self.key.get().strip(); secret=self.secret.get().strip()
        if not key or not secret: messagebox.showwarning("Eksik bilgi","Paper API Key ve Secret Key gir."); return
        self.test_button.configure(state="disabled")
        def work():
            try:
                broker=Alpaca(key,secret); account=broker.account(); saved=False
                try:
                    save_credentials(key,secret); saved=True
                except Exception as save_error:
                    self.events.put(("log","API bilgileri güvenli kaydedilemedi: "+str(save_error)))
                self.events.put(("connected",(broker,account,saved)))
            except Exception as exc: self.events.put(("error",str(exc)))
        threading.Thread(target=work,daemon=True).start()
    def start(self):
        if not self.key.get().strip() or not self.secret.get().strip():
            self.tabs.select(self.settings); messagebox.showwarning("Bağlantı","Önce Paper API bilgilerini gir."); return
        if self.thread and self.thread.is_alive(): return
        try:
            profit_target = float(self.profit_target_entry.get().strip().replace(",", "."))
            if not math.isfinite(profit_target) or not 1.0 <= profit_target <= 10000.0:
                raise ValueError
        except (TypeError, ValueError):
            messagebox.showwarning("Kâr hedefi", "Pozisyon başına kâr hedefini 1 ile 10.000 USD arasında gir.")
            self.tabs.select(self.settings)
            return
        self.broker=Alpaca(self.key.get().strip(),self.secret.get().strip())
        self.stop_event.clear()
        bot=Bot(self.broker,self.events,self.stop_event,profit_target_usd=profit_target); self.thread=threading.Thread(target=self.run,args=(bot,),daemon=True); self.thread.start()
        self.start_button.configure(state="disabled"); self.stop_button.configure(state="normal"); self.connection.configure(text="● BOT ÇALIŞIYOR",fg="#36c99a")
    def run(self,bot):
        try: bot.run()
        except Exception as exc: self.events.put(("fatal",str(exc)))
    def stop_bot(self):
        self.stop_event.set(); self.stop_button.configure(state="disabled"); self.add_log("Durdurma isteği gönderildi.")
    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=="log": self.add_log(value)
                elif kind=="portfolio": self.draw_portfolio_history(value)
                elif kind=="backtest":
                    symbol,result=value
                    pf="∞" if math.isinf(result["profit_factor"]) else f"{result['profit_factor']:.2f}"
                    report=(f"SEMBOL: {symbol} | DÖNEM: yaklaşık 30 gün / 5 dakika | BAŞLANGIÇ: USD {result['initial']:,.2f}\n"
                        f"TEST SONU ÖZKAYNAK: USD {result['final']:,.2f} | NET GETİRİ: {result['return_pct']:.2f}%\n"
                        f"İŞLEM SAYISI: {result['trades']} | KAZANMA ORANI: {result['win_rate']:.1f}%\n"
                        f"PROFIT FACTOR: {pf} | MAKS. DÜŞÜŞ: {result['max_drawdown']:.2f}%\n"
                        f"AYNI HİSSE AL-TUT: {result['benchmark']:.2f}% | SPY AL-TUT: {result['spy_benchmark']:.2f}%\n"
                        f"İŞLEM BAŞINA ORT. K/Z: USD {result['avg_trade']:.2f}\n\n"
                        "Not: Tek hisse üzerinde kısa dönem simülasyondur. Günlük risk kilidi ve gerçek emir kuyruğu/kısmi dolumlar tam modellenmez. %0,10 giriş/çıkış kayması ve aynı mumda stopun hedefe öncelikli olduğu varsayılmıştır; IEX hacmi tüm piyasa hacmini temsil etmez. Canlı sonuç garantisi değildir.\n")
                    self.backtest_output.delete("1.0","end"); self.backtest_output.insert("end",report)
                elif kind=="backtest_error":
                    self.backtest_output.insert("end","TEST HATASI: "+value+"\n")
                elif kind=="positions":
                    for item in self.position_tree.get_children(): self.position_tree.delete(item)
                    for pos in value:
                        pnl=float(pos.get("unrealized_pl",0) or 0)
                        vals=(pos.get("symbol",""),pos.get("qty",""),f"{float(pos.get('avg_entry_price',0) or 0):.2f}",f"{float(pos.get('current_price',0) or 0):.2f}",f"USD {float(pos.get('market_value',0) or 0):,.2f}",f"USD {pnl:+,.2f}",f"{float(pos.get('unrealized_plpc',0) or 0)*100:+.2f}%")
                        self.position_tree.insert("","end",values=vals,tags=("profit" if pnl>=0 else "loss",))
                elif kind=="chart_error": self.chart_status.configure(text="Grafik yüklenemedi: "+value)
                elif kind=="chart":
                    self.chart_name,self.chart_rows,self.chart_timeframe=value
                    self.chart_status.configure(text=f"{self.chart_name} | {len(self.chart_rows)} mum | {self.chart_timeframe} | IEX veri akışı")
                    self.draw_price_chart()
                elif kind=="connected":
                    self.broker,account,saved=value; self.connection.configure(text="● ALPACA PAPER BAĞLI",fg="#36c99a")
                    self.result.configure(text=f"Bağlantı başarılı | Hesap: {account.get('status')} | Sanal bakiye: USD {float(account.get('equity',0)):,.2f} | API: {'GÜVENLİ KAYDEDİLDİ' if saved else 'KAYDEDİLEMEDİ'}",fg="#36c99a" if saved else "#ffc36d")
                    self.metrics["equity"].configure(text=f"USD {float(account.get('equity',0)):,.2f}"); self.metrics["cash"].configure(text=f"USD {float(account.get('cash',0)):,.2f}"); last=float(account.get("last_equity",account.get("equity",0)) or 0); eq=float(account.get("equity",0) or 0); self.metrics["daily"].configure(text=f"{((eq-last)/last*100) if last else 0:+.2f}%")
                    self.test_button.configure(state="normal"); self.add_log("Alpaca Paper bağlantısı doğrulandı; API anahtarları Windows üzerinde güvenli kaydedildi." if saved else "Bağlantı doğrulandı ancak API anahtarları güvenli kaydedilemedi."); self.refresh_portfolio(); self.refresh_positions()
                elif kind=="error":
                    self.result.configure(text="Bağlantı hatası: "+value,fg="#ff7373"); self.test_button.configure(state="normal"); messagebox.showerror("Alpaca",value)
                elif kind=="decision":
                    sym,action,score,price,rsi=value; self.add_log(f"{sym} | {action} | skor {score:.2f} | USD {price:.2f} | RSI {rsi:.1f}")
                elif kind=="status":
                    n,count,equity,cash,positions,market,daily,managed=value
                    self.metrics["equity"].configure(text=f"USD {equity:,.2f}"); self.metrics["cash"].configure(text=f"USD {cash:,.2f}"); self.metrics["positions"].configure(text=f"{managed}/5")
                    self.metrics["daily"].configure(text=f"{daily*100:+.2f}%")
                    self.refresh_positions()
                    self.market.configure(text=f"Piyasa: {'Açık' if market else 'Kapalı'} | Hisse evreni: {n} | Bu tur: {count}")
                elif kind=="history":
                    for item in self.tree.get_children(): self.tree.delete(item)
                    for order in value:self.tree.insert("","end",values=(str(order.get("submitted_at",""))[:16],order.get("symbol",""),order.get("side","").upper(),order.get("qty",""),order.get("status","")))
                elif kind=="fatal":
                    self.add_log("HATA: "+value); self.connection.configure(text="● HATA",fg="#ff7373"); self.start_button.configure(state="normal"); self.stop_button.configure(state="disabled"); messagebox.showerror("Bot hatası",value)
        except queue.Empty: pass
        if self.thread and not self.thread.is_alive():
            self.start_button.configure(state="normal"); self.stop_button.configure(state="disabled")
        self.root.after(250,self.poll)
    def refresh_history(self):
        if not self.broker: messagebox.showinfo("Bilgi","Önce hesaba bağlan."); return
        def work():
            try:self.events.put(("history",self.broker.call(PAPER,"/v2/orders",{"status":"all","limit":100,"direction":"desc"})))
            except Exception as exc:self.events.put(("log","İşlem geçmişi hatası: "+str(exc)))
        threading.Thread(target=work,daemon=True).start()
    def close(self):
        if self.thread and self.thread.is_alive() and not messagebox.askyesno("Bot açık","Kapatınca bot duracak. Kapatılsın mı?"): return
        self.stop_event.set(); self.root.destroy()

if __name__=="__main__":
    root=tk.Tk(); App(root); root.mainloop()
