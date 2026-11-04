# Docker jobs on AC power

`docker_ac_guard.py` replaces the Docker command in a build or validation runner.
It samples AC power, owns each container until cleanup is confirmed, and handles
TERM, INT and HUP without signaling the calling runner or a live service.

The executable itself is the process the runner starts and waits for. It does
not launch another untracked supervisor. Commands other than `run` execute the
real Docker command unchanged.

## Use

Create a private directory for the run's power and command logs:

```bash
ac_logs=$(mktemp -d /tmp/docker-ac-logs.XXXXXX)
export DOCKER_AC_LOG_DIR="$ac_logs"
```

Configure the calling program's Docker-command override with the absolute path
of `scripts/docker_ac_guard.py`. For example, a proxy landing script that accepts
`CLIPROXY_DOCKER` can use:

```bash
CLIPROXY_DOCKER=/absolute/path/docker-ac-guard/scripts/docker_ac_guard.py \
DOCKER_AC_LOG_DIR="$ac_logs" \
scripts/swap-local-proxy.sh --land <approved-commit>
```

This does not authorize a landing or deployment. Follow the calling repository's
approval and landing procedure. Keep its existing image, network, resource,
configuration and credential settings. Do not wrap its entire process group in a
power monitor: that could signal a live swap as well as the validation job.

The supported `run` profile is a named, foreground, noninteractive job with
`--rm`. The guard accepts the options used by build runners: `--pull`, `--init`,
`--name`, `--label`, `--network`, `--user`, `--cpus`, `--memory`, `--env`/`-e`,
`--volume`/`-v` and `--workdir`/`-w`. Long options may use `=value`. Other options
are rejected before container creation. Arguments after the image keep their
original boundaries, including command arguments that happen to start with `-`.
Interactive, detached, privileged and custom logging configurations are not
supported by this interface.

Environment variables:

| Variable | Meaning |
| --- | --- |
| `DOCKER_AC_LOG_DIR` | Required private directory owned by the current user. |
| `DOCKER_AC_REAL_DOCKER` | Real Docker executable, default `/usr/bin/docker`. |
| `DOCKER_AC_PATH` | AC status file, default `/sys/class/power_supply/ACAD/online`. A value other than `1`, or a read error, means offline. |

Use an owned status file only for isolated power-loss tests. Do not substitute a
fake online value for physical AC when running real heavy work.

## Container and process ownership

The guard uses this lifecycle:

1. Validate the invocation and log directory, install signal handlers, and sample
   AC before creating a container.
2. Call `docker create` with the original image, command, networks, mounts,
   resource limits, user, environment and labels. Add an independent random owner
   label. The workload is inactive during creation.
3. Retain the returned immutable container ID and verify its owner label and
   inactive state. Recheck cancellation and AC before dispatching start.
4. Call `docker start --attach` for that ID and capture its output. Continue power
   observations while creation, execution or Docker control requests are pending.
5. Read the actual terminal container exit status. The attach client's status is
   recorded separately because it can differ from the workload's status.
6. Stop a running owned container through Docker's graceful stop, then remove the
   verified immutable ID without forced removal. If a pending start wins a race
   with removal, Docker refuses the non-forced removal; the guard reads the state
   again and stops it. If removal wins, a delayed start of that ID fails.
7. Confirm actual absence through a successful Docker query, reap the exact owned
   CLI process/group, finish the artifacts, and replay command output to the
   caller before returning.

The guard translates `--rm` into its own removal after reading the exit status.
The container remains available for inspection until the guard removes it. The
original runner's labels are preserved, so its own cancellation cleanup can also
find the container. The guard never stops or removes a mutable container name.
It verifies the owner token and uses the immutable ID for each destructive action.
A same-named replacement is not a cleanup target.

Signal handlers only record cancellation. They do not raise or exit inside
`Popen`, so an already-created child gets assigned to its owner before cleanup.
Cancellation while `create` is pending waits for its response and never starts
the resulting container. A failed or malformed create response triggers a query
for this invocation's owner token, then verification and cleanup by ID. An empty
successful query means absence; a failed query does not.

The caller must wait for the guard PID before removing exported directories or
other resources the job uses. A caller that kills the guard with SIGKILL, abandons
its wait, or removes resources before that wait has completed cannot rely on this
cleanup sequence.

## Power, errors and exit status

Power is sampled before work, at one-second intervals while child processes are
pending, at phase boundaries and after completion. An outage wholly between
observations cannot be detected. An observed offline value invalidates the whole
interval, even if the workload had otherwise succeeded.

Ordinary workload exits are preserved. Negative CLI signal returns become the
conventional `128 + signal` status. Cancellation returns `128 + signal`, initial
offline returns `2`, and observed AC loss after launch returns `3`. Setup,
protocol, artifact or output-replay errors make an otherwise successful command
return `125`. A previous nonzero workload status remains recorded and returned.
`result.json` records the create client, attach client, terminal container,
cancellation, power, cleanup and output outcomes separately.

A stopped Docker CLI does not prove that its container stopped. Failed Docker
queries or ownership mismatches keep cleanup unconfirmed. The guard retains
ownership and waits for a successful state/absence confirmation; it does not
return success or let a waiting caller assume cleanup has finished. A recovered
API error still makes an otherwise successful invocation fail.

Docker's configured graceful-stop settings may escalate after their grace period.
Neither that grace period nor a successful `stop` return proves cleanup: the
guard verifies removal afterwards. The guard does not invent a timer that proves
termination. If Docker never responds, cleanup can wait indefinitely and a
running container may continue until Docker responds. Restore Docker control
before expecting cleanup to finish. Killing only the CLI or guard cannot supply
that missing confirmation.

## Artifacts and privacy

Each invocation creates its own private subdirectory containing:

- `power.samples.jsonl`, with Zagreb timestamps, monotonic time, AC state and phase;
- `result.json`, with separate workload, client, cancellation and cleanup results;
- `output.log`, containing the merged command output replayed to the caller;
- `create.stdout` and the control-response files used for cleanup verification.

Container inspection requests only the immutable ID, owner token, status,
running flag and exit code. It does not dump container environment variables or
configuration. The guard does not print command arguments, environment variables
or owner tokens. Command output itself is retained and replayed as the caller
requested; apply the same access controls as for the original runner's logs.

## Tests

The isolated suite uses an owned executable named `docker` and a private Unix
socket. Explicit barriers cover creation/start and constructor cancellation,
TERM/INT/HUP, power transitions, missing create IDs, ownership mismatch and name
replacement, failed and blocked API requests, output replay and distinct
client/container exits. It does not contact Docker or a live proxy.

```bash
python3 -I -B tests/test_docker_ac_guard.py -v
```

Set `DOCKER_AC_TEST_OUTPUT` to a private directory to retain each test's command,
power and fake-protocol artifacts. The tests use owned fake power files; their
simulated transitions are not physical AC measurements.

Real-container controls require explicit opt-in, physical AC and a locally
available image. They use only uniquely named, network-isolated containers and
owned temporary files. See the real-control test instructions when available;
do not treat prepared or skipped controls as successful execution.
