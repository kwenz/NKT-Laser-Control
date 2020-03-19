import importlib
import sys
import socket
import selectors
import traceback
import threading
from collections import deque
import random
import logging
import time
from types import FunctionType
import functools
import json
import io
import struct
import inspect
from queue import Queue
import numpy as np
import copy
from influxdb import InfluxDBClient

#############################################
# Class for server side messages
#############################################

class ServerMessage:
    """
    ServerMessage class for communication between the SocketDeviceServer and
    SocketDeviceClient classes.
    A message has the following structure:
    - fixed-lenght header
    - json header
    - content
    See https://realpython.com/python-sockets/#application-client-and-server
    for a more thorough explanation, most of the code is adapted from this.
    """
    def __init__(self, obj, selector, sock, addr, data, timeout):
        self.selector = selector
        self.sock = sock
        self.addr = addr
        self._recv_buffer = b""
        self._send_buffer = b""
        self._jsonheader_len = None
        self.jsonheader = None
        self.request = None
        self.response_created = False

        self.obj = obj
        self.data = data
        self.timeout = timeout

    def _set_selector_events_mask(self, mode):
        """Set selector to listen for events: mode is 'r', 'w', or 'rw'."""
        if mode == "r":
            events = selectors.EVENT_READ
        elif mode == "w":
            events = selectors.EVENT_WRITE
        elif mode == "rw":
            events = selectors.EVENT_READ | selectors.EVENT_WRITE
        else:
            raise ValueError(f"Invalid events mask mode {repr(mode)}.")
        self.selector.modify(self.sock, events, data=self)

    def _read(self):
        try:
            # Should be ready to read
            data = self.sock.recv(4096)
        except BlockingIOError:
            # Resource temporarily unavailable (errno EWOULDBLOCK)
            pass
        else:
            if data:
                self._recv_buffer += data
            else:
                raise RuntimeError("Peer closed.")

    def _write(self):
        if self._send_buffer:
            try:
                # Should be ready to write
                sent = self.sock.send(self._send_buffer)
            except BlockingIOError:
                # Resource temporarily unavailable (errno EWOULDBLOCK)
                pass
            else:
                self._send_buffer = self._send_buffer[sent:]
                # Close when the buffer is drained. The response has been sent.
                if sent and not self._send_buffer:
                    self.close()

    def _json_encode(self, obj, encoding):
        return json.dumps(obj, ensure_ascii=False).encode(encoding)

    def _json_decode(self, json_bytes, encoding):
        tiow = io.TextIOWrapper(
            io.BytesIO(json_bytes), encoding=encoding, newline=""
        )
        obj = json.load(tiow)
        tiow.close()
        return obj

    def _create_message(
        self, *, content_bytes, content_type, content_encoding
    ):
        jsonheader = {
            "byteorder": sys.byteorder,
            "content-type": content_type,
            "content-encoding": content_encoding,
            "content-length": len(content_bytes),
        }
        jsonheader_bytes = self._json_encode(jsonheader, "utf-8")
        message_hdr = struct.pack(">H", len(jsonheader_bytes))
        message = message_hdr + jsonheader_bytes + content_bytes
        return message

    def _create_response_json_content(self):
        action = self.request.get("action")
        if action == "query":
            query = self.request.get("value")
            if self.data.get(query):
                content = {"result": self.data.get(query)}
            else:
                content = {"error": f'No match for "{query}".'}
        elif action == "command":
            command = self.request.get("value")
            try:
                retval = eval('self.obj.{0}'.format(command))
                if not retval is None:
                    content = {"result": retval}
                else:
                    content = {"result": "command {} performed".format(action)}
            except AttributeError:
                content = {"error": "no match for {0}.".format(action)}
        elif action == "info":
            content = {"result":self.data['info']}
        else:
            content = {"error": f'invalid action "{action}".'}
        content_encoding = "utf-8"
        response = {
            "content_bytes": self._json_encode(content, content_encoding),
            "content_type": "text/json",
            "content_encoding": content_encoding,
        }
        return response

    def _create_response_binary_content(self):
        response = {
            "content_bytes": b"First 10 bytes of request: "
            + self.request[:10],
            "content_type": "binary/custom-server-binary-type",
            "content_encoding": "binary",
        }
        return response

    def process_events(self, mask):
        if mask & selectors.EVENT_READ:
            self.read()
        if mask & selectors.EVENT_WRITE:
            self.write()

    def read(self):
        self._read()

        if self._jsonheader_len is None:
            self.process_protoheader()

        if self._jsonheader_len is not None:
            if self.jsonheader is None:
                self.process_jsonheader()

        if self.jsonheader:
            if self.request is None:
                self.process_request()

    def write(self):
        if self.request:
            if not self.response_created:
                self.create_response()

        self._write()

    def close(self):
        try:
            self.selector.unregister(self.sock)
        except Exception as e:
            logging.warning(
                f"error: selector.unregister() exception for",
                f"{self.addr}: {repr(e)}",
            )

        try:
            self.sock.close()
        except OSError as e:
            logging.warning(
                f"error: socket.close() exception for",
                f"{self.addr}: {repr(e)}",
            )
        finally:
            # Delete reference to socket object for garbage collection
            self.sock = None

    def process_protoheader(self):
        hdrlen = 2
        if len(self._recv_buffer) >= hdrlen:
            self._jsonheader_len = struct.unpack(
                ">H", self._recv_buffer[:hdrlen]
            )[0]
            self._recv_buffer = self._recv_buffer[hdrlen:]

    def process_jsonheader(self):
        hdrlen = self._jsonheader_len
        if len(self._recv_buffer) >= hdrlen:
            self.jsonheader = self._json_decode(
                self._recv_buffer[:hdrlen], "utf-8"
            )
            self._recv_buffer = self._recv_buffer[hdrlen:]
            for reqhdr in (
                "byteorder",
                "content-length",
                "content-type",
                "content-encoding",
            ):
                if reqhdr not in self.jsonheader:
                    raise ValueError(f'Missing required header "{reqhdr}".')

    def process_request(self):
        content_len = self.jsonheader["content-length"]
        if not len(self._recv_buffer) >= content_len:
            return
        data = self._recv_buffer[:content_len]
        self._recv_buffer = self._recv_buffer[content_len:]
        if self.jsonheader["content-type"] == "text/json":
            encoding = self.jsonheader["content-encoding"]
            self.request = self._json_decode(data, encoding)
        else:
            # Binary or unknown content-type
            self.request = data
        # Set selector to listen for write events, we're done reading.
        self._set_selector_events_mask("w")

    def create_response(self):
        if self.jsonheader["content-type"] == "text/json":
            response = self._create_response_json_content()
        else:
            # Binary or unknown content-type
            response = self._create_response_binary_content()
        message = self._create_message(**response)
        self.response_created = True
        self._send_buffer += message

