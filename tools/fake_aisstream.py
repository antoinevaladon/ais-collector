#!/usr/bin/env python3
"""
Fake aisstream.io server, for testing the collector without an API key or
internet connection. It speaks the same protocol: wait for the subscription,
send a confirmation, then stream made-up vessels inside your bounding boxes.

    venv/bin/python tools/fake_aisstream.py --rate 20
    AISSTREAM_URL=ws://localhost:8765 venv/bin/python collector.py

Rehearse failures:
    --drop-after 30     close every connection after 30 s (tests reconnecting)
    --stall-after 30    stop sending but keep the connection open (tests stale detection)
    --poison-every 100  every 100th message is one PostgreSQL can't store (tests rejection)
    API key "bad"       the server answers with an error, like a real invalid key
"""

import argparse
import asyncio
import json
import random
from datetime import datetime, timezone

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

NAMES = ["CARIB TRADER", "WINDWARD STAR", "GONAVE EXPRESS", "ATLANTIC HOPE", "SEA PIONEER",
         "BLUE MARLIN", "OCEAN SPIRIT", "CAPE VOYAGER", "ISLAND QUEEN", "NORTHERN LIGHT"]


def make_fleet(boxes, size):
    fleet = []
    for i in range(size):
        (lat1, lon1), (lat2, lon2) = random.choice(boxes)
        fleet.append({
            "mmsi": random.randint(200_000_000, 775_999_999),
            "name": f"{random.choice(NAMES)} {i + 1}",
            "lat": random.uniform(min(lat1, lat2), max(lat1, lat2)),
            "lon": random.uniform(min(lon1, lon2), max(lon1, lon2)),
            "cog": random.uniform(0, 360),
            "sog": random.uniform(0, 18),
            "type": random.choice([70, 71, 79, 80, 84, 89, 30, 90]),
        })
    return fleet


def envelope(vessel, mtype, body):
    now = datetime.now(timezone.utc)
    return {
        "MessageType": mtype,
        "MetaData": {
            "MMSI": vessel["mmsi"],
            "MMSI_String": vessel["mmsi"],
            "ShipName": vessel["name"].ljust(20),
            "latitude": vessel["lat"],
            "longitude": vessel["lon"],
            "time_utc": now.strftime("%Y-%m-%d %H:%M:%S.%f +0000 UTC"),
        },
        "Message": {mtype: body},
    }


def position_report(v):
    v["lat"] += random.uniform(-0.002, 0.002)
    v["lon"] += random.uniform(-0.002, 0.002)
    return envelope(v, "PositionReport", {
        "UserID": v["mmsi"], "MessageID": 1, "Latitude": v["lat"], "Longitude": v["lon"],
        "Cog": round(v["cog"], 1), "Sog": round(v["sog"], 1), "TrueHeading": int(v["cog"]),
        "NavigationalStatus": 0, "RateOfTurn": 0, "PositionAccuracy": True, "Raim": False,
        "Timestamp": datetime.now(timezone.utc).second, "Valid": True,
    })


def static_data(v):
    return envelope(v, "ShipStaticData", {
        "UserID": v["mmsi"], "MessageID": 5, "Name": v["name"].ljust(20), "Type": v["type"],
        "CallSign": "FAKE" + str(v["mmsi"])[-3:], "Destination": "PORT-AU-PRINCE",
        "ImoNumber": 0, "Dimension": {"A": 100, "B": 20, "C": 10, "D": 10}, "Valid": True,
    })


async def handler(ws, args):
    try:
        sub = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
    except asyncio.TimeoutError:
        await ws.close(code=1008, reason="no subscription within 3 s")
        return
    if sub.get("APIKey") in (None, "", "bad"):
        await ws.send(json.dumps({"error": "Api Key Is Not Valid"}).encode())
        await ws.close()
        return
    boxes = sub.get("BoundingBoxes") or [[[17, -81], [27, -68]]]
    print(f"client subscribed: {len(boxes)} box(es)")
    await ws.send(json.dumps({"MessageType": "SubscriptionConfirmation",
                              "Message": {"CompressionEnabled": True}}).encode())

    fleet = make_fleet(boxes, args.vessels)
    loop = asyncio.get_running_loop()
    started = loop.time()
    sent = 0
    while True:
        elapsed = loop.time() - started
        if args.drop_after and elapsed > args.drop_after:
            print("dropping connection (test)")
            await ws.close(code=1011, reason="test drop")
            return
        if args.stall_after and elapsed > args.stall_after:
            await asyncio.sleep(1)       # connection stays open, nothing is sent
            continue
        vessel = random.choice(fleet)
        msg = static_data(vessel) if random.random() < 0.1 else position_report(vessel)
        sent += 1
        if args.poison_every and sent % args.poison_every == 0:
            msg["Message"]["PositionReport" if "PositionReport" in msg["Message"] else "ShipStaticData"]["Sog"] = float("nan")
        await ws.send(json.dumps(msg).encode())
        await asyncio.sleep(1 / args.rate)


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--rate", type=float, default=10, help="messages per second")
    p.add_argument("--vessels", type=int, default=50)
    p.add_argument("--drop-after", type=float, default=0)
    p.add_argument("--stall-after", type=float, default=0)
    p.add_argument("--poison-every", type=int, default=0)
    args = p.parse_args()
    async def guarded(ws):
        try:
            await handler(ws, args)
        except ConnectionClosed:
            print("client disconnected")

    async with serve(guarded, args.host, args.port):
        print(f"fake aisstream listening on ws://{args.host}:{args.port}")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
