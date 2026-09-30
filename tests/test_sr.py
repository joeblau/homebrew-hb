"""Exercise image conversion and safe output publication without a GPU or network."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
MAGICK = shutil.which("magick")


class SrCliTests(unittest.TestCase):
    def test_help_and_version_need_no_dependencies(self):
        for option, expected in (("--help", "super-resolution"), ("--version", "sr 0.2.0")):
            result = subprocess.run(
                [sys.executable, str(REPO / "sr"), option],
                env={**os.environ, "PATH": ""}, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(expected, result.stdout)

    def test_bad_arguments(self):
        for args, expected in (
            (["missing.png"], "Input image does not exist"),
            (["image.png", "--scale", "5"], "invalid choice"),
            (["image.png", "--tile-size", "16"], "at least 32"),
            (["image.png", "--tile-size", "no"], "invalid tile_size"),
            (["image.png", "--gpu", "-1"], "nonnegative"),
            (["image.png", "a.png", "-o", "b.png"], "not both"),
            (["image.png", "--sharpen=-1"], "between 0 and 2"),
            (["image.png", "--sharpen=3"], "between 0 and 2"),
            (["image.png", "--sharpen=nan"], "between 0 and 2"),
            (["image.png", "--sharpen=inf"], "between 0 and 2"),
            (["image.png", "--ai-strength=-1"], "between 0 and 1"),
            (["image.png", "--ai-strength=2"], "between 0 and 1"),
            (["image.png", "--ai-strength=nan"], "between 0 and 1"),
            (["image.png", "--ai-strength=inf"], "between 0 and 1"),
            (["image.png", "--ai-passes", "2"], "only supported for scales 8 and 16"),
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, str(REPO / "sr"), *args], capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("Traceback", result.stderr)


@unittest.skipUnless(MAGICK, "ImageMagick is required for image integration checks")
class SrImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="test sr ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "my image.png"
        self.output = self.root / "my image-sr4x.png"
        self.magick("-size", "8x6", "xc:none", "-fill", "red", "-draw", "rectangle 0,0 3,5", self.source)
        self.original = self.source.read_bytes()
        self.models = self.root / "models"
        self.models.mkdir()
        for model in ("realesrgan-x4plus", "realesrgan-x4plus-anime"):
            for extension in (".param", ".bin"):
                (self.models / (model + extension)).write_text("test model")
        self.log = self.root / "backend.json"
        self.backend = self.root / "fake backend"
        self.backend.write_text(f"#!{sys.executable}\n" + r'''
import json
import os
from pathlib import Path
import subprocess
import sys

args = sys.argv[1:]
Path(os.environ["SR_TEST_LOG"]).write_text(json.dumps(args))
source = args[args.index("-i") + 1]
size = subprocess.check_output([os.environ["SR_TEST_MAGICK"], "identify", "-format", "%wx%h", source], text=True)
history = Path(os.environ["SR_TEST_LOG"] + ".jsonl")
pass_number = len(history.read_text().splitlines()) + 1 if history.exists() else 1
with history.open("a") as stream:
    stream.write(json.dumps({"args": args, "input_size": size}) + "\n")
target = args[args.index("-o") + 1]
if os.environ.get("SR_TEST_RACE"):
    Path(os.environ["SR_TEST_RACE"]).write_text("created by another process")
if os.environ.get("SR_TEST_FAIL") or str(pass_number) == os.environ.get("SR_TEST_FAIL_ON_PASS"):
    Path(target).write_text("partial output")
    print("simulated GPU failure", file=sys.stderr)
    sys.exit(7)
if os.environ.get("SR_TEST_CORRUPT"):
    Path(target).write_text("invalid image")
    sys.exit(0)
scale = args[args.index("-s") + 1]
if os.environ.get("SR_TEST_WRONG_SIZE"):
    scale = "2"
command = [os.environ["SR_TEST_MAGICK"], source, "-resize", str(int(scale) * 100) + "%"]
if os.environ.get("SR_TEST_TINT"):
    command += ["-channel", "RGB", "-evaluate", "set", "75%", "+channel"]
subprocess.run([*command, target], check=True)
''')
        self.backend.chmod(0o755)
        self.env = {
            **os.environ, "SR_BACKEND": str(self.backend), "SR_MODEL_DIR": str(self.models),
            "SR_TEST_LOG": str(self.log), "SR_TEST_MAGICK": MAGICK,
        }

    def magick(self, *args):
        result = subprocess.run([MAGICK, *map(str, args)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def sr(self, *args, **env):
        result = subprocess.run(
            [sys.executable, str(REPO / "sr"), *map(str, args)],
            env={**self.env, **env}, capture_output=True, text=True,
        )
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(list(self.root.glob(".sr-*")), [], "temporary files leaked")
        self.assertNotIn("Traceback", result.stderr)
        return result

    def size(self, path):
        return self.magick("identify", "-format", "%wx%h", path)

    def assert_error(self, result, expected):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(expected, result.stderr)

    def test_default_scale_and_transparency_with_spaces(self):
        result = self.sr(self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.output))
        self.assertEqual(self.size(self.output), "32x24")
        alpha = float(self.magick(self.output, "-format", "%[fx:p{31,0}.a]", "info:"))
        self.assertEqual(alpha, 0)
        args = json.loads(self.log.read_text())
        self.assertEqual(args[args.index("-n") + 1], "realesrgan-x4plus")

    def test_scales_always_use_native_4x_model(self):
        for scale in (2, 3):
            with self.subTest(scale=scale):
                output = self.root / f"scaled-{scale}.png"
                result = self.sr(self.source, output, "--scale", scale)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.size(output), f"{8 * scale}x{6 * scale}")
                args = json.loads(self.log.read_text())
                self.assertEqual(args[args.index("-s") + 1], "4")

    def test_anime_gpu_tiles_and_tta(self):
        result = self.sr(self.source, "--model", "anime", "--gpu", "0", "--tile-size", "32", "--tta")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.log.read_text())
        for flag, value in (("-n", "realesrgan-x4plus-anime"), ("-g", "0"), ("-t", "32")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("-x", args)

    def test_large_scales_run_two_ai_passes(self):
        for scale in (8, 16):
            with self.subTest(scale=scale):
                output = self.root / f"large-{scale}.png"
                result = self.sr(self.source, output, "--scale", scale, "--ai-passes", "2", "--model", "anime",
                                 "--gpu", "0", "--tile-size", "32", "--tta", "--sharpen")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.size(output), f"{8 * scale}x{6 * scale}")
                history = [json.loads(line) for line in Path(str(self.log) + ".jsonl").read_text().splitlines()]
                first, second = history[-2:]
                self.assertEqual(first["input_size"], "8x6")
                self.assertEqual(second["input_size"], "16x12" if scale == 8 else "32x24")
                for call in (first, second):
                    args = call["args"]
                    for flag, expected in (("-s", "4"), ("-n", "realesrgan-x4plus-anime"), ("-g", "0"), ("-t", "32")):
                        self.assertEqual(args[args.index(flag) + 1], expected)
                    self.assertIn("-x", args)
                self.assertIn("AI pass 2/2", result.stderr)
                self.assertEqual(float(self.magick(output, "-format", f"%[fx:p{{{8 * scale - 1},0}}.a]", "info:")), 0)

    def test_second_pass_failure_preserves_existing_output(self):
        self.output.write_bytes(b"keep me")
        result = self.sr(self.source, self.output, "--scale", "8", "--ai-passes", "2", "--force", SR_TEST_FAIL_ON_PASS="2")
        self.assert_error(result, "AI pass 2/2")
        self.assertIn("simulated GPU failure", result.stderr)
        self.assertEqual(self.output.read_bytes(), b"keep me")

    def test_large_scales_default_to_one_ai_pass(self):
        for scale in (8, 16):
            with self.subTest(scale=scale):
                output = self.root / f"single-pass-{scale}.png"
                result = self.sr(self.source, output, "--scale", scale)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.size(output), f"{8 * scale}x{6 * scale}")
                self.assertIn("AI pass 1/1", result.stderr)
                self.assertNotIn("AI pass 2", result.stderr)
        history = Path(str(self.log) + ".jsonl").read_text().splitlines()
        self.assertEqual(len(history), 2)

    def test_zero_strength_does_not_require_or_run_ai(self):
        result = self.sr(self.source, "--scale", "2", "--ai-strength", "0",
                         SR_BACKEND=str(self.root / "missing"), SR_MODEL_DIR=str(self.root / "no-models"))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.root / "my image-sr2x.png"
        self.assertEqual(self.size(output), "16x12")
        self.assertFalse(self.log.exists())
        self.assertNotIn("AI pass", result.stderr)

    def test_ai_strength_blends_pixels_and_preserves_alpha(self):
        baseline = self.root / "baseline.png"
        mixed = self.root / "mixed.png"
        for output, strength in ((baseline, "1"), (mixed, "0.5")):
            result = self.sr(self.source, output, "--ai-strength", strength, SR_TEST_TINT="1")
            self.assertEqual(result.returncode, 0, result.stderr)
        values = self.magick(mixed, "-format", "%[fx:p{2,12}.r] %[fx:p{2,12}.g] %[fx:p{2,12}.b]", "info:")
        for actual, expected in zip(map(float, values.split()), (0.875, 0.375, 0.375)):
            self.assertAlmostEqual(actual, expected, delta=0.01)
        for image, name in ((baseline, "alpha-before.png"), (mixed, "alpha-after.png")):
            self.magick(image, "-alpha", "extract", self.root / name)
        self.assertEqual(self.magick(self.root / "alpha-before.png", self.root / "alpha-after.png",
                                     "-compose", "difference", "-composite", "-format", "%[fx:mean]", "info:"), "0")

    def test_sharpening_increases_edge_contrast(self):
        source = self.root / "soft-edge.png"
        self.magick("-size", "8x6", "xc:gray25", "-fill", "gray75", "-draw", "rectangle 4,0 7,5", source)
        baseline = self.root / "baseline.png"
        sharpened = self.root / "sharpened.png"
        self.assertEqual(self.sr(source, baseline).returncode, 0)
        self.assertEqual(self.sr(source, sharpened, "--sharpen").returncode, 0)
        contrast = "%[fx:p{18,12}.r-p{13,12}.r]"
        before = float(self.magick(baseline, "-format", contrast, "info:"))
        after = float(self.magick(sharpened, "-format", contrast, "info:"))
        self.assertGreater(after, before)
        self.assertEqual(self.size(sharpened), self.size(baseline))

    def test_sharpening_strength_and_alpha(self):
        baseline = self.root / "baseline.png"
        disabled = self.root / "disabled.png"
        sharpened = self.root / "sharpened.png"
        for output, options in ((baseline, ()), (disabled, ("--sharpen", "0")), (sharpened, ("--sharpen", "0.5"))):
            result = self.sr(self.source, output, *options)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.magick(baseline, disabled, "-compose", "difference", "-composite",
                                     "-format", "%[fx:mean]", "info:"), "0")
        before_alpha = self.root / "alpha-before.png"
        after_alpha = self.root / "alpha-after.png"
        self.magick(baseline, "-alpha", "extract", before_alpha)
        self.magick(sharpened, "-alpha", "extract", after_alpha)
        self.assertEqual(self.magick(before_alpha, after_alpha, "-compose", "difference", "-composite",
                                     "-format", "%[fx:mean]", "info:"), "0")

    def test_tiff_input_and_jpeg_output(self):
        source = self.root / "scan.tiff"
        output = self.root / "result.jpg"
        self.magick(self.source, source)
        result = self.sr(source, "-o", output, "-s", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.size(output), "16x12")
        # JPEG compression can perturb white by a few levels.
        minimum = float(self.magick(output, "-format", "%[fx:min(p{15,0}.r,min(p{15,0}.g,p{15,0}.b))]", "info:"))
        self.assertGreater(minimum, 0.97)

    def test_animated_input_uses_first_frame(self):
        source = self.root / "animation.gif"
        self.magick("-size", "8x6", "xc:red", "-size", "4x2", "xc:blue", source)
        result = self.sr(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.size(self.root / "animation-sr4x.png"), "32x24")

    def test_existing_output_is_preserved_unless_forced(self):
        self.output.write_bytes(b"keep me")
        result = self.sr(self.source)
        self.assert_error(result, "Output already exists")
        self.assertEqual(self.output.read_bytes(), b"keep me")
        self.assertFalse(self.log.exists())
        result = self.sr(self.source, "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.size(self.output), "32x24")

    def test_force_cannot_overwrite_source_or_alias(self):
        symlink = self.root / "symlink.png"
        symlink.symlink_to(self.source)
        hardlink = self.root / "hardlink.png"
        os.link(self.source, hardlink)
        for output in (self.source, symlink, hardlink):
            with self.subTest(output=output):
                self.assert_error(self.sr(self.source, output, "--force"), "different files")
        self.assertFalse(self.log.exists())

    def test_failure_keeps_existing_output_and_cleans_up(self):
        self.output.write_bytes(b"keep me")
        result = self.sr(self.source, "--force", SR_TEST_FAIL="1")
        self.assert_error(result, "simulated GPU failure")
        self.assertEqual(self.output.read_bytes(), b"keep me")

    def test_invalid_input_creates_no_output(self):
        source = self.root / "broken.png"
        source.write_text("not an image")
        self.assert_error(self.sr(source), "Decoding input image failed")
        self.assertFalse((self.root / "broken-sr4x.png").exists())
        self.assertFalse(self.log.exists())

    def test_invalid_backend_output_creates_no_output(self):
        self.assert_error(self.sr(self.source, SR_TEST_CORRUPT="1"), "Reading image dimensions failed")
        self.assertFalse(self.output.exists())
        self.assert_error(self.sr(self.source, SR_TEST_WRONG_SIZE="1"), "unexpected image dimensions")
        self.assertFalse(self.output.exists())

    def test_output_created_during_inference_is_not_overwritten(self):
        result = self.sr(self.source, SR_TEST_RACE=str(self.output))
        self.assert_error(result, "Output already exists")
        self.assertEqual(self.output.read_text(), "created by another process")

    def test_dependency_errors(self):
        self.assert_error(self.sr(self.source, PATH=""), "ImageMagick is missing")
        self.assert_error(self.sr(self.source, SR_BACKEND=str(self.root / "missing")), "Real-ESRGAN is missing")
        (self.models / "realesrgan-x4plus.bin").unlink()
        self.assert_error(self.sr(self.source), "Model file is missing")
        self.assertFalse(self.output.exists())

    def test_invalid_output_paths(self):
        for output, expected in (
            (self.root / "unknown.xyz", "Output extension"),
            (self.root / "missing" / "output.png", "Output directory"),
            (self.models, "Output is a directory"),
        ):
            with self.subTest(output=output):
                self.assert_error(self.sr(self.source, output, "--force"), expected)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
