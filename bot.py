# bot.py
import os
import re
import json
import requests
import math

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


# ================= CONFIG =================

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

BASE_RPC_URL = "https://mainnet.base.org"
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/"

GROK_WALLET_URL = "https://thegrokwallet.com/"
UA_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DebtReliefBot/1.0)"}

GROK_WALLET = "0xb1058c959987e3513600eb5b4fd82aeee2a0e4f9"
DRB_TOKEN = "0x3ec2156d4c0a9cbdab4a016633b7bcf6a8d68ea2"
WETH_TOKEN = "0x4200000000000000000000000000000000000006"

DRB_COLOR = "#B49C94"
WETH_COLOR = "#627EEA"

# Put the background image here
# Save your starfield image as: assets/grok_wallet_bg.png
GROK_BG_PATH = "assets/grok_wallet_bg.png"

# Grok web card aspect
CARD_W = 896
CARD_H = 658


# ================= HELPERS =================

def fmt_usd(x: float) -> str:
    return f"${x:,.0f}"


def fmt_compact_b(n: float) -> str:
    return f"{n / 1_000_000_000:.2f}B"


def _rpc_call(method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(BASE_RPC_URL, json=payload, headers=UA_HEADERS, timeout=20)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(str(j["error"]))
    return j["result"]


def _pad32_hex_address(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


def _eth_call(to_addr: str, data: str) -> str:
    return _rpc_call("eth_call", [{"to": to_addr, "data": data}, "latest"])


def erc20_decimals(token: str) -> int:
    return int(_eth_call(token, "0x313ce567"), 16)


def erc20_balance_of(token: str, wallet: str) -> int:
    data = "0x70a08231" + _pad32_hex_address(wallet)
    return int(_eth_call(token, data), 16)


def fetch_price_usd(token: str) -> float:
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

    return best_price


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

    return {
        "title": _try_font(bold_candidates, 56),
        "big": _try_font(bold_candidates, 86),
        "mid": _try_font(regular_candidates, 28),
        "box_sym": _try_font(bold_candidates, 34),
        "box_amt": _try_font(bold_candidates, 44),
        "box_usd": _try_font(regular_candidates, 30),
    }


def _text_center(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, y: int, width: int, fill):
    try:
        tw = draw.textlength(text, font=font)
        x = int((width - tw) / 2)
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        x = int((width - (bbox[2] - bbox[0])) / 2)
    draw.text((x, y), text, font=font, fill=fill)


def _draw_text_shadow(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: ImageFont.ImageFont, fill, shadow=(0, 0, 0, 140), offset=(2, 2)):
    x, y = xy
    draw.text((x + offset[0], y + offset[1]), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)


def _draw_center_shadow(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, y: int, width: int, fill, shadow=(0, 0, 0, 140), offset=(2, 2)):
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


def _glass_panel(bg: Image.Image, rect: tuple[int, int, int, int], radius: int, tint=(20, 18, 40, 130), blur_radius=14) -> Image.Image:
    x1, y1, x2, y2 = rect
    crop = bg.crop((x1, y1, x2, y2)).filter(ImageFilter.GaussianBlur(blur_radius)).convert("RGBA")
    overlay = Image.new("RGBA", (x2 - x1, y2 - y1), tint)
    crop = Image.alpha_composite(crop, overlay)

    mask = _rounded_mask((x2 - x1, y2 - y1), radius)
    panel = Image.new("RGBA", (x2 - x1, y2 - y1), (0, 0, 0, 0))
    panel.paste(crop, (0, 0), mask)

    edge = Image.new("RGBA", (x2 - x1, y2 - y1), (255, 255, 255, 35))
    border = Image.new("RGBA", (x2 - x1, y2 - y1), (0, 0, 0, 0))
    border.paste(edge, (0, 0), mask)

    return Image.alpha_composite(panel, border)


# ================= BALANCES =================

def fetch_balances_and_values():
    drb_dec = erc20_decimals(DRB_TOKEN)
    weth_dec = erc20_decimals(WETH_TOKEN)

    drb_raw = erc20_balance_of(DRB_TOKEN, GROK_WALLET)
    weth_raw = erc20_balance_of(WETH_TOKEN, GROK_WALLET)

    drb_amt = drb_raw / 10 ** drb_dec
    weth_amt = weth_raw / 10 ** weth_dec

    drb_price = fetch_price_usd(DRB_TOKEN)
    weth_price = fetch_price_usd(WETH_TOKEN)

    drb_usd = drb_amt * drb_price
    weth_usd = weth_amt * weth_price

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
):
    total = drb_usd + weth_usd

    drb_amount_label = fmt_compact_b(drb_amount_float)
    weth_amount_label = f"{weth_amount_float:,.2f}"

    values = [drb_usd, weth_usd]
    colors = [DRB_COLOR, WETH_COLOR]

    fig, ax = plt.subplots(figsize=(6.6, 6.6))
    wedges, _ = ax.pie(
        values,
        colors=colors,
        startangle=90,
        wedgeprops=dict(width=0.35),
    )
    ax.set(aspect="equal")
    ax.set_title("Grok Wallet Balance", fontsize=18, fontweight="bold", pad=16)

    ax.text(0, 0, f"${total:,.0f}", ha="center", va="center", fontsize=22, fontweight="bold")
    ax.text(0, -0.18, "Total Balance", ha="center", va="center", fontsize=11, color="#666")

    labels = [f"DRB\n{drb_amount_label}", f"WETH\n{weth_amount_label}"]
    for w, t in zip(wedges, labels):
        ang = (w.theta1 + w.theta2) / 2.0
        r = 0.82
        x = r * (math.cos(math.radians(ang)))
        y = r * (math.sin(math.radians(ang)))
        ax.text(
            x, y,
            t,
            ha="center",
            va="center",
            fontsize=12,
            fontweight="bold",
            color="#111111",
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
    fees: str | None,
) -> str:
    drb_compact = fmt_compact_b(drb_amount_float)

    rows = [
        ("Token", "Amount", "USD"),
        ("-----", "------", "---"),
        ("DRB", drb_compact, drb_usd_str),
        ("WETH", weth_amount_str, weth_usd_str),
    ]

    c1 = max(len(r[0]) for r in rows)
    c2 = max(len(r[1]) for r in rows)
    c3 = max(len(r[2]) for r in rows)

    lines = [f"{a:<{c1}}  {b:>{c2}}  {c:>{c3}}" for a, b, c in rows]
    caption = "<pre>" + "\n".join(lines) + "</pre>"

    if fees:
        caption += f"\n\n{fees}\nHistorical Fees Claimed"

    return caption


# ================= GROK2 STYLE CARD (same as web layout) =================

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
    SOFT = (230, 230, 245, 255)

    canvas = bg.copy()

    # Outer glass card
    outer = (24, 24, CARD_W - 24, CARD_H - 24)
    outer_panel = _glass_panel(canvas, outer, radius=34, tint=(12, 10, 30, 145), blur_radius=18)
    canvas.alpha_composite(outer_panel, (outer[0], outer[1]))

    # Inner separator line
    d = ImageDraw.Draw(canvas)
    d.line((outer[0] + 28, 500, outer[2] - 28, 500), fill=(255, 255, 255, 40), width=1)

    # Header text (no auth, no address, no 24h, no footer)
    _draw_center_shadow(d, "GROK WALLET", fonts["title"], y=70, width=CARD_W, fill=WHITE, shadow=(0, 0, 0, 120))
    _draw_center_shadow(d, f"${total_usd:,.0f}", fonts["big"], y=160, width=CARD_W, fill=WHITE, shadow=(0, 0, 0, 120))
    _text_center(d, "Live Balance", fonts["mid"], y=270, width=CARD_W, fill=MUTED)

    # Two inner glass boxes
    box_y1, box_y2 = 330, 470
    left = (outer[0] + 28, box_y1, (CARD_W // 2) - 14, box_y2)
    right = ((CARD_W // 2) + 14, box_y1, outer[2] - 28, box_y2)

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

    # Left box content (ETH)
    lx, ly = left[0], left[1]
    d.text((lx + 34, ly + 34), "ETH", font=fonts["box_sym"], fill=SOFT)
    d.text((lx + 34, ly + 78), eth_amt_str, font=fonts["box_amt"], fill=WHITE)
    d.text((lx + 34, ly + 122), eth_usd_str, font=fonts["box_usd"], fill=MUTED)

    # Right box content (DRB)
    rx, ry = right[0], right[1]
    d.text((rx + 34, ry + 34), "DRB", font=fonts["box_sym"], fill=SOFT)
    d.text((rx + 34, ry + 78), drb_amt_str, font=fonts["box_amt"], fill=WHITE)
    d.text((rx + 34, ry + 122), drb_usd_str, font=fonts["box_usd"], fill=MUTED)

    buf = BytesIO()
    canvas.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ================= COMMANDS =================

async def grok_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    try:
        b = fetch_balances_and_values()

        donut = generate_balance_donut(
            b["DRB"]["usd_float"],
            b["WETH"]["usd_float"],
            b["DRB"]["amount_float"],
            b["WETH"]["amount_float"],
        )

        fees = fetch_historical_fees_claimed()

        caption = make_balance_table_caption(
            drb_amount_float=b["DRB"]["amount_float"],
            drb_usd_str=b["DRB"]["usd"],
            weth_amount_str=b["WETH"]["amount"],
            weth_usd_str=b["WETH"]["usd"],
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
        b = fetch_balances_and_values()
        total_usd = b["DRB"]["usd_float"] + b["WETH"]["usd_float"]

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


# ================= BOOT =================

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

    app.run_polling()


if __name__ == "__main__":
    main()
