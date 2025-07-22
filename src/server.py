import os.path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from functools import partial

import threading
import time


class RequestHandler(ThreadingMixIn, SimpleHTTPRequestHandler):
    def log_message(self, _, *args):
        pass  # TODO


class Server:
    def __init__(self, port, dst):
        self.running = True
        self.port = port
        self.dst = dst

        cls = partial(RequestHandler, directory=self.dst)
        self.server = ThreadingHTTPServer(('', self.port), cls)

        t = threading.Thread(target=self._run)
        t.start()

    def stop(self):
        self.server.shutdown()

    def _run(self):
        self.server.serve_forever(0.05)


class FileWatcher:
    def __init__(self, files, callback):
        self.files = files
        self.running = True
        self.callback = callback
        self.last_update = 0
        self.event = threading.Event()

        t = threading.Thread(target=self._run)
        t.start()

    def _check(self):
        for file in self.files:
            mod_time = os.path.getmtime(file)
            if mod_time > self.last_update:
                self.last_update = time.time()
                return True

        return False

    def stop(self):
        self.running = False
        self.event.set()

    def _run(self):
        while self.running:
            if self._check():
                self.callback()

            self.event.wait(0.5)

