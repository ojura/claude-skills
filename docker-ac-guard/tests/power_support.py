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
        self.thread = threading.Thread(target=self.observe)

    def online(self):
        try:
            return PHYSICAL_AC.read_text().strip() == "1"
        except OSError:
            return False

    def start(self):
        if not self.online():
            raise RuntimeError("Physical AC is offline before the test")
        self.thread.start()

    def observe(self):
        with (self.directory / "physical-power.samples.jsonl").open("w") as output:
            while True:
                online = self.online()
                output.write(json.dumps({"time_zagreb": datetime.datetime.now(ZoneInfo("Europe/Zagreb")).isoformat(), "online": online}) + "\n")
                output.flush()
                if not online:
                    self.lost.set()
                    for child in self.children():
                        if child.poll() is None:
                            try:
                                child.send_signal(signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                if self.done.wait(1):
                    online = self.online()
                    output.write(json.dumps({"time_zagreb": datetime.datetime.now(ZoneInfo("Europe/Zagreb")).isoformat(), "online": online}) + "\n")
                    output.flush()
                    if not online:
                        self.lost.set()
                    return

    def check_after_assignment(self, child):
        # The monitor can observe an empty child list while Popen is returning.
        # Recheck after the fixture has recorded ownership of the new process.
        if self.lost.is_set() or not self.online():
            self.lost.set()
            if child.poll() is None:
                try:
                    child.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def finish(self):
        self.done.set()
        self.thread.join()
        if self.lost.is_set():
            raise RuntimeError("Physical AC was lost; the test interval is invalid")
