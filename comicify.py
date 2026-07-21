r"""COMIC MAKER — one story prompt -> lettered comic pages, fully local (BUILD_SPEC.md).
Stages (resumable, like storyify):
  new       <prompt> [--style STYLE] [--pages N]   -> project scaffold
  script    <project>    LLM writes pages/panels/dialogue (strict JSON, think off)
  panels    <project>    z-image per panel (style block + character sheets locked)  [GPU]
  assemble  <project>    PIL page compositor: grid, gutters, bubbles, SFX, borders  [CPU]
  all       <project>    script -> panels -> assemble
Simple enough for Hermes: `python3 comicify.py all <project>` after `new`.
Projects: ComfyUI/longform/comic_<slug>/  Pages: page_NN.png (+ .cbz)
"""
import argparse, glob, json, math, os, random, re, sys, textwrap, time, urllib.request

WIN = sys.platform == "win32"
ROOT = os.environ.get("COMIC_ROOT",
    r"C:\Users\Ultim\Documents\ComfyUI" if WIN else "/mnt/c/Users/Ultim/Documents/ComfyUI")
FONTS = os.environ.get("COMIC_FONTS", r"C:\Windows\Fonts" if WIN else "/mnt/c/Windows/Fonts")
OLLAMA = os.environ.get("OLLAMA_URL",
    "http://172.29.160.1:11434" if not WIN else "http://127.0.0.1:11434")

STYLES = {
    "sunday-strip": ("colorful classic funnies cartoon art, bold clean ink outlines, flat "
                     "cel colors, halftone dot shading, expressive cartoon faces"),
    "manga": ("black and white manga art, screentone shading, dynamic ink lines, expressive "
              "large eyes, speed lines on action"),
    "noir": ("high contrast noir comic art, heavy black shadows, limited palette with one red "
             "accent, dramatic angular lighting, gritty ink texture"),
    "euro-album": ("european comic album art, ligne claire clean line style, rich flat colors, "
                   "detailed backgrounds, Tintin-adjacent clarity"),
}
NEG = ("photo, photorealistic, 3d render, blurry, watermark, signature, text, speech bubble, "
       "caption, lettering, deformed hands, extra fingers, child, chibi, toddler, baby")


# ---------- stage: new ----------
def cmd_new(a):
    slug = re.sub(r"[^a-z0-9]+", "_", a.prompt.lower())[:32].strip("_")
    proj = os.path.join(ROOT, "longform", f"comic_{slug}")
    os.makedirs(proj, exist_ok=True)
    json.dump({"prompt": a.prompt, "style": a.style, "pages": a.pages, "slug": slug,
               "cast": a.cast},
              open(os.path.join(proj, "project.json"), "w"), indent=1)
    print(f"PROJECT comic_{slug}  style={a.style} pages={a.pages}")
    print(f"next: python3 comicify.py all {slug}")


def load(project):
    proj = os.path.join(ROOT, "longform", f"comic_{project}")
    cfg = json.load(open(os.path.join(proj, "project.json")))
    return proj, cfg


# ---------- stage: script ----------
SCRIPT_PROMPT = """You are a comic writer. Story premise: {prompt}
Write a {pages}-page comic. Reply ONLY with JSON:
{{"title": "...", "characters": [{{"id": "hero", "look": "FULL repeatable visual description - body, face, hair, outfit, colors"}}],
 "pages": [{{"panels": [{{"char": "hero", "scene": "what we SEE - setting, action, camera",
   "emotion": "...", "dialogue": "spoken words or empty", "sfx": "WHAM etc or empty"}}]}}]}}
Rules: 3-5 panels/page; dialogue under 16 words/panel; sfx SPARINGLY (1-2 per page max, only on real impacts); visual storytelling first; every panel
scene must restate where we are; characters must be reusable across panels via their look."""

def ollama_json(prompt, model="qwen3.6-companion"):
    body = {"model": model, "stream": False, "think": False,
            "messages": [{"role": "user", "content": prompt}], "format": "json"}
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    out = json.load(urllib.request.urlopen(req, timeout=600))["message"]["content"]
    return json.loads(out)

CAST = {
    "xellia_toon": ("xellia", "cute cartoon YOUNG ADULT woman, tall with adult proportions, very "
                    "voluminous wavy copper-orange hair with bright pink gradient tips, big warm "
                    "pink-brown eyes, soft round friendly face, caramel skin, wearing a cream knit "
                    "sweater, cheerful and expressive"),
}

def cmd_script(a):
    proj, cfg = load(a.project)
    prompt_txt = SCRIPT_PROMPT.format(prompt=cfg["prompt"], pages=cfg["pages"])
    cast = cfg.get("cast")
    if cast and cast in CAST:
        cid, look = CAST[cast]
        prompt_txt += ("\nMANDATORY LEAD CHARACTER: id \"" + cid + "\" with EXACTLY this look: \""
                       + look + "\". Include it in characters[] verbatim and star it in most panels.")
    data = ollama_json(prompt_txt)
    json.dump(data, open(os.path.join(proj, "script.json"), "w"), indent=1)
    n = sum(len(p["panels"]) for p in data["pages"])
    print(f"script: '{data['title']}' {len(data['pages'])} pages, {n} panels -> script.json")
    print("EDIT/APPROVE script.json, then run panels")


