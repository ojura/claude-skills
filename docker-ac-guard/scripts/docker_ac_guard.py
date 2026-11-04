#!/usr/bin/env -S python3 -I
"""Run owned, named Docker jobs on AC without signaling their caller."""
from __future__ import annotations

import datetime
from enum import Enum
import io
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

OWNER_LABEL = "local.ac-guard.owner"
CID = re.compile(r"[a-f0-9]{64}\Z")
INSPECT_FORMAT = (
    '{"Id":{{json .Id}},'
    '"Owner":{{json (index .Config.Labels "local.ac-guard.owner")}},'
    '"Status":{{json .State.Status}},"Running":{{json .State.Running}},'
    '"ExitCode":{{json .State.ExitCode}}}'
)
VALUE_OPTIONS = {
    "--name", "--label", "--network", "--user", "--cpus", "--memory",
    "--pull", "--env", "--volume", "--workdir", "-e", "-v", "-w",
}


class CleanupState(Enum):
    PENDING = "pending"
    UNCERTAIN = "uncertain"
    CONFIRMED = "confirmed"


class InterfaceError(Exception):
    pass


class DockerError(Exception):
    pass


class OwnershipError(Exception):
    pass


def exit_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def parse_run(arguments: list[str]) -> tuple[str, list[str]]:
    """Accept the named, noninteractive --rm profile used by build runners."""
    options = []
    name = None
    remove = False
    has_init = False
    pull_value = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if not argument.startswith("-"):
            if not name or not remove or pull_value != "never":
                raise InterfaceError("Docker run requires a name, --rm and --pull never")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                raise InterfaceError("Docker name is invalid")
            if not has_init:
                options.append("--init")
            return name, options + arguments[index:]
        key, separator, value = argument.partition("=")
        if key == "--rm":
            if separator and value != "true":
                raise InterfaceError("Only --rm or --rm=true is supported")
            remove = True
            index += 1
            continue
        if key == "--init":
            if separator:
                raise InterfaceError("Use the standalone --init option")
            has_init = True
            options.append(argument)
            index += 1
            continue
        if key not in VALUE_OPTIONS:
            raise InterfaceError("Unsupported Docker run option")
        if not separator:
            index += 1
            if index >= len(arguments):
                raise InterfaceError("Docker option is missing its value")
            value = arguments[index]
            options.extend((argument, value))
        else:
            options.append(argument)
        if key == "--pull":
            pull_value = value
        if key == "--name":
            if name is not None:
                raise InterfaceError("Docker name must be specified once")
            name = value
        if key == "--label" and value.partition("=")[0] == OWNER_LABEL:
            raise InterfaceError("The owner label is reserved for this supervisor")
        index += 1
    raise InterfaceError("Docker image is missing")


def discard_failed_stream(name):
    # A later interpreter flush must not replace an already-recorded failure
    # status. These are this supervisor process's streams, not its caller's.
    try:
        getattr(sys, name).close()
    except (OSError, ValueError):
        pass
    setattr(sys, name, io.StringIO())


def private_file(filename: Path, mode: str = "wb"):
    descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, mode)