#############################################
# Socket Server Class
#############################################

class socketServer(threading.Thread):
    """
    Handles communication with external clients in a separate thread.
    """
    def __init__(self, communication, host, port, timeout):
        threading.Thread.__init__(self)
        self.communication = communication
        self.host = ''
        self.timeout = float(timeout)
        self.port = int(port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen()
        self.sock.setblocking(False)
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.sock, selectors.EVENT_READ, data=None)

        self.active = threading.Event()
        self.active.clear()

    def accept_wrapper(self, sock):
        conn, addr = sock.accept()  # Should be ready to read
        logging.info("{0} accepted connection from".format(self.communication.device_name), addr)
        conn.setblocking(False)
        message = ServerMessage(self.communication.transfer_cavity, self.sel, conn, addr, self.communication.data_server,
                                self.timeout)
        self.sel.register(conn, selectors.EVENT_READ, data=message)

    def run(self):
        self.active.set()
        logging.warning('socketServer running')
        while self.active.is_set():
            events = self.sel.select(timeout = self.timeout)
            for key, mask in events:
                if key.data is None:
                    self.accept_wrapper(key.fileobj)
                else:
                    message = key.data
                    try:
                        message.process_events(mask)
                    except Exception as err:
                        logging.warning("{2} socket warning for "
                                       +"{0}:{1} : ".format(self.host, self.port, self.device.device_name)
                                       +str(err))
                        message.close()

#############################################
# InfluxDB class
#############################################

class InfluxDBCommunication(threading.Thread):
    """

    """
    def __init__(self, device, host, port, username, password, dt):
        threading.Thread.__init__(self)
        self.active = threading.Event()
        self.device = device

        self.influxdb_client = InfluxDBClient(
                host = host,
                port = port,
                username = username,
                password = password
            )
        self.influxdb_client.switch_database("lasers")

        self.dt = dt

        self.col_names = ["cavity lock", "cavity error", "seed 1 lock",
                     "seed 2 lock", "seed 1 error", "seed 2 error", "seed 1 frequency",
                     "seed 2 frequency", "seed 1 lockpoint", "seed 2 lockpoint"]

    def run(self):
        logging.warning('influxDB running')
        while self.active.is_set():
            fields = dict( (key, val) for key, val in zip(self.col_names, self.device.data_server.get('ReadValue'))
                            if not np.isnan(val))

            json_body = [
                    {
                        "measurement": self.device.device_name,
                        "time": int(1000 * time.time()),
                        "fields": fields,
                        }
                    ]
            try:
                self.influxdb_client.write_points(json_body, time_precision='ms')
            except Exception as err:
                logging.warning("InfluxDB error: " + str(err))
                logging.warning(traceback.format_exc())

            time.sleep(self.dt)

#############################################
# Class for network communication with LaserLocking
#############################################

class NetworkIOLocking:
    """
    Network IO for laser lock parameters
    """
    def __init__(self, transfer_cavity, host, port):
        self.device_name = 'Laser Locking 1'

        self.master_locked_flag = False
        self.master_err = np.nan
        self.slave_locked_flags = [np.nan]*2
        self.slave_err = [np.nan]*2
        self.slave_frequency = [np.nan]*2
        self.slave_lockpoint = [np.nan]*2

        self.transfer_cavity = transfer_cavity

        self.thread_communication = socketServer(self, host, int(port), 2)
        # closes communication thread upon closing of main thread
        self.thread_communication.setDaemon(True)
        self.thread_communication.start()

        self.thread_influxdb = InfluxDBCommunication(
                self, "172.28.82.114", 8086, "bsmonitor", "molecules", 5
            )
        self.thread_influxdb.setDaemon(True)
        self.thread_influxdb.start()
        self.thread_influxdb.active.set()

    @property
    def data_server(self):
        return {
                'ReadValue':[self.master_locked_flag, self.master_err]+\
                            self.slave_locked_flags+self.slave_err+\
                            self.slave_frequency+self.slave_lockpoint,
                'verification':'laser locking',
                'info':self.device_name
               }
