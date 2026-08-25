# bot.py
import os
import re
import json
import time
import requests
import math

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter
from matplotlib.patches import Patch
from io import BytesIO
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ChatMemberHandler, filters, ContextTypes
import asyncio
from web3 import Web3


# ================= CONFIG =================

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
ETHERSCAN_APIKEY = os.environ.get("ETHERSCAN_APIKEY", "").strip() or os.environ.get("BASESCAN_API_KEY", "").strip()

# Alchemy RPC (default Scaffold-ETH 2 key) with Base mainnet fallback
ALCHEMY_RPC_URL = os.environ.get("RPC_URL", "").strip() or "https://base-mainnet.g.alchemy.com/v2/8GVG8WjDs-sGFRr6Rm839"
BASE_FALLBACK_RPC_URL = "https://mainnet.base.org"
BASE_RPC_URL = ALCHEMY_RPC_URL  # primary

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/"

GROK_WALLET_URL = "https://thegrokwallet.com/"
UA_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DebtReliefBot/1.0)"}

GROK_WALLET = "0xb1058c959987e3513600eb5b4fd82aeee2a0e4f9"
DRB_TOKEN = "0x3ec2156d4c0a9cbdab4a016633b7bcf6a8d68ea2"
WETH_TOKEN = "0x4200000000000000000000000000000000000006"
USDC_TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# Claim fees config
CLAIM_CONTRACT = "0x375c15db32d28cecdcab5c03ab889bf15cbd2c5e"
CLAIM_RECIPIENT = "0x3ec2156D4c0A9CBdAB4a016633b7BcF6a8d68Ea2"
CLAIM_PRIVATE_KEY = os.environ.get("CLAIM_PRIVATE_KEY", "").strip().removeprefix("0x")

CLAIM_ABI = [
    {
        "name": "claimRewards",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "token", "type": "address"}],
        "outputs": [],
    },
    {
        "name": "Transfer",
        "type": "event",
        "inputs": [
            {"name": "from", "type": "address", "indexed": True},
            {"name": "to",   "type": "address", "indexed": True},
            {"name": "value","type": "uint256",  "indexed": False},
        ],
    },
]

DRB_COLOR = "#0a0b0b"
WETH_COLOR = "#6c23e0"
ETH_COLOR = "#4a1a9e"   # lila más oscuro que WETH
USDC_COLOR = "#55aaff"  # azul más claro para diferenciarse del lila

# Save the starfield background as this file
GROK_BG_PATH = "assets/grok_wallet_bg.png"

# Grok web card size
CARD_W = 896
CARD_H = 658

# ---- Buy alerts (ported from the CLAWD bot) ----
ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", "-1002614749825"))  # DRB group (always posts here, even without saved state)
USDT_TOKEN = "0xd9aaEC86B65D86f6A7B5B1b0c42FFA531710b6CA"
BURN_ADDRESS = "0x000000000000000000000000000000000000dEaD"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
CHAINLINK_ETH_USD_FEED = "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70"  # ETH/USD on Base
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

ASSET_BUY = os.environ.get("ASSET_BUY", "assets/DRB_buy.png")
BUY_EMOJI = os.environ.get("BUY_EMOJI", "💸")
MAX_EMOJIS = 100

WATCH_POLL_SEC = max(10, int(os.environ.get("WATCH_POLL_SEC", "60")))
MAX_EVENT_AGE_SEC = int(os.environ.get("MAX_EVENT_AGE_SEC", "1800"))
WATCH_OVERLAP_BLOCKS = int(os.environ.get("WATCH_OVERLAP_BLOCKS", "8"))
WATCH_MAX_SEEN_EVENTS = int(os.environ.get("WATCH_MAX_SEEN_EVENTS", "4000"))
WATCH_CONFIRMATIONS = int(os.environ.get("WATCH_CONFIRMATIONS", "0"))
WATCH_LOG_CHUNK = int(os.environ.get("WATCH_LOG_CHUNK", "2000"))
BUY_RECEIPT_PREFILTER_PCT = float(os.environ.get("BUY_RECEIPT_PREFILTER_PCT", "0.10"))
DEFAULT_MIN_BUY_USD = float(os.environ.get("DEFAULT_MIN_BUY_USD", "10000"))   # used when no saved state exists
DEFAULT_EMOJI_USD = float(os.environ.get("DEFAULT_EMOJI_USD", "1000"))  # $1000 per emoji -> $10k = 40 emojis, 100 emojis at $25k+

