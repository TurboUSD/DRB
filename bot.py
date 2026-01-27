# main.py
import os
import re
import json
import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

GROK_WALLET_URL = "https://thegrokwallet.com/"

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DebtReliefBot/1.0; +https://thegrokwallet.com/)"
}


def _clean_number(s: str) -> str:
    return re.sub(r"[^\d\.\-]", "", s)


def _fmt_amount(s: str) -> str:
    try:
        x = float(_clean_number(s))
    except Exception:
        return s.strip()
    if abs(x) >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def _fmt_usd(s: str) -> str:
    try:
        x = float(_clean_number(s))
    except Exception:
        return s.strip()
    return f"${x:,.0f}"


def _deep_find_token(data, symbol: str):
    if isinstance(data, dict):
        sym = data.get("symbol") or data.get("ticker") or data.get("name")
        if isinstance(sym, str) and sym.upper() == symbol.upper():
            amount = data.get("amount") or data.get("balance") or data.get("qty")
            usd = data.get("usd") or data.get("usdValue") or data.get("valueUsd") or data.get("value")
            if amount is not None and usd is not None:
                return {"amount": str(amount), "usd": str(usd)}
        for v in data.values():
            r = _deep_find_token(v, symbol)
            if r:
                return r
    elif isinstance(data, list):
        for it in data:
            r = _deep_find_token(it, symbol)
            if r:
                return r
    return None


def _parse_next_data(html: str):
    m = re.search(r'id="__NEXT_DATA__"\s*type="application\/json"\s*>(.*?)<\/script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _parse_from_html_fallback(html: str):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    def find_block(symbol: str):
        sym = symbol.upper()
        for i, ln in enumerate(lines):
            if ln.upper() == sym:
                amt = None
                usd = None
                for j in range(i + 1, min(i + 12, len(lines))):
                    v = lines[j]
                    if amt is None and re.search(r"\d", v) and not v.startswith("$"):
                        amt = v
                        continue
                    if usd is None and v.startswith("$") and re.search(r"\d", v):
                        usd = v
                        break
                if amt and usd:
                    return {"amount": amt, "usd": usd}
        return None

    drb = find_block("DRB")
    eth = find_block("ETH")
    return {"DRB": drb, "ETH": eth}


def fetch_grok_wallet_balances():
    r = requests.get(GROK_WALLET_URL, headers=UA_HEADERS, timeout=20)
    r.raise_for_status()
    html = r.text

    next_data = _parse_next_data(html)
    drb = None
    eth = None

    if next_data is not None:
        drb = _deep_find_token(next_data, "DRB")
        eth = _deep_find_token(next_data, "ETH")

    if not drb or not eth:
        fb = _parse_from_html_fallback(html)
        if not drb:
            drb = fb.get("DRB")
        if not eth:
            eth = fb.get("ETH")

    if not drb or not eth:
        raise RuntimeError("Could not parse balances from thegrokwallet.com")

    drb_amount = _fmt_amount(drb["amount"])
    drb_usd = _fmt_usd(drb["usd"])
    eth_amount = _fmt_amount(eth["amount"])
    eth_usd = _fmt_usd(eth["usd"])

    return {
        "DRB": {"amount": drb_amount, "usd": drb_usd},
        "WETH": {"amount": eth_amount, "usd": eth_usd},
    }


async def grok_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    try:
        b = fetch_grok_wallet_balances()
        text = (
            "DebtReliefBot Balance\n"
            f"$DRB: {b['DRB']['amount']} ({b['DRB']['usd']})\n"
            f"$WETH: {b['WETH']['amount']} ({b['WETH']['usd']})"
        )
        await msg.reply_text(text)
    except Exception:
        await msg.reply_text("Error fetching balances from thegrokwallet.com")


async def on_startup(app):
    if ADMIN_ID <= 0:
        return
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
    app.run_polling()


if __name__ == "__main__":
    main()
