"""mac/icon/icon-1024.png has a rounded-rect mask with transparent corners.
iOS wants a full-bleed square with no alpha, so crop to the rounded rect and
flatten the corners onto the same deep-space black. Run with any python + PIL."""
from pathlib import Path
from PIL import Image

here = Path(__file__).resolve().parent
src = here.parents[2] / "mac" / "icon" / "icon-1024.png"
dst = here.parent / "Vibey" / "Assets.xcassets" / "AppIcon.appiconset" / "icon-1024.png"
im = Image.open(src).convert("RGBA")
bbox = im.getchannel("A").point(lambda a: 255 if a > 200 else 0).getbbox()
im = im.crop(bbox)
bg = Image.new("RGBA", im.size, (5, 6, 12, 255))
bg.alpha_composite(im)
bg.convert("RGB").resize((1024, 1024), Image.LANCZOS).save(dst)
print("wrote", dst, "from bbox", bbox)
