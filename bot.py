import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

BASE_RPC_URL = "https://mainnet.base.org"

GROK_WALLET = "0xb1058c959987e3513600eb5b4fd82aeee2a0e4f9"

DRB_TOKEN = "0x3ec2156d4c0a9cbdab4a016633b7bcf6a8d68ea2"
WETH_TOKEN = "0x4200000000000000000000000000000000000006"

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/"

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DebtReliefBot/1.0)"
}


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
    data = "0x313ce567"
    out = _eth_call(token_addr, data)
    return int(out, 16)


def erc20_balance_of(token_addr: str, wallet_addr: str) -> int:
    selector = "0x70a08231"
    data = selector + _pad32_hex_address(wallet_addr)
    out = _eth_call(token_addr, data)
    return int(out, 16)


def fetch_price_usd(token_addr: str) -> float:
    r = requests.get(
        DEXSCREENER_TOKEN_URL + token_addr,
        headers=UA_HEADERS,
        timeout=20
    )
    r.raise_for_status()
    j = r.json()
    pairs = j.get("pairs") or []
    best = None
    best_liq = -1.0

    for p in pairs:
        try:
            price = float(p.get("priceUsd") or 0.0)
            liq = float((p.get("liquidity") or {}).get("usd") or 0.0)
        except Exception:
            continue
        if price > 0 and liq > best_liq:
            best_liq = liq
            best = price

    if best is None:
        raise RuntimeError("No priceUsd found")
    return best


def fmt_amount(amount_int: int, decimals: int, max_decimals: int = 6) -> str:
    if decimals <= 0:
        return f"{amount_int:,}"
    value = amount_int / (10 ** decimals)
    s = f"{value:,.{max_decimals}f}".rstrip("0").rstrip(".")
    return s


def fmt_usd(x: float) -> str:
    return f"${x:,.0f}"


def fetch_balances_and_values():
    drb_dec = erc20_decimals(DRB_TOKEN)
    weth_dec = erc20_decimals(WETH_TOKEN)

    drb_bal = erc20_balance_of(DRB_TOKEN, GROK_WALLET)
    weth_bal = erc20_balance_of(WETH_TOKEN, GROK_WALLET)

    drb_price = fetch_price_usd(DRB_TOKEN)
    weth_price = fetch_price_usd(WETH_TOKEN)

    drb_amt = drb_bal / (10 ** drb_dec)
    weth_amt = weth_bal / (10 ** weth_dec)

    drb_usd = drb_amt * drb_price
    weth_usd = weth_amt * weth_price

    return {
        "DRB": {"amount": fmt_amount(drb_bal, drb_dec), "usd": fmt_usd(drb_usd)},
        "WETH": {"amount": fmt_amount(weth_bal, weth_dec), "usd": fmt_usd(weth_usd)},
    }


async def grok_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    try:
        b = fetch_balances_and_values()
        text = (
            "DebtReliefBot Balance\n"
            f"$DRB: {b['DRB']['amount']} ({b['DRB']['usd']})\n"
            f"$WETH: {b['WETH']['amount']} ({b['WETH']['usd']})"
        )
        await msg.reply_text(text)
    except Exception:
        await msg.reply_text("Error fetching balances")


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
