# ClipMaker for iPhone

Paste a YouTube URL and receive five 30–60 second, 9:16 MP4 clips with animated English captions. Use it only with videos you own or are authorized to reuse.

## What it does

- downloads one YouTube video (playlists are disabled)
- transcribes English speech locally with faster-whisper
- ranks spoken moments using hook, pace, punctuation, and spacing signals
- renders five non-overlapping vertical clips
- burns large animated captions into each clip
- provides individual MP4 downloads and one ZIP download

## Run locally

Docker is the easiest option:

```bash
docker build -t clipmaker .
docker run --rm -p 8080:8080 clipmaker
```

Open `http://localhost:8080`.

## Put it online

Deploy this folder to a Docker-compatible host with at least 2 CPU cores, 4 GB RAM, and persistent disk if you want completed jobs to survive restarts. The first job downloads the Whisper model and can take longer. Set:

- `PORT=8080`
- `WHISPER_MODEL=small.en` (use `base.en` for faster/cheaper processing)
- `MAX_VIDEO_SECONDS=7200`

Do not use a serverless host with a short request timeout; video rendering needs a long-running container.

### Railway

This project includes `railway.json`, so Railway will build it from the Dockerfile and check `/health` automatically. Create a service from the repository, generate a public domain, and use that domain in the iPhone Shortcut. For reliable rendering, select a plan/resource limit with at least 4 GB RAM.

## Make the iPhone Shortcut

After deployment, suppose your address is `https://YOUR-CLIPMAKER-ADDRESS`.

1. Open **Shortcuts** and tap **+**.
2. Add **Get Clipboard**.
3. Add **URL** and enter:
   `https://YOUR-CLIPMAKER-ADDRESS/?url=` followed by the **Clipboard** variable.
4. Add **Open URLs**.
5. Name it **Make Clips** and choose **Add to Home Screen**.

Now copy any YouTube link and tap **Make Clips**. The page opens with the link filled in; tap **Make my clips**, wait for processing, and save each MP4 to Files or Photos.

## Notes

- Processing time depends on video length and server speed.
- Center-crop works best when the main subject stays near the center.
- Job files are stored under `jobs/`. Add authentication and automatic cleanup before sharing a public deployment.
- YouTube may restrict automated downloads in some hosting data centers. If so, configure yt-dlp cookies from an account authorized to view the source video.
