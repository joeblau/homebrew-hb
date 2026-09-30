# Image super-resolution

`sr` applies [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) locally to restore
detail while enlarging an image. The Homebrew formula bundles the upstream
universal macOS executable and pretrained models, installs ImageMagick for
format conversion, and uses Homebrew Python. Apple Silicon and Intel Macs need
a Vulkan/Metal-capable GPU; there is no CPU fallback.

## Install and use

Install the HEAD formula (the first tagged release containing `sr` has not been
published yet):

```sh
brew install --HEAD joeblau/hb/sr
sr photo.jpg
sr photo.jpg enhanced.jpg --scale 2
sr scan.tiff -o enhanced.png --scale 3
sr drawing.png --model anime --tta
```

The default output is `<input-stem>-sr<scale>x.png` beside the input. Supported
scales are 2, 3, and 4; both models run AI inference at their native 4x scale,
then 2x/3x outputs are downsampled with Lanczos. The default `photo` model is
`realesrgan-x4plus`; `anime` uses `realesrgan-x4plus-anime`.

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