DATA_PATH = os.environ.get("DATA_PATH") or ("/data" if os.path.isdir("/data") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
STATE_PATH = os.environ.get("STATE_PATH", os.path.join(DATA_PATH, "watch_state.json"))


# ================= HELPERS =================

def fmt_usd(x: float) -> str:
    return f"${x:,.0f}"


def fmt_compact_b(n: float) -> str:
    return f"{n / 1_000_000_000:.2f}B"


def _rpc_call(method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    # Try Alchemy first, then fallback to Base public RPC
    for url in [ALCHEMY_RPC_URL, BASE_FALLBACK_RPC_URL]:
        try:
            r = requests.post(url, json=payload, headers=UA_HEADERS, timeout=20)
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                raise RuntimeError(str(j["error"]))
            return j["result"]
        except Exception:
            if url == BASE_FALLBACK_RPC_URL:
                raise
            continue


def _pad32_hex_address(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


def _eth_call(to_addr: str, data: str) -> str:
    return _rpc_call("eth_call", [{"to": to_addr, "data": data}, "latest"])


# Known token decimals; unknown tokens fall back to one eth_call cached for the process lifetime
_DECIMALS_CACHE = {
    DRB_TOKEN.lower(): 18,
    WETH_TOKEN.lower(): 18,
    USDC_TOKEN.lower(): 6,
}


def erc20_decimals(token: str) -> int:
    key = token.lower()
    dec = _DECIMALS_CACHE.get(key)
    if dec is None:
        dec = int(_eth_call(token, "0x313ce567"), 16)
        _DECIMALS_CACHE[key] = dec
    return dec


def erc20_balance_of(token: str, wallet: str) -> int:
    data = "0x70a08231" + _pad32_hex_address(wallet)
    return int(_eth_call(token, data), 16)


# Price cache (5 min) — shared by /claim estimate, /buys and balances
_PRICE_CACHE = {}
_PRICE_CACHE_TTL = 300


def _dexscreener_get(token: str):
    """GET DexScreener with retries (2 attempts, short backoff)."""
    last = None
    for attempt in range(2):
        try:
            r = requests.get(DEXSCREENER_TOKEN_URL + token, headers=UA_HEADERS, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1.0 * (attempt + 1))
    raise last


def _geckoterminal_price_fdv(token: str):
    """Fallback price source: GeckoTerminal (no API key). Returns (price, fdv) or (None, None)."""
    try:
        r = requests.get(
            f"https://api.geckoterminal.com/api/v2/networks/base/tokens/{token.lower()}",
            headers={"accept": "application/json", **UA_HEADERS}, timeout=15)
        r.raise_for_status()
        attrs = ((r.json().get("data") or {}).get("attributes") or {})
        price = float(attrs.get("price_usd") or 0) or None
        fdv = float(attrs.get("fdv_usd") or 0) or None
        return price, fdv
    except Exception:
        return None, None


def fetch_price_usd(token: str) -> float:
    now = time.time()
    c = _PRICE_CACHE.get(token.lower())
    if c and (now - c["ts"]) < _PRICE_CACHE_TTL:
        return c["price"]

    try:
        j = _dexscreener_get(token)
    except Exception:
        # DexScreener down/slow: try GeckoTerminal, then last known price (stale)
        gp, _ = _geckoterminal_price_fdv(token)
        if gp:
            _PRICE_CACHE[token.lower()] = {"ts": now, "price": gp}
            return gp
        if c:
            return c["price"]
        raise
    pairs = j.get("pairs") or []

    best_price = None
    best_liq = -1.0
    for p in pairs:
        try:
            price = float(p.get("priceUsd") or 0)
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
        except Exception:
            continue
        if price > 0 and liq > best_liq:
            best_price = price
            best_liq = liq

    if best_price is None:
        raise RuntimeError("No priceUsd found")

    _PRICE_CACHE[token.lower()] = {"ts": now, "price": best_price}
    return best_price


# ================= DRB STATS HELPERS =================

# 15-minute cache for /grok stats data
_GROK_STATS_CACHE = {"ts": 0, "data": None}
_GROK_STATS_CACHE_TTL = 900  # 15 minutes

# Holder count cache (60 min)
_HOLDERS_CACHE = {}


def _short_addr_dots(a: str, left: int = 5, right: int = 5) -> str:
    if not a:
        return ""
    a = a.strip()
    if len(a) <= (left + right):
        return a
    return f"{a[:left]}...{a[-right:]}"


def _fmt_price(price: float) -> str:
    s = f"{price:.10f}".rstrip("0").rstrip(".")
    return f"${s}"


def _fmt_int_usd(x: float) -> str:
    return f"${int(round(x)):,}"


def _fmt_big(n: float) -> str:
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.2f}K"
    return f"{n:.0f}"


def fetch_price_and_fdv(token_addr: str):
    """Fetch price and FDV (market cap) from DexScreener (with retry; (None, None) on failure)."""
    try:
        j = _dexscreener_get(token_addr)
    except Exception:
        return _geckoterminal_price_fdv(token_addr)
    pairs = j.get("pairs") or []

    best_price = None
    best_fdv = None
    best_liq = -1.0
    for p in pairs:
        try:
            price = float(p.get("priceUsd") or 0)
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
        except Exception:
            continue
        if price > 0 and liq > best_liq:
            best_price = price
            best_fdv = float(p.get("fdv") or 0)
            best_liq = liq

    return best_price, best_fdv


def basescan_token_holder_count(token_addr: str):
    """
    Return current holder count for an ERC-20 token on Base.
    1) Try Etherscan v2 tokenholdercount
    2) Fallback: scrape basescan.org/token/<addr>
    Cache TTL: 60 minutes (in-memory).
    """
    try:
        token = (token_addr or "").strip().lower()
        if not token or not token.startswith("0x"):
            return None

        now = time.time()
        c = _HOLDERS_CACHE.get(token)

        if c and (now - float(c.get("ts") or 0.0)) <= 3600:
            v = int(c.get("count") or 0)
            return v if v > 0 else None

        # 1) Etherscan v2
        try:
            params = {
                "chainid": 8453,
                "module": "token",
                "action": "tokenholdercount",
                "contractaddress": token,
            }
            if ETHERSCAN_APIKEY:
                params["apikey"] = ETHERSCAN_APIKEY

            r = requests.get("https://api.etherscan.io/v2/api", params=params, timeout=20)
            r.raise_for_status()
            j = r.json() if r.content else {}

            if str(j.get("status") or "") == "1":
                res = j.get("result")
                n = int(str(res)) if res is not None else 0
                if n > 0:
                    _HOLDERS_CACHE[token] = {"ts": now, "count": n}
                    return n
        except Exception:
            pass

        # 2) Fallback: scrape Basescan token page
        try:
            url = f"https://basescan.org/token/{token}"
            r = requests.get(
                url,
                timeout=20,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            r.raise_for_status()
            html = r.text or ""

            html = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
            html = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", html)
            text = re.sub(r"(?s)<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()

            start_idx = text.lower().find("overview")
            search_space = text[start_idx:] if start_idx != -1 else text

            m = re.search(r"\bHolders\b\s*([0-9][0-9,]*)\b", search_space, re.IGNORECASE)
            if not m:
                m = re.search(r"\bHolders\b\s*([0-9][0-9,]*)\b", text, re.IGNORECASE)

            if m:
                n = int(m.group(1).replace(",", ""))
                if n > 0:
                    _HOLDERS_CACHE[token] = {"ts": now, "count": n}
                    return n
        except Exception:
            pass

    except Exception:
        return None

    return None


def _try_font(paths: list[str], size: int) -> ImageFont.FreeTypeFont:
    for p in paths:
        try:
            return ImageFont.truetype(p, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _load_fonts():
    bold_candidates = [
        "assets/font_bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    regular_candidates = [
        "assets/font_regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    # Bigger to match the website look
    return {
        "title": _try_font(bold_candidates, 72),
        "big": _try_font(bold_candidates, 98),
        "mid": _try_font(regular_candidates, 34),
        "box_sym": _try_font(bold_candidates, 36),
        "box_amt": _try_font(bold_candidates, 54),
        "box_usd": _try_font(regular_candidates, 34),
    }


def _text_center(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, y: int, width: int, fill):
    try:
        tw = draw.textlength(text, font=font)
        x = int((width - tw) / 2)
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        x = int((width - (bbox[2] - bbox[0])) / 2)
    draw.text((x, y), text, font=font, fill=fill)


def _draw_text_shadow(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill,
    shadow=(0, 0, 0, 140),
    offset=(2, 2),
):
    x, y = xy
    draw.text((x + offset[0], y + offset[1]), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)


def _draw_center_shadow(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    y: int,
    width: int,
    fill,
    shadow=(0, 0, 0, 140),
    offset=(2, 2),
):
    try:
        tw = draw.textlength(text, font=font)
        x = int((width - tw) / 2)
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        x = int((width - (bbox[2] - bbox[0])) / 2)

    _draw_text_shadow(draw, (x, y), text, font, fill=fill, shadow=shadow, offset=offset)


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    return m


def _glass_panel(
    bg: Image.Image,
    rect: tuple[int, int, int, int],
    radius: int,
    tint=(20, 18, 40, 130),
    blur_radius=14,
) -> Image.Image:
    x1, y1, x2, y2 = rect
    crop = bg.crop((x1, y1, x2, y2)).filter(ImageFilter.GaussianBlur(blur_radius)).convert("RGBA")
    overlay = Image.new("RGBA", (x2 - x1, y2 - y1), tint)
    crop = Image.alpha_composite(crop, overlay)

    mask = _rounded_mask((x2 - x1, y2 - y1), radius)
    panel = Image.new("RGBA", (x2 - x1, y2 - y1), (0, 0, 0, 0))
    panel.paste(crop, (0, 0), mask)

    # Subtle border like the website, no extra separators
    edge = Image.new("RGBA", (x2 - x1, y2 - y1), (255, 255, 255, 26))
    border = Image.new("RGBA", (x2 - x1, y2 - y1), (0, 0, 0, 0))
    border.paste(edge, (0, 0), mask)

    return Image.alpha_composite(panel, border)


def _text_h(font: ImageFont.ImageFont, s: str) -> int:
    x0, y0, x1, y1 = font.getbbox(s)
    return y1 - y0


def draw_box_text_centered(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    sym: str,
    amount: str,
    usd: str,
    font_sym: ImageFont.ImageFont,
    font_amt: ImageFont.ImageFont,
    font_usd: ImageFont.ImageFont,
    color_sym=(235, 235, 245, 255),
    color_amt=(255, 255, 255, 255),
    color_usd=(175, 175, 200, 255),
):
    x1, y1, x2, y2 = box
    w = x2 - x1
    h = y2 - y1

    gap1 = 8
    gap2 = 6

    h1 = _text_h(font_sym, sym)
    h2 = _text_h(font_amt, amount)
    h3 = _text_h(font_usd, usd)

    total_h = h1 + gap1 + h2 + gap2 + h3
    start_y = y1 + (h - total_h) // 2

    px = x1 + 44

    draw.text((px, start_y), sym, font=font_sym, fill=color_sym)
    draw.text((px, start_y + h1 + gap1), amount, font=font_amt, fill=color_amt)
    draw.text((px, start_y + h1 + gap1 + h2 + gap2), usd, font=font_usd, fill=color_usd)


# ================= BALANCES =================

def fetch_balances_and_values():
    drb_dec = erc20_decimals(DRB_TOKEN)
    weth_dec = erc20_decimals(WETH_TOKEN)
    usdc_dec = erc20_decimals(USDC_TOKEN)

    drb_raw = erc20_balance_of(DRB_TOKEN, GROK_WALLET)
    weth_raw = erc20_balance_of(WETH_TOKEN, GROK_WALLET)
    usdc_raw = erc20_balance_of(USDC_TOKEN, GROK_WALLET)

    # Native ETH balance via eth_getBalance
    eth_raw = int(_rpc_call("eth_getBalance", [GROK_WALLET, "latest"]), 16)

    drb_amt = drb_raw / 10 ** drb_dec
    weth_amt = weth_raw / 10 ** weth_dec
    usdc_amt = usdc_raw / 10 ** usdc_dec  # USDC = 6 decimals, price = $1
    eth_amt = eth_raw / 10 ** 18

    drb_price = fetch_price_usd(DRB_TOKEN)
    weth_price = fetch_price_usd(WETH_TOKEN)

    drb_usd = drb_amt * drb_price
    weth_usd = weth_amt * weth_price
    usdc_usd = usdc_amt  # 1 USDC = $1
    eth_usd = eth_amt * weth_price  # ETH price ~= WETH price

    return {
        "DRB": {
            "amount": f"{drb_amt:,.0f}",
            "amount_float": float(drb_amt),
            "usd": fmt_usd(drb_usd),
            "usd_float": float(drb_usd),
        },
        "WETH": {
            "amount": f"{weth_amt:,.2f}",
            "amount_float": float(weth_amt),
            "usd": fmt_usd(weth_usd),
            "usd_float": float(weth_usd),
        },
        "ETH": {
            "amount": f"{eth_amt:,.4f}",
            "amount_float": float(eth_amt),
            "usd": fmt_usd(eth_usd),
            "usd_float": float(eth_usd),
        },
        "USDC": {
            "amount": f"{usdc_amt:,.2f}",
            "amount_float": float(usdc_amt),
            "usd": fmt_usd(usdc_usd),
            "usd_float": float(usdc_usd),
        },
    }


# ================= DONUT IMAGE (existing /grok) =================

def generate_balance_donut(
    drb_usd: float,
    weth_usd: float,
    drb_amount_float: float,
    weth_amount_float: float,
    eth_usd: float = 0.0,
    eth_amount_float: float = 0.0,
    usdc_usd: float = 0.0,
    usdc_amount_float: float = 0.0,
):
    total = drb_usd + weth_usd + eth_usd + usdc_usd

    drb_amount_label = fmt_compact_b(drb_amount_float)
    weth_amount_label = f"{weth_amount_float:,.2f}"
    eth_amount_label = f"{eth_amount_float:,.2f}"
    if usdc_amount_float >= 1_000:
        usdc_amount_label = f"{usdc_amount_float / 1_000:.1f}k"
    else:
        usdc_amount_label = f"{usdc_amount_float:.1f}"

    values = [drb_usd, weth_usd, eth_usd, usdc_usd]
    colors = [DRB_COLOR, WETH_COLOR, ETH_COLOR, USDC_COLOR]
    labels = [f"DRB\n{drb_amount_label}", f"WETH\n{weth_amount_label}", f"ETH\n{eth_amount_label}", f"USDC\n{usdc_amount_label}"]

    # Filter out zero values to avoid empty wedges
    filtered = [(v, c, l) for v, c, l in zip(values, colors, labels) if v > 0]
    if filtered:
        values, colors, labels = zip(*filtered)
    else:
        values, colors, labels = [1], ["#333"], ["N/A"]

    fig, ax = plt.subplots(figsize=(6.6, 6.6))
    wedges, _ = ax.pie(
        values,
        colors=colors,
        startangle=90,
        wedgeprops=dict(width=0.35),
    )
    ax.set(aspect="equal")
    ax.set_title("GROK WALLET", fontsize=24, fontweight="bold", pad=2, y=0.94)

    ax.text(0, 0, f"${total:,.0f}", ha="center", va="center", fontsize=30, fontweight="bold")
    ax.text(0, -0.20, "Total Balance", ha="center", va="center", fontsize=11, color="#666")

    for w, t in zip(wedges, labels):
        ang = (w.theta1 + w.theta2) / 2.0
        r = 0.82
        x = r * (math.cos(math.radians(ang)))
        y = r * (math.sin(math.radians(ang)))
        ax.text(
            x,
            y,
            t,
            ha="center",
            va="center",
            fontsize=12,
            fontweight="bold",
            color="#ffffff",
        )

    buf = BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=170, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf


def make_balance_table_caption(
    drb_amount_float: float,
    drb_usd_str: str,
    weth_amount_str: str,
    weth_usd_str: str,
    eth_amount_str: str,
    eth_usd_str: str,
    usdc_amount_str: str,
    usdc_usd_str: str,
    fees: str | None,
) -> str:
    """Build the CLAWD-style stats caption for /grok."""
    now = time.time()

    # Use 15-min cache for stats (price, fdv, holders)
    cached = _GROK_STATS_CACHE.get("data")
    if cached and (now - _GROK_STATS_CACHE["ts"]) < _GROK_STATS_CACHE_TTL:
        price = cached["price"]
        fdv = cached["fdv"]
        holders = cached["holders"]
    else:
        price, fdv = fetch_price_and_fdv(DRB_TOKEN)
        holders = basescan_token_holder_count(DRB_TOKEN)
        if price is None and cached:
            # DexScreener hiccup: keep serving the previous stats instead of N/A
            price, fdv = cached["price"], cached["fdv"]
            holders = holders if holders is not None else cached["holders"]
        _GROK_STATS_CACHE["ts"] = now
        _GROK_STATS_CACHE["data"] = {"price": price, "fdv": fdv, "holders": holders}

    # DRB Stats block
    lines = []
    lines.append("<b>📊 DRB Stats</b>")
    lines.append(f"Current price: {_fmt_price(price) if price else 'N/A'}")
    lines.append(f"Market cap: {_fmt_int_usd(fdv) if fdv else 'N/A'}")
    lines.append(f"Holders: {holders:,}" if holders is not None else "Holders: N/A")
    lines.append("")

    # Grok Wallet block
    grok_addr = "0xB1058c959987E3513600EB5b4fD82Aeee2a0E4F9"
    wallet_link = f"https://basescan.org/address/{grok_addr}"
    wallet_html = f'<a href="{wallet_link}">{_short_addr_dots(grok_addr)}</a>'

    lines.append("<b>Grok Wallet</b>")
    lines.append(wallet_html)

    drb_compact = _fmt_big(drb_amount_float)
    lines.append(f"{drb_compact} DRB ({drb_usd_str})")
    lines.append(f"{weth_amount_str} WETH ({weth_usd_str})")
    lines.append(f"{eth_amount_str} ETH ({eth_usd_str})")
    lines.append(f"{usdc_amount_str} USDC ({usdc_usd_str})")

    # Total value
    try:
        drb_val = float(drb_usd_str.replace("$", "").replace(",", ""))
        weth_val = float(weth_usd_str.replace("$", "").replace(",", ""))
        eth_val = float(eth_usd_str.replace("$", "").replace(",", ""))
        usdc_val = float(usdc_usd_str.replace("$", "").replace(",", ""))
        total_value = drb_val + weth_val + eth_val + usdc_val
        lines.append(f"Total value: {_fmt_int_usd(total_value)}")
    except Exception:
        pass

    return "\n".join(lines)


# ================= GROK2 STYLE CARD =================

def generate_grok_web_style_card(
    total_usd: float,
    weth_amount_float: float,
    weth_usd: float,
    drb_amount_float: float,
    drb_usd: float,
):
    bg = Image.open(GROK_BG_PATH).convert("RGBA")
    bg = bg.resize((CARD_W, CARD_H), Image.LANCZOS)

    fonts = _load_fonts()

    WHITE = (255, 255, 255, 255)
    MUTED = (175, 175, 200, 255)
    SOFT = (235, 235, 245, 255)

    canvas = bg.copy()

    # Outer glass card
    outer = (24, 24, CARD_W - 24, CARD_H - 24)
    outer_panel = _glass_panel(canvas, outer, radius=34, tint=(12, 10, 30, 145), blur_radius=18)
    canvas.alpha_composite(outer_panel, (outer[0], outer[1]))

    d = ImageDraw.Draw(canvas)

    # Header texts (no auth line, no address, no 24h, no footer)
    _draw_center_shadow(d, "GROK WALLET", fonts["title"], y=62, width=CARD_W, fill=WHITE, shadow=(0, 0, 0, 120))
    _draw_center_shadow(d, f"${total_usd:,.0f}", fonts["big"], y=148, width=CARD_W, fill=WHITE, shadow=(0, 0, 0, 120))
    _text_center(d, "Live Balance", fonts["mid"], y=264, width=CARD_W, fill=MUTED)

    # Inner boxes
    box_y1 = 324
    box_y2 = 488
    left = (outer[0] + 36, box_y1, (CARD_W // 2) - 18, box_y2)
    right = ((CARD_W // 2) + 18, box_y1, outer[2] - 36, box_y2)

    left_panel = _glass_panel(canvas, left, radius=22, tint=(18, 18, 38, 150), blur_radius=16)
    right_panel = _glass_panel(canvas, right, radius=22, tint=(18, 18, 38, 150), blur_radius=16)

    canvas.alpha_composite(left_panel, (left[0], left[1]))
    canvas.alpha_composite(right_panel, (right[0], right[1]))

    d = ImageDraw.Draw(canvas)

    # Values formatting
    eth_amt_str = f"{weth_amount_float:,.2f}"
    eth_usd_str = fmt_usd(weth_usd)

    drb_amt_str = fmt_compact_b(drb_amount_float)
    drb_usd_str = fmt_usd(drb_usd)

    # Center the 3-line blocks vertically inside each box
    draw_box_text_centered(
        draw=d,
        box=left,
        sym="ETH",
        amount=eth_amt_str,
        usd=eth_usd_str,
        font_sym=fonts["box_sym"],
        font_amt=fonts["box_amt"],
        font_usd=fonts["box_usd"],
        color_sym=SOFT,
        color_amt=WHITE,
        color_usd=MUTED,
    )

    draw_box_text_centered(
        draw=d,
        box=right,
        sym="DRB",
        amount=drb_amt_str,
        usd=drb_usd_str,
        font_sym=fonts["box_sym"],
        font_amt=fonts["box_amt"],
        font_usd=fonts["box_usd"],
        color_sym=SOFT,
        color_amt=WHITE,
        color_usd=MUTED,
    )

    buf = BytesIO()
    canvas.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ================= BALANCES CACHE (15 min) =================

_BALANCES_CACHE = {"ts": 0, "data": None}
_BALANCES_CACHE_TTL = 900  # 15 minutes


def fetch_balances_cached():
    """Fetch wallet balances with 15-minute cache."""
    now = time.time()
    if _BALANCES_CACHE["data"] and (now - _BALANCES_CACHE["ts"]) < _BALANCES_CACHE_TTL:
        return _BALANCES_CACHE["data"]

    data = fetch_balances_and_values()
    _BALANCES_CACHE["ts"] = now
    _BALANCES_CACHE["data"] = data
    return data


# ================= ANTI-SPAM GUARD =================
# Per-user sliding window over commands (private and group). Admin exempt.
RATE_LIMIT_N = int(os.environ.get("RATE_LIMIT_N", "5"))        # max commands
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))  # seconds
_RATE_CALLS: dict = {}   # user_id -> [timestamps]
_RATE_WARNED: dict = {}  # user_id -> last warn ts
_RATE_ADMIN_NOTIFIED: dict = {}  # user_id -> last admin-alert ts
_RATE_BLOCKED_COUNT: dict = {}   # user_id -> commands blocked since last admin alert
RATE_ADMIN_ALERT_COOLDOWN = int(os.environ.get("RATE_ADMIN_ALERT_COOLDOWN", "1800"))  # 30 min
BLACKLIST_THRESHOLD = int(os.environ.get("BLACKLIST_THRESHOLD", "15"))  # blocked cmds -> auto-blacklist
_BLACKLIST_CACHE = {"ts": 0.0, "ids": set()}


def _blacklist() -> set:
    now = time.time()
    if now - _BLACKLIST_CACHE["ts"] > 30:
        _BLACKLIST_CACHE["ids"] = set(_load_state().get("blacklist") or [])
        _BLACKLIST_CACHE["ts"] = now
    return _BLACKLIST_CACHE["ids"]


def _blacklist_set(uid: int, blocked: bool) -> None:
    def _m(st):
        cur = set(int(u) for u in (st.get("blacklist") or []))
        (cur.add(int(uid)) if blocked else cur.discard(int(uid)))
        st["blacklist"] = sorted(cur)
    _update_state_fields(_m)
    _BLACKLIST_CACHE["ts"] = 0.0


async def command_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram.ext import ApplicationHandlerStop
    msg = update.message
    user = update.effective_user
    if not msg or not user:
        return
    if ADMIN_ID > 0 and user.id == ADMIN_ID:
        return
    from telegram.ext import ApplicationHandlerStop as _Stop
    if user.id in _blacklist():
        raise _Stop()  # blacklisted: ignore silently
    now = time.time()
    q = [t for t in _RATE_CALLS.get(user.id, []) if now - t < RATE_LIMIT_WINDOW]
    if len(q) >= RATE_LIMIT_N:
        _RATE_CALLS[user.id] = q
        _RATE_BLOCKED_COUNT[user.id] = _RATE_BLOCKED_COUNT.get(user.id, 0) + 1
        # Warn at most once a minute per user; otherwise drop silently
        if now - _RATE_WARNED.get(user.id, 0) > 60:
            _RATE_WARNED[user.id] = now
            try:
                await msg.reply_text("\u23F3 Slow down — try again in a minute.")
            except Exception:
                pass
        blocked_total = _RATE_BLOCKED_COUNT.get(user.id, 0)
        uname = f"@{user.username}" if user.username else (user.full_name or "?")
        chat = update.effective_chat
        where = "private" if (chat and chat.type == "private") else f"group {getattr(chat, 'title', '') or chat.id}".strip()
        cmd = (msg.text or "")[:60]

        # Persistent offender -> auto-blacklist + admin alert with an Unblock button
        if blocked_total >= BLACKLIST_THRESHOLD and user.id not in _blacklist():
            _blacklist_set(user.id, True)
            _RATE_BLOCKED_COUNT.pop(user.id, None)
            if ADMIN_ID > 0:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                    "\u2705 Unblock", callback_data=f"blk:un:{user.id}")]])
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "\U0001F6D1 <b>User auto-blacklisted for spamming</b>\n"
                            f"User: {uname}\n"
                            f"ID: <code>{user.id}</code>\n"
                            f"Where: {where}\n"
                            f"Blocked commands: {blocked_total} (limit {RATE_LIMIT_N}/{RATE_LIMIT_WINDOW}s)\n"
                            f"Last command: <code>{cmd}</code>\n\n"
                            "The bot now ignores this user everywhere."
                        ),
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                except Exception:
                    pass
            raise ApplicationHandlerStop()

        # First-level alert to the admin (at most once per user per cooldown)
        if ADMIN_ID > 0 and now - _RATE_ADMIN_NOTIFIED.get(user.id, 0) > RATE_ADMIN_ALERT_COOLDOWN:
            _RATE_ADMIN_NOTIFIED[user.id] = now
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "\u26A0\uFE0F Rate limit hit\n"
                        f"User: {uname} (id {user.id})\n"
                        f"Where: {where}\n"
                        f"Blocked commands: {blocked_total} (limit {RATE_LIMIT_N}/{RATE_LIMIT_WINDOW}s)\n"
                        f"Last: {cmd}\n"
                        f"Auto-blacklist at {BLACKLIST_THRESHOLD} blocked commands."
                    ),
                )
            except Exception:
                pass
        raise ApplicationHandlerStop()
    q.append(now)
    _RATE_CALLS[user.id] = q
    if len(_RATE_CALLS) > 2000:  # bound memory
        cutoff = now - RATE_LIMIT_WINDOW
        for uid in [u for u, ts in _RATE_CALLS.items() if not ts or ts[-1] < cutoff]:
            _RATE_CALLS.pop(uid, None)


# ================= COMMANDS =================

async def grok_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    try:
        b = fetch_balances_cached()

        donut = generate_balance_donut(
            b["DRB"]["usd_float"],
            b["WETH"]["usd_float"],
            b["DRB"]["amount_float"],
            b["WETH"]["amount_float"],
            eth_usd=b["ETH"]["usd_float"],
            eth_amount_float=b["ETH"]["amount_float"],
            usdc_usd=b["USDC"]["usd_float"],
            usdc_amount_float=b["USDC"]["amount_float"],
        )

        caption = make_balance_table_caption(
            drb_amount_float=b["DRB"]["amount_float"],
            drb_usd_str=b["DRB"]["usd"],
            weth_amount_str=b["WETH"]["amount"],
            weth_usd_str=b["WETH"]["usd"],
            eth_amount_str=b["ETH"]["amount"],
            eth_usd_str=b["ETH"]["usd"],
            usdc_amount_str=b["USDC"]["amount"],
            usdc_usd_str=b["USDC"]["usd"],
            fees=None,
        )

        await msg.reply_photo(photo=donut, caption=caption, parse_mode="HTML")

    except Exception as e:
        err = repr(e)
        print("grok_command error:", err)
        if ADMIN_ID > 0:
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"grok_command error: {err}")
            except Exception:
                pass
        await msg.reply_text("Error fetching balances")