# ---------- ComfyUI client (self-contained; z-image turbo, no extra nodes) ----------
def _comfy_base():
    env = os.environ.get("COMFY_URL")
    cands = [env] if env else ["http://172.29.160.1:8000", "http://127.0.0.1:8000",
                               "http://127.0.0.1:8188", "http://127.0.0.1:8001"]
    for b in cands:
        try:
            urllib.request.urlopen(f"{b}/system_stats", timeout=5)
            return b
        except Exception:
            continue
    raise SystemExit("No ComfyUI server found (set COMFY_URL)")

def comfy_gen(prefix, pos, neg, w, h, batch, _identity_unused, seed):
    base = _comfy_base()
    oi = json.load(urllib.request.urlopen(f"{base}/object_info", timeout=30))
    ct = oi["CLIPLoader"]["input"]["required"]["type"][0]
    ct = "stable_diffusion" if "stable_diffusion" in ct else ct[0]
    wf = {
     "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
     "8": {"class_type": "CLIPLoader", "inputs": {"clip_name": "zImageTurbo_textEncoder.safetensors", "type": ct}},
     "9": {"class_type": "VAELoader", "inputs": {"vae_name": "zImageTurbo_vae.safetensors"}},
     "2": {"class_type": "CLIPTextEncode", "inputs": {"text": pos, "clip": ["8", 0]}},
     "3": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["8", 0]}},
     "4": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": batch}},
     "5": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 8, "cfg": 1.5,
           "sampler_name": "euler", "scheduler": "sgm_uniform", "denoise": 1.0, "model": ["1", 0],
           "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]}},
     "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["9", 0]}},
     "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["6", 0]}},
    }
    req = urllib.request.Request(f"{base}/prompt", data=json.dumps({"prompt": wf}).encode(),
                                 headers={"Content-Type": "application/json"})
    pid = json.load(urllib.request.urlopen(req, timeout=60))["prompt_id"]
    for _ in range(200):
        h_ = json.load(urllib.request.urlopen(f"{base}/history/{pid}", timeout=30))
        if pid in h_:
            return
        time.sleep(3)


# ---------- stage: panels (GPU) ----------
def cmd_panels(a):
    proj, cfg = load(a.project)
    data = json.load(open(os.path.join(proj, "script.json")))
    style = STYLES.get(cfg["style"], cfg["style"])
    chars = {c["id"]: c["look"] for c in data.get("characters", [])}
    seeds = {cid: 60000 + (hash(cid) % 9999) for cid in chars}
    k = 0
    for pi, page in enumerate(data["pages"], 1):
        for qi, panel in enumerate(page["panels"], 1):
            k += 1
            name = f"comic_{cfg['slug']}_p{pi:02d}_{qi}"
            if glob.glob(os.path.join(ROOT, "output", name + "_*.png")):
                print("skip", name); continue
            look = chars.get(panel.get("char", ""), "")
            pos = f"{style}, {look}, {panel['scene']}, {panel.get('emotion','')} expression"
            seed = seeds.get(panel.get("char"), 60000) + pi  # same char = same seed family
            comfy_gen(name, pos, NEG, 832, 832, 1, False, seed)
            print(f"panel {name}")
    print(f"panels done ({k}) -> run assemble")


# ---------- stage: assemble (CPU) ----------
def layout(n):
    """rows of panel counts for n panels on one page"""
    return {1: [1], 2: [1, 1], 3: [1, 2], 4: [2, 2], 5: [2, 3], 6: [3, 3],
            7: [2, 2, 3], 8: [3, 2, 3], 9: [3, 3, 3]}.get(n, [2, 2])

def bubble(draw, img, text, cx, anchor_y, width_max, font, fill="white"):
    """rounded speech bubble w/ wrapped text + tail; returns bottom y"""
    from PIL import ImageDraw
    lines = textwrap.wrap(text, width=max(10, int(width_max / (font.size * 0.52))))
    tw = max(draw.textlength(l, font=font) for l in lines)
    th = len(lines) * (font.size + 6)
    pad = 16
    x0, y0 = cx - tw / 2 - pad, anchor_y
    x1, y1 = cx + tw / 2 + pad, anchor_y + th + pad * 2
    draw.rounded_rectangle([x0, y0, x1, y1], radius=22, fill=fill, outline="black", width=4)
    draw.polygon([(cx - 14, y1 - 2), (cx + 22, y1 - 2), (cx + 2, y1 + 26)],
                 fill=fill, outline="black")
    y = y0 + pad
    for l in lines:
        draw.text((cx - draw.textlength(l, font=font) / 2, y), l, font=font, fill="black")
        y += font.size + 6
    return y1

