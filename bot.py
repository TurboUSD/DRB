# bot.py
import os
import re
import json
import requests

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from io import BytesIO

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

GROK_WALLET_URL = "https://thegrokwallet.com/"
UA_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DebtReliefBot/1.0)"}

WETH_COLOR = "#627EEA"
DRB_COLOR = "#B49C94"


def _parse_next_data(html: str):
    m = re.search(
        r'id="__NEXT_DATA__"\s*type="application\/json"\s*>(.*?)<\/script>',
        html,
        re.S,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _normalize_usd(s: str) -> str:
    s = str(s).strip()
    if s.startswith("$"):
        return s
    try:
        x = float(s.replace(",", ""))
        return f"${x:,.0f}"
    except Exception:
        return s


def _deep_find_token(obj, symbol: str):
    symbol = symbol.upper()

    def walk(x):
        if isinstance(x, dict):
            sym = x.get("symbol") or x.get("ticker") or x.get("name")
            if isinstance(sym, str) and sym.upper() == symbol:
                amount = x.get("amount") or x.get("balance") or x.get("qty")
                usd = x.get("usd") or x.get("usdValue") or x.get("valueUsd") or x.get("value") or x.get("priceUsd")
                if amount is not None and usd is not None:
                    return {"amount": str(amount), "usd": _normalize_usd(usd)}
            for v in x.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(x, list):
            for it in x:
                r = walk(it)
                if r:
                    return r
        return None

    return walk(obj)


def _extract_token_block_from_html(html: str, symbol: str):
    sym = symbol.upper()

    idx = re.search(rf"\b{re.escape(sym)}\b", html, re.IGNORECASE)
    if not idx:
        return None

    start = max(idx.start() - 1500, 0)
    end = min(idx.start() + 1500, len(html))
    chunk = html[start:end]

    usd_m = re.search(r"\$[\d\.,]+", chunk)
    amt_m = re.search(r"(?<!\$)\b\d[\d\.,]*\b", chunk)

    if not usd_m or not amt_m:
        return None

    return {"amount": amt_m.group(0), "usd": usd_m.group(0)}


def _deep_find_first_usd_near_label(obj, label_words):
    label_words = [w.lower() for w in label_words]

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.strip().lower())

    def find_usd_in_container(container):
        if isinstance(container, dict):
            for v in container.values():
                r = find_usd_in_container(v)
                if r:
                    return r
        elif isinstance(container, list):
            for it in container:
                r = find_usd_in_container(it)
                if r:
                    return r
        elif isinstance(container, str):
            m = re.search(r"\$[\d\.,]+", container)
            if m:
                return m.group(0)
        return None

    def walk_with_parent(x):
        if isinstance(x, dict):
            for v in x.values():
                if isinstance(v, str):
                    s = norm(v)
                    if all(w in s for w in label_words):
                        usd = find_usd_in_container(x)
                        if usd:
                            return usd
                r = walk_with_parent(v)
                if r:
                    return r
        elif isinstance(x, list):
            for it in x:
                r = walk_with_parent(it)
                if r:
                    return r
        return None

    return walk_with_parent(obj)


