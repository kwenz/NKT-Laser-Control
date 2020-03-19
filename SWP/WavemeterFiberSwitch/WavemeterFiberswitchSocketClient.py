from .SocketDeviceClient import *

class WavemeterFiberswitchSocketClient:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setblocking(False)
        self.sock.connect_ex((host, port))
        self.sel = selectors.DefaultSelector()
        self.device_name = "WavemeterFiberSwitch"

    def _createRequest(self, action, value):
        return dict(
            type="text/json",
            encoding="utf-8",
            content=dict(action=action, value=value),
        )

    def request(self, action, value):
        """
        Send a request to the DeviceServer
        """
        request = self._createRequest(action, value)
        message = ClientMessage(self.sel, self.sock, (self.host, self.sock), request)
        events = selectors.EVENT_READ | selectors.EVENT_WRITE
        self.sel.register(self.sock, events, data=message)
        # logging.warning('send request')
        try:
            while True:
                events = self.sel.select(timeout=1)
                for key, mask in events:
                    message = key.data
                    try:
                        message.process_events(mask)
                    except Exception as err:
                        logging.warning("{0} socket warning : ".format(self.device_name)
                                       +str(err))
                        message.close()
                # Check for a socket being monitored to continue.
                if not self.sel.get_map():
                    break
        except KeyboardInterrupt:
            logging.warning("{} socket warning : KeyboardInterrupt, ".format(self.device_name)\
                           +"exiting")
            message.close()
        finally:
            self.sel.close()

            return message.result

    def ReadValue(self):
        data = self.request('query', 'ReadValue')
        names = ['seed1', 'seed2', 'cesium']
        return dict((name, value) for name, value in zip(names, data[1]))
