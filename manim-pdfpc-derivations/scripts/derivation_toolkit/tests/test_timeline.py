"""Serialized version-1 defaults and bounds accepted by the native parser."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest

from ..timeline import load_timeline, validate_timeline
from .test_compiled_inputs import timeline


class TimelineContract(unittest.TestCase):
    def setUp(self):
        self.data = timeline()
        self.scene = self.data["scenes"][0]
        self.object = self.scene["objects"][0]
        self.frame = self.object["keyframes"][0]

    def reject(self, owner, key, values):
        for value in values:
            with self.subTest(member=key, value=repr(value)[:100]):
                original = owner[key]
                owner[key] = value
                try:
                    with self.assertRaises(ValueError):
                        validate_timeline(self.data)
                finally:
                    owner[key] = original

    def test_optional_z_fill_rule_and_sparse_geometry_keep_their_defaults(self):
        self.object.pop("z", None)
        self.object.pop("fill_rule", None)
        self.object["keyframes"].append({
            "time": .5, "fill": [1, 0, 0, .5], "stroke": [0, 0, 0, 1], "stroke_width": 2,
        })
        before = deepcopy(self.data)
        self.assertEqual(validate_timeline(self.data), {"example": self.scene})
        self.assertEqual(self.data, before, "Validation must not materialize defaults or sparse geometry")

    def test_version_one_accepts_json_number_one_but_not_boolean_or_string(self):
        self.data["version"] = 1.0
        validate_timeline(self.data)
        self.reject(self.data, "version", (True, False, "1", 0, 2, None))

    def test_all_supported_path_commands_and_optional_fill_rules(self):
        for commands, count in (("M", 2), ("ML", 4), ("MC", 8), ("MQ", 6),
                                ("MZ", 2), ("MLCQMZ", 16)):
            for rule in ("nonzero", "evenodd"):
                with self.subTest(commands=commands, rule=rule):
                    self.object["commands"] = commands
                    self.object["fill_rule"] = rule
                    self.frame["coords"] = [0] * count
                    validate_timeline(self.data)

    def test_path_topology_and_coordinate_arity_are_checked(self):
        self.reject(self.object, "commands", ("", "L", "MLH", "M A", "mL", 3))
        self.reject(self.frame, "coords", ([], [0, 0], [0] * 5, "0,0,1,1", [False, 0, 1, 1]))
        self.object["commands"] = "M" + "L" * 100000
        with self.assertRaisesRegex(ValueError, "too many coordinates"):
            validate_timeline(self.data)

    def test_geometry_and_z_numeric_bounds_reject_bool_nonfinite_and_overflow(self):
        for key in ("width", "height"):
            self.reject(self.scene, key, (0, -1, True, math.inf, math.nan, 1e9 + 1, "100"))
            self.scene[key] = 1e9
        self.object["z"] = -1e9
        self.frame["coords"] = [-1e9, 1e9, 1e9, -1e9]
        validate_timeline(self.data)
        self.reject(self.object, "z", (True, math.inf, math.nan, 1e9 + 1, -1e9 - 1))
        for value in (math.inf, math.nan, True, 1e9 + 1):
            self.frame["coords"][0] = value
            with self.subTest(coordinate=value), self.assertRaises(ValueError):
                validate_timeline(self.data)

    def test_background_is_opaque_but_path_alpha_can_be_partial(self):
        self.frame["fill"] = [.2, .3, .4, .5]
        self.frame["stroke"] = [.2, .3, .4, 0]
        validate_timeline(self.data)
        self.reject(self.scene, "background", ([1, 1, 1, 0], [1, 1, 1, .5],
                                               [1, 1, 1], [1, 1, 1, 1, 1], [True, 1, 1, 1]))
        self.reject(self.frame, "fill", ([0, 0, 0, -1], [0, 0, 0, 2], [0, math.nan, 0, 1]))
        self.reject(self.frame, "stroke_width", (-1, math.inf, True, 1e9 + 1))

    def test_duration_and_checkpoint_end_tolerance_match_native_limits(self):
        self.reject(self.scene, "duration", (0, -1, True, math.inf, 86401))
        self.scene["checkpoints"][-1]["time"] = 1 - 5e-7
        validate_timeline(self.data)
        self.scene["checkpoints"][-1]["time"] = 1 - 2e-6
        with self.assertRaisesRegex(ValueError, "final checkpoint"):
            validate_timeline(self.data)
        self.scene["duration"] = 86400
        self.scene["checkpoints"][-1]["time"] = 86400
        validate_timeline(self.data)

    def test_checkpoint_order_start_and_labels_are_checked(self):
        self.scene["checkpoints"] = [{"time": 0, "label": ""}, {"time": 1, "label": "End"}]
        validate_timeline(self.data)
        for points in ([{"time": 0}], [{"time": .01}, {"time": 1}],
                       [{"time": 0}, {"time": 0}, {"time": 1}],
                       [{"time": 0}, {"time": 1.1}], [{"time": False}, {"time": 1}],
                       [{"time": 0, "label": 7}, {"time": 1}]):
            self.scene["checkpoints"] = [{"label": "Step", **point} for point in points]
            with self.subTest(points=points), self.assertRaises(ValueError):
                validate_timeline(self.data)
        self.scene["checkpoints"] = [{"time": 0}, {"time": 1, "label": "End"}]
        with self.assertRaisesRegex(ValueError, "missing label"):
            validate_timeline(self.data)

    def test_adjacent_double_times_remain_distinct_and_duplicate_times_fail(self):
        frames = self.object["keyframes"]
        frames += [{**deepcopy(self.frame), "time": math.nextafter(1, -math.inf)},
                   {**deepcopy(self.frame), "time": 1}]
        validate_timeline(self.data)
        frames[-1]["time"] = frames[-2]["time"]
        with self.assertRaisesRegex(ValueError, "increase"):
            validate_timeline(self.data)

    def test_frame_start_sparse_first_frame_and_end_tolerance(self):
        self.frame["time"] = .01
        with self.assertRaisesRegex(ValueError, "start at zero"):
            validate_timeline(self.data)
        self.frame["time"] = 0
        coordinates = self.frame.pop("coords")
        with self.assertRaisesRegex(ValueError, "first keyframe"):
            validate_timeline(self.data)
        self.frame["coords"] = coordinates
        final = {**deepcopy(self.frame), "time": 1 + 5e-7}
        self.object["keyframes"].append(final)
        validate_timeline(self.data)
        final["time"] = 1 + 2e-6
        with self.assertRaises(ValueError):
            validate_timeline(self.data)

    def test_collection_limits_empty_objects_and_unique_identifiers(self):
        self.scene["objects"] = []
        validate_timeline(self.data)
        self.scene["objects"] = [self.object, deepcopy(self.object)]
        with self.assertRaisesRegex(ValueError, "duplicate object"):
            validate_timeline(self.data)
        self.scene["objects"] = [self.object]
        self.data["scenes"] = [self.scene, deepcopy(self.scene)]
        with self.assertRaisesRegex(ValueError, "[Dd]uplicate scene"):
            validate_timeline(self.data)
        self.data["scenes"] = [self.scene]
        for owner, key, count in ((self.data, "scenes", 1001), (self.scene, "objects", 20001),
                                  (self.scene, "checkpoints", 10001), (self.object, "keyframes", 200001)):
            saved = owner[key]
            owner[key] = [saved[0]] * count
            with self.subTest(collection=key), self.assertRaises(ValueError):
                validate_timeline(self.data)
            owner[key] = saved

    def test_json_input_rejects_nonstandard_constants_and_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "scene.vector.json"
            for payload in (b'{"version":NaN}', b'{"version":Infinity}', b'{"version":-Infinity}',
                            b'\xff', b'not JSON'):
                with self.subTest(payload=payload):
                    source.write_bytes(payload)
                    with self.assertRaises(ValueError):
                        load_timeline(source)
            source.write_text(json.dumps(self.data))
            self.assertEqual(load_timeline(source), {"example": self.scene})


if __name__ == "__main__":
    unittest.main(verbosity=2)
