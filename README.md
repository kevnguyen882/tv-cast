# TV Cast

Send videos from your computer to a smart TV over your home network (DLNA / UPnP), with a simple control page in your browser.

- **Cast a web video**: paste the page a video plays on, or a direct `.mp4` / `.m3u8` link. The app opens the page in a hidden browser, presses play, and picks up the stream (the master `.m3u8` playlist), the same way you would by hand in DevTools → Network. If nothing turns up, it opens a visible window so you can press play yourself, then falls back to yt-dlp.
- **Download** the same links to your Downloads folder as `.mp4`, with progress and cancel.
- **Cast a file from your computer** using the normal Mac file picker.
- **Remote control**: play, pause, stop, seek, skip and volume.
- Works with picky TVs: it asks the TV which formats it accepts and tries them until one is taken, and relays the video through your computer so sites that check where requests come from still work.

## Requirements

- macOS (the file picker uses AppleScript; everything else also runs on Linux)
- Python 3.10+
- A TV or other DLNA media renderer on the same network

## Setup

```bash
python3 -m pip install playwright yt-dlp   # page scanner + fallback extractor
python3 -m playwright install chromium     # browser used by the page scanner
brew install ffmpeg                         # needed for .m3u8 streams
```

All three are optional. Without them you can still cast direct `.mp4` links and files on your computer.

## Run

```bash
python3 tv_cast.py
```

A control page opens at <http://127.0.0.1:8765>. Pick your TV, paste a link or choose a file, and press **Cast to TV**.

When you start it, the Terminal output shows which extras were found.

## Troubleshooting

**No TVs found**
- On macOS, allow Terminal under System Settings → Privacy & Security → Local Network, then quit and reopen Terminal.
- Turn off any VPN.
- Make sure the TV's media sharing setting is on (often called Screen Share, Media Sharing or DLNA).
- Add the TV by its IP address (shown in the TV's network settings) using **Add TV by IP address**.

**The TV refuses a video**
The Terminal shows each format the TV turned down and the formats it says it plays. Downloading the video and casting the saved `.mp4` is the most reliable fallback.

**macOS asks whether Python may accept incoming connections**
Click **Allow**. The TV fetches the video from your computer. The control page itself only accepts connections from your own computer.

## Notes

Only use this with content you have the right to access.

## License

MIT — see [LICENSE](LICENSE). You're free to use, copy, modify and share it.
