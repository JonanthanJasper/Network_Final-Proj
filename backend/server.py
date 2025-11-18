"""Simple websocket chat server (no argparse).

Use this file if you prefer not to use argparse. It binds to 0.0.0.0:6789 by default.
"""

import asyncio
import json
import logging
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

clients = set()
messages = []  # list of {'id': int, 'text': str}
next_id = 1


async def broadcast(payload):
    if isinstance(payload, (dict, list)):
        data = json.dumps(payload)
    else:
        data = str(payload)

    if not clients:
        return

    coros = [c.send(data) for c in list(clients)]
    results = await asyncio.gather(*coros, return_exceptions=True)

    for c, res in zip(list(clients), results):
        if isinstance(res, Exception):
            logging.info("Removing dead client: %s", getattr(c, 'remote_address', str(c)))
            clients.discard(c)


async def handle(ws):
    global next_id
    clients.add(ws)
    logging.info("Client connected (%d total)", len(clients))
    try:
        await ws.send(json.dumps({"type": "init", "messages": messages}))

        async for msg in ws:
            if isinstance(msg, str) and msg.startswith("/delete "):
                arg = msg[len("/delete "):].strip()
                if arg == "last":
                    if not messages:
                        continue
                    target_id = messages[-1]["id"]
                else:
                    try:
                        target_id = int(arg)
                    except ValueError:
                        continue

                for i, m in enumerate(messages):
                    if m["id"] == target_id:
                        del messages[i]
                        await broadcast({"type": "delete", "id": target_id})
                        break
                continue

            mid = next_id
            next_id += 1
            messages.append({"id": mid, "text": msg})
            await broadcast({"type": "message", "id": mid, "text": msg})
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        clients.discard(ws)
        logging.info("Client disconnected (%d total)", len(clients))


async def main(host: str = "0.0.0.0", port: int = 6789):
    async with websockets.serve(handle, host, port):
        logging.info("Server running at ws://%s:%d", host, port)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Server shutting down")