async def grok2_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    try:
        b = fetch_balances_cached()
        total_usd = b["DRB"]["usd_float"] + b["WETH"]["usd_float"] + b["ETH"]["usd_float"] + b["USDC"]["usd_float"]

        card = generate_grok_web_style_card(
            total_usd=total_usd,
            weth_amount_float=b["WETH"]["amount_float"],
            weth_usd=b["WETH"]["usd_float"],
            drb_amount_float=b["DRB"]["amount_float"],
            drb_usd=b["DRB"]["usd_float"],
        )

        await msg.reply_photo(photo=card)

    except Exception as e:
        err = repr(e)
        print("grok2_command error:", err)
        if ADMIN_ID > 0:
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"grok2_command error: {err}")
            except Exception:
                pass
        await msg.reply_text("Error fetching balances")



# ================= STATS (/stats) =================

_TRANSFERS_CACHE = {}
_TRANSFERS_CACHE_TTL = 900  # 15 minutes
STATS_DEFAULT_DAYS = 30
STATS_MAX_DAYS = 365


def _alchemy_get_transfers(token: str, wallet: str, startblock: int = 0) -> list:
    """Fetch ERC-20 transfers of `token` involving `wallet` via alchemy_getAssetTransfers.
    Returns Etherscan-style dicts: {hash, from, to, value, tokenDecimal, timeStamp, blockNumber, uniqueId}."""
    out = []
    for direction in ("toAddress", "fromAddress"):
        page_key = None
        for _ in range(25):  # safety bound
            params = {
                "fromBlock": hex(int(startblock)),
                "toBlock": "latest",
                direction: wallet,
                "contractAddresses": [token],
                "category": ["erc20"],
                "withMetadata": True,
                "maxCount": "0x3e8",  # 1000
                "order": "asc",
            }
            if page_key:
                params["pageKey"] = page_key

            payload = {"jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers", "params": [params]}
            r = requests.post(ALCHEMY_RPC_URL, json=payload, headers=UA_HEADERS, timeout=30)
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                raise RuntimeError(str(j["error"]))
            res = j.get("result") or {}

            for t in res.get("transfers") or []:
                raw = t.get("rawContract") or {}
                try:
                    value = int(str(raw.get("value") or "0x0"), 16)
                    dec = int(str(raw.get("decimal") or "0x12"), 16)
                except Exception:
                    continue
                ts_iso = ((t.get("metadata") or {}).get("blockTimestamp") or "").replace("Z", "+00:00")
                try:
                    ts = int(datetime.fromisoformat(ts_iso).timestamp())
                except Exception:
                    continue
                out.append({
                    "hash": t.get("hash"),
                    "from": (t.get("from") or "").lower(),
                    "to": (t.get("to") or "").lower(),
                    "value": str(value),
                    "tokenDecimal": str(dec),
                    "timeStamp": str(ts),
                    "blockNumber": str(int(str(t.get("blockNum") or "0x0"), 16)),
                    "uniqueId": t.get("uniqueId"),
                })

            page_key = res.get("pageKey")
            if not page_key:
                break
    return out


def _etherscan_get_transfers(token: str, wallet: str, startblock: int = 0) -> list:
    """Fallback: Etherscan v2 tokentx (requires an API key for Base)."""
    txs = []
    startblock = int(startblock)
    for _ in range(20):  # safety bound
        params = {
            "chainid": 8453,
            "module": "account",
            "action": "tokentx",
            "contractaddress": token,
            "address": wallet,
            "startblock": startblock,
            "endblock": 999999999,
            "page": 1,
            "offset": 10000,
            "sort": "asc",
        }
        if ETHERSCAN_APIKEY:
            params["apikey"] = ETHERSCAN_APIKEY

        r = requests.get("https://api.etherscan.io/v2/api", params=params, timeout=30)
        r.raise_for_status()
        j = r.json() if r.content else {}
        result = j.get("result")
        if not isinstance(result, list):
            raise RuntimeError(f"etherscan tokentx: {str(result)[:120]}")
        txs.extend(result)
        if len(result) < 10000:
            break
        startblock = int(result[-1]["blockNumber"]) + 1
    return txs


def fetch_token_transfers(token: str, wallet: str, startblock: int = 0) -> list:
    """All ERC-20 transfers of `token` involving `wallet` on Base, cached 15 min.
    Primary source: Alchemy asset transfers (no API key needed).
    Fallback: Etherscan v2 (only useful if ETHERSCAN_APIKEY is set)."""
    key = (token.lower(), wallet.lower(), int(startblock))
    now = time.time()
    c = _TRANSFERS_CACHE.get(key)
    if c and (now - c["ts"]) < _TRANSFERS_CACHE_TTL:
        return c["data"]

    txs = None
    try:
        txs = _alchemy_get_transfers(token, wallet, startblock)
    except Exception as e:
        print("alchemy transfers error:", repr(e))

    if txs is None and ETHERSCAN_APIKEY:
        try:
            txs = _etherscan_get_transfers(token, wallet, startblock)
        except Exception as e:
            print("etherscan transfers error:", repr(e))

    if txs is None:
        raise RuntimeError("No transfer data source available (Alchemy failed, no ETHERSCAN_APIKEY)")

    # Dedupe (the two direction queries can overlap on self-transfers; pagination borders too)
    seen = set()
    unique = []
    for t in txs:
        k = t.get("uniqueId") or (t.get("hash"), t.get("from"), t.get("to"), t.get("value"), t.get("timeStamp"))
        if k in seen:
            continue
        seen.add(k)
        unique.append(t)
    unique.sort(key=lambda t: int(t.get("timeStamp") or 0))

    # Prune expired cache entries so it doesn't grow forever
    if len(_TRANSFERS_CACHE) > 40:
        for k in [k for k, v in _TRANSFERS_CACHE.items() if (now - v["ts"]) > _TRANSFERS_CACHE_TTL]:
            _TRANSFERS_CACHE.pop(k, None)

    _TRANSFERS_CACHE[key] = {"ts": now, "data": unique}
    return unique


# Incremental transfer cache for /stats: history never expires; only new blocks are fetched.
_INCR_TRANSFERS_CACHE = {}
_INCR_TRANSFERS_TTL = 900  # refresh newest blocks at most every 15 min
_INCR_TRANSFERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transfers_cache.json")
_INCR_REORG_OVERLAP = 50  # re-scan a few blocks to survive shallow reorgs


def _transfer_key(t: dict):
    return t.get("uniqueId") or "|".join(
        str(t.get(k) or "") for k in ("hash", "from", "to", "value", "timeStamp")
    )


def _incr_cache_load():
    if _INCR_TRANSFERS_CACHE.get("_loaded"):
        return
    _INCR_TRANSFERS_CACHE["_loaded"] = True
    try:
        with open(_INCR_TRANSFERS_FILE, "r") as f:
            data = json.load(f)
        for k, v in data.items():
            _INCR_TRANSFERS_CACHE[k] = {
                "transfers": v.get("transfers") or [],
                "last_block": int(v.get("last_block") or 0),
                "ts": 0.0,
            }
    except Exception:
        pass


def _incr_cache_save():
    try:
        data = {
            k: {"transfers": v["transfers"], "last_block": v["last_block"]}
            for k, v in _INCR_TRANSFERS_CACHE.items()
            if isinstance(v, dict) and "transfers" in v
        }
        tmp = _INCR_TRANSFERS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _INCR_TRANSFERS_FILE)
    except Exception as e:
        print("incremental transfer cache save error:", repr(e))


def fetch_token_transfers_incremental(token: str, wallet: str) -> list:
    """Full transfer history of `token` for `wallet`, kept incrementally.
    The historical portion never expires; on refresh only blocks after the
    last seen block are re-fetched and appended (deduped)."""
    _incr_cache_load()
    key = f"{token.lower()}|{wallet.lower()}"
    now = time.time()
    c = _INCR_TRANSFERS_CACHE.get(key)

    if c and (now - c.get("ts", 0)) < _INCR_TRANSFERS_TTL:
        return c["transfers"]

    if not c or not c.get("transfers"):
        txs = fetch_token_transfers(token, wallet, startblock=0)
        c = {
            "transfers": list(txs),
            "last_block": max((int(t.get("blockNumber") or 0) for t in txs), default=0),
            "ts": now,
        }
        _INCR_TRANSFERS_CACHE[key] = c
        _incr_cache_save()
        return c["transfers"]

    try:
        start = max(0, int(c.get("last_block") or 0) - _INCR_REORG_OVERLAP)
        new_txs = fetch_token_transfers(token, wallet, startblock=start)
        seen = {_transfer_key(t) for t in c["transfers"]}
        appended = False
        for t in new_txs:
            k2 = _transfer_key(t)
            if k2 in seen:
                continue
            seen.add(k2)
            c["transfers"].append(t)
            appended = True
        if appended:
            c["transfers"].sort(key=lambda t: int(t.get("timeStamp") or 0))
        c["last_block"] = max(
            int(c.get("last_block") or 0),
            max((int(t.get("blockNumber") or 0) for t in new_txs), default=0),
        )
        c["ts"] = now
        if appended:
            _incr_cache_save()
    except Exception as e:
        print("incremental transfer refresh error:", repr(e))
        c["ts"] = now  # back off; serve cached history
    return c["transfers"]


def _tx_amount(t: dict) -> float:
    dec = int(t.get("tokenDecimal") or 18)
    return int(t.get("value") or 0) / 10 ** dec


def _parse_stats_period(args, default_days: int = STATS_DEFAULT_DAYS) -> int:
    """Parse '7d' / '4w' / '15' from command args."""
    days = default_days
    if args:
        m = re.match(r"^(\d+)\s*([dwDW])?$", str(args[0]).strip())
        if m:
            n = int(m.group(1))
            unit = (m.group(2) or "d").lower()
            days = n * 7 if unit == "w" else n
    return max(1, min(days, STATS_MAX_DAYS))


def _daily_claims(txs: list, days: int, end_date) -> tuple:
    """Sum incoming transfers to the Grok wallet per day for the last `days` days.
    Prefers transfers coming from the claim contract; falls back to all incoming."""
    wallet_l = GROK_WALLET.lower()
    incoming = [t for t in txs if (t.get("to") or "").lower() == wallet_l]
    from_claim = [t for t in incoming if (t.get("from") or "").lower() == CLAIM_CONTRACT.lower()]
    use = from_claim if from_claim else incoming

    daily = {}
    for t in use:
        d = datetime.fromtimestamp(int(t["timeStamp"]), tz=timezone.utc).date()
        daily[d] = daily.get(d, 0.0) + _tx_amount(t)

    dates = [end_date - timedelta(days=i) for i in range(days - 1, -1, -1)]
    return dates, [daily.get(d, 0.0) for d in dates]


def _daily_claims_usd(txs: list, days: int, end_date, px_map: dict) -> tuple:
    """Daily USD value of claims (each day's amount x that day's close price)."""
    wallet_l = GROK_WALLET.lower()
    incoming = [t for t in txs if (t.get("to") or "").lower() == wallet_l]
    from_src = [t for t in incoming if (t.get("from") or "").lower() in FEES_SOURCES]
    use = from_src if from_src else incoming

    daily = {}
    for t in use:
        ts_ = int(t["timeStamp"])
        d = datetime.fromtimestamp(ts_, tz=timezone.utc).date()
        daily[d] = daily.get(d, 0.0) + _tx_amount(t) * _px_on_day(px_map, ts_)

    dates = [end_date - timedelta(days=i) for i in range(days - 1, -1, -1)]
    return dates, [daily.get(d, 0.0) for d in dates]


def _drb_balance_series(txs: list) -> list:
    """Running DRB balance of the Grok wallet over time: [(timestamp, balance), ...]."""
    wallet_l = GROK_WALLET.lower()
    events = []
    for t in txs:
        amt = _tx_amount(t)
        delta = 0.0
        if (t.get("to") or "").lower() == wallet_l:
            delta += amt
        if (t.get("from") or "").lower() == wallet_l:
            delta -= amt
        if delta != 0.0:
            events.append((int(t["timeStamp"]), delta))
    events.sort(key=lambda e: e[0])

    series = []
    bal = 0.0
    for ts, delta in events:
        bal += delta
        series.append((ts, max(bal, 0.0)))
    return series


# Rendered /stats result cache: days -> {ts, png, drb_total, weth_total} (5 min)
_STATS_RESULT_CACHE = {}
_STATS_RESULT_TTL = 300


