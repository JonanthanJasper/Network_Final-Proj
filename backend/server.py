"""Simple websocket chat server (no argparse).

Use this file if you prefer not to use argparse. It binds to 0.0.0.0:6789 by default.
"""

import asyncio
import json
import logging
import websockets
import uuid
import random
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

clients = set()
clients_info = {}  # websocket -> {'id': str, 'name': str}
id_to_ws = {}

messages = []  # list of {'id': int, 'text': str, 'reply_to': Optional[int], 'from_id': str, 'from_name': str, 'to_id': Optional[str]}
next_id = 1
temp_tasks = {}  # mid -> asyncio.Task


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
            # cleanup client info
            if c in clients_info:
                cid = clients_info[c]['id']
                id_to_ws.pop(cid, None)
                clients_info.pop(c, None)
                # notify remaining clients of updated client list
                await broadcast_clients()


async def handle(ws):
    global next_id
    clients.add(ws)
    # assign an id and name for this client
    short = uuid.uuid4().hex[:6]
    name = f"User{random.randint(100,999)}"
    client_id = f"{name}-{short}"
    clients_info[ws] = {'id': client_id, 'name': name}
    id_to_ws[client_id] = ws
    logging.info("Client %s connected (%d total)", client_id, len(clients))
    try:
        # send initial state including assigned id and current clients
        await ws.send(json.dumps({"type": "init", "you": clients_info[ws], "messages": messages, "clients": list(clients_info.values())}))
        # broadcast updated clients to all
        await broadcast_clients()

        async for msg in ws:
            # /delete command
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
                        # cancel any temp task
                        t = temp_tasks.pop(target_id, None)
                        if t:
                            t.cancel()
                        await broadcast({"type": "delete", "id": target_id})
                        break
                continue

            # /reply <id> <text>
            if isinstance(msg, str) and msg.startswith("/reply "):
                rest = msg[len("/reply "):].strip()
                parts = rest.split(" ", 1)
                if len(parts) != 2:
                    continue
                try:
                    reply_to = int(parts[0])
                except ValueError:
                    continue
                text = parts[1]
                sender = clients_info.get(ws, {'id': None, 'name': None})
                mid = next_id
                next_id += 1
                messages.append({"id": mid, "text": text, "reply_to": reply_to, 'from_id': sender['id'], 'from_name': sender['name'], 'to_id': None})
                await broadcast({"type": "message", "id": mid, "text": text, "reply_to": reply_to, 'from_id': sender['id'], 'from_name': sender['name'], 'to_id': None})
                continue

            # /temp <seconds> <text> -- temporary public message
            if isinstance(msg, str) and msg.startswith('/temp '):
                rest = msg[len('/temp '):].strip()
                parts = rest.split(' ', 1)
                if len(parts) != 2:
                    continue
                try:
                    ttl = int(parts[0])
                except ValueError:
                    continue
                text = parts[1]
                sender = clients_info.get(ws, {'id': None, 'name': None})
                mid = next_id
                next_id += 1
                expires_at = time.time() + ttl
                messages.append({"id": mid, "text": text, "reply_to": None, 'from_id': sender['id'], 'from_name': sender['name'], 'to_id': None, 'expires_at': expires_at})
                await broadcast({"type": "message", "id": mid, "text": text, "reply_to": None, 'from_id': sender['id'], 'from_name': sender['name'], 'to_id': None, 'expires_at': expires_at})

                # schedule deletion
                async def expire(mid_local, delay):
                    try:
                        await asyncio.sleep(delay)
                        # remove message if still present
                        for i, m in enumerate(messages):
                            if m['id'] == mid_local:
                                del messages[i]
                                temp_tasks.pop(mid_local, None)
                                await broadcast({"type": "delete", "id": mid_local})
                                break
                    except asyncio.CancelledError:
                        return

                task = asyncio.create_task(expire(mid, ttl))
                temp_tasks[mid] = task
                continue

            # /to <client_id> <text>  -> direct message to another client
            if isinstance(msg, str) and msg.startswith("/to "):
                rest = msg[len("/to "):].strip()
                parts = rest.split(" ", 1)
                if len(parts) != 2:
                    continue
                target_cid, text = parts[0], parts[1]
                target_ws = id_to_ws.get(target_cid)
                sender = clients_info.get(ws, {'id': None, 'name': None})
                mid = next_id
                next_id += 1
                messages.append({"id": mid, "text": text, "reply_to": None, 'from_id': sender['id'], 'from_name': sender['name'], 'to_id': target_cid})
                payload = {"type": "message", "id": mid, "text": text, "reply_to": None, 'from_id': sender['id'], 'from_name': sender['name'], 'to_id': target_cid}
                # send to target and sender only
                send_to = []
                if target_ws in clients:
                    send_to.append(target_ws)
                send_to.append(ws)
                coros = [c.send(json.dumps(payload)) for c in send_to]
                await asyncio.gather(*coros, return_exceptions=True)
                continue

            # /exit command: client requests to disconnect
            if isinstance(msg, str) and msg.strip() == "/exit":
                sender = clients_info.get(ws, {'id': None})
                logging.info("Client %s requested exit", sender.get('id'))
                # close the websocket; the finally block below will handle cleanup and broadcast
                try:
                    await ws.close()
                except Exception:
                    # ignore errors closing the socket; connection cleanup will follow
                    pass
                break

            # normal public message
            sender = clients_info.get(ws, {'id': None, 'name': None})
            mid = next_id
            next_id += 1
            messages.append({"id": mid, "text": msg, "reply_to": None, 'from_id': sender['id'], 'from_name': sender['name'], 'to_id': None})
            await broadcast({"type": "message", "id": mid, "text": msg, "reply_to": None, 'from_id': sender['id'], 'from_name': sender['name'], 'to_id': None})
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        clients.discard(ws)
        # cleanup client info
        info = clients_info.pop(ws, None)
        if info:
            id_to_ws.pop(info['id'], None)
        logging.info("Client disconnected (%d total)", len(clients))
        await broadcast_clients()


async def broadcast_clients():
    """Send the current client list to all connected clients."""
    lst = list(clients_info.values())
    await broadcast({"type": "clients", "clients": lst})


async def main(host: str = "0.0.0.0", port: int = 6789):
    async with websockets.serve(handle, host, port):
        logging.info("Server running at ws://%s:%d", host, port)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Server shutting down")
