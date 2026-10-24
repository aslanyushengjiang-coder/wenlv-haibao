# 腾讯云 TokenHub 调 Seedream 5.0 Pro 出图，只用 Python 自带的库。
#
# 用法：
#   python seedream_tokenhub.py --input 请求.json --output 出图文件夹 --dry-run   # 只检查，不联网、不花钱
#   python seedream_tokenhub.py --input 请求.json --output 出图文件夹             # 正式出图，一次请求扣一次钱
#
# 请求.json 有两种写法：
# 1. 模板模式（推荐，稳定）：
#   {"template": "蓝色撕纸拼贴", "fields": {"标题": "...", "日期": "...", ...},
#    "people": "男孩女孩", "photos": ["现场照片.jpg"], "reference_poster": "参考海报.jpg"}
#   提示词用 templates/提示词/<模板>.txt，fields 要填齐它的「必填」；样张自动当图一。
#   people：男孩女孩（默认）/ 男孩 / 女孩 / 不要人；给了 photos 就用现场照片里的人。
#   reference_poster：老板自己的参考海报，只能配 "照参考海报" 模板，代替样张当图一。
# 2. 自由模式：{"prompt": "...", "ref_paths": ["实拍照片.jpg"]}
# 两种都可加 size：默认竖版 1728x2304，可写 1K / 1.5K / 2K 或 宽x高。
# 图片路径都相对请求.json，参考图最多 10 张。
#
# 钥匙：环境变量 TOKENHUB_API_KEY，或文件 ~/.config/shengjiang/wenlv-haibao/tokenhub.env
# 里的一行 TOKENHUB_API_KEY=...（Windows 是 %USERPROFILE%\.config\shengjiang\wenlv-haibao\tokenhub.env）。
# 钥匙不打印、不写进任何输出文件。
# 接口说明：https://cloud.tencent.com/document/product/1823/136609 （2026-10-23 读）
import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://tokenhub.tencentmaas.com/v1/wand/si-image/generation"
MODEL = "seedream-image-v5.0-pro"
DEFAULT_SIZE = "1728x2304"
TIERS = {"1K", "1.5K", "2K"}
MAX_REFS = 10
MAX_BODY_BYTES = 20 * 1024 * 1024
DEFAULT_ENV = Path.home() / ".config" / "shengjiang" / "wenlv-haibao" / "tokenhub.env"
SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = SKILL_ROOT / "templates" / "提示词"
SAMPLE_DIR = SKILL_ROOT / "assets" / "样张"
FACE_DIR = SKILL_ROOT / "assets" / "人脸参考"
FACES = {"男孩女孩": [("男孩", "男孩.jpg"), ("女孩", "女孩.jpg")], "男孩": [("男孩", "男孩.jpg")], "女孩": [("女孩", "女孩.jpg")]}
CN_NUM = "零一二三四五六七八九十"


def fail(message):
    print(f"出错：{message}", file=sys.stderr)
    sys.exit(2)


def read_key(env_file):
    key = os.environ.get("TOKENHUB_API_KEY", "").strip()
    if key:
        return key
    if not env_file.is_file():
        fail(f"没有找到钥匙文件 {env_file}")
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "TOKENHUB_API_KEY" and value.strip():
            return value.strip().strip('"').strip("'")
    fail(f"钥匙文件 {env_file} 里没有 TOKENHUB_API_KEY=")


def check_size(size):
    if size in TIERS:
        return
    width, sep, height = size.lower().partition("x")
    if not (sep and width.isdigit() and height.isdigit()):
        fail(f"size 写错了：{size}，只能写 1K、1.5K、2K 或 宽x高")


def to_data_uri(path):
    if not path.is_file():
        fail(f"参考图不存在：{path}")
    mime = mimetypes.guess_type(path.name)[0]
    if not mime or not mime.startswith("image/"):
        fail(f"不是图片：{path.name}")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime.lower()};base64,{data}"


def read_template(name):
    path = TEMPLATE_DIR / f"{name}.txt"
    if not path.is_file():
        names = "、".join(sorted(p.stem for p in TEMPLATE_DIR.glob("*.txt")))
        fail(f"没有这个模板：{name}。可选：{names}")
    lines = path.read_text(encoding="utf-8").splitlines()
    required = []
    for line in lines:
        if line.startswith("# 必填："):
            required = [x.strip(" {}") for x in re.split(r"[、，,]", line[len("# 必填："):]) if x.strip(" {}")]
    text = " ".join(line for line in lines if line.strip() and not line.startswith("#"))
    return text, required


