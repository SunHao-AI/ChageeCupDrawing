"""M-C 提示词人工验收跑批：真实 provider 出图 + 客观指标 + 对照报告。

BYOK 改造后 Key 不再落盘，Key 只从环境变量读取（默认 CUPDRAW_AI_KEY，可用
--key-env 指定其它变量名），避免写进命令行历史。每次运行 9 次真实调用
（3 类 × 3 例），产物与 report.html 落在 data/mc_acceptance/run-<时间戳>/。

用法示例：
    $env:CUPDRAW_AI_KEY="sk-..."; python .benchmarks/mc_acceptance.py --provider dashscope
    python .benchmarks/mc_acceptance.py --provider openai-compatible --base-url http://127.0.0.1:11434/v1
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.services.editing import EditService, load_ink  # noqa: E402
from app.services.roi_pipeline import build_prompt  # noqa: E402
from app.services.settings_store import AiSettingsStore  # noqa: E402

WIDE = ROOT / "data" / "uploads" / "24a9d49e31a645d29ab326480b7fce7a" / "source_v2.png"
WIDE_ROI = {"x": 1520, "y": 0, "w": 400, "h": 500}

CASES = [
    {
        "id": "A1",
        "category": "加文字/艺术字",
        "source": ROOT / "data" / "examples" / "example2.png",
        "instruction": "在画面上方的空白处加一行中文「清晨好」，黑色手写体",
        "roi": None,
        "note": "中文文字渲染 + 留白处插入",
    },
    {
        "id": "A2",
        "category": "加文字/艺术字",
        "source": ROOT / "data" / "examples" / "example3.png",
        "instruction": "在人物右侧的空白处加英文单词 ENJOY，黑色粗体",
        "roi": None,
        "note": "英文文字渲染 + 整图指令",
    },
    {
        "id": "A3",
        "category": "加文字/艺术字",
        "source": WIDE,
        "instruction": "在方框中央加一个字「茶」，黑色实心",
        "roi": WIDE_ROI,
        "note": "宽画布 ROI：加字只回填选区内",
    },
    {
        "id": "B1",
        "category": "微调图像",
        "source": ROOT / "data" / "examples" / "example2.png",
        "instruction": "把角色和文字的线条统一加粗一些",
        "roi": None,
        "note": "整体线条加粗，二值化后不应糊死",
    },
    {
        "id": "B2",
        "category": "微调图像",
        "source": ROOT / "data" / "examples" / "example1.png",
        "instruction": "让头发和衣服的线条细节更丰富，保持黑白线稿风格",
        "roi": None,
        "note": "局部细节加密 + 风格锁定",
    },
    {
        "id": "B3",
        "category": "微调图像",
        "source": WIDE,
        "instruction": "把方框的线条加粗，其余保持不变",
        "roi": WIDE_ROI,
        "note": "宽画布 ROI：微调后接缝无新线",
    },
    {
        "id": "C1",
        "category": "删除元素",
        "source": ROOT / "data" / "examples" / "example2.png",
        "instruction": "删除画面右上角的云朵和上面的小动物，保持其他内容不变",
        "roi": None,
        "note": "大面积元素删除后的空白连续性",
    },
    {
        "id": "C2",
        "category": "删除元素",
        "source": ROOT / "data" / "examples" / "example1.png",
        "instruction": "删除人物手里的麦克风",
        "roi": None,
        "note": "局部元素删除 + 周边线条连续",
    },
    {
        "id": "C3",
        "category": "删除元素",
        "source": WIDE,
        "instruction": "去掉方框内的对角斜线，保持外框完整",
        "roi": WIDE_ROI,
        "note": "宽画布 ROI：删除元素不影响框外留白",
    },
]


def _metrics(base_path: Path, draft_path: Path, roi: dict | None) -> dict[str, object]:
    base = load_ink(base_path)
    draft = load_ink(draft_path)
    changed = base != draft
    ys, xs = np.where(changed)
    bbox = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1] if len(xs) else None
    outside = 0.0
    if roi is not None:
        height, width = changed.shape
        x0 = max(0, int(roi["x"]))
        y0 = max(0, int(roi["y"]))
        x1 = min(width, x0 + int(roi["w"]))
        y1 = min(height, y0 + int(roi["h"]))
        # 羽化过渡带最多 16px，选区外 20px 内的变化视为接缝副作用。
        band = 20
        mask = np.ones_like(changed, dtype=bool)
        mask[max(0, y0 - band) : min(height, y1 + band), max(0, x0 - band) : min(width, x1 + band)] = False
        outside = float(changed[mask].mean())
    return {
        "inkBefore": round(float((base > 0).mean()), 4),
        "inkAfter": round(float((draft > 0).mean()), 4),
        "changedRatio": round(float(changed.mean()), 4),
        "changedBbox": bbox,
        "outsideRoiChangeRatio": round(outside, 6),
    }


def _report(run_dir: Path, rows: list[dict[str, object]]) -> Path:
    cards: list[str] = []
    for row in rows:
        case = row["case"]
        metrics = row["metrics"]
        images = []
        pairs = [("原图", "source.png"), ("草稿", "draft.png"), ("差异", "diff.png")]
        for label, name in pairs:
            path = run_dir / str(case["id"]) / name
            if path.exists():
                images.append(
                    f'<figure><figcaption>{label}</figcaption>'
                    f'<img src="{case["id"]}/{name}" alt="{label}"></figure>'
                )
        if row.get("error"):
            meta_line = f'<p class="meta err">调用失败：{html.escape(str(row["error"]))}</p>'
        else:
            meta_line = (
                f'<p class="meta">墨量 {metrics["inkBefore"]:.2%} → {metrics["inkAfter"]:.2%} · '
                f'改动像素 {metrics["changedRatio"]:.2%} · '
                f'改动范围 {html.escape(str(metrics["changedBbox"]))} · '
                f'选区外变化 {metrics["outsideRoiChangeRatio"]:.4%} · '
                f'耗时 {row["seconds"]:.1f}s</p>'
            )
        cards.append(
            f"""
            <section class="card">
              <header><b>{case['id']}</b><span>{html.escape(str(case['category']))}</span>
                <span class="note">{html.escape(str(case['note']))}</span></header>
              <p class="ins">指令：{html.escape(str(case['instruction']))}</p>
              {meta_line}
              <div class="imgs">{''.join(images)}</div>
              <table class="judge">
                <tr><th>指令执行</th><th>风格一致（白底黑线、无彩色/底纹）</th><th>未改区域保持</th><th>接缝无新线</th></tr>
                <tr><td>□ 通过 □ 部分 □ 不通过</td><td>□ 通过 □ 部分 □ 不通过</td><td>□ 通过 □ 部分 □ 不通过</td><td>□ 通过 □ 部分 □ 不通过</td></tr>
              </table>
            </section>"""
        )
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>M-C 提示词人工验收</title>
<style>
  body{{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#0f1115;color:#e6e6e6;margin:0;padding:28px}}
  h1{{font-size:20px;margin:0 0 6px}} .sub{{color:#9aa0a6;font-size:13px;margin-bottom:20px}}
  .card{{background:#161a20;border:1px solid #262b33;border-radius:12px;padding:16px;margin-bottom:18px}}
  header{{display:flex;gap:10px;align-items:center;font-size:14px}} header span{{color:#9aa0a6;font-size:12px}}
  .note{{margin-left:auto;color:#7d848d}} .ins{{margin:10px 0 4px;font-size:14px}}
  .meta{{color:#8b929b;font-size:12px;margin:0 0 10px}}
  .imgs{{display:flex;gap:12px;flex-wrap:wrap}} figure{{margin:0;flex:1 1 260px}}
  figcaption{{font-size:12px;color:#9aa0a6;margin-bottom:4px}}
  img{{width:100%;background:#fff;border-radius:6px;display:block}}
  .err{{color:#ff8a80}} .judge{{margin-top:10px;border-collapse:collapse;width:100%;font-size:12px;color:#c9ced6}}
  .judge th,.judge td{{border:1px solid #2a3038;padding:6px 8px;text-align:left}}
</style></head><body>
<h1>M-C 提示词人工验收</h1>
<div class="sub">真实模型出图 · {html.escape(str(run_dir.name))} · 共 {len(rows)} 例；每类 ≥2/3 通过视为达标</div>
{''.join(cards)}
</body></html>"""
    path = run_dir / "report.html"
    path.write_text(page, encoding="utf-8")
    return path


