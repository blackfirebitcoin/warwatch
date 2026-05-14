# WarWatch demo recording -> docs/dashboard.gif
#
# Requires Charm's vhs (https://github.com/charmbracelet/vhs):
#     brew install vhs ffmpeg gifsicle
#     brew install --cask font-jetbrains-mono   # tape's requested font
# Then from the repo root:
#     vhs docs/demo.tape
#     gifsicle -O3 --lossy=80 -k 256 docs/dashboard.gif -o docs/dashboard.gif
#
# Output: docs/dashboard.gif (1280 x 960, ~32 s, looping; ~950 KB after gifsicle)
#
# After a UI change, re-record by running the same commands - vhs scripts
# the keystrokes so the recording stays reproducible. See README's Demo
# section for the optional MP4 conversion step.

Output docs/dashboard.gif

Set Theme "TokyoNight"
Set FontFamily "JetBrains Mono"
Set FontSize 16
Set Width 1280
Set Height 960
Set Padding 14
Set Framerate 18
Set PlaybackSpeed 1.0
Set TypingSpeed 30ms

# Hidden setup: activate venv + clear, so the visible session starts
# from a fresh prompt and the user just sees `python app.py`.
Hide
Type "source .venv/bin/activate"
Enter
Sleep 400ms
Type "clear"
Enter
Sleep 400ms
Show

# Boot the dashboard.
Type "python app.py"
Enter
Sleep 4s

# Foreground scrape — pulls fresh events across the 15 sources so the
# feed populates with the "v +N new, M merged" status flash.
Type "r"
Sleep 5s

# Navigate the live feed with arrow keys (proves it's interactive).
Down 2
Sleep 600ms
Down 2
Sleep 600ms
Down 2
Sleep 1200ms

# Open the highlighted event's detail report.
Enter
Sleep 4s

# Close the report and scroll back toward the top of the feed.
Escape
Sleep 800ms
Up 3
Sleep 800ms
Up 2
Sleep 800ms

# Open the theater filter modal — number-key selection menu.
Type "t"
Sleep 1500ms

# Pick Lebanon.
Type "1"
Sleep 2500ms

# Scroll the Lebanon-filtered feed.
Down 2
Sleep 500ms
Down 2
Sleep 1200ms

# Open a SITREP synthesis for the active theater.
Type "s"
Sleep 4s

# Close the SITREP overlay.
Escape
Sleep 800ms

# Clear filters back to the full feed and quit.
Type "x"
Sleep 1500ms
Type "q"
Sleep 800ms