def generate_stats_chart(days: int):
    """Build the /stats image: daily claims bars (DRB + WETH) and DRB accumulation area chart.
    Returns (png_buffer, drb_total_claimed, weth_total_claimed). Result cached 5 min per period."""
    now = time.time()
    c = _STATS_RESULT_CACHE.get(days)
    if c and (now - c["ts"]) < _STATS_RESULT_TTL:
        return BytesIO(c["png"]), c["drb_total"], c["weth_total"], c["growth"], c["growth_pct"], c.get("usd_total", 0.0)

    drb_txs = fetch_token_transfers_incremental(DRB_TOKEN, GROK_WALLET)
    weth_txs = fetch_token_transfers_incremental(WETH_TOKEN, GROK_WALLET)

    now_utc = datetime.now(timezone.utc)
    end_date = now_utc.date()

    dates, drb_vals = _daily_claims(drb_txs, days, end_date)
    _, weth_vals = _daily_claims(weth_txs, days, end_date)

    # USD value at claim time (daily closes: DRB pool + WETH pool)
    maps = _daily_price_maps()
    _, drb_usd_vals = _daily_claims_usd(drb_txs, days, end_date, maps["drb"])
    _, weth_usd_vals = _daily_claims_usd(weth_txs, days, end_date, maps["eth"])

    fig, (ax1, ax3) = plt.subplots(2, 1, figsize=(11, 10))
    fig.subplots_adjust(hspace=0.42)

    # ---- Bar chart: USD value of claims per day (stacked DRB + WETH) ----
    x = list(range(len(dates)))
    width = 0.62
    ax1.bar(x, drb_usd_vals, width=width, color=DRB_COLOR, label="DRB")
    ax1.bar(x, weth_usd_vals, width=width, bottom=drb_usd_vals, color=WETH_COLOR, label="WETH")

    ax1.set_title(f"Fees claimed per day (USD at claim time) — last {days}d",
                  fontsize=16, fontweight="bold")
    ax1.set_ylabel("USD", fontweight="bold")
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))

    step = max(1, len(dates) // 10)
    ax1.set_xticks(x[::step])
    ax1.set_xticklabels([d.strftime("%d %b") for d in dates][::step], rotation=45, ha="right", fontsize=9)
    ax1.grid(axis="y", alpha=0.22)
    ax1.set_axisbelow(True)
    ax1.set_ylim(bottom=0)
    ax1.legend(
        handles=[Patch(color=DRB_COLOR, label="DRB"), Patch(color=WETH_COLOR, label="WETH")],
        loc="upper left",
        frameon=False,
    )

    # ---- Area chart: DRB accumulating in the wallet ----
    series = _drb_balance_series(drb_txs)
    start_ts = int((now_utc - timedelta(days=days)).timestamp())

    bal_at_start = 0.0
    for ts, bal in series:
        if ts < start_ts:
            bal_at_start = bal
        else:
            break

    pts = [(ts, bal) for ts, bal in series if ts >= start_ts]
    xs = [datetime.fromtimestamp(start_ts, tz=timezone.utc)]
    ys = [bal_at_start]
    for ts, bal in pts:
        xs.append(datetime.fromtimestamp(ts, tz=timezone.utc))
        ys.append(bal)
    xs.append(now_utc)
    ys.append(ys[-1])

    ax3.fill_between(xs, ys, color=DRB_COLOR, alpha=0.15)
    ax3.plot(xs, ys, color=DRB_COLOR, linewidth=2)
    ax3.set_title("DRB accumulated in Grok Wallet", fontsize=16, fontweight="bold")
    ax3.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _fmt_big(v)))
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax3.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
    for lbl in ax3.get_xticklabels():
        lbl.set_rotation(45)
        lbl.set_ha("right")
    ax3.grid(axis="y", alpha=0.22)
    ax3.set_axisbelow(True)
    ax3.set_ylim(bottom=0)

    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    # Wallet DRB growth over the selected period
    growth = ys[-1] - bal_at_start
    growth_pct = (growth / bal_at_start * 100.0) if bal_at_start > 0 else None

    png = buf.getvalue()
    _STATS_RESULT_CACHE[days] = {
        "ts": now,
        "png": png,
        "drb_total": sum(drb_vals),
        "weth_total": sum(weth_vals),
        "usd_total": sum(drb_usd_vals) + sum(weth_usd_vals),
        "growth": growth,
        "growth_pct": growth_pct,
    }
    return BytesIO(png), sum(drb_vals), sum(weth_vals), growth, growth_pct, sum(drb_usd_vals) + sum(weth_usd_vals)


# ---- /stats total: all-time claimed fees ----
# Baseline hardcoded (computed 2026-08-25, covering everything up to 2026-08-24
# 23:59 UTC / block 50,398,573). At runtime only blocks AFTER the baseline are
# scanned and added, so the command stays fast with no persistent disk.
FEES_SOURCES = {
    "0x5ec4f99f342038c67a312a166ff56e6d70383d86",  # fee distributor -> grok wallet
    CLAIM_CONTRACT.lower(),
}
FEES_BASELINE = {
    "block": 50_398_573,           # last block included in the baseline
    "drb": 4_402_794_413.596866,   # DRB claimed up to the baseline
    "weth": 145.794064,            # WETH claimed up to the baseline
    # USD value AT CLAIM TIME (DRB: daily close via GeckoTerminal OHLCV;
    # WETH: Chainlink ETH/USD at each claim's block). Computed 2026-08-25.
    "drb_usd": 363_395.35,
    "weth_usd": 342_802.04,
}

# ---- daily close price maps (for valuing claims at claim time) ----
_DAY_PX_CACHE = {"ts": 0.0, "drb": {}, "eth": {}}
_DAY_PX_TTL = 3600


