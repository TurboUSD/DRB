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

BASE_RPC_URL = "https://mainnet.base.org"
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/"

GROK_WALLET = "0xb1058c959987e3513600eb5b4fd82aeee2a0e4f9"
DRB_TOKEN = "0x3ec2156d4c0a9cbdab4a016633b7bcf6a8d68ea2"
WETH_TOKEN = "0x4200000000000000000000000000000000000006"

WETH_COLOR = "#627EEA"
DRB_COLOR = "#B49C94"


def fmt_usd(x: float) -> str:
    return f"${x:,.0f}"


def fmt_amount(amount_int: int, decimals: int, max_decimals: int = 6) -> str:
    if decimals <= 0:
        return f"{amount_int:,}"
    v = amount_int / (10 ** decimals)
    s = f"{v:,.{max_decimals}f}".rstrip("0").rstrip(".")
    return s


def _rpc_call(method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(BASE_RPC_URL, json=payload, headers=UA_HEADERS, timeout=20)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(str(j["error"]))
    return j["result"]


def _pad32_hex_address(addr: str) -> str:
    a = addr.lower().replace("0x", "")
    return a.rjust(64, "0")


def _eth_call(to_addr: str, data: str) -> str:
    return _rpc_call("eth_call", [{"to": to_addr, "data": data}, "latest"])


def erc20_decimals(token_addr: str) -> int:
    out = _eth_call(token_addr, "0x313ce567")
    return int(out, 16)


def erc20_balance_of(token_addr: str, wallet_addr: str) -> int:
    selector = "0x70a08231"
    data = selector + _pad32_hex_address(wallet_addr)
    out = _eth_call(token_addr, data)
    return int(out, 16)


def fetch_price_usd_from_dexscreener(token_addr: str) -> float:
    r = requests.get(DEXSCREENER_TOKEN_URL + token_addr, headers=UA_HEADERS, timeout=20)
    r.raise_for_status()
    j = r.json()
    pairs = j.get("pairs") or []

    best_price = None
    best_liq = -1.0

    for p in pairs:
        try:
            price = float(p.get("priceUsd") or 0.0)
            liq = float((p.get("liquidity") or {}).get("usd") or 0.0)
        except Exception:
            continue

        if price > 0 and liq > best_liq:
            best_liq = liq
            best_price = price

    if best_price is None:
        raise RuntimeError("Dexscreener priceUsd not found")

    return best_price


def fetch_balances_and_values():
    drb_dec = erc20_decimals(DRB_TOKEN)
    weth_dec = erc20_decimals(WETH_TOKEN)

    drb_bal = erc20_balance_of(DRB_TOKEN, GROK_WALLET)
    weth_bal = erc20_balance_of(WETH_TOKEN, GROK_WALLET)

    drb_price = fetch_price_usd_from_dexscreener(DRB_TOKEN)
    weth_price = fetch_price_usd_from_dexscreener(WETH_TOKEN)

    drb_amt = drb_bal / (10 ** drb_dec)
    weth_amt = weth_bal / (10 ** weth_dec)

    drb_usd = drb_amt * drb_price
    weth_usd = weth_amt * weth_price

    return {
        "DRB": {"amount": fmt_amount(drb_bal, drb_dec), "usd": fmt_usd(drb_usd), "usd_float": float(drb_usd)},
        "WETH": {"amount": fmt_amount(weth_bal, weth_dec), "usd": fmt_usd(weth_usd), "usd_float": float(weth_usd)},
    }


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


def fetch_historical_fees_claimed():
    try:
        r = requests.get(GROK_WALLET_URL, headers=UA_HEADERS, timeout=25)
        r.raise_for_status()
        html = r.text or ""

        next_data = _parse_next_data(html)
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

        return None
    except Exception as e:
        print("fees scrape error:", repr(e))
        return None


def generate_balance_donut(drb_amount: str, drb_usd: float, weth_amount: str, weth_usd: float):
    total = drb_usd + weth_usd

    values = [drb_usd, weth_usd]
    colors = [DRB_COLOR, WETH_COLOR]

    fig, ax = plt.subplots(figsize=(6.8, 6.8))
    ax.pie(values, colors=colors, startangle=90, wedgeprops=dict(width=0.35))
    ax.set(aspect="equal")
    ax.set_title("DebtReliefBot Balance", fontsize=18, fontweight="bold", pad=16)

    ax.text(
        0, 0,
        f"${total:,.0f}",
        ha="center",
        va="center",
        fontsize=22,
        fontweight="bold",
        color="#111111"
    )
    ax.text(
        0, -0.18,
        "Total Balance",
        ha="center",
        va="center",
        fontsize=11,
        color="#666666"
    )

    # Legend layout: 2 lines per token to avoid overlap
    legend_rows = [
        ("DRB", drb_amount, fmt_usd(drb_usd), DRB_COLOR),
        ("WETH", weth_amount, fmt_usd(weth_usd), WETH_COLOR),
    ]

    # Push legend further down + widen canvas space
    y0 = -0.14
    row_gap = 0.17
    line_gap = 0.055

    for i, (sym, amt, usd_str, col) in enumerate(legend_rows):
        base_y = y0 - i * row_gap

        ax.add_patch(
            Rectangle(
                (0.08, base_y - 0.018),
                0.032,
                0.032,
                transform=ax.transAxes,
                clip_on=False,
                facecolor=col,
                edgecolor="none",
            )
        )

        # Line 1: symbol + amount (left)
        ax.text(
            0.14,
            base_y,
            f"{sym}: {amt}",
            transform=ax.transAxes,
            ha="left",
            va="center",
            fontsize=12,
            color="#111111",
            fontweight="bold",
        )

        # Line 2: USD value (left, under amount)
        ax.text(
            0.14,
            base_y - line_gap,
            usd_str,
            transform=ax.transAxes,
            ha="left",
            va="center",
            fontsize=12,
            color="#111111",
            fontweight="bold",
        )

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
        b = fetch_balances_and_values()

        drb_amount = b["DRB"]["amount"]
        drb_usd_val = b["DRB"]["usd_float"]
        drb_usd_str = b["DRB"]["usd"]

        weth_amount = b["WETH"]["amount"]
        weth_usd_val = b["WETH"]["usd_float"]
        weth_usd_str = b["WETH"]["usd"]

        donut = generate_balance_donut(
            drb_amount=drb_amount,
            drb_usd=drb_usd_val,
            weth_amount=weth_amount,
            weth_usd=weth_usd_val,
        )

        fees = fetch_historical_fees_claimed()
        fees_block = ""
        if fees:
            fees_block = f"\n\n{fees}\nHistorical Fees Claimed"

        caption = (
            "DebtReliefBot Balance\n"
            f"$DRB: {drb_amount} ({drb_usd_str})\n"
            f"$WETH: {weth_amount} ({weth_usd_str})"
            f"{fees_block}"
        )

        await msg.reply_photo(photo=donut, caption=caption)

    except Exception as e:
        err = repr(e)
        print("grok_command error:", err)
        if ADMIN_ID > 0:
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"grok_command error: {err}")
            except Exception:
                pass
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
