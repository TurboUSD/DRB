import os
import requests
import json
import re
import matplotlib.pyplot as plt
from io import BytesIO

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

GROK_WALLET_URL = "https://thegrokwallet.com/"

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DebtReliefBot/1.0)"
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

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                r = walk(v)
                if r:
                    return r
        elif isinstance(x, list):
            for it in x:
                r = walk(it)
                if r:
                    return r
        elif isinstance(x, str):
            s = norm(x)
            if all(w in s for w in label_words):
                return True
        return None

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
            for k, v in x.items():
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
    r = requests.get(GROK_WALLET_URL, headers=UA_HEADERS, timeout=20)
    r.raise_for_status()
    html = r.text

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

    raise RuntimeError("Could not parse Historical Fees Claimed")


async def grok_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    try:
        b = fetch_balances_and_values()

        drb_amount = b["DRB"]["amount"]
        drb_usd_str = b["DRB"]["usd"]
        weth_amount = b["WETH"]["amount"]
        weth_usd_str = b["WETH"]["usd"]

        donut = generate_balance_donut(
            drb_amount_str=drb_amount,
            drb_usd_str=drb_usd_str,
            weth_amount_str=weth_amount,
            weth_usd_str=weth_usd_str,
        )

        fees_claimed = fetch_historical_fees_claimed()

        caption = (
            "DebtReliefBot Balance\n"
            f"$DRB: {drb_amount} ({drb_usd_str})\n"
            f"$WETH: {weth_amount} ({weth_usd_str})\n\n"
            f"{fees_claimed}\n"
            "Historical Fees Claimed"
        )

        await msg.reply_photo(photo=donut, caption=caption)

    except Exception:
        await msg.reply_text("Error fetching balances")


def main():
    app = (
        ApplicationBuilder()
        .token(os.environ["BOT_TOKEN"])
        .build()
    )

    app.add_handler(CommandHandler("grok", grok_command))

    app.run_polling()


if __name__ == "__main__":
    main()