def _gt_pool_day_closes(pool_addr: str) -> dict:
    """{'YYYY-MM-DD': close_usd} from GeckoTerminal daily OHLCV (up to 1000 days)."""
    out = {}
    r = requests.get(
        f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_addr}/ohlcv/day",
        params={"limit": 1000, "currency": "usd"},
        headers={"accept": "application/json"}, timeout=20)
    r.raise_for_status()
    for ts, _o, _h, _l, c, _v in (((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []):
        day = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        out[day] = float(c)
    return out


_WETH_POOL_CACHE = {"ts": 0.0, "pair": None}


def _weth_main_pool() -> str:
    now = time.time()
    if _WETH_POOL_CACHE["pair"] and (now - _WETH_POOL_CACHE["ts"]) < 86400:
        return _WETH_POOL_CACHE["pair"]
    r = requests.get(DEXSCREENER_TOKEN_URL + WETH_TOKEN, headers=UA_HEADERS, timeout=15)
    r.raise_for_status()
    best, best_liq = None, -1.0
    for pr in (r.json().get("pairs") or []):
        try:
            if (pr.get("chainId") or "").lower() != "base":
                continue
            liq = float((pr.get("liquidity") or {}).get("usd") or 0)
            if liq > best_liq and pr.get("pairAddress"):
                best, best_liq = str(pr["pairAddress"]).lower(), liq
        except Exception:
            continue
    if best:
        _WETH_POOL_CACHE.update(ts=now, pair=best)
    return best or ""


def _daily_price_maps():
    """Cached {'drb': {day: px}, 'eth': {day: px}} of daily USD closes."""
    now = time.time()
    if _DAY_PX_CACHE["drb"] and (now - _DAY_PX_CACHE["ts"]) < _DAY_PX_TTL:
        return _DAY_PX_CACHE
    try:
        pool = _get_main_pool()
        if pool:
            _DAY_PX_CACHE["drb"] = _gt_pool_day_closes(pool["pair"]) or _DAY_PX_CACHE["drb"]
    except Exception as e:
        print("drb day prices error:", repr(e))
    try:
        wp = _weth_main_pool()
        if wp:
            _DAY_PX_CACHE["eth"] = _gt_pool_day_closes(wp) or _DAY_PX_CACHE["eth"]
    except Exception as e:
        print("eth day prices error:", repr(e))
    _DAY_PX_CACHE["ts"] = now
    return _DAY_PX_CACHE


def _px_on_day(px_map: dict, ts: int, fallback: float = 0.0) -> float:
    if not px_map:
        return fallback
    d = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    if d in px_map:
        return px_map[d]
    earlier = [k for k in px_map if k <= d]
    if earlier:
        return px_map[max(earlier)]
    return px_map[min(px_map)]
_STATS_TOTAL_CACHE = {"ts": 0.0, "text": None}

_FEES_BASELINE_FILE = os.path.join(DATA_PATH, "fees_baseline.json")


def _load_fees_baseline_file() -> None:
    """Baseline priority: DATA_PATH/fees_baseline.json (sent by the admin at
    runtime) > assets/fees_baseline.json (shipped with the deploy) > hardcoded."""
    for path in (_FEES_BASELINE_FILE, "assets/fees_baseline.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                j = json.load(f)
            for k in ("block", "drb", "weth", "drb_usd", "weth_usd"):
                if k in j:
                    FEES_BASELINE[k] = type(FEES_BASELINE[k])(j[k])
            print(f"[fees] baseline loaded from {path} (block {FEES_BASELINE['block']})")
            return
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"fees baseline load error ({path}):", repr(e))


def _seed_transfers_cache_from_assets() -> None:
    """If the incremental transfers cache is missing (fresh container) but a copy
    was shipped in assets/, seed it from there to skip the full-history rescan."""
    try:
        if os.path.exists(_INCR_TRANSFERS_FILE):
            return
        src = "assets/transfers_cache.json"
        if os.path.exists(src):
            import shutil
            os.makedirs(os.path.dirname(_INCR_TRANSFERS_FILE) or ".", exist_ok=True)
            shutil.copy(src, _INCR_TRANSFERS_FILE)
            print(f"[fees] transfers cache seeded from {src} ({os.path.getsize(src):,} bytes)")
    except Exception as e:
        print("transfers cache seed error:", repr(e))


_load_fees_baseline_file()
_seed_transfers_cache_from_assets()


async def admin_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends fees_baseline.json or transfers_cache.json in private ->
    saved to disk and picked up immediately (recovers precomputed work after a reset)."""
    msg = update.message
    user = update.effective_user
    if not msg or not user or not msg.document:
        return
    if ADMIN_ID <= 0 or user.id != ADMIN_ID or msg.chat.type != "private":
        return
    name = (msg.document.file_name or "").strip()
    if name == "fees_baseline.json":
        dest = _FEES_BASELINE_FILE
    elif name == "transfers_cache.json":
        dest = _INCR_TRANSFERS_FILE
    elif name == "watch_state.json":
        dest = STATE_PATH
    else:
        return
    try:
        f = await msg.document.get_file()
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        await f.download_to_drive(dest)
        if name == "fees_baseline.json":
            _load_fees_baseline_file()
            _STATS_TOTAL_CACHE.update(ts=0.0, text=None)
        elif name == "transfers_cache.json":
            _INCR_TRANSFERS_CACHE.clear()
        elif name == "watch_state.json":
            _BLACKLIST_CACHE["ts"] = 0.0  # reload blacklist and settings
        await msg.reply_text(f"\u2705 {name} restored ({os.path.getsize(dest):,} bytes).")
    except Exception as e:
        await msg.reply_text(f"Error saving {name}: {e!r}")


def build_stats_total_text() -> str:
    now = time.time()
    if _STATS_TOTAL_CACHE["text"] and (now - _STATS_TOTAL_CACHE["ts"]) < 300:
        return _STATS_TOTAL_CACHE["text"]

    start = FEES_BASELINE["block"] + 1
    drb_total = FEES_BASELINE["drb"]
    weth_total = FEES_BASELINE["weth"]
    drb_usd = FEES_BASELINE["drb_usd"]
    weth_usd = FEES_BASELINE["weth_usd"]
    wallet_l = GROK_WALLET.lower()
    maps = _daily_price_maps()

    # Claims after the baseline, each valued at ITS claim moment
    for token, key in ((DRB_TOKEN, "drb"), (WETH_TOKEN, "weth")):
        txs = fetch_token_transfers(token, GROK_WALLET, startblock=start)
        for t in txs:
            if (t.get("to") or "").lower() != wallet_l:
                continue
            if (t.get("from") or "").lower() not in FEES_SOURCES:
                continue
            if int(t.get("blockNumber") or 0) < start:
                continue
            amt = _tx_amount(t)
            ts_ = int(t.get("timeStamp") or 0)
            if key == "drb":
                drb_total += amt
                drb_usd += amt * _px_on_day(maps["drb"], ts_)
            else:
                weth_total += amt
                px = _chainlink_eth_usd_at_block(int(t.get("blockNumber") or 0)) or _px_on_day(maps["eth"], ts_)
                weth_usd += amt * (px or 0.0)

    lines = ["\U0001F916 <b>Total Fees Claimed</b> \U0001F4B0", ""]
    lines.append(f"DRB: <b>{_fmt_big(drb_total)}</b> ({_fmt_int_usd(drb_usd)})")
    lines.append(f"WETH: <b>{weth_total:,.2f}</b> ({_fmt_int_usd(weth_usd)})")
    lines.append("")
    lines.append(f"Total: <b>{_fmt_int_usd(drb_usd + weth_usd)}</b>")
    lines.append("<i>USD valued at the moment of each claim</i>")
    text = "\n".join(lines)
    _STATS_TOTAL_CACHE.update(ts=now, text=text)
    return text


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    # /stats total -> all-time claimed fees summary
    if context.args and str(context.args[0]).strip().lower() == "total":
        try:
            text = await asyncio.get_event_loop().run_in_executor(None, build_stats_total_text)
            await msg.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            print("stats total error:", repr(e))
            await msg.reply_text("Error building total stats")
        return

    days = _parse_stats_period(context.args)

    try:
        buf, drb_total, weth_total, growth, growth_pct, usd_total = await asyncio.get_event_loop().run_in_executor(
            None, generate_stats_chart, days
        )
        if growth_pct is not None:
            growth_str = f"{growth_pct:+.1f}%"
        else:
            sign = "+" if growth >= 0 else "-"
            growth_str = f"{sign}{_fmt_big(abs(growth))} DRB"
        caption = (
            f"📊 <b>Claim stats — last {days} days</b>\n"
            f"Total claimed: <b>{_fmt_drb_millions(drb_total)} DRB</b> · <b>{_fmt_weth(weth_total)} WETH</b> (~<b>{_fmt_int_usd(usd_total)}</b>)\n"
            f"Grok Wallet: <b>{growth_str}</b>"
        )
        await msg.reply_photo(photo=buf, caption=caption, parse_mode="HTML")
    except Exception as e:
        err = repr(e)
        print("stats_command error:", err)
        if ADMIN_ID > 0:
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"stats_command error: {err}")
            except Exception:
                pass
        await msg.reply_text("Error building stats")


# ================= BIGGEST BUYS (/buys) =================

BUYS_DEFAULT_DAYS = 7
BUYS_TOP_N = 5
BASE_BLOCKS_PER_DAY = 43200  # ~2s per block

_POOL_CACHE = {"ts": 0, "data": None}


def _get_main_pool():
    """Main DRB pool on Base from DexScreener (highest liquidity). Cached 1h."""
    now = time.time()
    if _POOL_CACHE["data"] and (now - _POOL_CACHE["ts"]) < 3600:
        return _POOL_CACHE["data"]

    r = requests.get(DEXSCREENER_TOKEN_URL + DRB_TOKEN, headers=UA_HEADERS, timeout=20)
    r.raise_for_status()
    pairs = r.json().get("pairs") or []

    best = None
    best_liq = -1.0
    for p in pairs:
        try:
            if (p.get("chainId") or "").lower() != "base":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if liq > best_liq and p.get("pairAddress"):
                best = p
                best_liq = liq
        except Exception:
            continue

    if not best:
        return None

    quote = best.get("quoteToken") or {}
    data = {
        "pair": str(best["pairAddress"]).lower(),
        "quote": str(quote.get("address") or WETH_TOKEN).lower(),
        "quote_symbol": quote.get("symbol") or "WETH",
    }
    _POOL_CACHE["ts"] = now
    _POOL_CACHE["data"] = data
    return data


def _short_addr_buys(a: str) -> str:
    a = (a or "").strip()
    if len(a) <= 16:
        return a
    return f"{a[:8]}...{a[-8:]}"


# --- Swap-event based buy scan (chunked eth_getLogs; works on any RPC provider) ---
BUYS_MAX_DAYS = 30
LOG_CHUNK = int(os.environ.get("LOG_CHUNK", "10000"))

# Uniswap V2 / Solidly-style: Swap(address indexed sender, uint amount0In, uint amount1In, uint amount0Out, uint amount1Out, address indexed to)
V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
# Uniswap V3-style: Swap(address indexed sender, address indexed recipient, int256 amount0, int256 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick)
V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

_POOL_TOKENS_CACHE = {}


def _pool_token0_token1(pool_addr: str):
    """token0()/token1() of the pool via eth_call, fetched once per process."""
    key = pool_addr.lower()
    c = _POOL_TOKENS_CACHE.get(key)
    if c:
        return c
    t0 = "0x" + _eth_call(pool_addr, "0x0dfe1681")[-40:]
    t1 = "0x" + _eth_call(pool_addr, "0xd21220a7")[-40:]
    c = (t0.lower(), t1.lower())
    _POOL_TOKENS_CACHE[key] = c
    return c


def _topic_addr(topic) -> str:
    t = str(topic or "").lower()
    if t.startswith("0x"):
        t = t[2:]
    return "0x" + t[-40:]


def _int256(word_hex: str) -> int:
    v = int(word_hex, 16)
    if v >= 2 ** 255:
        v -= 2 ** 256
    return v


def _fetch_pool_swap_logs(pool_addr: str, from_block: int, to_block: int) -> list:
    """Chunked eth_getLogs for V2/V3-style Swap events emitted by the pool."""
    logs = []
    start = from_block
    while start <= to_block:
        end = min(start + LOG_CHUNK - 1, to_block)
        res = _rpc_call("eth_getLogs", [{
            "address": pool_addr,
            "fromBlock": hex(start),
            "toBlock": hex(end),
            "topics": [[V2_SWAP_TOPIC, V3_SWAP_TOPIC]],
        }])
        logs.extend(res or [])
        start = end + 1
    return logs


def _pool_buys_via_swap_logs(pool_addr: str, quote_token: str, start_block: int,
                             latest_block: int, exclude_to: set) -> list:
    """Decode the pool's Swap events into DRB buys: [{hash, to, drb, quote, ts}, ...].
    DEX type is detected per log by topic0 (V2/Solidly vs V3 signature)."""
    token0, token1 = _pool_token0_token1(pool_addr)
    drb_l = DRB_TOKEN.lower()
    if drb_l == token0:
        drb_is_token0 = True
    elif drb_l == token1:
        drb_is_token0 = False
    else:
        raise RuntimeError(f"DRB is not token0/token1 of pool {pool_addr}")

    drb_dec = erc20_decimals(DRB_TOKEN)
    quote_dec = erc20_decimals(quote_token)

    by_hash = {}
    for log in _fetch_pool_swap_logs(pool_addr, start_block, latest_block):
        try:
            topics = log.get("topics") or []
            if len(topics) < 3:
                continue
            t0 = str(topics[0]).lower()
            data = str(log.get("data") or "0x")
            data = data[2:] if data.startswith("0x") else data
            words = [data[i:i + 64] for i in range(0, len(data) - 63, 64)]

            if t0 == V2_SWAP_TOPIC:
                if len(words) < 4:
                    continue
                a0_in, a1_in, a0_out, a1_out = (int(w, 16) for w in words[:4])
                if drb_is_token0:
                    drb_out, quote_in = a0_out, a1_in
                else:
                    drb_out, quote_in = a1_out, a0_in
                recipient = _topic_addr(topics[2])  # `to`
            elif t0 == V3_SWAP_TOPIC:
                if len(words) < 2:
                    continue
                a0 = _int256(words[0])
                a1 = _int256(words[1])
                drb_amt = a0 if drb_is_token0 else a1
                quote_amt = a1 if drb_is_token0 else a0
                # buy = pool sends DRB out (negative) and receives quote (positive)
                drb_out = -drb_amt if drb_amt < 0 else 0
                quote_in = quote_amt if quote_amt > 0 else 0
                recipient = _topic_addr(topics[2])  # `recipient`
            else:
                continue

            if drb_out <= 0:
                continue
            to = recipient.lower()
            if to in exclude_to:
                continue

            h = str(log.get("transactionHash") or "")
            drb_f = drb_out / 10 ** drb_dec
            quote_f = quote_in / 10 ** quote_dec
            b = by_hash.get(h)
            if b:
                b["drb"] += drb_f
                b["quote"] += quote_f
            else:
                by_hash[h] = {"hash": h, "to": to, "drb": drb_f, "quote": quote_f,
                              "ts": int(str(log.get("blockNumber") or "0x0"), 16)}
        except Exception:
            continue

    return list(by_hash.values())


# Rendered /buys result cache: days -> {ts, text} (5 min)
_BUYS_RESULT_CACHE = {}
_BUYS_RESULT_TTL = 300


def build_biggest_buys_text(days: int, top_n: int = BUYS_TOP_N) -> str:
    """Build the 'Biggest Buys' HTML message for the last `days` days. Result cached 5 min."""
    capped = days > BUYS_MAX_DAYS
    if capped:
        days = BUYS_MAX_DAYS
    now = time.time()
    c = _BUYS_RESULT_CACHE.get(days)
    if c and (now - c["ts"]) < _BUYS_RESULT_TTL:
        return c["text"]

    pool = _get_main_pool()
    if not pool:
        raise RuntimeError("Could not resolve DRB pool from DexScreener")

    pool_addr = pool["pair"]
    quote_token = pool["quote"]
    quote_sym = pool["quote_symbol"]

    latest_block = int(_rpc_call("eth_blockNumber", []), 16)

    exclude_to = {
        pool_addr,
        CLAIM_CONTRACT.lower(),
        CLAIM_RECIPIENT.lower(),
        GROK_WALLET.lower(),
        "0x0000000000000000000000000000000000000000",
    }

    # Primary path: decode the pool's own Swap events via chunked eth_getLogs.
    buys = None
    try:
        swap_start = max(0, latest_block - days * BASE_BLOCKS_PER_DAY)
        buys = _pool_buys_via_swap_logs(pool_addr, quote_token, swap_start, latest_block, exclude_to)
    except Exception as e:
        print("swap-log buys scan failed, falling back to transfer scan:", repr(e))

    if buys is None:
        # Legacy fallback: indexer transfer scans on the pool address.
        start_block = max(0, latest_block - days * BASE_BLOCKS_PER_DAY - 2000)
        # Round down so the transfers-cache key stays stable between calls
        # (otherwise every call gets a new startblock and never hits the cache)
        start_block -= start_block % 50_000
        cutoff_ts = time.time() - days * 86400

        drb_txs = fetch_token_transfers(DRB_TOKEN, pool_addr, startblock=start_block)
        quote_txs = fetch_token_transfers(quote_token, pool_addr, startblock=start_block)

        # Quote token paid INTO the pool per tx hash (what the buyer paid)
        paid = {}
        for t in quote_txs:
            if (t.get("to") or "").lower() == pool_addr:
                h = t.get("hash")
                paid[h] = paid.get(h, 0.0) + _tx_amount(t)

        # DRB sent OUT of the pool = buys (exclude fee collections to locker/wallet)
        by_hash = {}
        for t in drb_txs:
            if (t.get("from") or "").lower() != pool_addr:
                continue
            to = (t.get("to") or "").lower()
            if to in exclude_to:
                continue
            ts = int(t.get("timeStamp") or 0)
            if ts < cutoff_ts:
                continue
            h = t.get("hash")
            b = by_hash.get(h)
            amt = _tx_amount(t)
            if b:
                b["drb"] += amt
            else:
                by_hash[h] = {"hash": h, "to": to, "drb": amt, "quote": paid.get(h, 0.0), "ts": ts}

        buys = list(by_hash.values())

    # Prices for USD valuation
    try:
        quote_price = fetch_price_usd(quote_token)
    except Exception:
        quote_price = 0.0
    try:
        drb_price = fetch_price_usd(DRB_TOKEN)
    except Exception:
        drb_price = 0.0

    for b in buys:
        if b["quote"] > 0 and quote_price > 0:
            b["usd"] = b["quote"] * quote_price
        else:
            b["usd"] = b["drb"] * drb_price

    buys.sort(key=lambda b: b["usd"], reverse=True)
    top = buys[:top_n]

    period_label = f"Last {days // 7}w" if days % 7 == 0 and days > 7 else f"Last {days}d"
    if capped:
        period_label += " (max 30d)"

    if not top:
        text = f"🏆 <b>Biggest Buys — {period_label}</b>\n\nNo buys found in this period."
        _BUYS_RESULT_CACHE[days] = {"ts": now, "text": text}
        return text

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = [f"🏆 <b>Biggest Buys — {period_label}</b>"]

    for i, b in enumerate(top):
        wallet_url = f"https://basescan.org/address/{b['to']}"
        tx_url = f"https://basescan.org/tx/{b['hash']}"
        quote_str = f" ({_fmt_sig(b['quote'])} {quote_sym})" if b["quote"] > 0 else ""
        lines.append("")
        lines.append(medals[i] if i < len(medals) else f"{i + 1}.")
        lines.append(f"💲 | <b>${b['usd']:,.2f}</b>{quote_str}")
        lines.append(f"🪙 | Got: <b>{_fmt_big(b['drb'])} DRB</b>")
        lines.append(f'👛 | <a href="{wallet_url}">{_short_addr_buys(b["to"])}</a> | <a href="{tx_url}">Txn</a>')

    top_total_usd = sum(b["usd"] for b in top)
    top_total_drb = sum(b["drb"] for b in top)
    over_1k = sum(1 for b in buys if b["usd"] >= 1000)
    summary = f"📊 | {len(buys)} buys"
    if over_1k:
        summary += f" · {over_1k} over $1K"
    lines.append("")
    lines.append(summary)
    lines.append("")
    lines.append(f"Top {len(top)} total: <b>{_fmt_big(top_total_drb)} DRB</b> · <b>${top_total_usd:,.2f}</b>")

    text = "\n".join(lines)
    _BUYS_RESULT_CACHE[days] = {"ts": now, "text": text}
    return text


async def buys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    days = _parse_stats_period(context.args, default_days=BUYS_DEFAULT_DAYS)

    try:
        text = await asyncio.get_event_loop().run_in_executor(None, build_biggest_buys_text, days)
        await msg.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        err = repr(e)
        print("buys_command error:", err)
        if ADMIN_ID > 0:
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"buys_command error: {err}")
            except Exception:
                pass
        await msg.reply_text("Error building biggest buys")


# ================= CLAIM FEES =================

CLAIM_COOLDOWN_SECONDS = 8 * 3600  # 8 hours between claims
_last_claim_ts = 0.0
_last_claim_tx = None
_last_claim_amounts = None  # {"weth": float, "drb": float}


def _load_last_claim() -> None:
    """Restore last claim info (ts/tx/amounts) from the state file on startup."""
    global _last_claim_ts, _last_claim_tx, _last_claim_amounts
    try:
        lc = (_load_state().get("cache") or {}).get("last_claim") or {}
        if lc.get("ts"):
            _last_claim_ts = float(lc["ts"])
            _last_claim_tx = lc.get("tx")
            if lc.get("weth") is not None:
                _last_claim_amounts = {"weth": float(lc.get("weth") or 0.0), "drb": float(lc.get("drb") or 0.0)}
    except Exception:
        pass


def _save_last_claim() -> None:
    try:
        lc = {"ts": _last_claim_ts, "tx": _last_claim_tx,
              "weth": (_last_claim_amounts or {}).get("weth"),
              "drb": (_last_claim_amounts or {}).get("drb")}
        _update_state_fields(lambda st: st["cache"].__setitem__("last_claim", lc))
    except Exception:
        pass

# Track per-user "not admin" warnings to avoid spam: {user_id: (last_warn_ts, msg_count_since)}
_non_admin_warn: dict = {}
NON_ADMIN_WARN_COOLDOWN = 3600   # 1 hour
NON_ADMIN_WARN_MSG_GAP  = 20     # or 20 messages since last warning

async def _is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if the message sender is an admin (or creator) of the chat."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return False
    # In private chats, only allow ADMIN_ID
    if msg.chat.type == "private":
        return user.id == ADMIN_ID
    try:
        member = await context.bot.get_chat_member(msg.chat_id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def _transfer_topic_hex() -> str:
    t = Web3.keccak(text="Transfer(address,address,uint256)").hex()
    return t[2:] if t.startswith("0x") else t


_CLAIM_EST_CACHE = {"ts": 0, "data": None}
_CLAIM_EST_TTL = 180  # 3 min


def _estimate_pending_rewards():
    """
    Simulate the claimRewards tx (eth_simulateV1) and parse the Transfer logs
    to estimate how much WETH/DRB a claim would send to the Grok wallet right now.
    Returns dict {weth, drb, weth_usd, drb_usd} or None if simulation is unavailable.
    Cached 3 min; invalidated after a successful claim.
    """
    now = time.time()
    if _CLAIM_EST_CACHE["data"] is not None and (now - _CLAIM_EST_CACHE["ts"]) < _CLAIM_EST_TTL:
        return _CLAIM_EST_CACHE["data"]

    try:
        selector = Web3.keccak(text="claimRewards(address)")[:4].hex()
        if not selector.startswith("0x"):
            selector = "0x" + selector
        data = selector + _pad32_hex_address(DRB_TOKEN)

        from_addr = GROK_WALLET
        if CLAIM_PRIVATE_KEY:
            try:
                from_addr = Web3().eth.account.from_key(CLAIM_PRIVATE_KEY).address
            except Exception:
                pass

        # eth_simulateV1 works on both Alchemy and the public Base RPC and returns logs
        result = _rpc_call("eth_simulateV1", [
            {"blockStateCalls": [{"calls": [{
                "from": from_addr,
                "to": Web3.to_checksum_address(CLAIM_CONTRACT),
                "data": data,
            }]}]},
            "latest",
        ])
        call = ((result or [{}])[0].get("calls") or [{}])[0]
        status = str(call.get("status") or "").lower()
        if status not in ("0x1", "1"):
            raise RuntimeError(f"simulation reverted: {call.get('error')}")
        logs = call.get("logs") or []

        transfer_topic = _transfer_topic_hex()
        weth_amt = 0.0
        drb_amt = 0.0

        for log in logs:
            topics = log.get("topics") or []
            if len(topics) < 3:
                continue
            t0 = str(topics[0]).lower()
            t0 = t0[2:] if t0.startswith("0x") else t0
            if t0 != transfer_topic:
                continue

            token_addr = str(log.get("address") or "").lower()
            if token_addr not in (WETH_TOKEN.lower(), DRB_TOKEN.lower()):
                continue

            t2 = str(topics[2]).lower()
            t2 = t2[2:] if t2.startswith("0x") else t2
            to_addr = "0x" + t2[-40:]
            if to_addr != GROK_WALLET.lower():
                continue

            raw_value = int(str(log.get("data") or "0x0"), 16)

            if token_addr == WETH_TOKEN.lower():
                weth_amt += raw_value / 10 ** 18
            else:
                drb_amt += raw_value / 10 ** 18

        out = {"weth": weth_amt, "drb": drb_amt, "weth_usd": None, "drb_usd": None}
        try:
            out["weth_usd"] = weth_amt * fetch_price_usd(WETH_TOKEN)
        except Exception:
            pass
        try:
            out["drb_usd"] = drb_amt * fetch_price_usd(DRB_TOKEN)
        except Exception:
            pass
        _CLAIM_EST_CACHE["ts"] = now
        _CLAIM_EST_CACHE["data"] = out
        return out

    except Exception as e:
        print("claim preview simulation error:", repr(e))
        return None


def _do_claim_tx() -> dict:
    """
    Execute claimRewards on-chain.
    Returns dict with keys: tx_hash, weth_claimed, drb_claimed (floats).
    Raises on failure.
    """
    if not CLAIM_PRIVATE_KEY:
        raise RuntimeError("CLAIM_PRIVATE_KEY env variable not set")

    w3 = Web3(Web3.HTTPProvider(BASE_FALLBACK_RPC_URL))
    if not w3.is_connected():
        raise RuntimeError("Cannot connect to Base RPC")

    account = w3.eth.account.from_key(CLAIM_PRIVATE_KEY)
    print(f"[claim] Signer address: {account.address}")
    balance = w3.eth.get_balance(account.address)
    print(f"[claim] Signer ETH balance: {balance / 10**18:.6f} ETH")

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(CLAIM_CONTRACT),
        abi=CLAIM_ABI,
    )
    nonce = w3.eth.get_transaction_count(account.address, "pending")
    token_cs = Web3.to_checksum_address(DRB_TOKEN)

    tx = contract.functions.claimRewards(token_cs).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": 300_000,
        "gasPrice": w3.to_wei(0.02, "gwei"),
        "value": 0,
        "chainId": 8453,
    })

    signed = w3.eth.account.sign_transaction(tx, CLAIM_PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status != 1:
        raise RuntimeError("Transaction reverted")

    # Parse Transfer events — sum all WETH and DRB transfers in this tx
    weth_claimed = 0.0
    drb_claimed = 0.0

    # ERC-20 Transfer topic (without 0x prefix for comparison)
    transfer_topic = Web3.keccak(text="Transfer(address,address,uint256)").hex()
    if transfer_topic.startswith("0x"):
        transfer_topic = transfer_topic[2:]

    for log in receipt.logs:
        if len(log["topics"]) < 3:
            continue
        topic0 = log["topics"][0].hex()
        if topic0.startswith("0x"):
            topic0 = topic0[2:]
        if topic0 != transfer_topic:
            continue

        token_addr = log["address"].lower()
        if token_addr not in (WETH_TOKEN.lower(), DRB_TOKEN.lower()):
            continue

        to_addr = "0x" + log["topics"][2].hex().lstrip("0x").zfill(40)[-40:]
        if to_addr.lower() != GROK_WALLET.lower():
            continue

        data = log["data"]
        if isinstance(data, (bytes, bytearray)):
            raw_value = int(data.hex(), 16)
        else:
            raw_value = int(data, 16)

        if token_addr == WETH_TOKEN.lower():
            weth_claimed += raw_value / 10 ** 18
        elif token_addr == DRB_TOKEN.lower():
            drb_claimed += raw_value / 10 ** 18

    return {
        "tx_hash": tx_hash.hex(),
        "weth_claimed": weth_claimed,
        "drb_claimed": drb_claimed,
    }


def _fmt_sig(amount: float, suffix: str = "") -> str:
    """Format a float showing at least 2 decimals, or up to first significant digit if < 0.01."""
    if amount == 0:
        return f"0.00{suffix}"
    if abs(amount) >= 0.01:
        return f"{amount:.2f}{suffix}"
    # Find first significant digit and show 2 sig digits after it
    import math
    mag = -int(math.floor(math.log10(abs(amount)))) + 1
    decimals = max(2, mag)
    return f"{amount:.{decimals}f}{suffix}"


def _fmt_drb_millions(amount: float) -> str:
    """Format DRB: uses M suffix if >= 1M, otherwise shows as comma-separated integer."""
    if amount == 0:
        return "0.00M"
    if amount >= 1_000_000:
        return _fmt_sig(amount / 1_000_000, "M")
    # Less than 1M: show as integer with thousands separator
    if amount >= 1:
        return f"{amount:,.0f}"
    return _fmt_sig(amount)


def _fmt_weth(amount: float) -> str:
    """Format WETH amount showing at least first significant digit."""
    return _fmt_sig(amount)


async def claim_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _non_admin_warn
    msg = update.effective_message
    if not msg:
        return

    user = update.effective_user
    if not await _is_group_admin(update, context):
        now = time.time()
        uid = user.id if user else 0
        warn = _non_admin_warn.get(uid)
        # Only warn if enough time has passed OR enough messages since last warning
        msg_count = context.chat_data.get("msg_count", 0)
        should_warn = (
            warn is None
            or (now - warn[0]) >= NON_ADMIN_WARN_COOLDOWN
            or (msg_count - warn[1]) >= NON_ADMIN_WARN_MSG_GAP
        )
        if should_warn:
            _non_admin_warn[uid] = (now, msg_count)
            await msg.reply_text("⛔ Only group admins can use this command.")
        return

    # Cooldown check
    now = time.time()
    elapsed = now - _last_claim_ts
    if _last_claim_ts > 0 and elapsed < CLAIM_COOLDOWN_SECONDS:
        remaining_h = (CLAIM_COOLDOWN_SECONDS - elapsed) / 3600
        amounts_line = ""
        if _last_claim_amounts:
            amounts_line = (
                f"\nLast claim: <b>{_fmt_weth(_last_claim_amounts['weth'])} WETH</b>"
                f" · <b>{_fmt_drb_millions(_last_claim_amounts['drb'])} DRB</b>"
            )
        tx_line = ""
        if _last_claim_tx:
            tx_line = f'\n🔗 <a href="https://basescan.org/tx/{_last_claim_tx}">Last claim tx</a>'
        await msg.reply_text(
            f"⏳ Too soon! Next claim available in <b>{remaining_h:.1f}h</b>.{amounts_line}{tx_line}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    # Estimate how much a claim would collect right now (tx simulation)
    loading = await msg.reply_text("🔎 Estimating pending fees...")
    est = await asyncio.get_event_loop().run_in_executor(None, _estimate_pending_rewards)

    if est and (est["weth"] > 0 or est["drb"] > 0):
        weth_usd = f" (~{_fmt_int_usd(est['weth_usd'])})" if est.get("weth_usd") else ""
        drb_usd = f" (~{_fmt_int_usd(est['drb_usd'])})" if est.get("drb_usd") else ""
        est_lines = (
            "Estimated rewards to collect:\n"
            f"• <b>{_fmt_weth(est['weth'])} WETH</b>{weth_usd}\n"
            f"• <b>{_fmt_drb_millions(est['drb'])} DRB</b>{drb_usd}\n\n"
        )
    elif est:
        est_lines = "Estimated rewards to collect: <i>nothing pending right now</i>\n\n"
    else:
        est_lines = "⚠️ Could not estimate pending rewards.\n\n"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ CLAIM", callback_data="claim_confirm"),
            InlineKeyboardButton("❌ CANCEL", callback_data="claim_cancel"),
        ]
    ])
    await loading.edit_text(
        "💰 <b>Claim Trading Fees</b>\n\n"
        + est_lines +
        "Do you want to claim the accumulated trading fees from the contract?\n\n"
        "This will send a transaction on Base mainnet.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    # Admin check on the callback user
    if not await _is_group_admin(update, context):
        await query.answer("⛔ You are not an admin.", show_alert=True)
        return

    await query.answer()

    if query.data == "claim_cancel":
        await query.edit_message_text("❌ Claim cancelled.")
        return

    # claim_confirm
    await query.edit_message_text("⏳ Sending claim transaction, please wait...")

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _do_claim_tx)

        tx_url = f"https://basescan.org/tx/{result['tx_hash']}"
        weth = result["weth_claimed"]
        drb = result["drb_claimed"]

        global _last_claim_ts, _last_claim_tx, _last_claim_amounts
        _last_claim_ts = time.time()
        _last_claim_tx = result["tx_hash"]
        _last_claim_amounts = {"weth": float(weth), "drb": float(drb)}
        _save_last_claim()

        # Invalidate the pending-rewards estimate cache (it just changed)
        _CLAIM_EST_CACHE["ts"] = 0
        _CLAIM_EST_CACHE["data"] = None

        text = (
            "✅ <b>Fees claimed successfully!</b>\n\n"
            f"{_fmt_weth(weth)} WETH\n"
            f"{_fmt_drb_millions(drb)} DRB\n\n"
            f'🔗 <a href="{tx_url}">View transaction</a>'
        )
        await query.edit_message_text(text, parse_mode="HTML", disable_web_page_preview=True)

    except Exception as e:
        err = repr(e)
        print("claim_callback error:", err)
        await query.edit_message_text(
            f"❌ <b>Claim failed</b>\n\n<code>{err}</code>",
            parse_mode="HTML",
        )



# ================= BUY ALERTS (ported from CLAWD bot) =================
#
# Detects DRB buys from tx receipts (net ERC-20 deltas per address), with the same
# false-positive filters as the CLAWD bot:
#   - final DRB receiver must be an EOA (not a contract / pool / locker / grok wallet)
#   - payment path: native ETH (tx.value) > USDC/USDT outflow > WETH outflow
#   - payer must be tx.from (or an EOA when routed through an aggregator/relayer)
#   - coherence filter: stable-paid buys must be within [0.10x, 8x] of price*tokens
#   - ETH valued with Chainlink ETH/USD at the tx block (live fallback for realtime)
#   - dedup by tx hash (seen + sent lists persisted on disk), max event age filter

# ---- State (persisted JSON) ----

DEFAULT_STATE = {
    "min_usd": {"buy": DEFAULT_MIN_BUY_USD},
    "emoji_usd": {"buy": DEFAULT_EMOJI_USD},
    "alerts_dm": True,
    "blacklist": [],    # user ids blocked for spamming (auto; admin can unblock)
    "alert_chats": [],  # groups/channels where the bot has been added (auto-registered)
    "watch": {
        "last_scanned_block": 0,
        "seen": {"buy": []},
        "sent_public": {"buy": []},
        "sent_dm": {"buy": []},
    },
    "cache": {"token_price_usd": None},
}


def _ensure_data_dir() -> None:
    try:
        os.makedirs(DATA_PATH, exist_ok=True)
    except Exception:
        pass


def _load_state() -> dict:
    _ensure_data_dir()
    merged = json.loads(json.dumps(DEFAULT_STATE))
    if not os.path.exists(STATE_PATH):
        return merged
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict):
            return merged
        for k in ("min_usd", "emoji_usd", "cache"):
            if isinstance(s.get(k), dict):
                merged[k].update(s[k])
        if "alerts_dm" in s:
            merged["alerts_dm"] = bool(s["alerts_dm"])
        if isinstance(s.get("alert_chats"), list):
            merged["alert_chats"] = [int(c) for c in s["alert_chats"] if str(c).lstrip("-").isdigit()]
        if isinstance(s.get("blacklist"), list):
            merged["blacklist"] = [int(u) for u in s["blacklist"] if str(u).isdigit()]
        w = s.get("watch") or {}
        if isinstance(w, dict):
            merged["watch"]["last_scanned_block"] = int(w.get("last_scanned_block") or 0)
            for k in ("seen", "sent_public", "sent_dm"):
                if isinstance(w.get(k), dict):
                    merged["watch"][k]["buy"] = list(w[k].get("buy") or [])
        return merged
    except Exception:
        return merged


def _save_state(state: dict) -> None:
    _ensure_data_dir()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _update_state_fields(mutator) -> dict:
    """Load a FRESH state, apply mutator, save. Never save a long-held state object
    directly: a stale snapshot would clobber a concurrent /setmin."""
    fresh = _load_state()
    mutator(fresh)
    _save_state(fresh)
    return fresh


def _prune_seen(arr: list) -> list:
    if len(arr) <= WATCH_MAX_SEEN_EVENTS:
        return arr
    return arr[-WATCH_MAX_SEEN_EVENTS:]


# ---- RPC helpers ----

def _norm(a: str) -> str:
    return (a or "").lower()


def _rpc_batch(calls: list, _min_chunk: int = 10) -> list:
    """JSON-RPC batch (list of (method, params)); results in the same order.
    On failure splits into halves down to _min_chunk, then falls back to serial calls."""
    if not calls:
        return []
    payloads = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
    last_err = None
    for url in [ALCHEMY_RPC_URL, BASE_FALLBACK_RPC_URL]:
        try:
            r = requests.post(url, json=payloads, headers=UA_HEADERS, timeout=30)
            r.raise_for_status()
            raw = r.json()
            if not isinstance(raw, list):
                raise RuntimeError("batch response is not a list")
            by_id = {it.get("id"): it.get("result") for it in raw if isinstance(it, dict)}
            return [by_id.get(i) for i in range(len(calls))]
        except Exception as e:
            last_err = e
            continue
    print(f"[rpc_batch] batch of {len(calls)} failed: {last_err!r}")
    if len(calls) > max(1, _min_chunk):
        mid = len(calls) // 2
        return _rpc_batch(calls[:mid], _min_chunk) + _rpc_batch(calls[mid:], _min_chunk)
    out = []
    for m, p in calls:
        try:
            out.append(_rpc_call(m, p))
        except Exception:
            out.append(None)
    return out


def _get_latest_block() -> int:
    return int(_rpc_call("eth_blockNumber", []), 16)


def _get_receipt(tx_hash: str):
    return _rpc_call("eth_getTransactionReceipt", [tx_hash])


def _get_tx(tx_hash: str):
    return _rpc_call("eth_getTransactionByHash", [tx_hash])


_IS_CONTRACT_CACHE = {}


def _is_contract(addr: str, block_number=None) -> bool:
    key = _norm(addr)
    if key in _IS_CONTRACT_CACHE:
        return _IS_CONTRACT_CACHE[key]
    try:
        tag = hex(int(block_number)) if block_number is not None else "latest"
        code = _rpc_call("eth_getCode", [addr, tag])
        result = isinstance(code, str) and code not in ("0x", "0x0", "")
        _IS_CONTRACT_CACHE[key] = result
        return result
    except Exception:
        return False  # if in doubt, do not classify as contract


def _get_token_logs_chunked(token: str, from_block: int, to_block: int) -> list:
    """All Transfer logs of `token` in [from_block, to_block], chunked eth_getLogs."""
    logs = []
    cur = from_block
    while cur <= to_block:
        end = min(to_block, cur + max(1, WATCH_LOG_CHUNK) - 1)
        chunk = _rpc_call("eth_getLogs", [{
            "fromBlock": hex(cur),
            "toBlock": hex(end),
            "address": token,
            "topics": [TRANSFER_TOPIC0],
        }])
        logs.extend(chunk or [])
        cur = end + 1
    return logs


_BLOCK_TS_CACHE = {}


def _get_block_timestamp(block_number: int):
    if block_number in _BLOCK_TS_CACHE:
        return _BLOCK_TS_CACHE[block_number]
    try:
        blk = _rpc_call("eth_getBlockByNumber", [hex(block_number), False])
        ts = int(blk.get("timestamp"), 16)
        _BLOCK_TS_CACHE[block_number] = ts
        if len(_BLOCK_TS_CACHE) > 2000:
            for k in sorted(_BLOCK_TS_CACHE)[:500]:
                _BLOCK_TS_CACHE.pop(k, None)
        return ts
    except Exception:
        return None


def _event_is_too_old(block_number) -> bool:
    if MAX_EVENT_AGE_SEC <= 0 or block_number is None:
        return False
    ts = _get_block_timestamp(int(block_number))
    if not ts:
        return False
    return (int(time.time()) - int(ts)) > MAX_EVENT_AGE_SEC


# ---- ETH/USD pricing (Chainlink at block, live fallback) ----

_CL_DECIMALS = None
_CL_BLOCK_BUCKET = 30
_CL_PRICE_CACHE = {}


def _chainlink_eth_usd_at_block(block_number: int):
    global _CL_DECIMALS
    try:
        bucket = int(block_number) // _CL_BLOCK_BUCKET
        if bucket in _CL_PRICE_CACHE:
            return _CL_PRICE_CACHE[bucket]
        tag = hex(int(block_number))
        if _CL_DECIMALS is None:
            _CL_DECIMALS = int(_rpc_call("eth_call", [{"to": CHAINLINK_ETH_USD_FEED, "data": "0x313ce567"}, tag]), 16)
        data = _rpc_call("eth_call", [{"to": CHAINLINK_ETH_USD_FEED, "data": "0xfeaf968c"}, tag])  # latestRoundData()
        raw = data[2:] if isinstance(data, str) and data.startswith("0x") else ""
        if len(raw) < 64 * 5:
            return None
        answer = _int256(raw[64:128])
        if answer <= 0:
            return None
        price = answer / 10 ** _CL_DECIMALS
        _CL_PRICE_CACHE[bucket] = price
        if len(_CL_PRICE_CACHE) > 5000:
            _CL_PRICE_CACHE.clear()
        return price
    except Exception:
        return None


def _eth_usd_price(block_number=None, allow_live_fallback: bool = True):
    """1) Chainlink ETH/USD at the tx block. 2) Chainlink latest. 3) DexScreener WETH (live only)."""
    if block_number is not None:
        px = _chainlink_eth_usd_at_block(int(block_number))
        if px and px > 0:
            return float(px)
        if not allow_live_fallback:
            return None
    try:
        latest = _get_latest_block()
        px = _chainlink_eth_usd_at_block(latest)
        if px and px > 0:
            return float(px)
    except Exception:
        pass
    if allow_live_fallback:
        try:
            return float(fetch_price_usd(WETH_TOKEN))
        except Exception:
            pass
    return None


def _drb_price_cached():
    """DRB price from DexScreener with persisted fallback in state cache."""
    try:
        price = float(fetch_price_usd(DRB_TOKEN))
        _update_state_fields(lambda s: s["cache"].__setitem__("token_price_usd", price))
        return price
    except Exception:
        p = (_load_state().get("cache") or {}).get("token_price_usd")
        return float(p) if p else 0.0


# ---- Receipt analysis ----

def _aggregate_net_deltas_from_receipt(receipt: dict, tokens: list) -> dict:
    deltas = {_norm(t): {} for t in tokens}
    for lg in receipt.get("logs", []) or []:
        addr = _norm(lg.get("address", ""))
        if addr not in deltas:
            continue
        topics = lg.get("topics") or []
        if len(topics) < 3 or _norm(topics[0]) != TRANSFER_TOPIC0:
            continue
        frm = _topic_addr(topics[1])
        to = _topic_addr(topics[2])
        try:
            v = int(lg.get("data", "0x0"), 16)
        except Exception:
            continue
        d = deltas[addr]
        d[frm] = d.get(frm, 0) - v
        d[to] = d.get(to, 0) + v
    return deltas


def _pick_final_buyer(token_deltas: dict, exclude: set):
    best_addr, best_delta = None, 0
    for addr, delta in token_deltas.items():
        if addr in exclude:
            continue
        if delta > best_delta:
            best_delta, best_addr = delta, addr
    return best_addr


def _pick_final_seller(token_deltas: dict, exclude: set):
    best_addr, best_out = None, 0
    for addr, delta in token_deltas.items():
        if addr in exclude:
            continue
        if delta < 0 and -delta > best_out:
            best_out, best_addr = -delta, addr
    return best_addr


def _max_outflow_addr(d: dict):
    best_addr, best_out = None, 0
    for addr, v in (d or {}).items():
        if v < 0 and -v > best_out:
            best_out, best_addr = -v, addr
    return best_addr, best_out


def _max_inflow_addr(d: dict):
    best_addr, best_in = None, 0
    for addr, v in (d or {}).items():
        if v > 0 and v > best_in:
            best_in, best_addr = v, addr
    return best_addr, best_in


def _buy_exclude_set() -> set:
    ex = {DRB_TOKEN, WETH_TOKEN, USDC_TOKEN, USDT_TOKEN, BURN_ADDRESS, ZERO_ADDRESS,
          GROK_WALLET, CLAIM_CONTRACT, CLAIM_RECIPIENT}
    try:
        pool = _get_main_pool()
        if pool:
            ex.add(pool["pair"])
    except Exception:
        pass
    return {_norm(a) for a in ex}


def _receipt_block_number(receipt: dict):
    try:
        bn = receipt.get("blockNumber")
        return int(bn, 16) if isinstance(bn, str) and bn.startswith("0x") else None
    except Exception:
        return None


def _payer_ok(payer: str, tx_from: str, block_number, buyer: str = "") -> bool:
    """Anti false-positive rule from CLAWD:
    - the buyer itself paying is always fine (smart wallet paying with its own funds)
    - tx.from is an EOA: payer must be tx.from, or at least another EOA (aggregator path).
    - tx.from is a contract (relayer/router): payer must be an EOA."""
    if buyer and _norm(payer) == _norm(buyer):
        return True
    if not tx_from:
        return True
    if not _is_contract(tx_from, block_number):
        if _norm(payer) != _norm(tx_from) and _is_contract(payer, block_number):
            return False
    else:
        if _is_contract(payer, block_number):
            return False
    return True


def _buy_from_receipt(tx_hash: str, receipt: dict, allow_live_eth_fallback: bool = False):
    """Return {buyer, usd, tokens, eth, pay:{eth,usdc,usdt,weth}} or None."""
    if not receipt or str(receipt.get("status", "0x1")).lower() not in ("0x1", "1"):
        return None
    block_number = _receipt_block_number(receipt)

    deltas = _aggregate_net_deltas_from_receipt(receipt, [DRB_TOKEN, USDC_TOKEN, USDT_TOKEN, WETH_TOKEN])
    tdel = deltas.get(_norm(DRB_TOKEN)) or {}
    if not tdel:
        return None

    exclude = _buy_exclude_set()
    buyer = _pick_final_buyer(tdel, exclude)
    if not buyer:
        return None

    tx_from, tx_to, eth_value = "", "", 0
    try:
        tx = _get_tx(tx_hash)
        tx_from = _norm(tx.get("from", ""))
        tx_to = _norm(tx.get("to", "") or "")
        eth_value = int(tx.get("value", "0x0"), 16)
    except Exception:
        pass

    # Final receiver must be a person: an EOA, or a smart-contract wallet.
    # Accepted contract-buyer cases:
    #   a) buyer == tx.from  (account-abstraction wallet sending its own tx)
    #   b) buyer == tx.to    (owner EOA executing their own smart wallet)
    #   c) third-party smart wallet funded via an intent/solver settlement
    #      (relayer sends the tx, DRB lands on the user's wallet). For (c) we
    #      demand a strict coherence check later so pools/lockers never pass.
    third_party_contract_buyer = False
    if _is_contract(buyer, block_number) and _norm(buyer) not in (tx_from, tx_to):
        third_party_contract_buyer = True

    tokens_delta = int(tdel.get(buyer, 0))
    if tokens_delta <= 0:
        return None
    tokens_bought = tokens_delta / 10 ** erc20_decimals(DRB_TOKEN)

    price = _drb_price_cached()
    usd_est = price * tokens_bought

    usdc_del = deltas.get(_norm(USDC_TOKEN)) or {}
    usdt_del = deltas.get(_norm(USDT_TOKEN)) or {}
    weth_del = deltas.get(_norm(WETH_TOKEN)) or {}
    payer_usdc, usdc_out = _max_outflow_addr(usdc_del)
    payer_usdt, usdt_out = _max_outflow_addr(usdt_del)
    payer_weth, weth_out = _max_outflow_addr(weth_del)

    spent_usd = 0.0
    eth_spent = usdc_spent = usdt_spent = weth_spent = 0.0

    paid_with_eth = eth_value > 0
    if paid_with_eth:
        # Native ETH buy: value ONLY the ETH, ignore stable movements inside the tx
        eth_spent = eth_value / 10 ** 18
        wp = _eth_usd_price(block_number, allow_live_eth_fallback)
        if not wp:
            return None
        spent_usd = eth_spent * wp
    else:
        if usdc_out > 0 or usdt_out > 0:
            payer = payer_usdc if usdc_out >= usdt_out else payer_usdt
            if payer:
                if not _payer_ok(payer, tx_from, block_number, buyer):
                    return None
                usdc_spent = max(0, -usdc_del.get(payer, 0)) / 10 ** 6
                usdt_spent = max(0, -usdt_del.get(payer, 0)) / 10 ** 6
                spent_usd = usdc_spent + usdt_spent

        if spent_usd <= 0 and weth_out > 0 and payer_weth:
            if not _payer_ok(payer_weth, tx_from, block_number, buyer):
                return None
            wp = _eth_usd_price(block_number, allow_live_eth_fallback)
            if not wp:
                return None
            weth_spent = max(0, -weth_del.get(payer_weth, 0)) / 10 ** 18
            spent_usd = weth_spent * wp
            eth_spent = weth_spent

        if spent_usd <= 0:
            return None

    paid_with_weth = weth_spent > 0
    # Coherence filter (stable-paid buys only; ETH/WETH price estimates can drift)
    if not paid_with_eth and not paid_with_weth and usd_est > 0:
        if spent_usd < usd_est * 0.10 or spent_usd > usd_est * 8.0:
            return None
    # Third-party contract buyers (intent/solver settlements) must ALWAYS pass a
    # strict coherence check, whatever the payment token — this is what keeps
    # random contracts receiving DRB from ever counting as buys.
    if third_party_contract_buyer:
        if usd_est <= 0 or spent_usd < usd_est * 0.5 or spent_usd > usd_est * 2.0:
            return None

    return {
        "buyer": buyer,
        "usd": float(spent_usd),
        "tokens": float(tokens_bought),
        "eth": float(eth_spent),
        "pay": {"eth": float(eth_spent), "usdc": float(usdc_spent), "usdt": float(usdt_spent), "weth": float(weth_spent)},
    }


def _sell_from_receipt(tx_hash: str, receipt: dict):
    """Used only to mark sells as seen (so they are not re-processed). Returns dict or None."""
    if not receipt:
        return None
    block_number = _receipt_block_number(receipt)
    deltas = _aggregate_net_deltas_from_receipt(receipt, [DRB_TOKEN, USDC_TOKEN, USDT_TOKEN, WETH_TOKEN])
    tdel = deltas.get(_norm(DRB_TOKEN)) or {}
    if not tdel:
        return None
    seller = _pick_final_seller(tdel, _buy_exclude_set())
    if not seller:
        return None
    # The seller must be a person (EOA) or the smart wallet sending the tx. Pools,
    # routers and PoolManagers moving DRB internally are not sellers.
    tx_from, tx_to = "", ""
    try:
        _tx = _get_tx(tx_hash) or {}
        tx_from = _norm(_tx.get("from", ""))
        tx_to = _norm(_tx.get("to", "") or "")
    except Exception:
        pass
    if _is_contract(seller, block_number) and _norm(seller) not in (tx_from, tx_to):
        return None
    tokens_sold = -int(tdel.get(seller, 0)) / 10 ** erc20_decimals(DRB_TOKEN)
    if tokens_sold <= 0:
        return None
    usd_est = _drb_price_cached() * tokens_sold

    usdc_del = deltas.get(_norm(USDC_TOKEN)) or {}
    usdt_del = deltas.get(_norm(USDT_TOKEN)) or {}
    weth_del = deltas.get(_norm(WETH_TOKEN)) or {}
    r_usdc, usdc_in = _max_inflow_addr(usdc_del)
    r_usdt, usdt_in = _max_inflow_addr(usdt_del)
    r_weth, weth_in = _max_inflow_addr(weth_del)

    got_usd = 0.0
    if usdc_in > 0 or usdt_in > 0:
        recv = r_usdc if usdc_in >= usdt_in else r_usdt
        if recv:
            got_usd += max(0, usdc_del.get(recv, 0)) / 10 ** 6
            got_usd += max(0, usdt_del.get(recv, 0)) / 10 ** 6
    if got_usd <= 0 and weth_in > 0 and r_weth:
        wp = _eth_usd_price(block_number, allow_live_fallback=False) or 0.0
        got_usd += max(0, weth_del.get(r_weth, 0)) / 10 ** 18 * wp
    if got_usd <= 0:
        return None
    if usd_est > 0 and (got_usd < usd_est * 0.20 or got_usd > usd_est * 5.0):
        return None
    return {"seller": seller, "usd": float(got_usd), "tokens": float(tokens_sold)}


# ---- Alert formatting / sending ----

def _emoji_bar(total_usd: float, usd_per_emoji: float) -> str:
    if usd_per_emoji <= 0:
        usd_per_emoji = 100.0
    n = int(total_usd / usd_per_emoji)
    return BUY_EMOJI * max(1, min(n, MAX_EMOJIS))


def _payment_line(pay: dict) -> str:
    if not pay:
        return ""
    lines = []
    if pay.get("eth", 0) > 0:
        lines.append(f"ETH: {_fmt_sig(pay['eth'])}")
    if pay.get("usdc", 0) > 0:
        lines.append(f"USDC: {int(round(pay['usdc'])):,}")
    if pay.get("usdt", 0) > 0:
        lines.append(f"USDT: {int(round(pay['usdt'])):,}")
    return ("\n".join(lines) + "\n") if lines else ""


def _buy_caption(tx_hash: str, tokens: float, usd: float, buyer: str, pay: dict = None) -> str:
    state = _load_state()
    bar = _emoji_bar(usd, float(state["emoji_usd"]["buy"]))
    tx_url = f"https://basescan.org/tx/{tx_hash}"
    wallet_url = f"https://basescan.org/address/{buyer}"
    return (
        "<b>DRB BOUGHT!</b>\n\n"
        f"{bar}\n\n"
        f'DRB: {_fmt_big(tokens)} ({_fmt_int_usd(usd)}) (<a href="{tx_url}">Tx</a>)\n'
        + _payment_line(pay)
        + f'Wallet: <a href="{wallet_url}">{_short_addr_dots(buyer)}</a>'
    )


async def _send_buy_alert(app, chat_id: int, caption: str) -> None:
    if ASSET_BUY and os.path.exists(ASSET_BUY):
        with open(ASSET_BUY, "rb") as f:
            await app.bot.send_photo(chat_id=chat_id, photo=f, caption=caption, parse_mode="HTML")
    else:
        await app.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", disable_web_page_preview=True)


# ---- Alert chats: auto-register every group the bot is added to ----

def _alert_chat_ids() -> list:
    """Groups to post buy alerts in: auto-registered chats + ALLOWED_CHAT_ID (if set)."""
    ids = set(_load_state().get("alert_chats") or [])
    if ALLOWED_CHAT_ID:
        ids.add(ALLOWED_CHAT_ID)
    return sorted(ids)


def _register_alert_chat(chat_id: int, add: bool = True) -> None:
    def _m(s):
        cur = set(int(c) for c in (s.get("alert_chats") or []))
        if add:
            cur.add(int(chat_id))
        else:
            cur.discard(int(chat_id))
        s["alert_chats"] = sorted(cur)
    _update_state_fields(_m)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot added to / removed from a group -> register / unregister it for alerts."""
    cm = update.my_chat_member
    if not cm or not cm.chat or cm.chat.type not in ("group", "supergroup", "channel"):
        return
    status = cm.new_chat_member.status
    if status in ("member", "administrator", "restricted"):
        _register_alert_chat(cm.chat.id, True)
        print(f"[alerts] registered chat {cm.chat.id} ({cm.chat.title})")
    elif status in ("left", "kicked"):
        _register_alert_chat(cm.chat.id, False)
        print(f"[alerts] unregistered chat {cm.chat.id}")