def fetch_grokwallet_page_html():
    r = requests.get(GROK_WALLET_URL, headers=UA_HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def fetch_balances_from_grokwallet():
    html = fetch_grokwallet_page_html()
    next_data = _parse_next_data(html)

    drb = None
    eth = None

    if next_data is not None:
        drb = _deep_find_token(next_data, "DRB")
        eth = _deep_find_token(next_data, "ETH")

    if not drb:
        drb = _extract_token_block_from_html(html, "DRB")
    if not eth:
        eth = _extract_token_block_from_html(html, "ETH")

    if not drb or not eth:
        raise RuntimeError("Could not parse DRB or ETH from thegrokwallet.com")

    return {
        "DRB": {"amount": str(drb["amount"]).strip(), "usd": _normalize_usd(drb["usd"])},
        "WETH": {"amount": str(eth["amount"]).strip(), "usd": _normalize_usd(eth["usd"])},
        "html": html,
        "next_data": next_data,
    }


def fetch_historical_fees_claimed_from_grokwallet(html: str, next_data):
    if next_data is not None:
        usd = _deep_find_first_usd_near_label(next_data, ["historical", "fees", "claimed"])
        if usd:
            return usd

    m = re.search(
        r'(\$[\d\.,]+)\s*[\r\n\s]*Historical\s+Fees\s+Claimed',
        html,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    raise RuntimeError("Could not parse Historical Fees Claimed")


def _usd_to_float(s: str) -> float:
    return float(str(s).replace("$", "").replace(",", "").strip())


def generate_balance_donut(drb_amount_str: str, drb_usd_str: str, weth_amount_str: str, weth_usd_str: str):
    drb_usd = _usd_to_float(drb_usd_str)
    weth_usd = _usd_to_float(weth_usd_str)
    total = drb_usd + weth_usd

    values = [drb_usd, weth_usd]
    colors = [DRB_COLOR, WETH_COLOR]

    fig, ax = plt.subplots(figsize=(6.2, 6.2))
    ax.pie(values, colors=colors, startangle=90, wedgeprops=dict(width=0.35))
    ax.set(aspect="equal")
    ax.set_title("DebtReliefBot Balance", fontsize=18, fontweight="bold", pad=16)

    ax.text(0, 0, f"${total:,.0f}", ha="center", va="center", fontsize=22, fontweight="bold", color="#111111")
    ax.text(0, -0.18, "Total USD", ha="center", va="center", fontsize=11, color="#666666")

    legend_rows = [
        ("DRB", drb_amount_str, drb_usd_str, DRB_COLOR),
        ("WETH", weth_amount_str, weth_usd_str, WETH_COLOR),
    ]

    y0 = -0.10
    line_h = 0.11
    for i, (sym, amt, usd, col) in enumerate(legend_rows):
        y = y0 - i * line_h
        ax.add_patch(Rectangle((0.10, y - 0.018), 0.030, 0.030, transform=ax.transAxes,
                               clip_on=False, facecolor=col, edgecolor="none"))
        ax.text(0.15, y, f"{sym}: {amt}", transform=ax.transAxes, ha="left", va="center",
                fontsize=12, color="#111111", fontweight="bold")
        ax.text(0.90, y, f"{usd}", transform=ax.transAxes, ha="right", va="center",
                fontsize=12, color="#111111", fontweight="bold")

    buf = BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=170, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf


async def grok_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    try:
        data = fetch_balances_from_grokwallet()

        drb_amount = data["DRB"]["amount"]
        drb_usd_str = data["DRB"]["usd"]
        weth_amount = data["WETH"]["amount"]
        weth_usd_str = data["WETH"]["usd"]

        fees_claimed = fetch_historical_fees_claimed_from_grokwallet(data["html"], data["next_data"])

        donut = generate_balance_donut(
            drb_amount_str=drb_amount,
            drb_usd_str=drb_usd_str,
            weth_amount_str=weth_amount,
            weth_usd_str=weth_usd_str,
        )

        caption = (
            "DebtReliefBot Balance\n"
            f"$DRB: {drb_amount} ({drb_usd_str})\n"
            f"$WETH: {weth_amount} ({weth_usd_str})\n\n"
            f"{fees_claimed}\n"
            "Historical Fees Claimed"
        )

        await msg.reply_photo(photo=donut, caption=caption)

    except Exception as e:
        print("grok_command error:", repr(e))
        await msg.reply_text("Error fetching balances")


async def on_startup(app):
    if ADMIN_ID <= 0:
        return
    try:
        await app.bot.send_message(chat_id=ADMIN_ID, text="Bot started")
    except Exception as e:
        print("startup message failed:", repr(e))


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler("grok", grok_command))
    app.run_polling()


if __name__ == "__main__":
    main()
