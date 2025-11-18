import asyncio
import json
import websockets

clients = set()
messages = []  # list of {'id': int, 'text': str}
next_id = 1

async def handle(ws):
    global next_id
    clients.add(ws)
    try:
        # send existing history to the new client
        init_payload = json.dumps({"type": "init", "messages": messages})
        await ws.send(init_payload)

        async for msg in ws:
            # simple command protocol: /delete <id> or /delete last
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
                        # invalid id, ignore
                        continue

                # find and remove message with target_id
                for i, m in enumerate(messages):
                    if m["id"] == target_id:
                        del messages[i]
                        payload = json.dumps({"type": "delete", "id": target_id})
                        # broadcast delete to all clients
                        await asyncio.gather(*[c.send(payload) for c in clients])
                        break
                continue

            # normal message: assign id, store and broadcast
            mid = next_id
            next_id += 1
            messages.append({"id": mid, "text": msg})
            payload = json.dumps({"type": "message", "id": mid, "text": msg})
            # broadcast to all clients (including sender) so UIs stay consistent
            await asyncio.gather(*[c.send(payload) for c in clients])
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        clients.remove(ws)


async def main():
    async with websockets.serve(handle, "localhost", 6789):
        print("Server running at ws://localhost:6789")
        await asyncio.Future()  # Run forever


if __name__ == "__main__":
    asyncio.run(main())