async def _register_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback: any message seen in a group registers that group (covers bots added before this version)."""
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        if chat.id not in set(_load_state().get("alert_chats") or []):
            _register_alert_chat(chat.id, True)
            print(f"[alerts] registered chat {chat.id} ({chat.title}) from message")


# ---- Monitor loop ----

def _monitor_tick_sync() -> list:
    """Scan new blocks for DRB buys. Returns [(buy_id, caption), ...] above min_usd."""
    state = _load_state()
    latest = _get_latest_block()
    end = max(0, latest - max(0, WATCH_CONFIRMATIONS))
    last_scanned = int(state["watch"].get("last_scanned_block") or 0)
    start = max(0, end - 5) if last_scanned <= 0 else max(0, last_scanned - WATCH_OVERLAP_BLOCKS)
    if end < start:
        return []

    logs = _get_token_logs_chunked(DRB_TOKEN, start, end)
    seen_buy = set(state["watch"]["seen"].get("buy") or [])
    min_buy = float(state["min_usd"]["buy"])
    token_price = _drb_price_cached()
    drb_dec = erc20_decimals(DRB_TOKEN)

    # Prefilter: estimate DRB moved per tx so tiny txs never need a receipt fetch
    txs, tx_value_est = [], {}
    for lg in logs:
        h = lg.get("transactionHash")
        if not h:
            continue
        try:
            topics = lg.get("topics") or []
            if len(topics) >= 3 and _norm(topics[0]) == TRANSFER_TOPIC0:
                tx_value_est[h] = tx_value_est.get(h, 0.0) + int(lg.get("data", "0x0"), 16) / 10 ** drb_dec
        except Exception:
            pass
        if h not in txs:
            txs.append(h)

    need = []
    for h in txs:
        if f"buy:{h}" in seen_buy:
            continue
        if token_price > 0 and tx_value_est.get(h, 0.0) * token_price < min_buy * BUY_RECEIPT_PREFILTER_PCT:
            continue
        need.append(h)

    receipts = {}
    for i in range(0, len(need), 75):
        chunk = need[i:i + 75]
        try:
            res = _rpc_batch([("eth_getTransactionReceipt", [h]) for h in chunk])
        except Exception:
            res = [None] * len(chunk)
        receipts.update(dict(zip(chunk, res)))

    outgoing = []
    for h in need:
        buy_id = f"buy:{h}"
        try:
            receipt = receipts.get(h)
            if receipt is None:
                continue  # retry next tick within the overlap window
            buy = _buy_from_receipt(h, receipt, allow_live_eth_fallback=True)
            if buy:
                if buy["usd"] >= min_buy and not _event_is_too_old(_receipt_block_number(receipt)):
                    outgoing.append((buy_id, _buy_caption(h, buy["tokens"], buy["usd"], buy["buyer"], buy.get("pay"))))
                seen_buy.add(buy_id)
                continue
            # Sell -> mark as seen so it is not re-processed. Anything else: leave unseen for retry.
            try:
                if _sell_from_receipt(h, receipt):
                    seen_buy.add(buy_id)
            except Exception:
                pass
        except Exception as e:
            print("buy tick error:", h, repr(e))

    def _persist(s):
        s["watch"]["last_scanned_block"] = end
        s["watch"]["seen"]["buy"] = _prune_seen(list(seen_buy))
    _update_state_fields(_persist)
    return outgoing


async def buy_monitor(app) -> None:
    while True:
        try:
            outgoing = await asyncio.to_thread(_monitor_tick_sync)
            if outgoing:
                state = _load_state()
                sent_pub = set(state["watch"]["sent_public"].get("buy") or [])
                sent_dm = set(state["watch"]["sent_dm"].get("buy") or [])
                dm_enabled = bool(state.get("alerts_dm", True)) and ADMIN_ID > 0

                chats = _alert_chat_ids()
                for uid, caption in outgoing:
                    if chats and uid not in sent_pub:
                        for cid in chats:
                            try:
                                await _send_buy_alert(app, cid, caption)
                            except Exception as e:
                                print(f"[alerts] send to {cid} failed: {e!r}")
                                if "chat not found" in str(e).lower() or "kicked" in str(e).lower() or "forbidden" in str(e).lower():
                                    _register_alert_chat(cid, False)
                        sent_pub.add(uid)
                        _sp = _prune_seen(list(sent_pub))
                        _update_state_fields(lambda s, _sp=_sp: s["watch"]["sent_public"].__setitem__("buy", _sp))
                    if dm_enabled and uid not in sent_dm:
                        sent_dm.add(uid)
                        _sd = _prune_seen(list(sent_dm))
                        _update_state_fields(lambda s, _sd=_sd: s["watch"]["sent_dm"].__setitem__("buy", _sd))
                        await _send_buy_alert(app, ADMIN_ID, caption)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("buy_monitor error:", repr(e))
        await asyncio.sleep(WATCH_POLL_SEC)


# ---- Commands: /setmin /setemoji /alerts /scan ----

def _is_admin_user(update: Update) -> bool:
    u = update.effective_user
    return bool(u) and ADMIN_ID > 0 and u.id == ADMIN_ID


async def setmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not _is_admin_user(update):
        await update.message.reply_text("Not allowed.")
        return
    args = [a for a in (context.args or []) if a.lower() != "buy"]
    if len(args) != 1:
        await update.message.reply_text("Usage: /setmin <usd>")
        return
    try:
        usd = max(0.0, float(args[0]))
    except Exception:
        await update.message.reply_text("Invalid usd.")
        return
    _update_state_fields(lambda s: s["min_usd"].__setitem__("buy", usd))
    await update.message.reply_text(f"OK. Minimum buy alert = ${usd:,.0f}")


async def setemoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not _is_admin_user(update):
        await update.message.reply_text("Not allowed.")
        return
    args = [a for a in (context.args or []) if a.lower() != "buy"]
    if len(args) != 1:
        await update.message.reply_text("Usage: /setemoji <usd_per_emoji>")
        return
    try:
        usd_per = max(0.01, float(args[0]))
    except Exception:
        await update.message.reply_text("Invalid usd_per_emoji.")
        return
    _update_state_fields(lambda s: s["emoji_usd"].__setitem__("buy", usd_per))
    await update.message.reply_text(f"OK. 1 {BUY_EMOJI} = ${usd_per:,.2f} (max {MAX_EMOJIS})")


async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not _is_admin_user(update):
        await update.message.reply_text("Not allowed.")
        return
    arg = (context.args[0].strip().lower() if context.args else "")
    if arg == "chats":
        chats = _alert_chat_ids()
        await update.message.reply_text("Alert groups: " + (", ".join(str(c) for c in chats) if chats else "none") + "\nRemove one: /alerts remove <chat_id>")
        return
    if arg == "remove" and len(context.args) == 2:
        try:
            _register_alert_chat(int(context.args[1]), False)
            await update.message.reply_text("Removed.")
        except Exception:
            await update.message.reply_text("Invalid chat id.")
        return
    if arg not in ("on", "off"):
        await update.message.reply_text("Usage: /alerts on|off | /alerts chats | /alerts remove <chat_id>")
        return
    _update_state_fields(lambda s: s.__setitem__("alerts_dm", arg == "on"))
    await update.message.reply_text(f"OK. DM alerts {'ON' if arg == 'on' else 'OFF'}.")


async def _scan_range_and_dm(app, user_id: int, blocks_back: int, min_usd: float) -> None:
    loop = asyncio.get_running_loop()

    def _dm(text: str):
        fut = asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id=user_id, text=text, disable_web_page_preview=True), loop)
        try:
            fut.result(timeout=15)
        except Exception:
            pass

    def _run():
        t0 = time.time()
        end = max(0, _get_latest_block() - max(0, WATCH_CONFIRMATIONS))
        start = max(0, end - blocks_back + 1)
        _dm(f"Scan started. Range {start} → {end}, min ${min_usd:,.0f}. Fetching logs...")
        logs = _get_token_logs_chunked(DRB_TOKEN, start, end)
        hashes = list(dict.fromkeys(lg.get("transactionHash") for lg in logs if lg.get("transactionHash")))
        _dm(f"Logs: {len(logs):,} · unique txs: {len(hashes):,}. Fetching receipts...")

        matches, ok, fail = [], 0, 0
        for i in range(0, len(hashes), 75):
            chunk = hashes[i:i + 75]
            try:
                res = _rpc_batch([("eth_getTransactionReceipt", [h]) for h in chunk])
            except Exception:
                res = [None] * len(chunk)
            for h, r in zip(chunk, res):
                try:
                    if r is None:
                        raise RuntimeError("no receipt")
                    ok += 1
                    b = _buy_from_receipt(h, r, allow_live_eth_fallback=True)
                    if b and b["usd"] >= min_usd:
                        matches.append((h, b))
                except Exception:
                    fail += 1
            if (i // 75) % 4 == 3:
                _dm(f"Progress: {min(i + 75, len(hashes)):,}/{len(hashes):,} matches={len(matches)} ({time.time() - t0:.0f}s)")

        matches.sort(key=lambda x: x[1]["usd"], reverse=True)
        lines = [
            "Scan finished",
            f"Blocks: {blocks_back} ({start} → {end})",
            f"Unique txs: {len(hashes):,} · receipts ok={ok:,} fail={fail:,}",
            f"Matches (>= ${min_usd:,.0f}): {len(matches):,}",
            f"Time: {time.time() - t0:.1f}s",
        ]
        if matches:
            usd_per = float(_load_state()["emoji_usd"]["buy"])
            lines += ["", "Top results (max 20):"]
            for h, b in matches[:20]:
                lines += ["", f"{_emoji_bar(b['usd'], usd_per)} ${b['usd']:,.2f}",
                          f"Buyer: {b['buyer']}",
                          f"Tokens: {_fmt_big(b['tokens'])} DRB",
                          f"Tx: https://basescan.org/tx/{h}"]
        _dm("\n".join(lines))

    await asyncio.to_thread(_run)


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/scan <tx_hash>  -> test detection on one tx (sends the alert in DM)
       /scan <blocks_back> <min_usd> -> scan a range, results in DM"""
    msg = update.message
    if not msg or not update.effective_user:
        return
    if not _is_admin_user(update):
        await msg.reply_text("Not allowed.")
        return
    user_id = update.effective_user.id
    args = context.args or []

    if len(args) == 1:
        tx_hash = args[0].strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash):
            await msg.reply_text("Usage: /scan <tx_hash>  OR  /scan <blocks_back> <min_usd>")
            return
        await msg.reply_text("Scanning tx...")
        try:
            receipt = await asyncio.to_thread(_get_receipt, tx_hash)
        except Exception:
            receipt = None
        if not receipt:
            await msg.reply_text("Transaction not found (no receipt).")
            return
        buy = None
        try:
            buy = await asyncio.to_thread(_buy_from_receipt, tx_hash, receipt, True)
        except Exception as e:
            print("scan tx error:", repr(e))
        if buy:
            caption = _buy_caption(tx_hash, buy["tokens"], buy["usd"], buy["buyer"], buy.get("pay"))
            await _send_buy_alert(context.application, user_id, caption)
            await msg.reply_text(f"Buy detected (${buy['usd']:,.2f}). Alert sent in DM.")
            return
        sell = None
        try:
            sell = await asyncio.to_thread(_sell_from_receipt, tx_hash, receipt)
        except Exception:
            pass
        await msg.reply_text("That tx looks like a SELL." if sell else "That tx is not detected as a DRB buy.")
        return

    if len(args) != 2:
        await msg.reply_text("Usage: /scan <blocks_back> <min_usd>  OR  /scan <tx_hash>")
        return
    try:
        blocks_back = max(1, min(20000, int(args[0])))
        min_usd = float(args[1])
    except Exception:
        await msg.reply_text("Invalid args. Example: /scan 5000 500")
        return
    await msg.reply_text(f"Scanning last {blocks_back} blocks for buys >= ${min_usd:,.0f}. Check your DM.")
    asyncio.create_task(_scan_range_and_dm(context.application, user_id, blocks_back, min_usd))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        "Commands\n\n"
        "/grok – Grok wallet balances\n"
        "/grok2 – Grok wallet card\n"
        "/stats [7d|4w] – claim stats\n"
        "/buys [7d] – biggest buys\n"
        "/claim – claim trading fees (admins)\n\n"
        "Buy alerts (admin only)\n"
        "/setmin <usd> – minimum buy to alert\n"
        "/setemoji <usd> – USD per emoji in the bar\n"
        "/alerts on|off – DM alerts to admin\n"
        "/alerts chats – list groups receiving alerts (auto-registered when the bot is added)\n"
        "/scan <tx_hash> – test detection on a tx\n"
        "/scan <blocks_back> <min_usd> – scan a range"
    )