# 生成通道 provider id 与是否需要 Key（openai-compatible 指向本机地址时免 Key）。
PROVIDER_IDS = ("gemini-image", "openai-compatible", "dashscope")
LOCAL_URL_PREFIXES = ("http://localhost", "http://127.0.0.1", "http://[::1]", "http://0.0.0.0")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="M-C 提示词人工验收跑批（9 例真实调用）")
    parser.add_argument(
        "--provider",
        choices=PROVIDER_IDS,
        help="显式指定生成通道 provider；缺省沿用 data/config.json 的选择",
    )
    parser.add_argument("--model", help="模型名；缺省沿用 config.json 或 provider 默认模型")
    parser.add_argument(
        "--base-url",
        help="openai-compatible 通道的服务地址（本机部署可免 Key）",
    )
    parser.add_argument(
        "--key-env",
        default="CUPDRAW_AI_KEY",
        metavar="NAME",
        help="存放 API Key 的环境变量名（默认 CUPDRAW_AI_KEY）；Key 不走命令行参数",
    )
    return parser.parse_args(argv)


def _apply_env(args: argparse.Namespace) -> tuple[str, AiSettingsStore]:
    """把命令行参数落到本进程环境变量，复用 AiSettingsStore 的环境覆盖通道。"""
    if args.provider:
        os.environ["CUPDRAW_AI_PROVIDER"] = args.provider
    if args.model:
        os.environ["CUPDRAW_AI_MODEL"] = args.model
    if args.base_url:
        os.environ["CUPDRAW_AI_BASE_URL"] = args.base_url
    key = os.environ.get(args.key_env, "").strip()
    if key:
        os.environ["CUPDRAW_AI_KEY"] = key
    store = AiSettingsStore(ROOT / "data")
    return key, store


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    key, store = _apply_env(args)
    ai = store.load()
    service = EditService(settings=store)

    provider_id = args.provider or ai.provider
    needs_key = not (
        provider_id == "openai-compatible"
        and ai.base_url
        and ai.base_url.startswith(LOCAL_URL_PREFIXES)
    )
    if needs_key and not key:
        print(
            f"缺少 API Key：请设置环境变量 {args.key_env} 后重试，例如（PowerShell）：\n"
            f'  $env:{args.key_env}="sk-..."; python .benchmarks/mc_acceptance.py'
            f"{' --provider ' + args.provider if args.provider else ''}\n"
            "BYOK 改造后 Key 不再存盘，也不要写进命令行参数。"
        )
        return 2

    try:
        provider = service.route("", None, "generative", provider_id).provider
    except Exception as exc:
        print(f"生成通道不可用：{type(exc).__name__}: {exc}")
        return 2
    print(f"使用 provider={provider.id} model={ai.model} baseUrl={ai.base_url or '(默认)'}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = ROOT / "data" / "mc_acceptance" / f"run-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for case in CASES:
        case_dir = run_dir / str(case["id"])
        case_dir.mkdir(parents=True, exist_ok=True)
        source = Path(case["source"])
        source_gray = np.asarray(Image.open(source).convert("L"), dtype=np.uint8)
        Image.fromarray(source_gray, mode="L").save(case_dir / "source.png")
        started = time.monotonic()
        try:
            result = service.create_draft(
                base_image=source,
                instruction=str(case["instruction"]),
                roi=case["roi"],  # type: ignore[arg-type]
                provider_id=provider_id,
                out_dir=case_dir,
                channel="generative",
            )
        except Exception as exc:  # 单例失败不能中断整批，记录后继续
            print(f"{case['id']} 失败：{type(exc).__name__}: {exc}")
            rows.append(
                {
                    "case": case,
                    "metrics": {},
                    "seconds": time.monotonic() - started,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        seconds = time.monotonic() - started
        metrics = _metrics(case_dir / "source.png", result.draft_path, case["roi"])  # type: ignore[arg-type]
        rows.append({"case": case, "metrics": metrics, "seconds": seconds, "error": None})
        (case_dir / "meta.json").write_text(
            json.dumps(
                {
                    "instruction": case["instruction"],
                    "roi": case["roi"],
                    "prompt": build_prompt(str(case["instruction"])),
                    "provider": result.provider_id,
                    "inkRatio": result.ink_ratio,
                    **metrics,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            f"{case['id']} 完成 {seconds:.1f}s 墨量 {metrics['inkBefore']:.2%}→{metrics['inkAfter']:.2%} "
            f"改动 {metrics['changedRatio']:.2%} 选区外 {metrics['outsideRoiChangeRatio']:.4%}"
        )
    (run_dir / "run.json").write_text(
        json.dumps(
            {"provider": provider.id, "model": ai.model, "baseUrl": ai.base_url, "rows": rows},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    path = _report(run_dir, rows)
    failed = [r for r in rows if r["error"]]
    print(f"报告：{path}")
    print(f"成功 {len(rows) - len(failed)} / {len(rows)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
