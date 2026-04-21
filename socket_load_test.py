"""
Minimal Socket.IO load test for concurrent JWT-authenticated connections.

Usage:
    python socket_load_test.py

This script focuses only on connection scalability:
- opens many websocket connections
- keeps them alive briefly
- reports connection success/failure counts
- prints simple latency and connections-per-second metrics
"""

import asyncio
import math
import statistics
import time
import uuid

import jwt
import socketio

# ── Config ────────────────────────────────────────────────────────────────────
# The URL is stripped to avoid accidental trailing whitespace causing failures.
SERVER_URL = "https://stage-love-doc-socket-server.lovedr.in ".strip()
JWT_SECRET = "peoplelovedocsocketsecret@2026"
TOTAL_CONNECTIONS = 100
CONCURRENCY = 20
HOLD_DURATION = 10
RAMP_DELAY = 0.05
HANDSHAKE_TIMEOUT = 10
# ──────────────────────────────────────────────────────────────────────────────

stats = {
    "connected": 0,
    "failed": 0,
    "latencies": [],
}
lock = asyncio.Lock()


def mint_token(user_index: int) -> str:
    """Mint a socket JWT for one synthetic user."""
    now = int(time.time())
    payload = {
        "userId": f"loadtest-user-{user_index}",
        "role": "user",
        "name": f"Load Test {user_index}",
        "sessionId": str(uuid.uuid4()),
        "jti": f"{now}-{uuid.uuid4().hex[:8]}",
        "iat": now,
        "exp": now + 7200,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


async def run_single_connection(user_index: int) -> None:
    """Open one socket, wait briefly, then disconnect."""
    sio = socketio.AsyncClient(
        reconnection=False,
        logger=False,
        engineio_logger=False,
    )
    handshake_event = asyncio.Event()
    connect_start = time.monotonic()
    result_recorded = False

    async def record_success() -> None:
        nonlocal result_recorded
        latency_ms = (time.monotonic() - connect_start) * 1000
        async with lock:
            if result_recorded:
                return
            result_recorded = True
            stats["connected"] += 1
            stats["latencies"].append(latency_ms)
            print(
                f"[+] #{user_index:>4} connected  | "
                f"latency={latency_ms:6.1f}ms | "
                f"total_connected={stats['connected']}"
            )
        handshake_event.set()

    async def record_failure(reason: str) -> None:
        nonlocal result_recorded
        async with lock:
            if result_recorded:
                return
            result_recorded = True
            stats["failed"] += 1
            print(f"[!] #{user_index:>4} FAILED     | {reason}")
        handshake_event.set()

    @sio.event
    async def connect():
        await record_success()

    @sio.event
    async def connect_error(data):
        await record_failure(str(data))

    token = mint_token(user_index)
    try:
        await sio.connect(
            SERVER_URL,
            auth={"token": token},
            transports=["websocket"],
        )
        await asyncio.wait_for(handshake_event.wait(), timeout=HANDSHAKE_TIMEOUT)

        if sio.connected:
            await asyncio.sleep(HOLD_DURATION)
    except asyncio.TimeoutError:
        await record_failure("TIMEOUT")
    except Exception as exc:
        await record_failure(f"ERROR: {exc}")
    finally:
        if sio.connected:
            await sio.disconnect()


async def main() -> None:
    print(f"\n{'=' * 60}")
    print(f"  Load test: {TOTAL_CONNECTIONS} connections to {SERVER_URL}")
    print(f"  Concurrency: {CONCURRENCY} | Hold: {HOLD_DURATION}s")
    print(f"{'=' * 60}\n")

    overall_start = time.monotonic()
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def bounded(connection_number: int) -> None:
        async with semaphore:
            await asyncio.sleep(connection_number * RAMP_DELAY)
            await run_single_connection(connection_number)

    await asyncio.gather(
        *[bounded(i) for i in range(1, TOTAL_CONNECTIONS + 1)]
    )

    elapsed = time.monotonic() - overall_start
    latencies = sorted(stats["latencies"])
    connections_per_second = stats["connected"] / elapsed if elapsed else 0.0

    print(f"\n{'=' * 60}")
    print("  RESULTS")
    print(f"{'=' * 60}")
    print(f"  Total attempted : {TOTAL_CONNECTIONS}")
    print(f"  Connected OK    : {stats['connected']}")
    print(f"  Failed          : {stats['failed']}")
    print(f"  Elapsed         : {elapsed:.1f}s")
    print(f"  Connected/sec   : {connections_per_second:.2f}")
    if latencies:
        p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1)
        print(f"  Latency min     : {latencies[0]:.1f}ms")
        print(f"  Latency median  : {statistics.median(latencies):.1f}ms")
        print(f"  Latency p95     : {latencies[p95_index]:.1f}ms")
        print(f"  Latency max     : {latencies[-1]:.1f}ms")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    asyncio.run(main())