async def blacklist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    user = update.effective_user
    if not (ADMIN_ID > 0 and user and user.id == ADMIN_ID):
        await query.answer("\u26D4 Admin only.", show_alert=True)
        return
    data = query.data or ""
    if data.startswith("blk:un:"):
        try:
            uid = int(data.split(":")[2])
        except Exception:
            await query.answer()
            return
        _blacklist_set(uid, False)
        _RATE_CALLS.pop(uid, None)
        _RATE_ADMIN_NOTIFIED.pop(uid, None)
        await query.answer("Unblocked.")
        try:
            await query.edit_message_text(
                (query.message.text_html or query.message.text or "") +
                "\n\n\u2705 <b>Unblocked by admin.</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /backup -> the bot sends you the current state file (blacklist,
    min buy, groups, sent-alert dedup). Send it back after a redeploy to restore."""
    msg = update.message
    user = update.effective_user
    if not msg or not user or not (ADMIN_ID > 0 and user.id == ADMIN_ID):
        return
    if not os.path.exists(STATE_PATH):
        await msg.reply_text("No state file yet.")
        return
    with open(STATE_PATH, "rb") as f:
        await msg.reply_document(
            document=f, filename="watch_state.json",
            caption="Current state. Send this file back to me after a redeploy to restore it.")


async def blacklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /blacklist -> list blocked users with Unblock buttons."""
    msg = update.message
    user = update.effective_user
    if not msg or not user or not (ADMIN_ID > 0 and user.id == ADMIN_ID):
        return
    ids = sorted(_blacklist())
    if not ids:
        await msg.reply_text("Blacklist empty.")
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"\u2705 Unblock {uid}", callback_data=f"blk:un:{uid}")]
                               for uid in ids[:30]])
    await msg.reply_text(f"\U0001F6D1 Blacklisted users ({len(ids)}):", reply_markup=kb)


