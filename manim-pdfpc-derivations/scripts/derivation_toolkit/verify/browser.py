"""Exercise a standalone derivation presentation in real Chrome and Firefox."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, suppress
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from urllib.parse import quote

from .browser_fixture import load_fixture, scene_at
from .browser_protocol import (
    assert_blank_playback_ignored, assert_transition, begin_transition,
    browser, check_document_resources, ready,
)


def wait_time(view, at):
    view.wait("document.getElementById('clock').textContent.startsWith(" +
              json.dumps(f"{at:.2f} /") + ") && "
              "document.getElementById('clock').textContent.includes('paused')",
              timeout=max(30, at + 5))


def select_theme(view, theme):
    view.eval("(()=>{const t=document.getElementById('theme');t.value=" + json.dumps(theme) +
              ";t.dispatchEvent(new Event('change'));})()")


def surface(view, fixture, theme, page):
    ready(view, page, theme)
    observed = view.eval("""(()=>{
        const slide=document.getElementById('slide').getBoundingClientRect();
        const pdf=document.getElementById('pdf');
        return {width:slide.width,height:slide.height,pdfWidth:pdf.width,pdfHeight:pdf.height,
                vector:getComputedStyle(document.getElementById('vector')).display,
                textSpans:document.querySelectorAll('.textLayer span').length};
    })()""")
    meta = fixture["manifest"]["themes"][theme]["pages"][page - 1]
    assert observed["pdfWidth"] > 0 and observed["pdfHeight"] > 0, observed
    assert abs(observed["width"] / observed["height"] - meta["width"] / meta["height"]) < .005, (meta, observed)
    assert (observed["vector"] != "none") == bool(meta.get("scene")), (meta, observed)
    return observed


def move_to(view, fixture, page, theme, audience=None):
    """Navigate through the ordinary controls while retaining the projector."""
    participants = (view, audience) if audience else (view,)
    if view.eval("document.getElementById('theme').value") != theme:
        select_theme(view, theme)
    current = int(view.eval("document.getElementById('slide').dataset.renderedPage"))
    for participant in participants:
        ready(participant, current, theme)
    while current != page:
        forward = current < page
        view.key("e" if forward else "r")
        view.click("next" if forward else "previous")
        current += 1 if forward else -1
        for participant in participants:
            ready(participant, current, theme)
    view.key("r")
    if scene_at(fixture, theme, page):
        for participant in participants:
            wait_time(participant, 0)


def transition(participants, fixture, page, theme, name, reports, *, preview=False, same_page=False):
    count = len(fixture["manifest"]["themes"][theme]["pages"])
    for label, view in participants:
        reports.append({"case": name + "/" + label,
                        **assert_transition(view, page, theme,
                                            preview=preview and label == "presenter",
                                            total_pages=count,
                                            require_commit=not (same_page and label == "projector"))})
        surface(view, fixture, theme, page)


def open_projector(view, page, theme):
    before = {item["id"] for item in view.pages()}
    view.click("projector")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        item = next((item for item in view.pages() if item["id"] not in before and
                     "audience=1" in item["url"]), None)
        if item:
            projector = view.view(item)
            ready(projector, page, theme)
            return projector
        time.sleep(.1)
    raise AssertionError("The projector button did not open a separate audience window")


def exercise_scene(view, fixture, theme, page, output, prefix, reports):
    source = scene_at(fixture, theme, page)
    points = [point["time"] for point in source["checkpoints"]]
    assert len(points) >= 2 and points[0] == 0 and points[-1] == source["duration"], source["id"]
    initial = surface(view, fixture, theme, page)
    assert view.eval("document.getElementById('step-caption').textContent").startswith(f"1/{len(points)}")
    assert view.eval("document.getElementById('next').disabled") is False
    view.click("next")
    wait_time(view, points[1])
    state = view.playback_state()
    assert abs(state["time"] - points[1]) < 1e-6 and state["page"] == page - 1, state
    finished_moving = False
    if len(points) > 2:
        view.click("next")
        if view.playback_state()["playing"]:
            view.click("next")
            finished_moving = True
        wait_time(view, points[2])
    view.key("r")
    view.key("ArrowRight", ctrl=True)
    wait_time(view, points[1])
    view.key("ArrowLeft", ctrl=True)
    wait_time(view, 0)
    assert view.eval("document.getElementById('previous').disabled") is (page == 1)
    view.eval("window.__derivationInitialCanvas=document.getElementById('vector').toDataURL()")
    view.key("v")
    view.wait("document.getElementById('vector').toDataURL() !== window.__derivationInitialCanvas",
              timeout=max(30, source["duration"] + 5))
    moving = view.playback_state()
    paused = False
    if moving["playing"]:
        view.key("a")
        stopped = view.playback_state()
        assert not stopped["playing"], stopped
        time.sleep(.2)
        assert view.playback_state() == stopped
        view.key("a")
        view.wait("parseFloat(document.getElementById('clock').textContent) > " +
                  json.dumps(stopped["time"]))
        view.key("a")
        paused = True
    view.key("e")
    wait_time(view, source["duration"])
    assert view.eval("document.getElementById('step-caption').textContent").startswith(f"{len(points)}/{len(points)}")
    assert view.eval("document.getElementById('previous').disabled") is False
    view.click("previous")
    wait_time(view, points[-2])
    view.key("e")
    wait_time(view, source["duration"])
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", source["id"])
    view.capture(output / f"{prefix}-{theme}-{safe_name}-final.png")
    count = len(fixture["manifest"]["themes"][theme]["pages"])
    assert view.eval("document.getElementById('next').disabled") is (page == count)
    if page < count:
        begin_transition(view)
        view.click("next")
        transition((("presenter", view),), fixture, page + 1, theme,
                   f"{theme}/{source['id']}-forward", reports)
        begin_transition(view)
        view.click("previous")
        wait_time(view, source["duration"])
        transition((("presenter", view),), fixture, page, theme,
                   f"{theme}/{source['id']}-backward-complete", reports)
    return {"theme": theme, "scene": source["id"], "page": page, "checkpoints": points,
            "finishMovingSegment": finished_moving, "pauseResume": paused,
            "continuousMotion": True, "initial": initial}


def exercise_projector(view, fixture, url, output, prefix, reports):
    themes = list(fixture["manifest"]["themes"])
    theme = themes[-1]
    pages = fixture["manifest"]["themes"][theme]["pages"]
    count = len(pages)
    animated = [page["number"] for page in pages if page.get("scene")]
    anchor = animated[0] if animated else 1
    view.navigate(url + "?theme=" + quote(theme) + "#p=" + str(anchor))
    surface(view, fixture, theme, anchor)
    begin_transition(view)
    view.click("presenter-mode")
    transition((("presenter", view),), fixture, anchor, theme, "presenter-resize", reports, preview=True)
    projector = open_projector(view, anchor, theme)
    participants = (("presenter", view), ("projector", projector))
    checks = []
    try:
        surface(projector, fixture, theme, anchor)
        source = scene_at(fixture, theme, anchor)
        if source:
            view.click("next")
            for _, participant in participants:
                wait_time(participant, source["checkpoints"][1]["time"])
            view.key("r")
            for _, participant in participants:
                wait_time(participant, 0)
            if source["duration"] >= .5:
                view.key("v")
                for _, participant in participants:
                    participant.wait("document.getElementById('clock').textContent.includes('playing')")
                view.key("a")
                clock = view.eval("document.getElementById('clock').textContent")
                projector.wait("document.getElementById('clock').textContent === " + json.dumps(clock))
            checks.append("segment/play/pause")
        if len(themes) > 1:
            other = themes[0]
            for _, participant in participants:
                begin_transition(participant)
            select_theme(view, other)
            transition(participants, fixture, anchor, other, "projector-theme", reports, preview=True)
            theme = other
            checks.append("theme")
        pages = fixture["manifest"]["themes"][theme]["pages"]
        source = scene_at(fixture, theme, anchor)
        static = [page["number"] for page in pages if not page.get("scene")]
        cases = []
        if source:
            if source["duration"] >= .5:
                cases.append(("intermediate", anchor, "ArrowRight"))
            cases.append(("completed", anchor, " "))
        if static:
            cases.append(("static", min(static, key=lambda page: abs(page - anchor)), "button"))
        for state_name, start, action in cases:
            move_to(view, fixture, start, theme, projector)
            if state_name == "intermediate":
                view.key("v")
                view.wait("document.getElementById('clock').textContent.includes('playing') && "
                          "parseFloat(document.getElementById('clock').textContent) > 0")
            elif state_name == "completed":
                view.key("e")
                for _, participant in participants:
                    wait_time(participant, source["duration"])
            view.key("b")
            before = assert_blank_playback_ignored(view, projector)
            for _, participant in participants:
                begin_transition(participant)
            if action == "button":
                assert view.eval("document.getElementById('next').disabled") is False
                view.click("next")
            else:
                view.key(action)
            destination = min(start + 1, count)
            for _, participant in participants:
                participant.wait("document.getElementById('blank').hidden === true")
            transition(participants, fixture, destination, theme,
                       "blanked-" + state_name + "-advance", reports, preview=True,
                       same_page=destination == start)
            after = view.playback_state()
            expected_time = 0 if destination != start else before["time"]
            assert after["page"] == destination - 1 and not after["blank"] and not after["playing"], after
            assert abs(after["time"] - expected_time) < 1e-6, (before, after)
            checks.append("blanked-" + state_name + "-advance")
        if source and anchor < count:
            move_to(view, fixture, anchor + 1, theme, projector)
            for _, participant in participants:
                begin_transition(participant)
            view.click("previous")
            for _, participant in participants:
                wait_time(participant, source["duration"])
            transition(participants, fixture, anchor, theme,
                       "projector-backward-complete", reports, preview=True)
            checks.append("backward-complete")
        if count >= 3:
            view.key("End")
            view.key("Home")
            view.key("End")
            for _, participant in participants:
                ready(participant, count, theme)
            time.sleep(.2)
            for _, participant in participants:
                assert participant.eval("document.getElementById('slide').dataset.renderedPage") == str(count)
            checks.append("rapid-navigation")
        for _, participant in participants:
            check_document_resources(participant)
            assert participant.eval("document.getElementById('error').hidden") is True
        projector.capture(output / f"{prefix}-projector.png")
        return checks
    finally:
        try:
            with suppress(Exception):
                projector.close_page()
        finally:
            projector.close()


def exercise(view, fixture, url, output, prefix, protocol):
    checked, static, transitions = [], [], []
    themes = list(fixture["manifest"]["themes"])
    for theme in themes:
        pages = fixture["manifest"]["themes"][theme]["pages"]
        animated = [page["number"] for page in pages if page.get("scene")]
        for page in animated:
            view.navigate(url + "?theme=" + quote(theme) + "#p=" + str(page))
            checked.append(exercise_scene(view, fixture, theme, page, output, prefix, transitions))
            check_document_resources(view)
        static_pages = [page["number"] for page in pages if not page.get("scene")]
        selected = {static_pages[0], static_pages[-1]} if static_pages else set()
        aspects = {}
        for page in pages:
            if not page.get("scene"):
                aspects.setdefault((page["width"], page["height"]), page["number"])
        selected.update(aspects.values())
        for page in sorted(selected):
            view.navigate(url + "?theme=" + quote(theme) + "#p=" + str(page))
            static.append({"theme": theme, "page": page, **surface(view, fixture, theme, page)})
            assert view.eval("document.getElementById('play').disabled") is True
            check_document_resources(view)
    projector = exercise_projector(view, fixture, url, output, prefix, transitions)
    return {"browser": prefix.split("-", 1)[0], "protocol": protocol,
            "themes": themes, "scenes": checked, "staticPages": static,
            "documentExternalResourceRequests": 0, "projectorChecks": projector,
            "atomicTransitions": transitions}


class QuietFiles(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@contextmanager
def presentation_url(html, protocol):
    if protocol == "file":
        yield html.as_uri()
        return
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietFiles, directory=str(html.parent)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{quote(html.name)}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=4)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--browser", choices=("both", "chrome", "firefox"), default="both")
    parser.add_argument("--protocol", choices=("both", "file", "http"), default="file")
    parser.add_argument("--chrome", type=Path)
    parser.add_argument("--firefox", type=Path)
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture = load_fixture(args.html)
    results = []
    html_digest = hashlib.sha256(args.html.read_bytes()).hexdigest()

    def record_progress(status, failure=None):
        receipt = {"status": status, "html": str(args.html.resolve()),
                   "html_sha256": html_digest, "results": results}
        if failure is not None:
            receipt["failure"] = failure
        temporary = output / ".progress.json.tmp"
        temporary.write_text(json.dumps(receipt, indent=2) + "\n")
        temporary.replace(output / "progress.json")

    record_progress("running")
    with tempfile.TemporaryDirectory(prefix="browser-acceptance-", dir=output) as scratch:
        temporary = Path(scratch)
        standalone = temporary / "only-html"
        standalone.mkdir()
        html = standalone / "presentation with spaces.html"
        shutil.copyfile(args.html.resolve(), html)
        for kind in (("chrome", "firefox") if args.browser == "both" else (args.browser,)):
            executable = ((args.chrome or shutil.which("google-chrome") or shutil.which("chromium"))
                          if kind == "chrome" else (args.firefox or shutil.which("firefox")))
            if not executable:
                raise SystemExit("Missing installed browser: " + kind)
            directory = temporary / kind
            directory.mkdir()
            protocol = None
            try:
                with browser(kind, str(executable), directory) as view:
                    for protocol in (("file", "http") if args.protocol == "both" else (args.protocol,)):
                        with presentation_url(html, protocol) as url:
                            try:
                                results.append(exercise(view, fixture, url, output, kind + "-" + protocol, protocol))
                                record_progress("running")
                            except Exception:
                                with suppress(Exception):
                                    view.capture(output / f"{kind}-{protocol}-failure.png")
                                raise
            except Exception as error:
                record_progress("failed", {"browser": kind, "protocol": protocol,
                                           "error": f"{type(error).__name__}: {error}"})
                raise
            finally:
                log = directory / (kind + ".log")
                if log.is_file():
                    shutil.copyfile(log, output / log.name)
    record_progress("passed")
    print(json.dumps({"status": "passed", "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
