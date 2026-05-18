#!/usr/bin/env python3
from hotstuff import InfoClient
from hotstuff.methods.info.market import InstrumentsParams

info = InfoClient(is_testnet=False)
instruments = info.instruments(InstrumentsParams(type='perps')).perps

print(f"{'ID':<6} {'Market':<20}")
print("-" * 26)
for p in sorted(instruments, key=lambda x: x['id'] if isinstance(x, dict) else x.id):
    pid  = p['id']   if isinstance(p, dict) else p.id
    name = p['name'] if isinstance(p, dict) else p.name
    print(f"{pid:<6} {name:<20}")

print(f"\nTotal: {len(instruments)} markets")
