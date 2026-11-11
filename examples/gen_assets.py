# Synthetic stand-in for a talking-head clip: 1080x1920, 12 s, 30 fps.
# Writes (a) the full composite as h264, (b) the person alone as VP9+alpha.
import subprocess, math, numpy as np
from PIL import Image, ImageDraw
W, H, FPS, SECS = 1080, 1920, 30, 12
import os, sys
out = sys.argv[1] if len(sys.argv) > 1 else "examples/assets"
os.makedirs(out, exist_ok=True)
def person(t):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    cx = W//2 + int(40*math.sin(t*1.3)); cy = 760 + int(18*math.sin(t*2.1))
    d.ellipse((cx-210, 1180, cx+210+0, 1180+520), fill=(40, 60, 110, 255))          # shoulders
    d.rectangle((cx-60, cy+200, cx+60, 1230), fill=(230, 180, 150, 255))              # neck
    d.ellipse((cx-190, cy-250, cx+190, cy+250), fill=(240, 190, 160, 255))            # head
    mouth = 6 + int(14*abs(math.sin(t*9)))
    d.ellipse((cx-60, cy+90, cx+60, cy+90+mouth*2), fill=(120, 40, 50, 255))          # mouth
    for ex in (-70, 70): d.ellipse((cx+ex-18, cy-40, cx+ex+18, cy-4), fill=(30, 30, 30, 255))
    return im
def background(t):
    a = np.linspace(0, 1, H)[:, None, None]
    g = (np.array([18, 24, 48])*(1-a) + np.array([90, 40, 110])*a).astype(np.uint8)
    im = Image.fromarray(np.broadcast_to(g, (H, W, 3)).copy(), "RGB")
    d = ImageDraw.Draw(im)
    for i in range(6):
        x = (i*230 + int(t*60)) % (W+200) - 100; d.ellipse((x, 200+i*40, x+120, 320+i*40), outline=(255,255,255), width=3)
    return im
def enc(cmd): return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
full = enc(["ffmpeg","-y","-f","rawvideo","-pix_fmt","rgb24","-s",f"{W}x{H}","-r",str(FPS),"-i","-",
            "-f","lavfi","-i",f"sine=frequency=220:duration={SECS}","-shortest",
            "-c:v","libx264","-g",str(FPS),"-pix_fmt","yuv420p","-crf","20","-c:a","aac",f"{out}/speaker.mp4"])
cut = enc(["ffmpeg","-y","-f","rawvideo","-pix_fmt","rgba","-s",f"{W}x{H}","-r",str(FPS),"-i","-",
           "-c:v","libvpx-vp9","-pix_fmt","yuva420p","-b:v","1M","-auto-alt-ref","0","-deadline","realtime","-cpu-used","8",f"{out}/cutout.webm"])
for n in range(FPS*SECS):
    t = n/FPS; p = person(t); bg = background(t).convert("RGBA"); bg.alpha_composite(p)
    full.stdin.write(bg.convert("RGB").tobytes()); cut.stdin.write(p.tobytes())
full.stdin.close(); cut.stdin.close(); full.wait(); cut.wait()
