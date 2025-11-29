
"""GTK (PyGObject) GUI websocket client for the chat server.

This client uses GTK3 via PyGObject which is commonly available on Debian-based
systems as the `python3-gi` package. It avoids tkinter and runs the websocket
asyncio loop in a background thread, communicating with the GUI through a
thread-safe queue. The GUI polls the queue via GLib timeout.

Usage:
  python3 client.py [--host HOST] [--port PORT]

If PyGObject is not installed, the script will print a helpful message.
"""

import argparse
import asyncio
import json
import threading
import queue
import sys

try:
	import gi
	gi.require_version("Gtk", "3.0")
	from gi.repository import Gtk, GLib
except Exception as e:
	print("PyGObject (GTK) is required for the GUI client.")
	print("On Debian/Ubuntu install: sudo apt install python3-gi gir1.2-gtk-3.0")
	raise

import websockets


class GtkClientGUI:
	def __init__(self, uri: str):
		self.uri = uri
		self.gui_to_ws = queue.Queue()
		self.ws_to_gui = queue.Queue()

		# build UI
		self.window = Gtk.Window(title="Chat Client")
		self.window.set_default_size(800, 480)
		self.window.connect("delete-event", self._on_window_close)

		hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
		self.window.add(hbox)

		# left: clients
		vleft = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
		hbox.pack_start(vleft, False, False, 6)
		lbl = Gtk.Label(label="Clients")
		lbl.set_xalign(0)
		vleft.pack_start(lbl, False, False, 0)
		self.clients_box = Gtk.ListBox()
		self.clients_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
		self.clients_box.connect("row-activated", self._on_client_activated)
		client_scroll = Gtk.ScrolledWindow()
		client_scroll.set_min_content_width(260)
		client_scroll.add(self.clients_box)
		vleft.pack_start(client_scroll, True, True, 0)

		# right: messages and entry
		vright = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
		hbox.pack_start(vright, True, True, 0)

		msg_label = Gtk.Label(label="Messages")
		msg_label.set_xalign(0)
		vright.pack_start(msg_label, False, False, 0)

		self.msg_buffer = Gtk.TextBuffer()
		self.msg_view = Gtk.TextView(buffer=self.msg_buffer)
		self.msg_view.set_editable(False)
		self.msg_view.set_wrap_mode(Gtk.WrapMode.WORD)
		msg_scroll = Gtk.ScrolledWindow()
		msg_scroll.add(self.msg_view)
		vright.pack_start(msg_scroll, True, True, 0)

		entry_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
		vright.pack_start(entry_box, False, False, 0)
		self.entry = Gtk.Entry()
		self.entry.set_hexpand(True)
		self.entry.connect("activate", lambda w: self._on_send())
		entry_box.pack_start(self.entry, True, True, 0)
		send_btn = Gtk.Button(label="Send")
		send_btn.connect("clicked", lambda w: self._on_send())
		entry_box.pack_start(send_btn, False, False, 0)
		exit_btn = Gtk.Button(label="Exit")
		exit_btn.connect("clicked", lambda w: self._on_exit())
		entry_box.pack_start(exit_btn, False, False, 0)

		# storage
		self.messages = {}
		self.clients = []

		# websocket thread
		self.ws_thread = threading.Thread(target=self._ws_thread_entry, daemon=True)

	def run(self):
		self.window.show_all()
		self.ws_thread.start()
		# poll queue every 100ms
		GLib.timeout_add(100, self._poll)
		Gtk.main()

	def _on_window_close(self, *args):
		# send /exit then quit GTK main loop
		try:
			self.gui_to_ws.put('/exit')
		except Exception:
			pass
		GLib.idle_add(Gtk.main_quit)
		return True

	def _append_text(self, text: str):
		end_iter = self.msg_buffer.get_end_iter()
		self.msg_buffer.insert(end_iter, text + "\n")

	def _render_clients(self):
		# clear
		for child in self.clients_box.get_children():
			self.clients_box.remove(child)
		for c in self.clients:
			lbl = Gtk.Label(label=f"{c.get('name')} — {c.get('id')}", xalign=0)
			row = Gtk.ListBoxRow()
			row.add(lbl)
			row.client_id = c.get('id')
			self.clients_box.add(row)
		self.clients_box.show_all()

	def _on_client_activated(self, listbox, row):
		# prefill /to
		cid = getattr(row, 'client_id', None)
		if cid:
			self.entry.set_text(f"/to {cid} ")
			self.entry.grab_focus()

	def _on_send(self):
		txt = self.entry.get_text().strip()
		if not txt:
			return
		self.gui_to_ws.put(txt)
		self.entry.set_text("")

	def _on_exit(self):
		# send /exit and quit
		self.gui_to_ws.put('/exit')
		GLib.idle_add(Gtk.main_quit)

	def _poll(self):
		try:
			while True:
				obj = self.ws_to_gui.get_nowait()
				self._handle_ws_event(obj)
		except queue.Empty:
			pass
		return True  # continue calling

	def _handle_ws_event(self, obj):
		t = obj.get('type') if isinstance(obj, dict) else None
		if t == 'init':
			you = obj.get('you')
			if you:
				self.window.set_title(f"Chat — {you.get('name')}")
			self.clients = obj.get('clients', [])
			self.messages.clear()
			# clear messages
			start = self.msg_buffer.get_start_iter()
			end = self.msg_buffer.get_end_iter()
			self.msg_buffer.delete(start, end)
			for mm in obj.get('messages', []):
				self.messages[mm['id']] = mm
				self._append_text(self._format_message(mm))
			self._render_clients()
		elif t == 'clients':
			self.clients = obj.get('clients', [])
			self._render_clients()
		elif t == 'message':
			self.messages[obj.get('id')] = obj
			self._append_text(self._format_message(obj))
		elif t == 'delete':
			mid = obj.get('id')
			if mid in self.messages:
				# remove from local cache then re-render full history so deleted
				# messages don't appear in the UI (keeps ordering consistent)
				self.messages.pop(mid, None)
				self._refresh_messages()
		elif t == 'connected':
			self._append_text('Connected to server')
		elif t == 'closed':
			self._append_text('Disconnected from server')
		elif t == 'error':
			self._append_text(f"Error: {obj.get('error')}")
		else:
			try:
				self._append_text(json.dumps(obj))
			except Exception:
				self._append_text(str(obj))

	def _format_message(self, m: dict) -> str:
		mid = m.get('id')
		txt = m.get('text')
		frm = m.get('from_name') or m.get('from_id')
		to = m.get('to_id')
		reply_to = m.get('reply_to')
		parts = [f"[{mid}]"]
		if frm:
			parts.append(str(frm))
		if to:
			parts.append(f"-> {to}")
		header = ' '.join(parts)
		out = f"{header}: {txt}"
		if reply_to is not None:
			orig = self.messages.get(reply_to, {}).get('text')
			out += f"\n  ↪ reply to [{reply_to}]: {orig or '(deleted)'}"
		return out

	def _refresh_messages(self):
		"""Clear the message buffer and re-render messages in ID order.

		The server broadcasts deletions as individual events; to ensure the
		UI matches server state and ordering we rebuild the visible history
		from our local cache whenever a delete occurs.
		"""
		# clear buffer
		start = self.msg_buffer.get_start_iter()
		end = self.msg_buffer.get_end_iter()
		self.msg_buffer.delete(start, end)
		# render in ascending id order
		for mid in sorted(self.messages.keys()):
			mm = self.messages[mid]
			self._append_text(self._format_message(mm))

	def _ws_thread_entry(self):
		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)

		async def ws_main():
			uri = self.uri
			try:
				async with websockets.connect(uri) as ws:
					self.ws_to_gui.put({'type': 'connected'})

					async def receiver():
						try:
							async for raw in ws:
								try:
									obj = json.loads(raw)
								except Exception:
									obj = {'type': 'raw', 'data': raw}
								self.ws_to_gui.put(obj)
						except Exception:
							return

					async def sender():
						try:
							while True:
								msg = await loop.run_in_executor(None, self.gui_to_ws.get)
								if msg is None:
									break
								await ws.send(msg)
								if msg.strip() == '/exit':
									break
						except Exception:
							return

					await asyncio.gather(receiver(), sender())
			except Exception as e:
				self.ws_to_gui.put({'type': 'error', 'error': str(e)})
			finally:
				self.ws_to_gui.put({'type': 'closed'})

		loop.run_until_complete(ws_main())


def run_gui(uri: str):
	gui = GtkClientGUI(uri)
	gui.run()


def main():
	ap = argparse.ArgumentParser(description='GTK chat client')
	ap.add_argument('--host', default='localhost')
	ap.add_argument('--port', default=6789, type=int)
	args = ap.parse_args()
	uri = f"ws://{args.host}:{args.port}"
	run_gui(uri)


if __name__ == '__main__':
	main()

