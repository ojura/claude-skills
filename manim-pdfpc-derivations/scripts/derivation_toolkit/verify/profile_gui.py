#!/usr/bin/env python3
"""Measure native playback using monotonic draw samples and display feedback."""

import argparse
import hashlib
import json
import os
import platform
import re
import select
import signal
import shutil
import statistics
import subprocess
import time
from pathlib import Path

from pypdf import PdfReader


def distribution(values):
    if not values:
        return None
    ordered = sorted(values)
    return {"count": len(ordered), "mean": statistics.mean(ordered),
            "p50": statistics.median(ordered),
            "p95": ordered[min(len(ordered)-1, int(len(ordered)*0.95))],
            "p99": ordered[min(len(ordered)-1, int(len(ordered)*0.99))],
            "max": ordered[-1]}


def frame_identity(event, counter=None):
    return event["frame_clock_id"], event["frame_counter"] if counter is None else counter


def scene_metrics(events, scene_id, playback_id, duration, view):
    # A completed timing belongs to the draw with this clock/counter, even if
    # the receipt arrives after a checkpoint, pause, or scene change.
    origins = {}
    for event in events:
        if (event["event"] == "frame" and event["view"] == view and
                event.get("scene") == scene_id and event.get("playback_id") == playback_id):
            origins[frame_identity(event)] = event
    frames = []
    for event in sorted(origins.values(), key=lambda item: item["clock_us"]):
        frames.append(event)
        if event["displayed_time"] >= duration - 1e-6:
            break
    selected = {frame_identity(event): event for event in frames}
    timings = {}
    timing_receipts = {}
    for event in events:
        if "frame_clock_id" not in event:
            continue
        for timing in event.get("completed_timings", []):
            key = frame_identity(event, timing["counter"])
            if key in selected:
                timings[key] = timing
                timing_receipts[key] = event
    known_keys = {key for key, timing in timings.items()
                  if timing["presentation_time_us"] > 0 and not timing.get("expired", False)}
    clocks = {key[0] for key in selected}
    # Collection-event counts cover the supplied events for these frame clocks,
    # including later states; verified/unverified frame counts stay scene-selected.
    collection_events = [event for event in events if "completed_timings" in event and
                         event.get("frame_clock_id") in clocks]
    missing_collection = sum("feedback_collected_us" not in event for event in collection_events)
    feedback_clock_errors = {"invalid_collection_time": 0, "collection_before_event": 0,
                             "presentation_before_sample": 0, "presentation_after_collection": 0}
    for event in collection_events:
        if "feedback_collected_us" not in event:
            continue
        collected = event["feedback_collected_us"]
        if type(collected) is not int or collected <= 0:
            feedback_clock_errors["invalid_collection_time"] += 1
        elif collected < event["clock_us"]:
            feedback_clock_errors["collection_before_event"] += 1
    verified_bounds = 0
    unverified_bounds = 0
    for key in known_keys:
        stamp = timings[key]["presentation_time_us"]
        sample = selected[key].get("sample_clock_us", 0)
        receipt = timing_receipts[key]
        before_sample = stamp < sample
        feedback_clock_errors["presentation_before_sample"] += before_sample
        if "feedback_collected_us" not in receipt:
            # Historical traces recorded event construction before querying GDK.
            # Their event clock cannot serve as an upper receipt-time bound.
            unverified_bounds += 1
            continue
        collected = receipt["feedback_collected_us"]
        if type(collected) is int and collected > 0:
            after_collection = stamp > collected
            feedback_clock_errors["presentation_after_collection"] += after_collection
            if not before_sample and not after_collection and collected >= receipt["clock_us"]:
                verified_bounds += 1
    feedback = [(timings[key]["presentation_time_us"], frame)
                for key, frame in selected.items() if key in known_keys]
    source_indices = {frame_identity(frame): index for index, frame in enumerate(frames)}
    unknown_prefix = [0]
    for frame in frames:
        unknown_prefix.append(unknown_prefix[-1] + (frame_identity(frame) not in known_keys))
    # Several submitted draws can be reported at one display refresh. Count
    # that refresh once, using its newest originating scene state.
    by_presentation = {stamp: frame for stamp, frame in
                       sorted(feedback, key=lambda item: item[1]["clock_us"])}
    presented = sorted(by_presentation.items())
    actual = [stamp for stamp, _ in presented]
    ticks = sorted({event["frame_time_us"] for event in frames if event.get("frame_time_us", 0) > 0})
    sampled = [event for event in frames if event.get("sample_clock_us", 0) > 0]
    samples = [event["sample_clock_us"] for event in sampled]
    presentation_intervals = []
    unknown_spans = []
    step_errors = []
    for (ta, a), (tb, b) in zip(presented, presented[1:]):
        left, right = sorted((source_indices[frame_identity(a)], source_indices[frame_identity(b)]))
        interval = (tb-ta)/1000
        if unknown_prefix[right + 1] - unknown_prefix[left]:
            unknown_spans.append(interval)
        else:
            presentation_intervals.append(interval)
            step_errors.append(abs((b["displayed_time"] - a["displayed_time"])*1000 - interval))
    complete_feedback = bool(selected) and len(known_keys) == len(selected)
    observed_positive_fps = ((len(actual)-1)*1e6/(actual[-1]-actual[0])
                             if len(actual) > 1 and actual[-1] > actual[0] else None)
    clock_errors = [abs(event["displayed_time"] - min(event["target"], event["anchor_time"] +
                    max(0, event["sample_clock_us"] - event["anchor_clock_us"])/1e6*event["speed"]))*1000
                    for event in sampled if event["playing"]]
    sample_clock_errors = {
        "missing": len(frames) - len(sampled),
        "before_anchor": sum(event["playing"] and event["sample_clock_us"] < event["anchor_clock_us"]
                             for event in sampled),
        "after_record": sum(event["sample_clock_us"] > event["clock_us"] for event in sampled),
        "backwards": sum(b < a for a, b in zip(samples, samples[1:])),
    }
    first_final = next((event for event in frames if event["displayed_time"] >= duration - 1e-6), None)
    anchor = frames[0] if frames else None
    expected_wall = ((duration - anchor["anchor_time"]) / anchor["speed"] if anchor else None)
    final_wall = ((first_final["clock_us"] - anchor["anchor_clock_us"]) / 1e6
                  if first_final is not None else None)
    # advance() accepts a target within 1e-7 scene seconds. The additional
    # microsecond covers monotonic clock resolution and serialized frame time.
    early_final = (final_wall is not None and
                   final_wall + 1e-6 < expected_wall - 1e-7 / anchor["speed"])
    timing_errors = []
    if not frames:
        timing_errors.append("The run did not record any frames for this view")
    if any(sample_clock_errors.values()):
        timing_errors.append("Invalid monotonic sample clock: " + str(sample_clock_errors))
    if clock_errors and max(clock_errors) > 0.001:
        timing_errors.append("Displayed scene time differs from its monotonic sample")
    if any(feedback_clock_errors.values()):
        timing_errors.append("Invalid presentation feedback clock: " + str(feedback_clock_errors))
    if first_final is None:
        timing_errors.append("The run did not draw its final scene state")
    elif early_final:
        timing_errors.append("The final scene state was drawn before its wall-time deadline")
    cpu_frames = [event for event in frames if event.get("render_cpu_us", -1) >= 0]
    return {
        "frames": len(frames),
        "native_draw_ms": distribution([event["render_us"]/1000 for event in frames]),
        "native_cpu_ms": distribution([event["render_cpu_us"]/1000 for event in cpu_frames]),
        "native_wall_minus_cpu_ms": distribution([
            max(0, event["render_us"] - event["render_cpu_us"])/1000 for event in cpu_frames]),
        "frame_clock_interval_ms": distribution([(b-a)/1000 for a, b in zip(ticks, ticks[1:])]),
        "sample_interval_ms": distribution([(b-a)/1000 for a, b in zip(samples, samples[1:])]),
        "frame_clock_lead_ms": distribution([
            (event["frame_time_us"] - event["sample_clock_us"])/1000 for event in sampled]),
        "sample_to_record_ms": distribution([
            (event["clock_us"] - event["sample_clock_us"])/1000 for event in sampled]),
        "presentation_interval_ms": distribution(presentation_intervals),
        "presentation_gap_counts": {str(limit): sum(value > limit for value in presentation_intervals)
                                    for limit in (16.7, 25, 33.4, 50)},
        "completed_frames": len(timings),
        "presented_frames": len(presented),
        "positive_timestamp_frames": len(known_keys),
        "feedback_coverage": len(known_keys)/len(selected) if selected else None,
        "feedback_collection_events": len(collection_events),
        "feedback_collection_missing_events": missing_collection,
        "feedback_clock_errors": feedback_clock_errors,
        "feedback_timestamp_bounds_verified_frames": verified_bounds,
        "feedback_timestamp_bounds_unverified_frames": unverified_bounds,
        "complete_feedback": complete_feedback,
        "unknown_feedback_span_count": len(unknown_spans),
        "unknown_feedback_span_ms": distribution(unknown_spans),
        "coalesced_presentation_frames": len(feedback) - len(presented),
        "missing_completion_frames": len(selected.keys() - timings.keys()),
        "unavailable_presentation_frames": sum(timing["presentation_time_us"] <= 0
                                               for timing in timings.values()),
        "expired_frames": sum(timing.get("expired", False) for timing in timings.values()),
        "scene_step_error_ms": distribution(step_errors),
        "per_view_clock_error_ms": distribution(clock_errors),
        "sample_clock_errors": sample_clock_errors,
        "expected_wall_seconds": expected_wall,
        "first_final_wall_seconds": final_wall,
        "early_final_frame": early_final,
        "timing_errors": timing_errors,
        "duplicate_scene_times": sum(b["displayed_time"] == a["displayed_time"]
                                     for (_, a), (_, b) in zip(presented, presented[1:])),
        "refresh_interval_ms": distribution([event["refresh_interval_us"]/1000 for event in frames
                                             if event.get("refresh_interval_us", 0) > 0]),
        "draw_fps": ((len(samples)-1)*1e6/(samples[-1]-samples[0])
                     if len(samples) > 1 and samples[-1] > samples[0] else 0),
        "frame_clock_fps": (len(ticks)-1)*1e6/(ticks[-1]-ticks[0]) if len(ticks) > 1 else 0,
        "observed_positive_fps": observed_positive_fps,
        "presented_fps": observed_positive_fps if complete_feedback else None,
        "size": [frames[-1]["width"], frames[-1]["height"]] if frames else None,
    }


