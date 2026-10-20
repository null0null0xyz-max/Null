#!/usr/bin/env python3
"""Генерация картинок и видео для бота (быстрый режим и Claude Code).

CLI (для Claude Code, команда tgmedia):
  tgmedia image "промпт" [--model M] [--provider puter|hf|horde] [--nsfw] [--size 1024x1024] [-o файл.png]
  tgmedia video "промпт" [--image путь_или_url] [--provider hf|puter] [--model M] [--seconds 3] [-o файл.mp4]
  tgmedia models
Печатает путь к готовому файлу. Чтобы показать пользователю — вставь в ответ ![подпись](file:путь).

Источники (бесплатные):
  puter  — PUTER_TOKEN: GPT Image 2.5, Nano Banana (gemini-*), видео Wan/Seedance/Veo. $0.50 в месяц бесплатно.
  hf     — HF_TOKEN: Hugging Face ZeroGPU (FLUX.1-schnell; видео LTX text→video, Wan 2.2 image→video). ~5 мин GPU/день.
  horde  — AI Horde: бесплатно, без цензуры (nsfw), но очередь — может идти несколько минут.
"""
import os, re, sys, time, json, base64, argparse, subprocess

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.environ.get("WORKSPACE") or os.path.join(HERE, "workspace"), "media")
PUTER = "https://api.puter.com/drivers/call"
HORDE = "https://aihorde.net/api/v2"
HF_IMAGE = ("black-forest-labs/FLUX.1-schnell", "/infer")
HF_T2V = ("Lightricks/ltx-video-distilled", "/text_to_video")
HF_I2V = ("zerogpu-aoti/wan2-2-fp8da-aoti-faster", "/generate_video")
HF_I2V_NSFW = ("hf-Asdqqq/Uncensored-video2", "/generate_video")        # Wan 2.2 без фильтра, фото→видео
HF_IMG_NSFW = ("IbarakiDouji/WAI-NSFW-illustrious-SDXL", "/generate")   # WAI-NSFW на ZeroGPU
HORDE_MODELS = {"sfw": ["Flux.1-Schnell fp8 (Compact)", "Juggernaut XL", "AlbedoBase XL (SDXL)"],
                "nsfw": ["WAI-NSFW-illustrious-SDXL", "ICBINP - I Can't Believe It's Not Photography", "CyberRealistic Pony"]}
PROGRESS = None  # бот ставит сюда функцию(текст) — показывать очередь Horde


def _progress(t):
    try:
        PROGRESS and PROGRESS(t)
    except Exception:
        pass


def _is_black(path):
    """Horde-воркеры иногда отдают пустой чёрный кадр — проверяем и переделываем."""
    try:
        from PIL import Image
        im = Image.open(path).convert("L")
        lo, hi = im.getextrema()
        return hi < 12
    except Exception:
        return False


def _to_jpg(path):
    """webp → jpg: Telegram показывает webp как стикер/чёрный фон."""
    try:
        from PIL import Image
        if path.endswith(".webp"):
            j = path[:-5] + ".jpg"
            Image.open(path).convert("RGB").save(j, quality=93)
            os.remove(path)
            return j
    except Exception:
        pass
    return path

MODELS_HELP = """Картинки:
  puter: gpt-image-2.5-flare, gpt-image-2.5-sunburst, gpt-image-2, gemini-nano-banana-2.1 (Nano Banana 2.1),
         gemini-3-pro-image (Nano Banana Pro), gemini-3.1-flash-image (Nano Banana 2)
  hf: flux-schnell
  horde (без цензуры, медленно): WAI-NSFW-illustrious-SDXL, CyberRealistic Pony, Juggernaut XL, Flux.1-Schnell fp8 (Compact)
Видео:
  hf: ltx (текст→видео, ~2–5 с), wan (картинка→видео, Wan 2.2)
  puter: видео бесплатных $0.50/мес не хватает (нужен платный баланс): wan2.7-t2v, dreamina-seedance-2-5-260628"""


def _out(ext, out=None):
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        return out
    os.makedirs(OUT_DIR, exist_ok=True)
    return os.path.join(OUT_DIR, time.strftime("%m%d-%H%M%S") + f"-{os.getpid() % 1000}.{ext}")


