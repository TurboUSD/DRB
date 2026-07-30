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
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import asyncio
from web3 import Web3


# ================= CONFIG =================

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
ETHERSCAN_APIKEY = os.environ.get("ETHERSCAN_APIKEY", "").strip() or os.environ.get("BASESCAN_API_KEY", "").strip()

# Alchemy RPC (default Scaffold-ETH 2 key) with Base mainnet fallback
ALCHEMY_RPC_URL = "https://base-mainnet.g.alchemy.com/v2/8GVG8WjDs-sGFRr6Rm839"
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


def erc20_decimals(token: str) -> int:
    return int(_eth_call(token, "0x313ce567"), 16)


def erc20_balance_of(token: str, wallet: str) -> int:
    data = "0x70a08231" + _pad32_hex_address(wallet)
    return int(_eth_call(token, data), 16)


# Price cache (5 min) — shared by /claim estimate, /buys and balances
_PRICE_CACHE = {}
_PRICE_CACHE_TTL = 300


def fetch_price_usd(token: str) -> float:
    now = time.time()
    c = _PRICE_CACHE.get(token.lower())
    if c and (now - c["ts"]) < _PRICE_CACHE_TTL:
        return c["price"]

    r = requests.get(DEXSCREENER_TOKEN_URL + token, headers=UA_HEADERS, timeout=20)
    r.raise_for_status()
    pairs = r.json().get("pairs") or []

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
    """Fetch price and FDV (market cap) from DexScreener."""
    r = requests.get(DEXSCREENER_TOKEN_URL + token_addr, headers=UA_HEADERS, timeout=20)
    r.raise_for_status()
    pairs = r.json().get("pairs") or []

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


# ================= FEES =================