def people_refs(people, photos, start):
    # 返回 (参考图路径列表, 开头说明, {人物} 填什么)，start 是第一张人物图的序号
    if photos:
        nums = [CN_NUM[start + i] for i in range(len(photos))]
        which = "、".join(f"图{n}" for n in nums)
        intro = (f"{which}是这场活动的实拍照片，画面里的孩子就是照片里的孩子：脸型、五官、发型和肤色都和照片一致，不换人；"
                 "衣服、动作和场景按下面重新拍，不照搬照片里的背景。")
        return list(photos), intro, f"{which}照片里的孩子"
    if people == "不要人":
        return [], "", "两个背对镜头的中国小朋友"
    if people not in FACES:
        fail(f"people 只能写：{'、'.join(list(FACES) + ['不要人'])}")
    refs, parts, names = [], [], []
    for i, (label, file) in enumerate(FACES[people]):
        n = CN_NUM[start + i]
        refs.append(FACE_DIR / file)
        parts.append(f"图{n}是一个{label}的真实照片")
        names.append(f"图{n}的{label}")
    intro = ("，".join(parts) + "，只用来确定孩子长什么样：画面里" + "、".join(names) +
             "的脸型、五官、发型和肤色都和照片一致，不换人；衣服、动作和场景按下面重新拍，不照搬照片里的衣服和背景。")
    return refs, intro, "和".join(names)


def build_template_prompt(request, base):
    name = request["template"]
    text, required = read_template(name)
    fields = {k: str(v).strip() for k, v in request.get("fields", {}).items()}
    missing = [k for k in required if not fields.get(k)]
    if missing:
        fail(f"模板「{name}」还缺这几项：{'、'.join(missing)}。资料里没有就问用户，不要编")
    unknown = set(fields) - set(required)
    if unknown:
        fail(f"模板「{name}」用不到这几项：{'、'.join(sorted(unknown))}")
    poster = request.get("reference_poster")
    if (name == "照参考海报") != bool(poster):
        fail("「照参考海报」模板必须给 reference_poster，其他模板不要给 reference_poster")
    style = base / poster if poster else SAMPLE_DIR / f"{name}.jpg"
    refs, intro = [], []
    if style.is_file():
        refs.append(style)
        intro.append("图一是一张海报样张，只学它的版式、配色、字体样子、装饰和画面质感；图一里原来的字、人物、风景、地标一律不要出现，画面内容按下面这场活动重新画。")
    photos = [base / p for p in request.get("photos", [])]
    p_refs, p_intro, who = people_refs(request.get("people", "男孩女孩"), photos, len(refs) + 1)
    refs += p_refs
    if p_intro:
        intro.append(p_intro)
    filled = re.sub(r"\{([^{}]+)\}", lambda m: who if m.group(1) == "人物" else fields[m.group(1)], text)
    return "".join(intro) + filled, refs


def build_body(request_path):
    request = json.loads(request_path.read_text(encoding="utf-8-sig"))
    unknown = set(request) - {"prompt", "size", "ref_paths", "template", "fields", "people", "photos", "reference_poster"}
    if unknown:
        fail(f"请求.json 里有不认识的字段：{sorted(unknown)}")
    if "template" in request:
        if "prompt" in request or "ref_paths" in request:
            fail("模板模式不要再写 prompt 和 ref_paths")
        prompt, ref_files = build_template_prompt(request, request_path.parent)
    else:
        prompt = str(request.get("prompt", "")).strip()
        ref_files = [request_path.parent / ref for ref in request.get("ref_paths", [])]
    if not prompt:
        fail("请求.json 里没有 prompt，也没有 template")
    size = request.get("size", DEFAULT_SIZE)
    check_size(size)
    refs = ref_files
    if len(refs) > MAX_REFS:
        fail(f"参考图最多 {MAX_REFS} 张")
    body = {
        "model": MODEL,
        "prompt": prompt,
        "size": size,
        "response_format": "b64_json",
        "output_format": "png",
        "watermark": False,
    }
    if refs:
        body["images"] = [to_data_uri(ref) for ref in refs]
    return body


def post(body, key):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_BODY_BYTES:
        fail("参考图太大，请求超过 20MB，换小一点的照片")
    req = urllib.request.Request(
        ENDPOINT,
        data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:500]
        fail(f"接口返回 {err.code}：{detail}")
    except urllib.error.URLError as err:
        fail(f"连不上腾讯云：{err.reason}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    body = build_body(args.input.resolve())
    if (args.output / "image-001.png").exists():
        fail(f"{args.output} 里已经有图了，换一个新文件夹，避免重复扣钱")
    summary = {"model": MODEL, "size": body["size"], "refs": len(body.get("images", [])), "prompt_chars": len(body["prompt"])}

    if args.dry_run:
        read_key(args.env_file)
        print(json.dumps({"dry_run": True, **summary, "key": "已找到"}, ensure_ascii=False))
        return

    key = read_key(args.env_file)
    started = time.time()
    result = post(body, key)
    seconds = round(time.time() - started, 1)
    images = result.get("data") or []
    if not images or not images[0].get("b64_json"):
        fail(f"接口没有返回图片：{json.dumps(result, ensure_ascii=False)[:500]}")

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "提示词.txt").write_text(body["prompt"], encoding="utf-8")
    image_path = args.output / "image-001.png"
    image_path.write_bytes(base64.b64decode(images[0]["b64_json"]))
    record = {
        **summary,
        "seconds": seconds,
        "tokens": (result.get("tokenhub_usage") or {}).get("total_tokens"),
        "request_id": result.get("request_id"),
        "image": image_path.name,
    }
    (args.output / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, **record, "path": str(image_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
