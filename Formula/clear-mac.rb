# typed: false
# frozen_string_literal: true

class ClearMac < Formula
  desc "Clear Dock items and hide desktop widgets with preference backups"
  homepage "https://github.com/joeblau/homebrew-hb"
  url "https://github.com/joeblau/homebrew-hb/archive/refs/tags/clear-mac-v0.2.0.tar.gz"
  sha256 "b495a0c6e76ef3c7e99604257e8d1c0d2690d0d843fdca05e8bc923ca8ccea7c"
  license "MIT"

  depends_on macos: :sonoma

  def install
    bin.install "clear-mac"
  end

  def caveats
    <<~EOS
      Run as the user whose desktop you want to clear, without sudo:
        clear-mac

      Clears pinned Dock items and hides recent apps, desktop widgets and
      Stage Manager widgets. Saved widget layouts are retained.
      Preferences are backed up first; restore commands are printed.
      Finder, Trash, running apps and minimized windows can still appear.
    EOS
  end

  test do
    assert_match "Usage: clear-mac", shell_output("#{bin}/clear-mac --help")
  end
end
