# Image super-resolution

`sr` applies [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) locally to restore
detail while enlarging an image. The Homebrew formula bundles the upstream
universal macOS executable and pretrained models, installs ImageMagick for
format conversion, and uses Homebrew Python. Apple Silicon and Intel Macs need
a Vulkan/Metal-capable GPU; there is no CPU fallback.

## Install and use

Install the versioned formula:

```sh
brew install joeblau/hb/sr
sr photo.jpg
sr photo.jpg enhanced.jpg --scale 2
sr scan.tiff -o enhanced.png --scale 3
sr drawing.png --model anime --tta
sr photo.jpg --scale 8 --sharpen
sr photo.jpg --scale 16 --sharpen 0.5 -o large.png
sr photo.jpg --scale 2 --ai-strength 0.5 -o gentle.png
```

For future stable releases, run `brew update` followed by
`brew upgrade joeblau/hb/sr`. Check the installed version with `sr --version`.
The optional `--HEAD` installation tracks unreleased changes on `main`.

The default output is `<input-stem>-sr<scale>x.png` beside the input. Supported
scales are 2, 3, 4, 8, and 16. Both models run AI inference at their native 4x
scale, then outputs at other scales are resized with Lanczos. By default,
8x/16x enlarge the single 4x AI result; this adds pixels, not another level of
AI-generated detail. Use `--ai-passes 2` to opt into repeated AI restoration:
for 8x, the first AI result is downsampled to 2x before a second 4x AI pass;
for 16x, two 4x AI passes run consecutively. The 8x path avoids allocating an
unnecessary 16x image. The default `photo` model is
`realesrgan-x4plus`; `anime` uses `realesrgan-x4plus-anime`.

AI restoration already adds detail. `--sharpen` optionally applies
[unsharp masking](https://imagemagick.org/command-line-options/#unsharp) to the
final color channels after resizing, preserving alpha. This finishing filter
is conventional image processing; Real-ESRGAN provides the AI enhancement.
The default strength when the flag is enabled is 1; `--sharpen 0.5` is gentler,
`--sharpen 2` is stronger, and `--sharpen 0` disables it. Sharpening is off unless
requested.

Larger scales take more time and disk space: 8x has 64 times the input's pixel
count, and 16x has 256 times. Additional AI passes can amplify artifacts or
invent textures; larger output does not guarantee more accurate detail.

If a result looks waxy, overprocessed, or has invented texture, try 2x/4x
without sharpening first. `--ai-strength 0.5` blends the AI result with a
Lanczos resize of the original before the final resize and sharpening. This
is an output blend, not a model denoising parameter. Strength 1 (the default)
keeps the full AI result; 0 skips AI entirely and needs only ImageMagick.
For text, logos, or pixel art, compare against `--ai-strength 0` before using
AI restoration. Strong sharpening can add halos around edges.

Output formats are PNG, JPEG, WebP, TIFF, BMP, HEIC, and AVIF, selected by the
output extension. Codec availability follows the installed ImageMagick build.
Inputs may use any format ImageMagick can decode. Animated and multipage images
use only their first frame/page. Images are auto-oriented and converted to
8-bit sRGB before inference. PNG preserves transparency; JPEG and BMP flatten
it against white. Original metadata, HDR, and high bit depth are not retained.

Existing outputs require `--force`. The input cannot be used as the output,
even with `--force`. Failed runs leave existing outputs untouched, and staging
files are removed automatically. The output directory must already exist.

`--tile-size 32` reduces GPU memory usage; `0` (the default) selects a size
automatically. `--gpu N` selects a GPU index, and `--tta` enables slower
test-time augmentation. Run `sr --help` for all options. Models are installed
once; no image is uploaded and inference needs no network access.

## Run from a checkout

Install ImageMagick and unpack the
[upstream macOS release](https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip)
outside this repository. Make its executable runnable, then point `sr` at it:

```sh
brew install imagemagick
chmod +x /path/to/realesrgan/realesrgan-ncnn-vulkan
export SR_BACKEND=/path/to/realesrgan/realesrgan-ncnn-vulkan
export SR_MODEL_DIR=/path/to/realesrgan/models
./sr image.jpg
```

`SR_BACKEND` may also name an executable on `PATH`. Without an override, `sr`
uses its Homebrew-bundled executable or `realesrgan-ncnn-vulkan` on `PATH`.
`SR_MODEL_DIR` defaults to the `models` directory beside the backend executable.

Run regression checks with:

```sh
python3 -m unittest discover -s tests -p 'test_sr.py' -v
```
