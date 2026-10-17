# typed: false
# frozen_string_literal: true

class ClearDock < Formula
  desc "Clear removable macOS Dock items with a preference backup"
  homepage "https://github.com/joeblau/homebrew-hb"
  url "https://github.com/joeblau/homebrew-hb/archive/refs/tags/clear-dock-v0.1.0.tar.gz"
  sha256 "c25f1bdf0a3d05e6bbf80ae4b3950e57a18900e27c6d5a12e6ad924dfac83bea"
  license "MIT"

  depends_on :macos

  def install
    bin.install "clear-dock"
  end

  def caveats
    <<~EOS
      Run as the user whose Dock you want to clear, without sudo:
        clear-dock

      Dock preferences are backed up before clearing pinned items and hiding
      recent apps. The command prints the backup path and a restore command.
      Finder, Trash, running apps and minimized windows can still appear.
    EOS
  end

  test do
    assert_match "Usage: clear-dock", shell_output("#{bin}/clear-dock --help")
  end
end