def fit_window_layout(width, height, decoration_height=0):
    if width <= 0 or height <= decoration_height or decoration_height < 0:
        raise ValueError("Desktop dimensions must be positive")
    scale = min(1.0, width / 2480, (height - decoration_height) / 1140)
    original = {"presenter": {"x": 1470, "y": 60, "width": 1000, "height": 800},
                "presentation": {"x": 10, "y": 60, "width": 1440, "height": 1080}}
    geometry = {name: {key: max(1 if key in ("width", "height") else 0, int(value * scale))
                       for key, value in values.items()} for name, values in original.items()}
    return scale, geometry


def desktop_workarea(properties, width, height):
    fallback = {"x": 0, "y": 0, "width": width, "height": height}
    # A missing xprop value has no '='; do not read the next property's value.
    current = re.search(r"_NET_CURRENT_DESKTOP[^\n=]*=\s*(\d+)", properties)
    workareas = re.search(r"_NET_WORKAREA[^\n=]*=([^\n]+)", properties)
    if workareas is None:
        return fallback
    values = [int(value) for value in re.findall(r"-?\d+", workareas.group(1))]
    if current is None:
        if not values or len(values) % 4:
            return fallback
        rectangles = [values[offset:offset + 4] for offset in range(0, len(values), 4)]
        if any(rectangle != rectangles[0] for rectangle in rectangles[1:]):
            return fallback
        index = 0
    else:
        index = int(current.group(1)) * 4
    if len(values) < index + 4:
        return fallback
    x, y, available_width, available_height = values[index:index + 4]
    if (x < 0 or y < 0 or available_width <= 0 or available_height <= 0 or
            x + available_width > width or y + available_height > height):
        return fallback
    return {"x": x, "y": y, "width": available_width, "height": available_height}


