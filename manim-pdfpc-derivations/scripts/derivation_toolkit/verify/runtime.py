#!/usr/bin/env python3
"""Prepare PDF-derived fixtures, then test the relocated AppImage using stdlib only."""

import argparse
import csv
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback



def digest(filename):
    return hashlib.sha256(Path(filename).read_bytes()).hexdigest()


def prepare(options):
    from pypdf import PdfReader
    from derivation_toolkit.catalog import Catalog
    from derivation_toolkit.package_assets import collect_assets

    catalog = Catalog(options.project)
    inputs = json.loads(options.inputs.read_text()) if options.inputs else None
    rows, files = collect_assets(catalog, options.assets, inputs=inputs)
    sources = {relative.as_posix(): source for source, relative in files}
    result = {"schema": 3, "default": catalog.default, "catalog": []}
    for row in rows:
        identifier, stem, language, archived, animation, themes, title = row.split("\t")
        entry = {"id": identifier, "language": language, "title": title,
                 "archived": archived == "1", "animation": animation == "1", "themes": {}}
        for theme in themes.split(","):
            name = stem + "-" + theme + "-live.pdf"
            pdf = sources["decks/" + identifier + "/" + name]
            reader = PdfReader(pdf)
            item = {"pdf_name": name, "pdf_sha256": digest(pdf), "pages": len(reader.pages)}
            if entry["animation"]:
                manifest = sources["decks/" + identifier + "/" + name[:-4] + ".vector.json"]
                data = json.loads(manifest.read_text())
                scenes = {scene["id"]: scene for scene in data["scenes"]}
                bindings = []
                for index, page in enumerate(reader.pages):
                    for reference in page.get("/Annots", []):
                        action = reference.get_object().get("/A", {})
                        uri = str(action.get("/URI", ""))
                        if not uri.startswith("pdfpc-vector:"):
                            continue
                        scene_id = uri.split(":", 1)[1]
                        label = reader.page_labels[index]
                        assert scene_id in scenes, scene_id
                        scene = scenes[scene_id]
                        bindings.append({"scene": scene_id, "physical_page": index + 1,
                                         "logical_page": int(label) if str(label).isdigit() else index + 1,
                                         "duration": scene["duration"],
                                         "checkpoints": [point["time"] for point in scene["checkpoints"]],
                                         "checkpoint_count": len(scene["checkpoints"])})
                assert bindings, "The animated document has no bound scene"
                item.update(manifest_sha256=digest(manifest), bindings=bindings)
            entry["themes"][theme] = item
        result["catalog"].append(entry)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


def command(argv, environment=None, check=True, timeout=15, cwd=None):
    return subprocess.run(argv, env=environment, check=check, timeout=timeout,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)


