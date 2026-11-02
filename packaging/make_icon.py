"""Genera el icono de Mikoshi: el tejado de un santuario portátil sobre sus varas."""
from pathlib import Path

from PIL import Image, ImageDraw

BG, GOLD, DARK = "#1e2026", "#e0a458", "#8c5a24"
S = 1024


def draw() -> Image.Image:
    image = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=210, fill=BG)
    # Remate, tejado curvado, cuerpo y varas de transporte.
    d.ellipse((462, 130, 562, 230), fill=GOLD)
    d.polygon([(512, 200), (800, 430), (880, 430), (880, 480), (144, 480), (144, 430), (224, 430)], fill=GOLD)
    d.rectangle((292, 500, 732, 720), fill=GOLD)
    d.rectangle((462, 560, 562, 720), fill=BG)
    d.rounded_rectangle((90, 750, 934, 800), radius=25, fill=DARK)
    d.rounded_rectangle((90, 830, 934, 880), radius=25, fill=DARK)
    return image


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "mikoshi" / "assets"
    out.mkdir(exist_ok=True)
    icon = draw()
    icon.resize((256, 256), Image.LANCZOS).save(out / "mikoshi.png")
    icon.save(out / "mikoshi.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