def window_rectangles(attributes):
    labels = {"x": "Absolute upper-left X", "y": "Absolute upper-left Y",
              "width": "Width", "height": "Height"}
    client = {}
    for key, label in labels.items():
        match = re.search(r"^\s*" + label + r":\s*(-?\d+)\s*$", attributes, re.MULTILINE)
        if match is None:
            raise ValueError("xwininfo omitted " + label)
        client[key] = int(match.group(1))
    extents = re.search(r"Frame extents:\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)", attributes)
    left, right, top, bottom = map(int, extents.groups()) if extents else (0, 0, 0, 0)
    decorated = {"x": client["x"] - left, "y": client["y"] - top,
                 "width": client["width"] + left + right,
                 "height": client["height"] + top + bottom}
    return {"client": client, "decorated": decorated}


def validate_window_layout(windows, workarea):
    for name, rectangles in windows.items():
        rectangle = rectangles["decorated"]
        if (rectangle["x"] < workarea["x"] or rectangle["y"] < workarea["y"] or
                rectangle["x"] + rectangle["width"] > workarea["x"] + workarea["width"] or
                rectangle["y"] + rectangle["height"] > workarea["y"] + workarea["height"]):
            raise AssertionError(f"{name} extends outside the desktop work area: {rectangle}")
    audience = windows["presentation"]["decorated"]
    presenter = windows["presenter"]["decorated"]
    overlap_width = min(audience["x"] + audience["width"], presenter["x"] + presenter["width"]) - \
        max(audience["x"], presenter["x"])
    overlap_height = min(audience["y"] + audience["height"], presenter["y"] + presenter["height"]) - \
        max(audience["y"], presenter["y"])
    if overlap_width > 0 and overlap_height > 0:
        raise AssertionError(f"The profiler windows overlap by {overlap_width}×{overlap_height} pixels")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfpc", type=Path)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--real-display", action="store_true",
                        help="Use the current X11 display instead of a private Xvfb display")
    parser.add_argument("--scene", action="append")
    args = parser.parse_args()
    required = ["dbus-send", "xdotool", "import", "xrandr", "xprop", "xwininfo", "git", "ps"]
    if not os.environ.get("PDFPC_VECTOR_PROFILE_BUS"):
        required.append("dbus-run-session")
    if not args.real_display:
        required.append("Xvfb")
    missing = [command for command in required if shutil.which(command) is None]
    if missing:
        parser.error("Missing profiler commands: " + ", ".join(missing))
    if args.real_display and not os.environ.get("DISPLAY"):
        parser.error("--real-display requires DISPLAY; this profiler uses the X11 GTK backend")
    if not args.pdfpc.is_file() or not os.access(args.pdfpc, os.X_OK):
        parser.error(f"pdfpc is not an executable file: {args.pdfpc}")
    if not os.environ.get("PDFPC_VECTOR_PROFILE_BUS"):
        command = ["dbus-run-session", "--", "env", "PDFPC_VECTOR_PROFILE_BUS=1", "python3",
                   str(Path(__file__).resolve()), str(args.pdfpc.resolve()), str(args.pdf.resolve()),
                   "--output", str(args.output.resolve())]
        if args.manifest:
            command += ["--manifest", str(args.manifest.resolve())]
        if args.real_display:
            command += ["--real-display"]
        for scene in args.scene or []:
            command += ["--scene", scene]
        return subprocess.run(command).returncode

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = args.manifest.resolve() if args.manifest else args.pdf.with_suffix(".vector.json").resolve()
    data = json.loads(manifest.read_text())
    scenes = {scene["id"]: scene for scene in data["scenes"]}
    reader = PdfReader(args.pdf)
    bindings = []
    for index, page in enumerate(reader.pages):
        for reference in page.get("/Annots", []):
            action_data = reference.get_object().get("/A", {})
            uri = action_data.get("/URI", "")
            if str(uri).startswith("pdfpc-vector:"):
                scene_id = str(uri).split(":", 1)[1]
                label = reader.page_labels[index]
                if not str(label).isdigit():
                    raise ValueError(f"Expected numeric Beamer page label, got {label!r}")
                if args.scene is None or scene_id in args.scene:
                    bindings.append((scene_id, index + 1, int(label)))
    if not bindings:
        raise ValueError("The PDF contains no requested vector scenes")

    trace = output / "trace.jsonl"
    trace.write_text("")
    environment = {**os.environ, "GDK_BACKEND": "x11", "PDFPC_VECTOR_MANIFEST": str(manifest),
                   "PDFPC_VECTOR_TRACE": str(trace), "PDFPC_VECTOR_SPEED": "1",
                   "XDG_CONFIG_HOME": str(output / "config"),
                   "XDG_CACHE_HOME": str(output / "cache"), "GIO_USE_VFS": "local",
                   "NO_AT_BRIDGE": "1"}
    xvfb = None
    xvfb_log = None
    app = None
    app_log = None
    trace_reader = None
    previous_focus = ""
    owned_windows = []
    events = []
    current = None
    binary = args.pdfpc.resolve()
    report = {"display": "desktop" if args.real_display else "Xvfb", "backend": "x11",
              "desktop_session_type": os.environ.get("XDG_SESSION_TYPE"),
              "pdf": str(args.pdf.resolve()), "manifest": str(manifest), "scenes": [],
              "owned_pdfpc_pid": None, "pdfpc": str(binary)}

    def collect():
        nonlocal current
        if trace_reader is None:
            return current
        while True:
            position = trace_reader.tell()
            line = trace_reader.readline()
            if not line:
                break
            if not line.endswith("\n"):
                trace_reader.seek(position)
                break
            event = json.loads(line)
            events.append(event)
            if event["event"] == "state":
                current = event
        return current

    def wait_for(predicate, description, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if app.poll() is not None:
                raise RuntimeError(f"pdfpc exited with {app.returncode}; inspect {output/'pdfpc.log'}")
            collect()
            result = predicate()
            if result:
                return result
            time.sleep(0.025)
        raise AssertionError(f"Timed out waiting for {description}")

    def bus_ready():
        reply = subprocess.run(
            ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
             "/org/freedesktop/DBus", "org.freedesktop.DBus.NameHasOwner", "string:io.github.pdfpc"],
            env=environment, capture_output=True, text=True, timeout=2)
        return reply.returncode == 0 and "boolean true" in reply.stdout

    def action(name, argument=None, expect_reply=True):
        method = "io.github.pdfpc.TriggerAction" if argument is None else "io.github.pdfpc.TriggerActionArg"
        command = ["dbus-send", "--session", "--print-reply", "--dest=io.github.pdfpc",
                   "/io/github/pdfpc", method, f"string:{name}"]
        if argument is not None:
            command.append(f"string:{argument}")
        return subprocess.run(command, env=environment, check=expect_reply, capture_output=True,
                              text=True, timeout=5)

    def window_id(name):
        result = subprocess.run(
            ["xdotool", "search", "--all", "--pid", str(app.pid), "--name", name],
            env=environment, capture_output=True, text=True, timeout=2)
        matches = result.stdout.split()
        return matches[-1] if result.returncode == 0 and matches else None

    def active_window():
        return subprocess.run(["xdotool", "getactivewindow"], env=environment,
                              capture_output=True, text=True, timeout=2).stdout.strip()

    def group_members():
        listing = subprocess.check_output(["ps", "-eo", "pid=,pgid=,stat=,comm="],
                                          text=True, timeout=2)
        return [line.strip() for line in listing.splitlines() if int(line.split()[1]) == app.pid]

    def signal_group(signal_number):
        try:
            os.killpg(app.pid, signal_number)
        except ProcessLookupError:
            pass

    try:
        revision = subprocess.run(["git", "-C", str(binary.parent), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=5)
        source_status = subprocess.run(["git", "-C", str(binary.parent), "status", "--porcelain",
                                        "--untracked-files=no"], capture_output=True, text=True, timeout=5)
        report.update({
            "pdfpc_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "source_revision": revision.stdout.strip() if revision.returncode == 0 else None,
            "source_dirty": bool(source_status.stdout) if source_status.returncode == 0 else None,
            "platform": platform.platform(), "cpu_count": os.cpu_count(),
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "pdf_sha256": hashlib.sha256(args.pdf.read_bytes()).hexdigest(),
        })
        if not args.real_display:
            read_fd, write_fd = os.pipe()
            xvfb_log = (output / "xvfb.log").open("w")
            try:
                xvfb = subprocess.Popen(["Xvfb", "-displayfd", str(write_fd), "-screen", "0",
                                         "3200x1800x24", "-nolisten", "tcp", "-noreset"],
                                        pass_fds=(write_fd,), stdout=xvfb_log, stderr=xvfb_log)
            except OSError:
                os.close(read_fd)
                raise
            finally:
                os.close(write_fd)
            with os.fdopen(read_fd) as stream:
                if not select.select([stream], [], [], 10)[0]:
                    raise RuntimeError("Xvfb did not report a display within 10 seconds")
                display = stream.readline().strip()
            if not display:
                raise RuntimeError("Xvfb did not start")
            environment["DISPLAY"] = f":{display}"
        report["display_name"] = environment.get("DISPLAY")
        monitor = subprocess.run(["xrandr", "--current"], env=environment, text=True,
                                 capture_output=True, check=True, timeout=5)
        (output / "monitor.txt").write_text(monitor.stdout)
        desktop = subprocess.check_output(["xdotool", "getdisplaygeometry"], env=environment,
                                          text=True, timeout=5).split()
        desktop_width, desktop_height = map(int, desktop)
        properties = subprocess.run(["xprop", "-root", "_NET_CURRENT_DESKTOP", "_NET_WORKAREA"],
                                    env=environment, text=True, capture_output=True, timeout=5)
        workarea = desktop_workarea(properties.stdout, desktop_width, desktop_height)
        report["desktop_geometry"] = {"width": desktop_width, "height": desktop_height}
        report["desktop_workarea"] = workarea
        previous_focus = active_window()
        app_log = (output / "pdfpc.log").open("w")
        app = subprocess.Popen([str(binary), "-N", "-w", "both", "-Z", "1440:1080",
                                "-P", str(bindings[0][2]), str(args.pdf.resolve())],
                               env=environment, stdout=app_log, stderr=app_log, start_new_session=True)
        report["owned_pdfpc_pid"] = app.pid
        trace_reader = trace.open()
        wait_for(bus_ready, "private D-Bus service ownership")
        report["dbus_ready"] = True
        wait_for(lambda: current and current["scene"] == bindings[0][0], "initial scene")
        presenter = wait_for(lambda: window_id("pdfpc - presenter"), "owned presenter window")
        audience = wait_for(lambda: window_id("pdfpc - presentation"), "owned audience window")
        if presenter == audience:
            raise AssertionError("Presenter and audience must be distinct owned windows")
        owned_windows = [presenter, audience]
        windows = {"presenter": presenter, "presentation": audience}
        report["owned_windows"] = windows
        initial_rectangles = [window_rectangles(subprocess.check_output(
            ["xwininfo", "-all", "-id", window], env=environment, text=True, timeout=5))
            for window in windows.values()]
        decoration_height = max(rectangles["decorated"]["height"] - rectangles["client"]["height"]
                                for rectangles in initial_rectangles)
        scale, requested_geometry = fit_window_layout(workarea["width"], workarea["height"],
                                                       decoration_height)
        for geometry in requested_geometry.values():
            geometry["x"] += workarea["x"]
            geometry["y"] += workarea["y"]
        report["decoration_height_reserve"] = decoration_height
        report["window_layout_scale"] = scale
        report["requested_window_geometry"] = requested_geometry
        command = ["xdotool"]
        for name, window in windows.items():
            geometry = requested_geometry[name]
            command += ["windowsize", window, str(geometry["width"]), str(geometry["height"])]
        subprocess.run(command, env=environment, check=True, timeout=5)
        # The timer's font size follows window height. Let GTK recompute its
        # minimum width before applying the final width and placement.
        time.sleep(0.2)
        command = ["xdotool"]
        for name, window in windows.items():
            geometry = requested_geometry[name]
            command += ["windowsize", window, str(geometry["width"]), str(geometry["height"]),
                        "windowmove", window, str(geometry["x"]), str(geometry["y"])]
        subprocess.run(command, env=environment, check=True, timeout=5)
        time.sleep(0.2)
        actual = {}
        for name, window in windows.items():
            attributes = subprocess.check_output(["xwininfo", "-all", "-id", window],
                                                 env=environment, text=True, timeout=5)
            actual[name] = window_rectangles(attributes)
        report["actual_window_rectangles"] = actual
        report["actual_window_geometry"] = {name: rectangles["client"] for name, rectangles in actual.items()}
        validate_window_layout(actual, workarea)
        report["nonoverlapping_windows"] = True

        for scene_id, physical_page, logical_page in bindings:
            action("gotoPage", str(logical_page))
            wait_for(lambda: current and current["scene"] == scene_id and current["page"] == physical_page,
                     f"scene {scene_id}")
            action("vectorRestart")
            wait_for(lambda: current["time"] == 0 and not current["playing"], "reset")
            begin = time.monotonic()
            start_index = len(events)
            play_action = "next" if len(scenes[scene_id]["checkpoints"]) == 2 else "vectorPlayAll"
            action(play_action)
            wait_for(lambda: current["time"] >= scenes[scene_id]["duration"] - 1e-6 and not current["playing"],
                     f"complete {scene_id}", scenes[scene_id]["duration"] + 15)
            elapsed = time.monotonic() - begin
            collect()
            scene_events = events[start_index:]
            play = next(e for e in scene_events if e["event"] == "state" and e.get("reason") == "play")
            playback_id = play["playback_id"]
            duration = scenes[scene_id]["duration"]

            def endpoint_drained():
                final_frames = [e for e in events[start_index:] if e["event"] == "frame" and
                                e.get("playback_id") == playback_id and e.get("scene") == scene_id and
                                e["displayed_time"] >= duration - 1e-6]
                if {e["view"] for e in final_frames} != {"presentation", "presenter"}:
                    return False
                last_final = max(e["clock_us"] for e in final_frames)
                return any(e["event"] == "trace-drained" and e.get("playback_id") == playback_id and
                           e["last_frame_recorded_us"] >= last_final for e in events[start_index:])

            wait_for(endpoint_drained, f"completed timing drain for {scene_id}", timeout=3)
            scene_events = events[start_index:]
            views = {view: scene_metrics(scene_events, scene_id, playback_id, duration, view)
                     for view in ("presentation", "presenter")}
            drain = next(e for e in reversed(scene_events) if e["event"] == "trace-drained")
            result = {"scene": scene_id, "duration": scenes[scene_id]["duration"],
                      "wall_seconds": elapsed, "action": play_action, "play_started_us": play["clock_us"],
                      "playback_id": playback_id, "pending_after_drain": drain["pending_frames"],
                      "scene_sha256": hashlib.sha256(json.dumps(scenes[scene_id], sort_keys=True,
                                                     separators=(",", ":")).encode()).hexdigest(),
                      "views": views}
            report["scenes"].append(result)
            for view, metrics in views.items():
                if metrics["timing_errors"]:
                    raise AssertionError(f"{scene_id} {view}: " + "; ".join(metrics["timing_errors"]))
            time.sleep(0.1)
            for name, window in (("presenter", presenter), ("presentation", audience)):
                subprocess.run(["import", "-window", window, str(output/f"{scene_id}-{name}.png")],
                               env=environment, check=True, timeout=10)
            print(json.dumps(result), flush=True)
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        cleanup_errors = []
        current_focus = ""
        current_focus_owned = False
        try:
            if app is not None:
                current_focus = active_window()
                current_focus_owned = current_focus in owned_windows
                if current_focus and not current_focus_owned:
                    owner = subprocess.run(["xdotool", "getwindowpid", current_focus],
                                           env=environment, capture_output=True, text=True, timeout=2)
                    current_focus_owned = owner.returncode == 0 and owner.stdout.strip() == str(app.pid)
        except (OSError, subprocess.SubprocessError) as error:
            cleanup_errors.append(str(error))
        if app is not None:
            if app.poll() is None:
                try:
                    if bus_ready():
                        action("quit", expect_reply=False)
                        report["shutdown_method"] = "quit"
                        app.wait(timeout=5)
                except (OSError, subprocess.SubprocessError) as error:
                    cleanup_errors.append(str(error))
                if app.poll() is None:
                    report["shutdown_method"] = "SIGTERM"
                    signal_group(signal.SIGTERM)
                    try:
                        app.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        report["shutdown_method"] = "SIGKILL"
                        signal_group(signal.SIGKILL)
                        app.wait()
            report["owned_pdfpc_exit"] = app.returncode
            try:
                remaining = group_members()
                if any(not line.split()[2].startswith("Z") for line in remaining):
                    signal_group(signal.SIGTERM)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        remaining = group_members()
                        if not any(not line.split()[2].startswith("Z") for line in remaining):
                            break
                        time.sleep(0.025)
                    if any(not line.split()[2].startswith("Z") for line in remaining):
                        signal_group(signal.SIGKILL)
                report["owned_process_group_remaining"] = group_members()
            except (OSError, subprocess.SubprocessError) as error:
                cleanup_errors.append(str(error))
        if previous_focus and current_focus_owned:
            try:
                subprocess.run(["xdotool", "windowactivate", previous_focus], env=environment,
                               capture_output=True, timeout=2)
            except (OSError, subprocess.SubprocessError) as error:
                cleanup_errors.append(str(error))
        try:
            collect()
            report["trace_closed"] = bool(events and events[-1]["event"] == "trace-closed")
        except (OSError, ValueError) as error:
            cleanup_errors.append(str(error))
        if xvfb is not None and xvfb.poll() is None:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill()
                xvfb.wait()
        if xvfb_log:
            xvfb_log.close()
        if trace_reader:
            trace_reader.close()
        if app_log:
            app_log.close()
        if cleanup_errors:
            report["cleanup_errors"] = cleanup_errors
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