def wait_for(predicate, description, process=None, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise AssertionError("AppImage exited with {} while waiting for {}".format(
                process.returncode, description))
        value = predicate()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError("Timed out waiting for " + description)


def events(filename):
    result = []
    for line in filename.read_text().splitlines():
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            # A writer may be between its payload and newline.
            pass
    return result


def bus_call(environment, method, *arguments, destination="io.github.pdfpc",
             object_path="/io/github/pdfpc", check=True):
    return command(["dbus-send", "--session", "--print-reply", "--reply-timeout=3000",
                    "--dest=" + destination, object_path, method] + list(arguments),
                   environment, check=check, timeout=5)


def bus_owner(environment):
    reply = bus_call(environment, "org.freedesktop.DBus.GetConnectionUnixProcessID",
                     "string:io.github.pdfpc", destination="org.freedesktop.DBus",
                     object_path="/org/freedesktop/DBus", check=False)
    found = re.search(r"uint32 (\d+)", reply.stdout)
    return int(found.group(1)) if reply.returncode == 0 and found else None


def viewer_windows(native_pid, environment, process):
    windows = {}
    for view in ("presenter", "presentation"):
        def find_window():
            reply = command(["xdotool", "search", "--all", "--pid", str(native_pid),
                             "--name", "pdfpc - " + view], environment, check=False)
            matches = reply.stdout.split()
            return matches[-1] if reply.returncode == 0 and matches else None
        windows[view] = wait_for(find_window, view + " window", process)
    assert len(set(windows.values())) == 2
    return windows


def action(environment, name, argument=None, check=True):
    method = "io.github.pdfpc.TriggerAction" if argument is None else "io.github.pdfpc.TriggerActionArg"
    arguments = ["string:" + name]
    if argument is not None:
        arguments.append("string:" + str(argument))
    return bus_call(environment, method, *arguments, check=check)


def group_members(pgid):
    listing = command(["ps", "-eo", "pid=,pgid=,stat=,comm="]).stdout
    return [line.strip() for line in listing.splitlines() if int(line.split()[1]) == pgid]


def stop_group(process):
    if process is None:
        return []
    if process.poll() is None or group_members(process.pid):
        for sig, timeout in ((signal.SIGTERM, 3), (signal.SIGKILL, 3)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                continue
            if not group_members(process.pid):
                break
    return group_members(process.pid)


def preflight(expected):
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                   if "=" in line)
    version = release["VERSION_ID"].strip('"')
    assert release["ID"].strip('"') == "ubuntu" and version == expected, release
    forbidden = ("pdfpc", "pdflatex", "latex", "manim", "gcc", "g++", "cc", "clang",
                 "make", "cmake", "ninja", "pip", "pip3")
    present = {name: shutil.which(name) for name in forbidden if shutil.which(name)}
    assert not present, present
    assert importlib.util.find_spec("manim") is None
    installed = command(["dpkg-query", "-W", "-f=${binary:Package}\t${Status}\n"]).stdout
    native_prefixes = ("libgtk-3", "libpoppler", "libgee-0.8", "libjson-glib", "librsvg2", "libcairo2")
    native = [line for line in installed.splitlines()
              if line.startswith(native_prefixes) and line.endswith("install ok installed")]
    assert not native, native
    source_paths = ("/src", "/source")
    assert not any(Path(item).exists() for item in source_paths), source_paths
    return {"ubuntu": version, "libc": command(["getconf", "GNU_LIBC_VERSION"]).stdout.strip(),
            "absent_commands": list(forbidden), "absent_native_package_prefixes": list(native_prefixes),
            "absent_source_build_paths": list(source_paths), "installed_packages": installed.splitlines()}


def font_resolution(appdir):
    # This runs in a separate stdlib Python process with the viewer's actual
    # Fontconfig and loader environment, so host libraries cannot supply fonts.
    import ctypes
    import xml.etree.ElementTree as ET

    appdir = appdir.resolve()
    font_directory = (appdir / "usr/share/fonts").resolve()
    configuration = Path(os.environ["FONTCONFIG_FILE"])
    assert configuration.is_absolute() and configuration.is_file(), configuration
    assert Path(os.environ["FONTCONFIG_PATH"]).resolve() == configuration.parent.resolve()
    directories = ET.parse(configuration).getroot().findall("dir")
    assert len(directories) == 1 and directories[0].text, directories
    configured_directory = Path(directories[0].text)
    assert configured_directory.is_absolute() and configured_directory.resolve() == font_directory

    pointer = ctypes.c_void_p
    integer = ctypes.c_int
    string = ctypes.c_char_p
    library = ctypes.CDLL(str(appdir / "usr/lib/libfontconfig.so.1"))
    signatures = {
        "FcInitLoadConfigAndFonts": ([], pointer),
        "FcConfigDestroy": ([pointer], None),
        "FcNameParse": ([string], pointer),
        "FcConfigSubstitute": ([pointer, pointer, integer], integer),
        "FcDefaultSubstitute": ([pointer], None),
        "FcFontMatch": ([pointer, pointer, ctypes.POINTER(integer)], pointer),
        "FcPatternGetString": ([pointer, string, integer, ctypes.POINTER(string)], integer),
        "FcPatternGetCharSet": ([pointer, string, integer, ctypes.POINTER(pointer)], integer),
        "FcCharSetHasChar": ([pointer, ctypes.c_uint32], integer),
        "FcPatternDestroy": ([pointer], None),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = result
    configuration_handle = library.FcInitLoadConfigAndFonts()
    assert configuration_handle, "Bundled Fontconfig could not load its actual configuration"
    matches = {}
    try:
        for generic, expected_family in (("sans-serif", "DejaVu Sans"),
                                         ("monospace", "DejaVu Sans Mono")):
            pattern = library.FcNameParse(generic.encode())
            assert pattern
            matched = None
            try:
                assert library.FcConfigSubstitute(configuration_handle, pattern, 0)
                library.FcDefaultSubstitute(pattern)
                match_result = integer()
                matched = library.FcFontMatch(configuration_handle, pattern,
                                              ctypes.byref(match_result))
                assert matched and match_result.value == 0, (generic, match_result.value)
                filename, family, charset = string(), string(), pointer()
                assert library.FcPatternGetString(matched, b"file", 0, ctypes.byref(filename)) == 0
                assert library.FcPatternGetString(matched, b"family", 0, ctypes.byref(family)) == 0
                assert library.FcPatternGetCharSet(matched, b"charset", 0, ctypes.byref(charset)) == 0
                resolved_font = Path(os.fsdecode(filename.value)).resolve()
                resolved_font.relative_to(font_directory)
                assert resolved_font.is_file() and family.value.decode() == expected_family
                glyphs = "0123456789:/\u2197"
                assert all(library.FcCharSetHasChar(charset, ord(glyph)) for glyph in glyphs), generic
                matches[generic] = {"family": family.value.decode(), "file": str(resolved_font),
                                    "verified_glyphs": glyphs}
            finally:
                if matched:
                    library.FcPatternDestroy(matched)
                library.FcPatternDestroy(pattern)
    finally:
        library.FcConfigDestroy(configuration_handle)
    return {"configuration": str(configuration), "font_directory": str(font_directory),
            "matches": matches}


def validate_feedback(records):
    """Require the current trace schema and check actual collection-time bounds."""
    frames = {}
    for event in records:
        if event["event"] != "frame":
            continue
        key = (event["frame_clock_id"], event["frame_counter"])
        assert key not in frames, ("duplicate frame identity", key)
        frames[key] = event
    completed = set()
    positive = unavailable = expired = collection_events = 0
    for event in records:
        if "completed_timings" not in event:
            continue
        collection_events += 1
        collected = event.get("feedback_collected_us")
        assert type(collected) is int and collected > 0, ("missing or invalid collection time", event)
        assert collected >= event["clock_us"], ("collection predates event construction", event)
        for timing in event["completed_timings"]:
            key = (event["frame_clock_id"], timing["counter"])
            assert key in frames, ("unknown completed frame", key)
            assert key not in completed, ("duplicate completed frame", key)
            completed.add(key)
            stamp = timing["presentation_time_us"]
            if timing.get("expired", False):
                expired += 1
            elif stamp <= 0:
                unavailable += 1
            else:
                assert frames[key]["sample_clock_us"] <= stamp <= collected, \
                    ("presentation outside sample-to-collection interval", frames[key], timing, event)
                positive += 1
    return {"draws": len(frames), "completed_frames": len(completed),
            "pending_frames": len(frames) - len(completed),
            "collection_events": collection_events, "positive_timestamp_frames": positive,
            "unavailable_timestamp_frames": unavailable, "expired_frames": expired}


def catalog_smoke(appimage, fixtures, output, environment):
    directory = output / "catalog"
    directory.mkdir()
    cache = directory / "cache path # catalog"
    environment = dict(environment, XDG_CACHE_HOME=str(directory / "desktop-cache"))
    result = {"selections": [], "static_launches": []}
    with tempfile.TemporaryDirectory(prefix="extracted package ", dir=directory) as temporary:
        command([str(appimage), "--appimage-extract"], environment, timeout=120, cwd=temporary)
        appdir = Path(temporary) / "squashfs-root"
        invocation = [str(appdir / "AppRun"), "--cache-root", str(cache)]
        environment["APPDIR"] = str(appdir)
        listing = command(invocation + ["--list"], environment)
        rows = list(csv.DictReader(listing.stdout.splitlines(), delimiter="\t"))
        expected_rows = [{"ID": deck["id"], "Language": deck["language"],
                          "Archive": str(int(deck["archived"])), "Themes": ",".join(deck["themes"]), "Title": deck["title"]}
                         for deck in fixtures["catalog"]]
        assert rows == expected_rows, (rows, expected_rows)
        payload = appdir / "usr/share/derivation-presentations"
        assert (payload / "default-deck").read_text().strip() == fixtures["default"]
        bundle = (payload / "bundle-id").read_text().strip()
        assert re.fullmatch("[0-9a-f]{64}", bundle), bundle
        result["catalog"] = rows
        selections = set()
        for deck in fixtures["catalog"]:
            for theme, fixture in deck["themes"].items():
                pdf = payload / "decks" / deck["id"] / fixture["pdf_name"]
                assert digest(pdf) == fixture["pdf_sha256"], (deck["id"], theme)
                if deck["animation"]:
                    assert digest(pdf.with_suffix(".vector.json")) == fixture["manifest_sha256"]
                query = command(invocation + ["--deck", deck["id"], "--" + theme,
                                              "--print-pdf"], environment)
                expected = cache / "bundles" / bundle / deck["id"] / "pdf" / pdf.name
                assert Path(query.stdout.strip()) == expected, query.stdout
                assert expected not in selections, expected
                selections.add(expected)
                result["selections"].append({"deck": deck["id"], "theme": theme,
                                               "pages": fixture["pages"], "pdf": pdf.name})
        default = next(deck for deck in fixtures["catalog"] if deck["id"] == fixtures["default"])
        default_pdf = cache / "bundles" / bundle / default["id"] / "pdf" / default["themes"]["light"]["pdf_name"]
        assert Path(command(invocation + ["--print-pdf"], environment).stdout.strip()) == default_pdf
        assert not cache.exists(), "Catalog queries created the presentation cache"
        samples = {}
        for deck in fixtures["catalog"]:
            if not deck["animation"]:
                samples.setdefault((deck["language"], deck["archived"]), deck)
        for deck in samples.values():
            for theme, fixture in deck["themes"].items():
                case = {"deck": deck["id"], "theme": theme, "pages": fixture["pages"]}
                log_path = directory / (deck["id"] + "-" + theme + ".log")
                app = None
                with log_path.open("w") as log:
                    try:
                        argv = invocation + ["--deck", deck["id"], "--" + theme, "--",
                                             "--windowed=both", "-Z", "1000:750"]
                        app = subprocess.Popen(argv, env=environment, stdout=log, stderr=log,
                                               start_new_session=True)
                        native_pid = wait_for(lambda: bus_owner(environment), "static pdfpc owner", app, 60)
                        assert os.getpgid(native_pid) == app.pid
                        assert Path(os.readlink("/proc/{}/exe".format(native_pid))) == appdir / "usr/bin/pdfpc"
                        windows = viewer_windows(native_pid, environment, app)
                        count = bus_call(environment, "org.freedesktop.DBus.Properties.Get",
                                         "string:io.github.pdfpc", "string:NumberOfOverlays")
                        found = re.search(r"int32 (\d+)", count.stdout)
                        assert found and int(found.group(1)) == fixture["pages"], count.stdout
                        copied = cache / "bundles" / bundle / deck["id"] / "pdf" / fixture["pdf_name"]
                        assert digest(copied) == fixture["pdf_sha256"]
                        url = bus_call(environment, "org.freedesktop.DBus.Properties.Get",
                                       "string:io.github.pdfpc", "string:Url")
                        assert copied.as_uri() in url.stdout, url.stdout
                        case["window_titles"] = {view: command(["xdotool", "getwindowname", window],
                                                                environment).stdout.strip()
                                                 for view, window in windows.items()}
                        action(environment, "quit", check=False)
                        assert app.wait(timeout=10) == 0, app.returncode
                        wait_for(lambda: bus_owner(environment) is None, "static viewer bus release")
                        case["clean_exit"] = True
                    finally:
                        case["remaining_process_group"] = stop_group(app)
                        result["static_launches"].append(case)
                        (directory / "report.json").write_text(json.dumps(result, indent=2) + "\n")
                assert not case["remaining_process_group"], case["remaining_process_group"]
    return result


def theme_smoke(appimage, deck_id, theme, fixture, output, environment):
    directory = output / deck_id / theme
    directory.mkdir(parents=True)
    trace = directory / "trace.jsonl"
    trace.write_text("")
    desktop_cache = directory / "desktop-cache"
    assert not desktop_cache.exists()
    environment = dict(environment, PDFPC_VECTOR_TRACE=str(trace), PDFPC_VECTOR_SPEED="4",
                       XDG_CACHE_HOME=str(desktop_cache),
                       GIO_MODULE_DIR="/unavailable/host/gio/modules",
                       GIO_EXTRA_MODULES="/unavailable/host-extra/gio/modules")
    cache = directory / "cache path # smoke"
    custom = directory / "PDF output # smoke"
    arguments = ["--deck", deck_id, "--" + theme, "--cache-root", str(cache)]
    if theme == "dark":
        arguments += ["--output-dir", str(custom)]
    invocation = [str(appimage), "--appimage-extract-and-run"]
    help_result = command(invocation + ["--help"], environment, timeout=60)
    assert "bundled catalog" in help_result.stdout
    query = command(invocation + arguments + ["--print-pdf"], environment, timeout=60)
    copied_pdf = Path(query.stdout.strip())
    assert copied_pdf.name == fixture["pdf_name"] and copied_pdf.is_absolute(), query.stdout
    assert not cache.exists() and not custom.exists(), "A pure CLI query created its output directory"
    assert not desktop_cache.exists(), "A pure CLI query initialized desktop/font caches"
    if theme == "dark":
        assert copied_pdf.parent == custom
    else:
        assert copied_pdf.parent.parent.name == deck_id
        assert copied_pdf.parent.parent.parent.parent == cache / "bundles"
        assert re.fullmatch("[0-9a-f]{64}", copied_pdf.parent.parent.parent.name)

    app = None
    result = {"theme": theme, "cli_help": True, "pure_path_query": str(copied_pdf), "scenes": []}
    log = (directory / "pdfpc.log").open("w")
    try:
        # The default installed-resource path is deliberate: relocation must work.
        argv = invocation + arguments + ["--", "--windowed=both", "-Z", "1000:750",
                                         "-P", str(fixture["bindings"][0]["logical_page"])]
        app = subprocess.Popen(argv, env=environment, stdout=log, stderr=log, start_new_session=True)
        native_pid = wait_for(lambda: bus_owner(environment), "private pdfpc D-Bus owner", app, 60)
        assert os.getpgid(native_pid) == app.pid, (native_pid, app.pid, os.getpgid(native_pid))
        executable = os.readlink("/proc/{}/exe".format(native_pid))
        assert executable.endswith("/usr/bin/pdfpc"), executable
        result.update({"runtime_pid": app.pid, "native_pid": native_pid, "native_executable": executable})
        appdir = Path(executable).parents[2]
        native_environment = dict(entry.split(b"=", 1) for entry in
                                  Path("/proc/{}/environ".format(native_pid)).read_bytes().split(b"\0")
                                  if b"=" in entry)
        resource_paths = {"PDFPC_DATA_DIR": appdir / "usr/share/pdfpc",
                          "PDFPC_CONFIG_DIR": appdir / "etc",
                          "GIO_MODULE_DIR": appdir / "usr/lib/gio/modules"}
        for name, expected_path in resource_paths.items():
            assert os.fsdecode(native_environment[name.encode()]) == str(expected_path), name
        assert resource_paths["GIO_MODULE_DIR"].is_dir()
        assert b"GIO_EXTRA_MODULES" not in native_environment
        resources = (resource_paths["PDFPC_DATA_DIR"] / "css/pdfpc.css",
                     resource_paths["PDFPC_DATA_DIR"] / "icons/pause.svg",
                     resource_paths["PDFPC_CONFIG_DIR"] / "pdfpcrc")
        assert all(filename.is_file() for filename in resources), resources
        result["relocated_resources"] = [str(filename.relative_to(appdir)) for filename in resources]
        result["gio_module_isolation"] = True
        font_environment = dict(environment)
        for name in ("FONTCONFIG_FILE", "FONTCONFIG_PATH", "LD_LIBRARY_PATH", "XDG_CACHE_HOME"):
            font_environment[name] = os.fsdecode(native_environment[name.encode()])
        font_probe = command([sys.executable, "-B", str(Path(__file__).resolve()),
                              "font-check", "--appdir", str(appdir)], font_environment)
        assert not font_probe.stderr.strip(), font_probe.stderr
        result["bundled_font_resolution"] = json.loads(font_probe.stdout)
        result["font_cache_initially_absent"] = True
        payload = appdir / "usr/share/derivation-presentations"
        source_revision = (payload / "source-revision").read_text().strip()
        build_timestamp = (payload / "build-timestamp").read_text().strip()
        assert re.fullmatch("[A-Za-z0-9._-]+", source_revision), source_revision
        parsed_timestamp = datetime.datetime.strptime(build_timestamp, "%Y-%m-%dT%H:%M:%SZ")
        assert parsed_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") == build_timestamp
        project_title = (payload / "project-title").read_text().strip()
        expected_title = "[{} {} | built {}]".format(project_title, source_revision[:8], build_timestamp)
        result["build_metadata"] = {"source_revision": source_revision,
                                    "build_timestamp": build_timestamp,
                                    "title_suffix": expected_title}

        def current():
            return next((item for item in reversed(events(trace)) if item["event"] == "state"), None)

        def at(scene, physical, position=None, playing=None):
            state = current()
            return state and state["scene"] == scene and state["page"] == physical and \
                (position is None or abs(state["time"] - position) < 1e-6) and \
                (playing is None or state["playing"] == playing)

        windows = viewer_windows(native_pid, environment, app)

        def check_window_titles():
            titles = {view: command(["xdotool", "getwindowname", window], environment).stdout.strip()
                      for view, window in windows.items()}
            assert all(title.endswith(expected_title) for title in titles.values()), titles
            return titles

        result["build_metadata"]["window_titles"] = check_window_titles()
        command(["xdotool", "windowsize", windows["presenter"], "1280", "900",
                 "windowmove", windows["presenter"], "0", "0",
                 "windowsize", windows["presentation"], "1000", "750",
                 "windowmove", windows["presentation"], "1500", "0"], environment)
        result["windows"] = windows
        assert digest(copied_pdf) == fixture["pdf_sha256"]
        assert digest(copied_pdf.with_suffix(".vector.json")) == fixture["manifest_sha256"]
        result["copied_assets_match"] = True

        for binding in fixture["bindings"]:
            scene = binding["scene"]
            physical = binding["physical_page"]
            duration = binding["duration"]
            action(environment, "gotoPage", binding["logical_page"])
            wait_for(lambda: at(scene, physical), scene + " page binding", app)
            check_window_titles()
            action(environment, "vectorRestart")
            wait_for(lambda: at(scene, physical, 0, False), scene + " initial state", app)
            checkpoints = binding["checkpoints"]
            assert checkpoints[0] == 0 and checkpoints[-1] == duration and len(checkpoints) >= 2
            action(environment, "next")
            wait_for(lambda: at(scene, physical, checkpoints[1], False), scene + " first checkpoint",
                     app, timeout=duration + 10)
            action(environment, "vectorRestart")
            wait_for(lambda: at(scene, physical, 0, False), scene + " restarted state", app)
            action(environment, "vectorPlayAll")
            wait_for(lambda: at(scene, physical, playing=True), scene + " playing", app)
            playback_id = current()["playback_id"]
            wait_for(lambda: at(scene, physical, duration, False), scene + " exact endpoint",
                     app, timeout=duration + 10)

            def endpoint_views():
                records = events(trace)
                final = {item["view"] for item in records if item["event"] == "frame" and
                         item["scene"] == scene and item["playback_id"] == playback_id and
                         abs(item["displayed_time"] - duration) < 1e-6}
                drained = any(item["event"] == "trace-drained" and
                              item["playback_id"] == playback_id for item in records)
                return final == {"presentation", "presenter"} and drained

            wait_for(endpoint_views, scene + " final draws and trace drain", app, 3)
            frames = [item for item in events(trace) if item["event"] == "frame" and
                      item["scene"] == scene and item["playback_id"] == playback_id]
            counts = {}
            wall_durations = {}
            for view in ("presentation", "presenter"):
                view_frames = sorted((item for item in frames if item["view"] == view),
                                     key=lambda item: item["clock_us"])
                moving = [item for item in view_frames if 0 < item["displayed_time"] < duration]
                assert len(moving) >= 3, (scene, view, len(moving))
                assert len({item["displayed_time"] for item in moving}) >= 3
                counts[view] = len(moving)
                anchor = view_frames[0]
                final = next(item for item in view_frames
                             if abs(item["displayed_time"] - duration) < 1e-6)
                expected_wall = (duration - anchor["anchor_time"]) / anchor["speed"]
                final_wall = (final["clock_us"] - anchor["anchor_clock_us"]) / 1000000
                # Native advance() accepts 1e-7 scene seconds at the target;
                # allow one additional microsecond for clock/trace resolution.
                assert final_wall + 1e-6 >= expected_wall - 1e-7 / anchor["speed"], \
                    (scene, view, "early final draw", final_wall, expected_wall)
                wall_durations[view] = {"expected_seconds": expected_wall,
                                        "first_final_seconds": final_wall}
            for item in frames:
                assert item["sample_clock_us"] <= item["clock_us"], item
                if item["playing"]:
                    assert item["sample_clock_us"] >= item["anchor_clock_us"], item
                    expected = min(item["target"], item["anchor_time"] +
                                   (item["sample_clock_us"] - item["anchor_clock_us"]) /
                                   1000000 * item["speed"])
                    assert abs(item["displayed_time"] - expected) <= 1e-6, item
            result["scenes"].append({"scene": scene, "physical_page": physical,
                                     "duration": duration, "moving_frames": counts,
                                     "both_final_draws": True, "per_view_clock_equation": True,
                                     "monotonic_sample_domain": True,
                                     "wall_duration": wall_durations})

        action(environment, "quit", check=False)
        assert app.wait(timeout=10) == 0, app.returncode
        records = events(trace)
        assert records[-1]["event"] == "trace-closed", records[-1]
        assert sum(item["event"] == "trace-closed" for item in records) == 1
        result["feedback"] = validate_feedback(records)
        assert result["feedback"]["pending_frames"] == records[-1]["pending_frames"], result["feedback"]
        result["clean_exit"] = True
        result["closed_pending_frames"] = records[-1]["pending_frames"]
    finally:
        result["remaining_process_group"] = stop_group(app)
        log.close()
        (directory / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    assert not result["remaining_process_group"], result["remaining_process_group"]
    return result


def run(options):
    if not os.environ.get("DERIVATION_SMOKE_PRIVATE_BUS"):
        environment = dict(os.environ, DERIVATION_SMOKE_PRIVATE_BUS="1")
        return subprocess.run(["dbus-run-session", "--", sys.executable, "-B",
                               str(Path(__file__).resolve())] + sys.argv[1:], env=environment).returncode
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"passed": False, "themes": [], "backend": "x11", "display": "private Xvfb"}
    xvfb = None
    xvfb_log = None
    try:
        report["environment"] = preflight(options.expected_ubuntu)
        appimage = options.appimage.resolve()
        assert appimage.suffix.lower() == ".appimage" and os.access(appimage, os.X_OK)
        report["appimage_sha256"] = digest(appimage)
        fixtures = json.loads(options.fixtures.read_text())
        assert fixtures["schema"] == 3
        environment = os.environ.copy()
        for name in ("DERIVATION_CACHE_ROOT", "DERIVATION_BUILD_DIR", "PDFPC_VECTOR_MANIFEST", "PDFPC_VECTOR_TRACE",
                     "PDFPC_DATA_DIR", "PDFPC_CONFIG_DIR"):
            environment.pop(name, None)
        environment.update({"GDK_BACKEND": "x11", "NO_AT_BRIDGE": "1", "GIO_USE_VFS": "local",
                            "XDG_CONFIG_HOME": str(output / "config"),
                            "XDG_CACHE_HOME": str(output / "cache")})
        read_fd, write_fd = os.pipe()
        xvfb_log = (output / "xvfb.log").open("w")
        try:
            xvfb = subprocess.Popen(["Xvfb", "-displayfd", str(write_fd), "-screen", "0",
                                     "3200x1800x24", "-nolisten", "tcp", "-noreset"],
                                    pass_fds=(write_fd,), stdout=xvfb_log, stderr=xvfb_log,
                                    start_new_session=True)
        finally:
            os.close(write_fd)
        with os.fdopen(read_fd) as reader:
            assert select.select([reader], [], [], 10)[0], "Xvfb startup timed out"
            display = reader.readline().strip()
        assert display.isdigit(), display
        environment["DISPLAY"] = ":" + display
        report["display_name"] = environment["DISPLAY"]
        report["catalog"] = catalog_smoke(appimage, fixtures, output, environment)
        for deck in fixtures["catalog"]:
            if deck["animation"]:
                for theme, fixture in deck["themes"].items():
                    report["themes"].append(theme_smoke(appimage, deck["id"], theme, fixture,
                                                        output, environment))
        report["passed"] = True
    except Exception as error:
        report["error"] = "{}: {}".format(type(error).__name__, error)
        traceback.print_exc()
    finally:
        report["remaining_xvfb_group"] = stop_group(xvfb)
        if xvfb_log is not None:
            xvfb_log.close()
        if report["remaining_xvfb_group"]:
            report["passed"] = False
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--project", type=Path, required=True)
    preparation.add_argument("--inputs", type=Path)
    preparation.add_argument("--assets", type=Path, required=True)
    preparation.add_argument("--output", type=Path, required=True)
    runtime = commands.add_parser("run")
    runtime.add_argument("--appimage", type=Path, required=True)
    runtime.add_argument("--fixtures", type=Path, required=True)
    runtime.add_argument("--output", type=Path, required=True)
    runtime.add_argument("--expected-ubuntu", choices=("20.04", "24.04"), required=True)
    fonts = commands.add_parser("font-check", help="Check the actual bundled Fontconfig environment")
    fonts.add_argument("--appdir", type=Path, required=True)
    options = parser.parse_args()
    if options.operation == "font-check":
        print(json.dumps(font_resolution(options.appdir), indent=2))
        return 0
    return prepare(options) if options.operation == "prepare" else run(options)


if __name__ == "__main__":
    raise SystemExit(main())
