"""Rebuild our original vector-style application icon. Optional Pillow tooling."""
from pathlib import Path
from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent / 'assets'
SCALE = 4
image = Image.new('RGBA', (256 * SCALE, 256 * SCALE))
draw = ImageDraw.Draw(image)


def box(coords):
    return tuple(int(n * SCALE) for n in coords)


def rounded(coords, radius, color):
    draw.rounded_rectangle(box(coords), radius=radius * SCALE, fill=color)


rounded((8, 8, 248, 248), 55, '#235dd7')
rounded((67, 51, 199, 215), 18, '#18449a')
rounded((51, 39, 183, 203), 18, '#ffffff')
for y, length in [(72, 60), (103, 60), (134, 40)]:
    rounded((74, y, 86, y + 12), 3, '#235dd7')
    rounded((98, y + 2, 98 + length, y + 10), 4, '#b4c6e8')
draw.ellipse(box((133, 132, 223, 222)), fill='#235dd7')
draw.ellipse(box((143, 142, 213, 212)), fill='#a7f0da')
points = [(160, 177), (172, 189), (197, 162)]
draw.line([box(point) for point in points], fill='#124c50', width=9 * SCALE, joint='curve')
for x, y in points:
    draw.ellipse(box((x - 4.5, y - 4.5, x + 4.5, y + 4.5)), fill='#124c50')
image = image.resize((256, 256), Image.Resampling.LANCZOS)
BASE.mkdir(exist_ok=True)
image.save(BASE / 'list-pick.png')
image.save(BASE / 'list-pick.ico', sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
print('Created PNG and multi-resolution ICO from original geometry.')