def _save_any(data, ctype, ext, out):
    """data: bytes, url, data-URI или dict с url/path."""
    if isinstance(data, str) and data.startswith("data:") and not out:
        m = re.match(r"data:\w+/(\w+)", data)
        if m:
            ext = {"jpeg": "jpg"}.get(m.group(1), m.group(1))
    path = _out(ext, out)
    if isinstance(data, dict):
        data = data.get("url") or data.get("path") or data.get("video") or data.get("image") or ""
        if isinstance(data, dict):
            data = data.get("url") or data.get("path") or ""
    if isinstance(data, bytes):
        open(path, "wb").write(data)
    elif isinstance(data, str) and data.startswith("data:"):
        open(path, "wb").write(base64.b64decode(data.split(",", 1)[1]))
    elif isinstance(data, str) and data.startswith("http"):
        r = requests.get(data, timeout=300)
        r.raise_for_status()
        open(path, "wb").write(r.content)
    elif isinstance(data, str) and os.path.exists(data):
        import shutil
        shutil.copy(data, path)
    else:
        raise RuntimeError(f"непонятный ответ генератора: {str(data)[:200]}")
    return path


# ---------------- Puter ----------------
def _puter(iface, driver, args, ext, out, timeout=660):
    tok = os.environ.get("PUTER_TOKEN") or os.environ.get("PUTER_AUTH_TOKEN")
    if not tok:
        raise RuntimeError("нет PUTER_TOKEN")
    body = {"interface": iface, "driver": driver, "test_mode": False, "method": "generate",
            "args": args, "auth_token": tok}
    r = requests.post(PUTER, data=json.dumps(body), timeout=timeout,
                      headers={"Content-Type": "text/plain;actually=json", "Authorization": "Bearer " + tok})
    ct = r.headers.get("Content-Type", "")
    if "json" in ct or r.content[:1] in (b"{", b"["):
        j = r.json()
        if r.status_code != 200 or (isinstance(j, dict) and j.get("success") is False):
            raise RuntimeError(f"puter {r.status_code}: {json.dumps(j, ensure_ascii=False)[:300]}")
        return _save_any(j.get("result", j) if isinstance(j, dict) else j, ct, ext, out)
    if r.status_code != 200:
        raise RuntimeError(f"puter {r.status_code}: {r.text[:300]}")
    return _save_any(r.content, ct, ext, out)


# ---------------- Hugging Face ZeroGPU ----------------
def _gradio():
    try:
        from gradio_client import Client, handle_file
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "gradio_client"], check=False)
        from gradio_client import Client, handle_file
    return Client, handle_file


def _hf(space, api, ext, out, **kw):
    Client, handle_file = _gradio()
    tok = os.environ.get("HF_TOKEN")
    try:
        c = Client(space, hf_token=tok, verbose=False) if tok else Client(space, verbose=False)
    except TypeError:
        c = Client(space, token=tok, verbose=False)
    for k, v in list(kw.items()):
        if isinstance(v, str) and k.endswith("image") and v:
            kw[k] = handle_file(v)
    res = c.predict(api_name=api, **kw)
    first = res[0] if isinstance(res, (list, tuple)) else res
    return _save_any(first, "", ext, out)


