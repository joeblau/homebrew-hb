# Clear Dock items and desktop widgets (`clear-mac`)

```sh
brew update
brew install joeblau/hb/clear-mac
clear-mac
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

Previously installed `clear-dock` users can run `brew upgrade clear-mac` after
`brew update`; Homebrew's rename mapping migrates the old formula. Existing
`clear-dock` backups are retained in their original directory.
