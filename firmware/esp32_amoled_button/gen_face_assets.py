#!/usr/bin/env python3
"""Render the DrawBox face as anti-aliased bitmaps and emit LVGL v8 image
arrays (face_assets.h). Runs on the host at build time; the firmware only
blits the results, so the screen never shows runtime vector artifacts.

Outputs:
  face_assets.h            LVGL image descriptors (RGB565, pupils with alpha)
  /tmp/face_preview.png    composed screens for eyeballing before a flash
"""

from PIL import Image, ImageDraw, ImageFilter

SS = 3  # supersample factor; LANCZOS downscale does the anti-aliasing

BG = (10, 12, 24)
FACE_TOP = (255, 214, 74)
FACE_BOTTOM = (244, 158, 32)
RIM_SHADOW = (196, 120, 18)
FEATURE = (64, 40, 18)
MOUTH_DARK = (74, 32, 22)
TONGUE = (238, 106, 100)
BLUSH = (255, 132, 116)
EYE_WHITE = (255, 255, 255)
SPARKLE = (255, 255, 255)

# Screen geometry (480x480). One face placement for every state.
FACE_CX, FACE_CY, FACE_R = 240, 230, 160
DISC_BOX = (70, 60, 410, 400)          # 340x340
EYES_BOX = (120, 130, 360, 250)        # 240x120
MOUTH_BOX = (146, 244, 334, 364)       # 188x120
EYE_DX, EYE_DY = 58, 38                # eye centers vs face center
PUPIL_SIZE = 44


def _radial_face(size):
    """Face disc: vertical warm gradient in a circle, rim shading, gloss."""
    grad = Image.new("RGB", (1, size))
    for yy in range(size):
        t = yy / (size - 1)
        grad.putpixel((0, yy), tuple(
            int(FACE_TOP[c] + (FACE_BOTTOM[c] - FACE_TOP[c]) * t)
            for c in range(3)))
    grad = grad.resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ds = ImageDraw.Draw(shadow)
    ds.ellipse([size * 0.06, size * 0.10, size * 0.94, size * 0.98],
               outline=RIM_SHADOW + (110,), width=int(size * 0.035))
    shadow = shadow.filter(ImageFilter.GaussianBlur(size * 0.02))
    img.alpha_composite(shadow)
    gloss = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dg = ImageDraw.Draw(gloss)
    dg.ellipse([size * 0.16, size * 0.05, size * 0.84, size * 0.42],
               fill=(255, 255, 255, 54))
    gloss = gloss.filter(ImageFilter.GaussianBlur(size * 0.03))
    img.alpha_composite(gloss)
    return img


def _soft_dot(draw_target, center, radius, color, alpha):
    dot = Image.new("RGBA", draw_target.size, (0, 0, 0, 0))
    dd = ImageDraw.Draw(dot)
    dd.ellipse([center[0] - radius, center[1] - radius,
                center[0] + radius, center[1] + radius],
               fill=color + (alpha,))
    dot = dot.filter(ImageFilter.GaussianBlur(radius * 0.45))
    draw_target.alpha_composite(dot)


def render_canvas():
    """Full 480x480 canvas (supersampled) with bg + face disc + blush."""
    s = 480 * SS
    canvas = Image.new("RGBA", (s, s), BG + (255,))
    disc = _radial_face(2 * FACE_R * SS)
    canvas.alpha_composite(disc, (int((FACE_CX - FACE_R) * SS),
                                  int((FACE_CY - FACE_R) * SS)))
    for sx in (-1, 1):
        _soft_dot(canvas,
                  ((FACE_CX + sx * FACE_R * 0.60) * SS,
                   (FACE_CY + FACE_R * 0.28) * SS),
                  FACE_R * 0.17 * SS, BLUSH, 150)
    return canvas


def _round_stroke_arc(d, box, start, end, width, color):
    d.arc(box, start, end, fill=color, width=width)
    import math
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    rx, ry = (box[2] - box[0]) / 2, (box[3] - box[1]) / 2
    rmid_x, rmid_y = rx - width / 2, ry - width / 2
    for ang in (start, end):
        a = math.radians(ang)
        px, py = cx + rmid_x * math.cos(a), cy + rmid_y * math.sin(a)
        d.ellipse([px - width / 2, py - width / 2,
                   px + width / 2, py + width / 2], fill=color)


