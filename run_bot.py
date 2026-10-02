"""Avvio del bot di trading (Alpaca paper)."""
import logging
import sys

from dotenv import load_dotenv

from bot.config import ROOT
from bot.engine import TradingEngine


def main() -> None:
    load_dotenv(ROOT / ".env")
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(ROOT / "logs" / "bot.log")],
    )
    TradingEngine().run_forever()


if __name__ == "__main__":
    main()
