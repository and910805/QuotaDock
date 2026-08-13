from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    output = Path(__file__).with_name("codex_usage.ico")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = []
    for size in sizes:
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        margin = max(1, size // 32)
        radius = size * 0.25
        draw.rounded_rectangle(
            (margin, margin, size - margin - 1, size - margin - 1),
            radius=radius,
            fill="#0B1220",
            outline="#334155",
            width=max(1, size // 32),
        )
        stroke = max(2, size // 10)
        inset = size * 0.22
        draw.arc(
            (inset, inset, size - inset, size - inset),
            start=40,
            end=320,
            fill="#35E28A",
            width=stroke,
        )
        images.append(image)
    images[-1].save(output, format="ICO", sizes=[(size, size) for size in sizes], append_images=images[:-1])


if __name__ == "__main__":
    main()