def cmd_assemble(a):
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    proj, cfg = load(a.project)
    data = json.load(open(os.path.join(proj, "script.json")))
    PW, PH, M, G = 1600, 2263, 60, 28   # page, margin, gutter (A4-ish @150dpi)
    f_dlg = ImageFont.truetype(os.path.join(FONTS, "comicbd.ttf"), 34)
    f_sfx = ImageFont.truetype(os.path.join(FONTS, "impact.ttf"), 92)
    f_ttl = ImageFont.truetype(os.path.join(FONTS, "impact.ttf"), 72)
    pages_out = []
    for pi, page in enumerate(data["pages"], 1):
        canvas = Image.new("RGB", (PW, PH), "white")
        d = ImageDraw.Draw(canvas)
        top = M
        if pi == 1:  # title strip on page 1
            d.text((M, top), data.get("title", "").upper(), font=f_ttl, fill="black")
            top += 110
        rows = layout(len(page["panels"]))
        avail_h = PH - top - M
        row_h = int((avail_h - G * (len(rows) - 1)) / len(rows))
        idx = 0
        rng = random.Random(pi * 977)  # deterministic per page
        for r, ncols in enumerate(rows):
            y = top + r * (row_h + G)
            # column boundary CENTERLINES (gutter centers); internal ones get a diagonal skew
            nominal = [M + i * (PW - 2 * M) / ncols for i in range(ncols + 1)]
            nominal[0], nominal[-1] = M, PW - M
            b_top = list(nominal)
            b_bot = list(nominal)
            for i in range(1, ncols):
                sk = rng.randint(18, 44) * rng.choice((-1, 1))
                b_top[i] += sk
                b_bot[i] -= sk
            for c in range(ncols):
                panel = page["panels"][idx]; idx += 1
                g0 = 0 if c == 0 else G / 2
                g1 = 0 if c == ncols - 1 else G / 2
                quad = [(b_top[c] + g0, y), (b_top[c + 1] - g1, y),
                        (b_bot[c + 1] - g1, y + row_h), (b_bot[c] + g0, y + row_h)]
                bx0 = int(min(q[0] for q in quad)); bx1 = int(max(q[0] for q in quad))
                bw = bx1 - bx0
                pat = os.path.join(ROOT, "output", f"comic_{cfg['slug']}_p{pi:02d}_{idx}_*.png")
                hits = sorted(glob.glob(pat))
                if hits:
                    art = Image.open(hits[-1]).convert("RGB")
                    art = ImageOps.fit(art, (bw, row_h), Image.LANCZOS)
                else:  # placeholder (lets assemble be tested without GPU)
                    art = Image.new("RGB", (bw, row_h), (235, 228, 238))
                    ImageDraw.Draw(art).text((24, 24), panel["scene"][:80],
                                             font=f_dlg, fill=(120, 100, 120))
                mask = Image.new("L", (PW, PH), 0)
                ImageDraw.Draw(mask).polygon(quad, fill=255)
                canvas.paste(art, (bx0, y), mask.crop((bx0, y, bx0 + bw, y + row_h)))
                d.polygon(quad, outline="black", width=6)
                cx = (quad[0][0] + quad[1][0]) / 2
                cw = quad[1][0] - quad[0][0]
                if panel.get("dialogue"):
                    # tall+narrow bubble docked in a top SIDE corner (faces live top-center):
                    # alternate sides by panel for reading rhythm; ~42% width wraps text tall.
                    narrow = cw * 0.42
                    if idx % 2 == 1:  # odd panel -> left corner
                        bcx = quad[0][0] + narrow / 2 + 26
                    else:              # even panel -> right corner
                        bcx = quad[1][0] - narrow / 2 - 26
                    bubble(d, canvas, panel["dialogue"], bcx, y + 16, narrow, f_dlg)
                if panel.get("sfx"):
                    sx, sy = quad[3][0] + cw * 0.06, y + row_h * 0.80
                    for off in ((4, 4), (-4, 4), (4, -4), (-4, -4)):
                        d.text((sx + off[0], sy + off[1]), panel["sfx"], font=f_sfx, fill="black")
                    d.text((sx, sy), panel["sfx"], font=f_sfx, fill=(255, 210, 40))
        out = os.path.join(proj, f"page_{pi:02d}.png")
        canvas.save(out); pages_out.append(out)
        print("PAGE", out)
    import zipfile
    cbz = os.path.join(proj, f"{cfg['slug']}.cbz")
    with zipfile.ZipFile(cbz, "w") as z:
        for p in pages_out:
            z.write(p, os.path.basename(p))
    print("CBZ", cbz)


def cmd_all(a):
    proj, _ = load(a.project)
    if not os.path.exists(os.path.join(proj, "script.json")):
        cmd_script(a)
    cmd_panels(a); cmd_assemble(a)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(required=True)
    p = sub.add_parser("new"); p.add_argument("prompt"); p.add_argument("--style", default="sunday-strip")
    p.add_argument("--pages", type=int, default=3); p.add_argument("--cast", default="")
    p.set_defaults(fn=cmd_new)
    for name, fn in (("script", cmd_script), ("panels", cmd_panels),
                     ("assemble", cmd_assemble), ("all", cmd_all)):
        p = sub.add_parser(name); p.add_argument("project"); p.set_defaults(fn=fn)
    a = ap.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
