# typed: false
# frozen_string_literal: true

require "language/python"

class Sr < Formula
  include Language::Python::Shebang

  desc "Local AI image super-resolution with Real-ESRGAN"
  homepage "https://github.com/joeblau/homebrew-hb"
  license "MIT"
  # Use HEAD until the first release containing sr is tagged.
  head "https://github.com/joeblau/homebrew-hb.git", branch: "main"

  depends_on "imagemagick"
  depends_on :macos
  depends_on "python@3.14"

  resource "realesrgan" do
    url "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip"
    sha256 "e0ad05580abfeb25f8d8fb55aaf7bedf552c375b5b4d9bd3c8d59764d2cc333a"
  end

  def install
    libexec.install "sr"
    rewrite_shebang detected_python_shebang, libexec/"sr"
    resource("realesrgan").stage do
      (libexec/"realesrgan").install "realesrgan-ncnn-vulkan", "models"
      (libexec/"realesrgan/realesrgan-ncnn-vulkan").chmod 0755
    end
    (bin/"sr").write_env_script libexec/"sr", PATH: "#{formula_opt_bin("imagemagick")}:$PATH"
  end

  def caveats
    <<~EOS
      Upscale an image locally (4x by default):
        sr image.jpg
        sr image.heic -s 2 -o enhanced.png
        sr image.jpg -s 8 --sharpen

      A Vulkan/Metal-capable GPU is required. Models are bundled; processing
      needs no network access. Use --model anime for illustrations.
    EOS
  end

  test do
    assert_match "sr 0.2.0", shell_output("#{bin}/sr --version")
    assert_match "super-resolution", shell_output("#{bin}/sr --help")
    assert_match "Input image does not exist", shell_output("#{bin}/sr missing.png 2>&1", 2)

    system formula_opt_bin("imagemagick")/"magick", "-size", "8x6", "gradient:", "input.png"
    [4, 8, 16].each do |scale|
      system bin/"sr", "input.png", "--scale", scale.to_s, "--sharpen", "0.5", "--tile-size", "32"
      size = shell_output("#{formula_opt_bin("imagemagick")}/magick identify -format '%wx%h' input-sr#{scale}x.png")
      assert_equal "#{8 * scale}x#{6 * scale}", size
    end
  end
end