async def _count_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Increment per-chat message counter for non-admin warning cooldown."""
    context.chat_data["msg_count"] = context.chat_data.get("msg_count", 0) + 1


async def on_startup(app):
    if ADMIN_ID > 0:
        try:
            chats = _alert_chat_ids()
            mode = f"posting to {len(chats)} group(s)" if chats else "no group registered yet (DM only) – add the bot to a group or send any message there"
            await app.bot.send_message(chat_id=ADMIN_ID, text=f"Bot started – buy alerts: {mode}. Use /help")
        except Exception:
            pass
    _ensure_data_dir()
    _load_last_claim()
    app.bot_data["monitor_task"] = asyncio.create_task(buy_monitor(app))
    if ADMIN_ID > 0:
        try:
            await app.bot.send_message(chat_id=ADMIN_ID, text=f"ASSET_BUY={ASSET_BUY} exists={os.path.exists(ASSET_BUY)}")
        except Exception:
            pass


async def on_shutdown(app):
    t = app.bot_data.get("monitor_task")
    if t and not t.done():
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("grok", grok_command))
    app.add_handler(CommandHandler("grok2", grok2_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("buys", buys_command))
    app.add_handler(CommandHandler("claim", claim_command))
    app.add_handler(CallbackQueryHandler(claim_callback, pattern="^claim_"))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("setmin", setmin_command))
    app.add_handler(CommandHandler("setemoji", setemoji_command))
    app.add_handler(CommandHandler("alerts", alerts_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("blacklist", blacklist_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CallbackQueryHandler(blacklist_callback, pattern=r"^blk:"))
    app.add_handler(MessageHandler(filters.COMMAND, command_guard, block=True), group=-1)
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, admin_file_handler), group=0)
    app.add_handler(MessageHandler(filters.ALL, _count_message), group=1)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, _register_from_message), group=2)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