def draw_eyes(canvas, kind):
    d = ImageDraw.Draw(canvas)
    for sx in (-1, 1):
        ex, ey = (FACE_CX + sx * EYE_DX) * SS, (FACE_CY - EYE_DY) * SS
        if kind in ("open", "wide"):
            r = (30 if kind == "open" else 36) * SS
            d.ellipse([ex - r, ey - r * 1.08, ex + r, ey + r * 1.08],
                      fill=EYE_WHITE)
        elif kind == "closed":
            _round_stroke_arc(d, [ex - 30 * SS, ey - 26 * SS,
                                  ex + 30 * SS, ey + 14 * SS],
                              25, 155, 9 * SS, FEATURE)
        elif kind == "joy":
            _round_stroke_arc(d, [ex - 32 * SS, ey - 14 * SS,
                                  ex + 32 * SS, ey + 34 * SS],
                              205, 335, 11 * SS, FEATURE)


def draw_mouth(canvas, kind):
    d = ImageDraw.Draw(canvas)
    mx, my = FACE_CX * SS, (FACE_CY + 62) * SS
    if kind == "smile":
        _round_stroke_arc(d, [mx - 62 * SS, my - 66 * SS,
                              mx + 62 * SS, my + 18 * SS],
                          30, 150, 13 * SS, FEATURE)
    elif kind == "flat":
        w, h = 46 * SS, 6 * SS
        d.rounded_rectangle([mx - w, my - h, mx + w, my + h],
                            radius=h, fill=FEATURE)
    elif kind == "joy":
        d.pieslice([mx - 58 * SS, my - 46 * SS, mx + 58 * SS, my + 46 * SS],
                   8, 172, fill=MOUTH_DARK)
        d.pieslice([mx - 34 * SS, my + 2 * SS, mx + 34 * SS, my + 52 * SS],
                   180, 360, fill=TONGUE)
    elif kind.startswith("o"):
        t = int(kind[1]) / 4.0
        rx = (22 + 12 * t) * SS
        ry = (12 + 30 * t) * SS
        d.ellipse([mx - rx, my - ry, mx + rx, my + ry], fill=MOUTH_DARK)
        if t >= 0.5:
            d.ellipse([mx - rx * 0.62, my + ry * 0.25,
                       mx + rx * 0.62, my + ry * 0.95], fill=TONGUE)


def render_pupil():
    s = PUPIL_SIZE * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, s - 1, s - 1], fill=FEATURE)
    d.ellipse([s * 0.52, s * 0.14, s * 0.80, s * 0.42],
              fill=SPARKLE + (235,))
    d.ellipse([s * 0.20, s * 0.55, s * 0.38, s * 0.73],
              fill=SPARKLE + (140,))
    return img.resize((PUPIL_SIZE, PUPIL_SIZE), Image.LANCZOS)


def downscale(canvas):
    return canvas.resize((480, 480), Image.LANCZOS).convert("RGB")


def crop(img, box):
    return img.crop(box)


def rgb565_bytes(img):
    out = bytearray()
    for r, g, b in img.getdata():
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        out += bytes((v & 0xFF, v >> 8))
    return bytes(out)


def rgb565_alpha_bytes(img):
    out = bytearray()
    for r, g, b, a in img.convert("RGBA").getdata():
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        out += bytes((v & 0xFF, v >> 8, a))
    return bytes(out)


def c_array(name, data):
    lines = [f"static const uint8_t {name}_map[] = {{"]
    for i in range(0, len(data), 20):
        lines.append("".join(f"0x{b:02x}," for b in data[i:i + 20]))
    lines.append("};")
    return "\n".join(lines)


def img_dsc(name, w, h, alpha=False):
    cf = "LV_IMG_CF_TRUE_COLOR_ALPHA" if alpha else "LV_IMG_CF_TRUE_COLOR"
    return (f"const lv_img_dsc_t {name} = {{\n"
            f"    {{{cf}, 0, 0, {w}, {h}}},\n"
            f"    sizeof({name}_map), {name}_map}};")