# ---------------- AI Horde ----------------
def _horde(prompt, nsfw, w, h, model, out, wait=900):
    hdr = {"apikey": os.environ.get("HORDE_KEY", "0000000000"), "Client-Agent": "tg-claude-bot:1.0:anon"}
    models = [model] if model else HORDE_MODELS["nsfw" if nsfw else "sfw"]
    flux = any("Flux" in m for m in models)
    sd15 = any(k in m for m in models for k in ("ICBINP", "Deliberate", "Realistic Vision", "URPM", "Anything", "Hentai"))
    side = 512 if sd15 else 832  # меньше размер — меньше kudos — быстрее очередь для анонима
    ww, hh = (side, side) if w == h else ((side * 3 // 4) // 64 * 64, side) if h > w else (side, (side * 3 // 4) // 64 * 64)
    params = {"width": ww, "height": hh, "n": 1}
    if flux:
        params.update(steps=4, cfg_scale=1, sampler_name="k_euler")
    else:  # k_euler_a + clip_skip 2: с k_euler/dpmpp WAI-NSFW отдаёт чёрный кадр
        params.update(steps=24, cfg_scale=6, sampler_name="k_euler_a", clip_skip=2)
    bad_workers = []
    for attempt in range(3):
        body = {"prompt": prompt, "nsfw": bool(nsfw), "censor_nsfw": False, "r2": True, "models": models,
                "params": dict(params, seed=str(int(time.time()) + attempt))}
        if bad_workers:
            body.update(workers=bad_workers, worker_blacklist=True)
        path, wid = _horde_job(body, hdr, out, wait)
        if not _is_black(path):
            return _to_jpg(path)
        os.remove(path)
        bad_workers.append(wid)
        _progress("воркер отдал чёрный кадр, переделываю…")
    raise RuntimeError("horde: воркеры отдают пустые кадры, попробуй другую модель")


def _horde_job(body, hdr, out, wait):
    r = None
    for _ in range(5):
        r = requests.post(f"{HORDE}/generate/async", json=body, headers=hdr, timeout=60)
        if r.status_code != 429:
            break
        time.sleep(2)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"horde {r.status_code}: {r.text[:300]}")
    jid, t0 = r.json()["id"], time.time()
    while time.time() - t0 < wait:
        c = requests.get(f"{HORDE}/generate/check/{jid}", headers=hdr, timeout=30).json()
        if c.get("faulted") or c.get("is_possible") is False:
            raise RuntimeError("horde: задача не может быть выполнена")
        if c.get("done"):
            g = requests.get(f"{HORDE}/generate/status/{jid}", headers=hdr, timeout=60).json()["generations"][0]
            return _save_any(g["img"], "", "webp", out), g.get("worker_id")
        if c.get("processing"):
            _progress("рисует воркер…")
        else:
            _progress(f"очередь Horde: место {c.get('queue_position', '?')}, ~{fmt_wait(c.get('wait_time'))}")
        time.sleep(6)
    requests.delete(f"{HORDE}/generate/status/{jid}", headers=hdr, timeout=30)
    raise RuntimeError("horde: очередь слишком длинная, не дождался")


def fmt_wait(s):
    s = int(s or 0)
    return f"{s // 60} мин {s % 60} с" if s >= 60 else f"{s} с"


# ---------------- общий интерфейс ----------------
def default(kind):
    """Модель по умолчанию из настроек бота: MEDIA_IMAGE / MEDIA_VIDEO = 'провайдер|модель'."""
    v = os.environ.get("MEDIA_" + kind.upper(), "")
    p, _, m = v.partition("|")
    return (p or "auto"), (m or None)


def image(prompt, provider="auto", model=None, nsfw=False, size="1024x1024", out=None):
    w, h = (int(x) for x in re.findall(r"\d+", size or "1024x1024")[:2])
    if provider == "auto" and not model:
        provider, model = default("image")
    model = (model or "").strip()
    if provider == "auto":
        if nsfw:
            order = ["horde"]
        elif model.startswith(("gpt-image", "gemini", "nano")):
            order = ["puter", "hf", "horde"]
        else:
            order = ["puter", "hf", "horde"] if os.environ.get("PUTER_TOKEN") else ["hf", "horde"]
    else:
        order = [provider]
    errs = []
    for p in order:
        try:
            if p == "puter":
                m = model if model and not model.startswith("flux") else "gpt-image-2.5-flare"
                if m in ("nano-banana", "nano", "nanobanana"):
                    m = "gemini-nano-banana-2.1"
                args = {"prompt": prompt, "model": m, "ratio": {"w": w, "h": h}}
                if m.startswith("gpt-image"):
                    args["quality"] = os.environ.get("PUTER_IMG_QUALITY", "low")  # бережём бесплатные $0.50/мес
                return _puter("puter-image-generation", "ai-image", args, "png", out, 300)
            if p == "hf" and "nsfw" in model.lower():
                return _to_jpg(_hf(HF_IMG_NSFW[0], HF_IMG_NSFW[1], "webp", out, prompt=prompt,
                                   negative_prompt="bad quality, worst quality, lowres, bad anatomy", seed=0,
                                   custom_width=1024, custom_height=1024, guidance_scale=6.0, num_inference_steps=24,
                                   sampler="Euler a", model_name="v17", aspect_ratio_selector="Custom",
                                   use_upscaler=False, upscaler_strength=0.55, upscale_by=1.5, add_quality_tags=True))
            if p == "hf":
                return _hf(HF_IMAGE[0], HF_IMAGE[1], "webp", out, prompt=prompt, seed=0, randomize_seed=True,
                           width=min(w, 1024), height=min(h, 1024), num_inference_steps=4)
            if p == "horde":
                return _horde(prompt, nsfw or "nsfw" in model.lower() or "pony" in model.lower(), w, h,
                              model if p == provider and model else None, out)
        except Exception as e:
            errs.append(f"{p}: {str(e)[:300]}")
    raise RuntimeError("не получилось: " + " | ".join(errs))


def video(prompt, image_path=None, provider="auto", model=None, seconds=3, out=None):
    if provider == "auto" and not model:
        provider, model = default("video")
    order = [provider] if provider != "auto" else ["hf", "puter"]
    errs = []
    for p in order:
        try:
            if p == "hf" and model == "nsfw":  # без цензуры: нет фото — сначала рисуем кадр в Horde, потом оживляем
                if not image_path:
                    _progress("рисую первый кадр (Horde, без цензуры)…")
                    image_path = image(prompt, "horde", "ICBINP - I Can't Believe It's Not Photography", nsfw=True,
                                       size="1024x768")
                _progress("оживляю кадр (Wan 2.2 без фильтра, HF)…")
                return _hf(HF_I2V_NSFW[0], HF_I2V_NSFW[1], "mp4", out, input_image=image_path, prompt=prompt, steps=6,
                           duration_seconds=min(float(seconds or 3), 5), randomize_seed=True)
            if p == "hf":
                if image_path and model != "ltx":
                    return _hf(HF_I2V[0], HF_I2V[1], "mp4", out, input_image=image_path, prompt=prompt, steps=6,
                               duration_seconds=min(float(seconds or 3), 5), randomize_seed=True)
                return _hf(HF_T2V[0], HF_T2V[1], "mp4", out, prompt=prompt,
                           negative_prompt="worst quality, inconsistent motion, blurry, jittery, distorted",
                           input_image_filepath=None, input_video_filepath=None, height_ui=512, width_ui=704,
                           mode="text-to-video", duration_ui=min(float(seconds or 3), 5), ui_frames_to_use=9,
                           seed_ui=42, randomize_seed=True, ui_guidance_scale=1, improve_texture_flag=True)
            if p == "puter":
                args = {"prompt": prompt, "model": model or "wan2.7-t2v", "seconds": seconds}
                if image_path:
                    if os.path.exists(image_path):
                        args["input_reference"] = "data:image/png;base64," + base64.b64encode(open(image_path, "rb").read()).decode()
                    else:
                        args["input_reference"] = image_path
                return _puter("puter-video-generation", "ai-video", args, "mp4", out)
        except Exception as e:
            errs.append(f"{p}: {str(e)[:300]}")
    raise RuntimeError("не получилось: " + " | ".join(errs))


def main(argv):
    ap = argparse.ArgumentParser(prog="tgmedia", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["image", "video", "models"])
    ap.add_argument("prompt", nargs="?", default="")
    ap.add_argument("--model"), ap.add_argument("--provider", default="auto",
                                                 help="puter | hf | horde; без --model берётся модель из настроек бота")
    ap.add_argument("--nsfw", action="store_true"), ap.add_argument("--size", default="1024x1024")
    ap.add_argument("--image"), ap.add_argument("--seconds", type=float, default=3)
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    if a.kind == "models":
        print(MODELS_HELP)
        return 0
    try:
        if a.kind == "image":
            print(image(a.prompt, a.provider, a.model, a.nsfw, a.size, a.out))
        else:
            print(video(a.prompt, a.image, a.provider, a.model, a.seconds, a.out))
        return 0
    except Exception as e:
        print("ошибка:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
