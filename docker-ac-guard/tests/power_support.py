"""Physical power observations for explicitly monitored owned test processes."""
import datetime
import json
from pathlib import Path
import signal
import threading
from zoneinfo import ZoneInfo

PHYSICAL_AC = Path("/sys/class/power_supply/ACAD/online")


class PowerMonitor:
    def __init__(self, directory, children):
        self.directory = directory
        self.children = children
        self.lost = threading.Event()
        self.done = threading.Event()
        self.finished = threading.Event()
        self.errors = []
        self.thread = threading.Thread(target=self.observe)

    @property
    def failed(self):
        return bool(self.errors)

    @property
    def valid_interval(self):
        return self.finished.is_set() and not self.failed and not self.lost.is_set()

    def online(self):
        value = PHYSICAL_AC.read_text().strip()
        if value not in ("0", "1"):
            raise ValueError("Physical AC status is invalid")
        return value == "1"

    def remember_error(self, error):
        self.errors.append(error)

    def cancel_child(self, child):
        try:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        except BaseException as error:
            self.remember_error(error)

    def cancel_owned(self):
        try:
            children = self.children()
        except BaseException as error:
            self.remember_error(error)
            return
        for child in children:
            self.cancel_child(child)

    def start(self):
        try:
            online = self.online()
        except BaseException as error:
            self.remember_error(error)
            self.cancel_owned()
            raise RuntimeError("Physical AC observation could not start") from error
        if not online:
            self.lost.set()
            self.cancel_owned()
            raise RuntimeError("Physical AC is offline before the test")
        try:
            self.thread.start()
        except BaseException as error:
            self.remember_error(error)
            self.cancel_owned()
            raise RuntimeError("Physical observer thread could not start") from error

    def record(self, output):
        online = self.online()
        if not online:
            self.lost.set()
            self.cancel_owned()
        output.write(json.dumps({
            "time_zagreb": datetime.datetime.now(ZoneInfo("Europe/Zagreb")).isoformat(),
            "online": online,
        }) + "\n")
        output.flush()

    def observe(self):
        try:
            with (self.directory / "physical-power.samples.jsonl").open("w") as output:
                while True:
                    self.record(output)
                    if self.done.wait(1):
                        self.record(output)
                        return
        except BaseException as error:
            # Logging and observation failures invalidate the interval without
            # claiming that the physical AC input reported an outage.
            self.remember_error(error)
            self.cancel_owned()
        finally:
            self.finished.set()

    def check_after_assignment(self, child):
        # A failure can occur while Popen is returning and the tracked list is
        # still empty. Recheck both failure causes after ownership is recorded.
        try:
            if not self.online():
                self.lost.set()
        except BaseException as error:
            self.remember_error(error)
        if self.failed or self.lost.is_set():
            self.cancel_child(child)

    def finish(self):
        if self.thread.ident is None and not self.failed and not self.lost.is_set():
            self.remember_error(RuntimeError("Physical observer was not started"))
        elif self.thread.ident is not None and not self.thread.is_alive() and not self.done.is_set() and not self.failed:
            self.remember_error(RuntimeError("Physical observer ended before completion was requested"))
        self.done.set()
        if self.thread.ident is not None:
            self.thread.join()
        if not self.finished.is_set() and not self.failed and not self.lost.is_set():
            self.remember_error(RuntimeError("Physical observation did not finish"))
        if self.failed:
            raise RuntimeError("Physical observation failed; the test interval is invalid") from self.errors[0]
        if self.lost.is_set():
            raise RuntimeError("Physical AC was offline; the test interval is invalid")
