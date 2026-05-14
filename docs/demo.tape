# WarWatch demo recording -> docs/dashboard.gif
#
# Requires Charm's vhs (https://github.com/charmbracelet/vhs):
#     brew install vhs
# Then from the repo root:
#     vhs docs/demo.tape
#
# Output: docs/dashboard.gif (1100 x 700, ~14 s, looping)
#
# After a UI change, re-record by running the same command - vhs scripts
# the keystrokes so the recording stays reproducible.

Output docs/dashboard.gif

Set Theme "TokyoNight"
Set FontFamily "JetBrains Mono"
Set FontSize 13
Set Width 1100
Set Height 700
Set Padding 16
Set Framerate 24
Set PlaybackSpeed 1.0

# Boot the dashboard. Activate your venv first if you have one.
Type ".venv/bin/python app.py"
Enter
Sleep 5s

# Trigger a foreground scrape so the "v +N new" status flashes.
Type "r"
Sleep 5s

# Open the theater filter modal and pick Lebanon (1).
Type "t"
Sleep 1s
Type "1"
Sleep 3s

# Open a SITREP for the active theater, then close it.
Type "s"
Sleep 4s
Escape
Sleep 1s

# Clear filters, then quit.
Type "x"
Sleep 1s
Type "q"
Sleep 1s