def _parse_next_data(html: str):
    m = re.search(r'id="__NEXT_DATA__".*?>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _deep_find_first_usd(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            r = _deep_find_first_usd(v)
            if r:
                return r
    elif isinstance(obj, list):
        for it in obj:
            r = _deep_find_first_usd(it)
            if r:
                return r
    elif isinstance(obj, str):
        m = re.search(r"\$[\d\.,]+", obj)
        if m:
            return m.group(0)
    return None


def fetch_historical_fees_claimed():
    try:
        r = requests.get(GROK_WALLET_URL, headers=UA_HEADERS, timeout=20)
        r.raise_for_status()
        html = r.text or ""

        next_data = _parse_next_data(html)
        if next_data:
            usd = _deep_find_first_usd(next_data)
            if usd:
                return usd

        m = re.search(
            r'(\$[\d\.,]+)\s*Historical\s+Fees\s+Claimed',
            html,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)

    except Exception:
        pass

    return None


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

        fees = fetch_historical_fees_claimed()

        caption = make_balance_table_caption(
            drb_amount_float=b["DRB"]["amount_float"],
            drb_usd_str=b["DRB"]["usd"],
            weth_amount_str=b["WETH"]["amount"],
            weth_usd_str=b["WETH"]["usd"],
            eth_amount_str=b["ETH"]["amount"],
            eth_usd_str=b["ETH"]["usd"],
            usdc_amount_str=b["USDC"]["amount"],
            usdc_usd_str=b["USDC"]["usd"],
            fees=fees,
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
        return BytesIO(c["png"]), c["drb_total"], c["weth_total"], c["growth"], c["growth_pct"]

    drb_txs = fetch_token_transfers(DRB_TOKEN, GROK_WALLET)
    weth_txs = fetch_token_transfers(WETH_TOKEN, GROK_WALLET)

    now_utc = datetime.now(timezone.utc)
    end_date = now_utc.date()

    dates, drb_vals = _daily_claims(drb_txs, days, end_date)
    _, weth_vals = _daily_claims(weth_txs, days, end_date)

    fig, (ax1, ax3) = plt.subplots(2, 1, figsize=(11, 10))
    fig.subplots_adjust(hspace=0.42)

    # ---- Bar chart: claims per day (two bars per day, twin axes) ----
    x = list(range(len(dates)))
    width = 0.4
    ax1.bar([i - width / 2 for i in x], drb_vals, width=width, color=DRB_COLOR, label="DRB")
    ax2 = ax1.twinx()
    ax2.bar([i + width / 2 for i in x], weth_vals, width=width, color=WETH_COLOR, label="WETH")

    ax1.set_title(f"Fees claimed per day — last {days}d", fontsize=16, fontweight="bold")
    ax1.set_ylabel("DRB", color=DRB_COLOR, fontweight="bold")
    ax2.set_ylabel("WETH", color=WETH_COLOR, fontweight="bold")
    ax1.tick_params(axis="y", labelcolor=DRB_COLOR)
    ax2.tick_params(axis="y", labelcolor=WETH_COLOR)
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _fmt_big(v)))
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))

    step = max(1, len(dates) // 10)
    ax1.set_xticks(x[::step])
    ax1.set_xticklabels([d.strftime("%d %b") for d in dates][::step], rotation=45, ha="right", fontsize=9)
    ax1.grid(axis="y", alpha=0.22)
    ax1.set_axisbelow(True)
    ax1.set_ylim(bottom=0)
    ax2.set_ylim(bottom=0)
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
        "growth": growth,
        "growth_pct": growth_pct,
    }
    return BytesIO(png), sum(drb_vals), sum(weth_vals), growth, growth_pct


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    days = _parse_stats_period(context.args)

    try:
        buf, drb_total, weth_total, growth, growth_pct = await asyncio.get_event_loop().run_in_executor(
            None, generate_stats_chart, days
        )
        sign = "+" if growth >= 0 else "-"
        pct_str = f" ({growth_pct:+.1f}%)" if growth_pct is not None else ""
        caption = (
            f"📊 <b>Claim stats — last {days} days</b>\n"
            f"Total claimed: <b>{_fmt_drb_millions(drb_total)} DRB</b> · <b>{_fmt_weth(weth_total)} WETH</b>\n"
            f"Grok Wallet: <b>{sign}{_fmt_big(abs(growth))} DRB</b>{pct_str}"
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


# Rendered /buys result cache: days -> {ts, text} (5 min)
_BUYS_RESULT_CACHE = {}
_BUYS_RESULT_TTL = 300


def build_biggest_buys_text(days: int, top_n: int = BUYS_TOP_N) -> str:
    """Build the 'Biggest Buys' HTML message for the last `days` days. Result cached 5 min."""
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
    exclude_to = {
        pool_addr,
        CLAIM_CONTRACT.lower(),
        CLAIM_RECIPIENT.lower(),
        GROK_WALLET.lower(),
        "0x0000000000000000000000000000000000000000",
    }
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

    top_total = sum(b["usd"] for b in top)
    total_drb_bought = sum(b["drb"] for b in buys)
    over_1k = sum(1 for b in buys if b["usd"] >= 1000)
    summary = f"📊 | {len(buys)} buys"
    if over_1k:
        summary += f" · {over_1k} over $1K"
    summary += f" · <b>{_fmt_big(total_drb_bought)} DRB</b> bought"
    summary += f" | Top {len(top)} total: <b>${top_total:,.2f}</b>"
    lines.append("")
    lines.append(summary)

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
        tx_line = ""
        if _last_claim_tx:
            tx_line = f'\n🔗 <a href="https://basescan.org/tx/{_last_claim_tx}">Last claim tx</a>'
        await msg.reply_text(
            f"⏳ Too soon! Next claim available in <b>{remaining_h:.1f}h</b>.{tx_line}",
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

        global _last_claim_ts, _last_claim_tx
        _last_claim_ts = time.time()
        _last_claim_tx = result["tx_hash"]

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



async def _count_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Increment per-chat message counter for non-admin warning cooldown."""
    context.chat_data["msg_count"] = context.chat_data.get("msg_count", 0) + 1


async def on_startup(app):
    if ADMIN_ID > 0:
        try:
            await app.bot.send_message(chat_id=ADMIN_ID, text="Bot started")
        except Exception:
            pass


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler("grok", grok_command))
    app.add_handler(CommandHandler("grok2", grok2_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("buys", buys_command))
    app.add_handler(CommandHandler("claim", claim_command))
    app.add_handler(CallbackQueryHandler(claim_callback, pattern="^claim_"))
    app.add_handler(MessageHandler(filters.ALL, _count_message), group=1)

    app.run_polling()


if __name__ == "__main__":
    main()
