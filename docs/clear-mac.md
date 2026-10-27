# Clear Dock items and desktop widgets (`clear-mac`)

`clear-mac` is a private helper bundled with `runner-setup`. `runner-bootstrap`
runs it automatically when the Dock or desktop needs clearing. There is no
separate `clear-mac` formula to install or trust. Bootstrap's
`--skip-clear-desktop` flag opts out.

To run just the desktop cleanup manually:

```sh
brew update
brew install joeblau/hb/runner-setup
"$(brew --prefix joeblau/hb/runner-setup)/libexec/clear-mac"
```

Run as the user whose desktop you want to clear, without `sudo`. The command
clears pinned apps, files and folders from the Dock, hides recent apps, and
hides all desktop widgets in both the standard desktop and Stage Manager.
Widget layouts are retained; Notification Center widgets are preserved.
These are the [Show Widgets settings](https://support.apple.com/guide/mac-help/add-and-customize-widgets-mchl52be5da5/mac)
in System Settings > Desktop & Dock. Requires macOS Sonoma or later.

Both preference domains are backed up before any changes under
`~/Library/Application Support/clear-mac/backups/`. The command prints restore
commands and refreshes that user's Dock and Notification Center. Finder, Trash,
running apps and minimized windows can still appear.

The standalone `clear-mac` formula and the legacy `clear-dock` rename have
been removed. Backups from either command remain in their original directories.