class Guard:
    def __init__(self, docker: str, arguments: list[str], log_root: Path, ac_path: Path):
        self.name, self.create_arguments = parse_run(arguments)
        root_stat = log_root.stat()
        if not log_root.is_dir() or root_stat.st_uid != os.getuid() or root_stat.st_mode & 0o077:
            raise InterfaceError("The AC log directory must be private and owned by this user")
        self.directory = Path(tempfile.mkdtemp(prefix=self.name + ".", dir=log_root))
        self.docker = docker
        self.ac_path = ac_path
        self.token = secrets.token_hex(16)
        self.container_id = None
        self.attached = None
        self.creating = None
        self.api_child = None
        self.signal_received = None
        self.reason = None
        self.phase = "initial"
        self.initial_offline = False
        self.start_submitted = False
        self.cleanup_state = CleanupState.PENDING
        self.had_api_error = False
        self.logging_failed = False
        self.power_log_failed = False
        self.replay_failed = False
        self.actual_status = None
        self.create_status = None
        self.attached_status = None
        self.container_exit_status = None
        self.sample_count = 0
        self.offline_observed = False
        self.next_sample = 0.0
        self.begin = time.monotonic()
        self.last_cleanup_error = None
        self.samples = private_file(self.directory / "power.samples.jsonl", "w")
        self.output = private_file(self.directory / "output.log")
        self.created_output = private_file(self.directory / "create.stdout")
        self.api_sequence = 0
        self.previous_handlers = {}

    @property
    def cleanup_confirmed(self):
        return self.cleanup_state is CleanupState.CONFIRMED

    @property
    def cleanup_uncertain(self):
        return self.cleanup_state is CleanupState.UNCERTAIN

    def confirm_cleanup(self):
        self.cleanup_state = CleanupState.CONFIRMED

    def diagnostic(self, message):
        try:
            print("docker-ac-guard: " + message, file=sys.stderr, flush=True)
        except (OSError, ValueError):
            self.logging_failed = True
            discard_failed_stream("stderr")

    def close_artifact(self, output):
        try:
            output.close()
        except OSError:
            self.logging_failed = True

    def record_signal(self, number, _frame):
        # Recording state is safe even inside Popen before its return value has
        # been assigned. Cleanup starts only after that ownership assignment.
        if self.signal_received is None:
            self.signal_received = number

    def install_handlers(self):
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            self.previous_handlers[number] = signal.signal(number, self.record_signal)

    def observe(self, force=False):
        if self.reason is None and self.signal_received is not None:
            self.reason = "signal"
        now = time.monotonic()
        if not force and now < self.next_sample:
            return
        self.next_sample = now + 1.0
        try:
            online = self.ac_path.read_text().strip() == "1"
        except (OSError, UnicodeError):
            online = False
        self.offline_observed |= not online
        if not online and self.reason is None:
            self.reason = "power"
        self.sample_count += 1
        row = {
            "time_zagreb": datetime.datetime.now(ZoneInfo("Europe/Zagreb")).isoformat(),
            "monotonic_ns": time.monotonic_ns(),
            "online": online,
            "phase": self.phase,
        }
        if not self.power_log_failed:
            try:
                self.samples.write(json.dumps(row) + "\n")
                self.samples.flush()
            except OSError:
                self.logging_failed = True
                self.power_log_failed = True
                if self.reason is None:
                    self.reason = "artifact-error"

    def wait_tick(self, child=None):
        delay = max(0.001, self.next_sample - time.monotonic())
        if child is None:
            # Retry unresolved Docker state at the next power observation.
            # This is not a deadline or evidence that cleanup has completed.
            time.sleep(delay)
        else:
            try:
                child.wait(timeout=delay)
            except subprocess.TimeoutExpired:
                pass
        self.observe()

    def api(self, arguments):
        """Own one control CLI until its response, including during cancellation."""
        self.api_sequence += 1
        destination = self.directory / ("api-%03d.output" % self.api_sequence)
        self.api_child = subprocess.Popen(
            [self.docker, *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
        self.snapshot()
        while True:
            self.observe()
            try:
                data, _ = self.api_child.communicate(
                    timeout=max(0.001, self.next_sample - time.monotonic())
                )
                break
            except subprocess.TimeoutExpired:
                pass
        status = exit_status(self.api_child.wait())
        self.api_child = None
        self.snapshot()
        try:
            with private_file(destination) as output:
                output.write(data)
        except OSError:
            self.logging_failed = True
            if self.reason is None:
                self.reason = "artifact-error"
        return status, data

    def inspect(self):
        status, data = self.api(["inspect", "--type", "container", "--format", INSPECT_FORMAT, self.container_id])
        if status:
            status, data = self.api(["ps", "--all", "--quiet", "--no-trunc", "--filter", "id=" + self.container_id])
            if not status and not data.strip():
                return None
            raise DockerError("Docker did not confirm container state")
        try:
            state = json.loads(data)
            if state["Id"] != self.container_id or state["Owner"] != self.token:
                raise OwnershipError("Docker container ownership does not match")
            if type(state["Running"]) is not bool or type(state["ExitCode"]) is not int:
                raise DockerError("Docker returned invalid state fields")
            if not isinstance(state["Status"], str):
                raise DockerError("Docker returned an invalid status field")
            return state
        except (ValueError, KeyError, TypeError):
            raise DockerError("Docker returned invalid container state") from None

    def snapshot(self):
        try:
            self.write_result(None)
        except OSError:
            self.logging_failed = True
            if self.reason is None:
                self.reason = "artifact-error"

    def report_uncertain(self, error):
        self.cleanup_state = CleanupState.UNCERTAIN
        self.had_api_error = True
        kind = type(error).__name__
        if self.last_cleanup_error != kind:
            self.diagnostic("cleanup is unconfirmed; waiting for Docker ownership/state confirmation (" + kind + ")")
            self.last_cleanup_error = kind
        self.snapshot()

    def cleanup(self):
        if self.cleanup_confirmed:
            return
        self.phase = "cleanup"
        # A failed create can still have allocated an inactive container. Discover
        # only this invocation's token, then use its immutable ID for all actions.
        while self.container_id is None:
            try:
                status, data = self.api(["ps", "--all", "--quiet", "--no-trunc", "--filter", "label=" + OWNER_LABEL + "=" + self.token])
            except OSError as error:
                self.report_uncertain(error)
                self.wait_tick()
                continue
            identifiers = data.decode("ascii", "replace").split()
            if status or len(identifiers) > 1 or any(not CID.fullmatch(item) for item in identifiers):
                self.report_uncertain(DockerError("Docker did not confirm creation cleanup"))
                self.wait_tick()
                continue
            if not identifiers:
                self.confirm_cleanup()
                return
            self.container_id = identifiers[0]
        while not self.cleanup_confirmed:
            try:
                state = self.inspect()
                if state is None:
                    self.confirm_cleanup()
                    break
                if state["Running"] or state["Status"] in ("paused", "restarting"):
                    # Use Docker's configured graceful stop before removal. Its
                    # grace period is not evidence that termination completed.
                    self.api(["stop", self.container_id])
                    state = self.inspect()
                    if state is None:
                        self.confirm_cleanup()
                        break
                if state["Status"] == "exited":
                    self.container_exit_status = state["ExitCode"]
                # Non-forced removal cannot kill a concurrently starting job.
                # If start wins this race, removal fails and we stop it on the
                # next verified-state pass. If removal wins, that ID cannot start.
                status, _ = self.api(["rm", "--volumes", self.container_id])
                state = self.inspect()
                if state is None:
                    self.confirm_cleanup()
                    break
                raise DockerError("Docker did not confirm container removal")
            except (DockerError, OwnershipError, OSError) as error:
                self.report_uncertain(error)
                self.wait_tick()

    def reap_attached(self):
        if self.attached is None:
            return
        self.phase = "cli-reap"
        self.snapshot()
        if self.attached.poll() is None:
            # Container absence is already confirmed. Signal only the recorded
            # session/group created for this exact CLI, never its caller.
            try:
                os.killpg(self.attached.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        while self.attached.poll() is None:
            self.observe()
            self.wait_tick(self.attached)
        self.attached_status = exit_status(self.attached.wait())

    def finish_create(self):
        if self.creating is None:
            return
        while self.creating.poll() is None:
            self.observe()
            self.wait_tick(self.creating)
        self.create_status = exit_status(self.creating.wait())
        self.creating = None
        self.created_output.flush()
        created = (self.directory / "create.stdout").read_bytes().strip()
        if CID.fullmatch(created.decode("ascii", "replace")):
            self.container_id = created.decode("ascii")

    def execute(self):
        self.observe(force=True)
        if self.reason is not None:
            self.initial_offline = self.reason == "power"
            self.confirm_cleanup()
            return
        self.phase = "create"
        # The image and its command follow all Docker flags. Insert the owner
        # label before them, without interpreting command arguments as flags.
        create = ["create", "--label", OWNER_LABEL + "=" + self.token, *self.create_arguments]
        self.creating = subprocess.Popen(
            [self.docker, *create], stdin=subprocess.DEVNULL,
            stdout=self.created_output, stderr=self.output, start_new_session=True,
        )
        self.snapshot()
        self.finish_create()
        if self.create_status:
            self.actual_status = self.create_status
            return
        if self.container_id is None:
            raise DockerError("Docker create did not return an immutable container ID")
        self.phase = "before-start"
        self.snapshot()
        self.observe(force=True)
        if self.reason is not None:
            return
        state = self.inspect()
        if state is None or state.get("Status") != "created" or state.get("Running"):
            raise DockerError("Docker create did not leave an inactive owned container")
        self.observe(force=True)
        if self.reason is not None:
            return
        self.phase = "start-attach"
        self.attached = subprocess.Popen(
            [self.docker, "start", "--attach", self.container_id],
            stdin=subprocess.DEVNULL, stdout=self.output, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.start_submitted = True
        self.snapshot()
        while self.attached.poll() is None:
            self.observe()
            if self.reason is not None:
                return
            self.wait_tick(self.attached)
        attached_status = exit_status(self.attached.wait())
        self.attached_status = attached_status
        state = self.inspect()
        if state is not None and not state.get("Running") and state.get("Status") == "exited":
            self.container_exit_status = state["ExitCode"]
            self.actual_status = self.container_exit_status
            if attached_status and not self.actual_status:
                self.actual_status = attached_status
        elif attached_status:
            self.actual_status = attached_status
        else:
            raise DockerError("Docker did not confirm the workload's terminal status")

    def result_status(self):
        if self.reason is None and self.signal_received is not None:
            self.reason = "signal"
        if self.reason == "signal":
            return 128 + self.signal_received
        if self.reason == "power":
            return 2 if self.initial_offline else 3
        if self.actual_status:
            return self.actual_status
        if self.reason is not None or self.had_api_error or self.logging_failed or self.replay_failed:
            return 125
        return 0

    def write_result(self, status):
        record = {
            "phase": self.phase, "reason": self.reason,
            "supervisor_pid": os.getpid(),
            "create_client_pid": self.creating.pid if self.creating is not None else None,
            "attach_client_pid": self.attached.pid if self.attached is not None else None,
            "active_api_client_pid": self.api_child.pid if self.api_child is not None else None,
            "signal": self.signal_received, "container_id": self.container_id,
            "start_submitted": self.start_submitted,
            "cleanup_state": self.cleanup_state.value,
            "cleanup_confirmed": self.cleanup_confirmed,
            "cleanup_uncertain": self.cleanup_uncertain,
            "api_error_observed": self.had_api_error,
            "create_client_status": self.create_status,
            "attach_client_status": self.attached_status,
            "container_exit_status": self.container_exit_status,
            "selected_docker_status": self.actual_status,
            "replay_failed": self.replay_failed,
            "logging_failed": self.logging_failed,
            "power_log_failed": self.power_log_failed,
            "samples": self.sample_count, "offline_observed": self.offline_observed,
            "valid_power_interval": not self.offline_observed and not self.power_log_failed,
            "duration_seconds": time.monotonic() - self.begin, "exit_status": status,
        }
        temporary = self.directory / "result.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(record, output, indent=2)
            output.write("\n")
        os.replace(temporary, self.directory / "result.json")

    def run(self):
        self.install_handlers()
        try:
            try:
                self.execute()
            except Exception as error:
                if self.reason is None:
                    self.reason = "supervisor-error"
                self.diagnostic("execution failed (" + type(error).__name__ + ")")
            finally:
                # Cleanup and CLI reaping finish before the official parent can
                # finish waiting for this PID and remove its exported mounts.
                self.finish_create()
                self.cleanup()
                self.reap_attached()
                self.observe(force=True)
                self.close_artifact(self.created_output)
                self.close_artifact(self.output)
            try:
                with (self.directory / "output.log").open("rb") as output:
                    while chunk := output.read(65536):
                        sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
            except (OSError, ValueError):
                self.replay_failed = True
                discard_failed_stream("stdout")
            self.phase = "complete"
            self.observe(force=True)
            self.close_artifact(self.samples)
            status = self.result_status()
            try:
                self.write_result(status)
            except OSError:
                self.diagnostic("final artifact write failed")
                return status or 125
            return status
        finally:
            for number, handler in self.previous_handlers.items():
                signal.signal(number, handler)


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    docker = os.environ.get("DOCKER_AC_REAL_DOCKER", "/usr/bin/docker")
    if not arguments or arguments[0] != "run":
        os.execv(docker, [docker, *arguments])
    try:
        guard = Guard(
            docker, arguments[1:], Path(os.environ["DOCKER_AC_LOG_DIR"]),
            Path(os.environ.get("DOCKER_AC_PATH", "/sys/class/power_supply/ACAD/online")),
        )
    except (OSError, KeyError, InterfaceError) as error:
        try:
            print("docker-ac-guard: invalid setup (" + type(error).__name__ + ")", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            discard_failed_stream("stderr")
        return 125
    return guard.run()


if __name__ == "__main__":
    raise SystemExit(main())
