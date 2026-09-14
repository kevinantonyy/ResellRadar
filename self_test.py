"""Offline smoke test for ResellRadar core. Does not call retailer/market APIs."""
from __future__ import annotations

import re
import sqlite3
import sys
import types
from pathlib import Path

src = Path(__file__).with_name("resell_core.py").read_text(encoding="utf-8")
src = re.sub(r"\ninit_db\(\)\s*$", "\n", src)
mod = types.ModuleType("resell_core_smoke")
mod.__file__ = "resell_core.py"
sys.modules[mod.__name__] = mod
exec(src, mod.__dict__)
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
mod.db = lambda: conn
mod.init_db()
mod.add_watch("RTX 5070 Ti", "GPU", 3)
mod.add_comp("RTX 5070 Ti", "eBay", 850, 12, 13.25, 20, 30, identifier="5070ti")
hit = mod.DealCandidate("NVIDIA GeForce RTX 5070 Ti 16GB 5070ti", "Test", "https://example.com", 150, identifier="5070ti")
scored = mod.score_candidate(hit)
assert scored["status"] == "BUY"
assert scored["grade"] == "A+"
hit_id, created = mod.upsert_hit(hit, scored)
assert created and hit_id > 0
inventory_id = mod.purchase_from_hit(hit_id)
assert inventory_id > 0
profit = mod.mark_sold(inventory_id, 300, "eBay", 20, 10)
assert profit == 120.0
print("ResellRadar core smoke test: PASS")