def main():
    base = render_canvas()

    eye_variants = {}
    for kind in ("open", "wide", "closed", "joy"):
        c = base.copy()
        draw_eyes(c, kind)
        eye_variants[kind] = crop(downscale(c), EYES_BOX)

    mouth_variants = {}
    for kind in ("smile", "o1", "o2", "o3", "o4", "joy", "flat"):
        c = base.copy()
        draw_mouth(c, kind)
        mouth_variants[kind] = crop(downscale(c), MOUTH_BOX)

    disc = crop(downscale(base), DISC_BOX)
    pupil = render_pupil()

    parts = ["// Generated by gen_face_assets.py — do not edit.",
             "#pragma once", '#include <lvgl.h>', ""]
    parts.append(c_array("img_face_disc", rgb565_bytes(disc)))
    parts.append(img_dsc("img_face_disc", *disc.size))
    for kind, img in eye_variants.items():
        parts.append(c_array(f"img_eyes_{kind}", rgb565_bytes(img)))
        parts.append(img_dsc(f"img_eyes_{kind}", *img.size))
    for kind, img in mouth_variants.items():
        parts.append(c_array(f"img_mouth_{kind}", rgb565_bytes(img)))
        parts.append(img_dsc(f"img_mouth_{kind}", *img.size))
    parts.append(c_array("img_pupil", rgb565_alpha_bytes(pupil)))
    parts.append(img_dsc("img_pupil", *pupil.size, alpha=True))
    parts.append(f"""
#define FACE_DISC_X {DISC_BOX[0]}
#define FACE_DISC_Y {DISC_BOX[1]}
#define EYES_X {EYES_BOX[0]}
#define EYES_Y {EYES_BOX[1]}
#define MOUTH_X {MOUTH_BOX[0]}
#define MOUTH_Y {MOUTH_BOX[1]}
#define FACE_CX_PX {FACE_CX}
#define FACE_CY_PX {FACE_CY}
#define FACE_R_PX {FACE_R}
#define EYE_DX_PX {EYE_DX}
#define EYE_DY_PX {EYE_DY}
#define PUPIL_SIZE_PX {PUPIL_SIZE}
""")
    out = "\n".join(parts)
    with open(__file__.replace("gen_face_assets.py", "face_assets.h"), "w") as f:
        f.write(out)
    total = sum(len(rgb565_bytes(i)) for i in
                list(eye_variants.values()) + list(mouth_variants.values()))
    total += len(rgb565_bytes(disc)) + len(rgb565_alpha_bytes(pupil))
    print(f"face_assets.h written, {total // 1024} KB of pixel data")

    # Preview sheet: composed screens for host-side eyeballing.
    def compose(eyes, mouth, pupils=None):
        scr = Image.new("RGB", (480, 480), BG)
        scr.paste(disc, (DISC_BOX[0], DISC_BOX[1]))
        scr.paste(eye_variants[eyes], (EYES_BOX[0], EYES_BOX[1]))
        scr.paste(mouth_variants[mouth], (MOUTH_BOX[0], MOUTH_BOX[1]))
        if pupils is not None:
            for sx in (-1, 1):
                px = FACE_CX + sx * EYE_DX - PUPIL_SIZE // 2 + pupils
                py = FACE_CY - EYE_DY - PUPIL_SIZE // 2
                scr.paste(pupil, (px, py), pupil)
        return scr

    shots = [compose("open", "smile", 0), compose("open", "smile", 9),
             compose("wide", "o3", 0), compose("closed", "smile"),
             compose("joy", "joy"), compose("open", "flat", 0)]
    sheet = Image.new("RGB", (480 * len(shots) + 10 * (len(shots) + 1), 500),
                      (40, 40, 40))
    for i, s in enumerate(shots):
        sheet.paste(s, (10 + i * 490, 10))
    sheet.save("/tmp/face_preview.png")
    print("preview at /tmp/face_preview.png")


if __name__ == "__main__":
    main()
