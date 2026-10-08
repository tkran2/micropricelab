"""Export only observed trusted publications, with explicit connection epochs."""
import csv
from decimal import Decimal
from pathlib import Path

from .feed import SCALE, decimal
from .journal import records

def features(path, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output.open("x", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["generation", "sequence", "received_ns", "bid_price", "bid_size",
                         "ask_price", "ask_size", "mid_price", "spread", "top_imbalance", "microprice"])
        for _, event in records(path):
            if event.get("kind") != "publication":
                continue
            quote = event["quote"]
            if not quote["trusted"]:
                raise ValueError("untrusted publication in journal")
            bid, ask = quote["bid"], quote["ask"]
            row = [quote["generation"], quote["sequence"], event.get("source_received_ns", "")]
            row += [decimal(bid[0]), decimal(bid[1])] if bid else ["", ""]
            row += [decimal(ask[0]), decimal(ask[1])] if ask else ["", ""]
            if bid and ask:
                total = Decimal(bid[1] + ask[1])
                row += [str(Decimal(bid[0] + ask[0]) / (2 * SCALE)),
                        decimal(ask[0] - bid[0]), str(Decimal(bid[1] - ask[1]) / total),
                        str((Decimal(ask[0]) * bid[1] + Decimal(bid[0]) * ask[1]) / total / SCALE)]
            else:
                row += ["", "", "", ""]
            writer.writerow(row)
            count += 1
    return {"rows": count, "output": str(output)}
