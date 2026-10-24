# 腾讯云 TokenHub 调 Seedream 5.0 Pro 出图，只用 Python 自带的库。
#
# 用法：
#   python seedream_tokenhub.py --input 请求.json --output 出图文件夹 --dry-run   # 只检查，不联网、不花钱
#   python seedream_tokenhub.py --input 请求.json --output 出图文件夹             # 正式出图，一次请求扣一次钱
#
# 请求.json：
#   {"prompt": "...", "size": "1728x2304", "ref_paths": ["实拍照片.jpg"]}
#   prompt 必填；size 默认竖版 1728x2304，可写 1K / 1.5K / 2K 或 宽x高；
#   ref_paths 是相对请求.json 的本地图片，最多 10 张。
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


def build_body(request_path):
    request = json.loads(request_path.read_text(encoding="utf-8-sig"))
    unknown = set(request) - {"prompt", "size", "ref_paths"}
    if unknown:
        fail(f"请求.json 里有不认识的字段：{sorted(unknown)}")
    prompt = str(request.get("prompt", "")).strip()
    if not prompt:
        fail("请求.json 里没有 prompt")
    size = request.get("size", DEFAULT_SIZE)
    check_size(size)
    refs = request.get("ref_paths", [])
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
        body["images"] = [to_data_uri(request_path.parent / ref) for ref in refs]
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
