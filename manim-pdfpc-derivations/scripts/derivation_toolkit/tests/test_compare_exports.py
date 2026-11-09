"""The native comparison command follows exported metadata and checks real pixels."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from ..verify.compare_exports import main


class NativeComparisonInputs(unittest.TestCase):
    def test_configured_manifest_and_historical_default_compare_all_printable_states(self):
        for configured in ("experiment.motion.json", None):
            with self.subTest(manifest=configured), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                assets, reference, output = (root / name for name in ("assets/amber", "previews", "actual"))
                assets.mkdir(parents=True)
                reference.mkdir()
                manifest = assets / (configured or "derivations.vector.json")
                manifest.write_text(json.dumps({"version": 1, "scenes": [{"id": "growth"}]}))
                metadata = {"scenes": [{"id": "growth", "reference_time": .5, "steps": [
                    {"time": 0, "full_preview": "initial.png", "full_figure": "initial.pdf"},
                    {"time": 1.25, "full_preview": "print-only.png", "full_figure": None},
                ]}]}
                if configured:
                    metadata["manifest"] = configured
                (assets.parent / "steps.json").write_text(json.dumps(metadata))
                colors = {0: "red", 1.25: "blue", .5: "green"}
                names = {0: "initial.png", 1.25: "print-only.png", .5: "growth-midpoint.png"}
                for position, name in names.items():
                    Image.new("RGBA", (13, 7), colors[position]).save(reference / name)
                rendered = []
                corrupt = False

                def render(command, **_kwargs):
                    self.assertEqual(command[1:4], ["render", str(manifest.resolve()), "growth"])
                    position = float(command[4])
                    self.assertEqual(command[6:], ["13", "7"])
                    rendered.append(position)
                    image = Image.new("RGBA", (13, 7), colors[position])
                    if corrupt and position == 1.25:
                        image.putpixel((0, 0), (255, 255, 255, 255))
                    image.save(command[5])

                arguments = [str(root / "probe"), str(reference), "--assets", str(assets),
                             "--output", str(output), "--midpoints"]
                with patch("derivation_toolkit.verify.compare_exports.subprocess.run", side_effect=render), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(arguments), 0)
                    self.assertEqual(rendered, [0, 1.25, .5])
                    report = json.loads((output / "report.json").read_text())
                    self.assertEqual(report["manifest"], str(manifest.resolve()))
                    self.assertTrue(report["pixel_identical"])
                    corrupt = True
                    self.assertEqual(main(arguments), 1)
                report = json.loads((output / "report.json").read_text())
                self.assertEqual([row["changed_pixels"] for row in report["comparisons"]], [0, 1, 0])


if __name__ == "__main__":
    unittest.main()
