"""OOTG agent fine-tuning: 4-bit QLoRA SFT (+ optional DPO) on the PII-tokenized trajectories
exported by Opportunities On The Go (`/settings/training` -> sft.jsonl / dpo.jsonl).

Fits a free 16 GB Colab / Kaggle T4 for 7-8B-class instruct models. Runs anywhere with a CUDA GPU
(Azure ML / any VM):

    pip install "unsloth" "trl>=0.12" "datasets" "huggingface_hub"
    python train_qlora_unsloth.py --base-model <a 4-bit instruct model you choose> \
        --sft sft.jsonl --dpo dpo.jsonl --out ootg-agent-lora [--push-to <hf-user>/<repo>]

The base model is YOUR choice (no default is shipped on purpose). The exported data contains only
symbolic profile tokens ({{PROFILE.FULL_NAME}}, ...), never personal values, so the dataset and
the adapter can be published.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Any

LORA = {
    "r": 16,
    "lora_alpha": 32,
    "lora_dropout": 0.0,
    "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
}
DECOY = re.compile(
    r"subscribe|newsletter|share|facebook|login|log in|sign in|counsel|inquiry|search|cookie",
    re.I,
)


def load_jsonl(path: str | None) -> list[dict[str, Any]]:
    if not path or not Path(path).is_file():
        return []
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r]


def assert_no_raw_pii(rows: list[dict[str, Any]]) -> None:
    """The exporter already guarantees this; refuse to train on anything that looks personal."""
    email = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
    for row in rows:
        text = json.dumps(row, ensure_ascii=False)
        if email.search(text):
            raise SystemExit("refusing to train: a record contains an e-mail address")


def split(rows: list[dict[str, Any]], holdout: float, seed: int = 7) -> tuple[list, list]:
    rows = rows[:]
    random.Random(seed).shuffle(rows)
    n = max(1, int(len(rows) * holdout)) if len(rows) > 4 else 0
    return rows[n:], rows[:n]


def parse_action(text: str) -> dict[str, Any] | None:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def evaluate(generate, sft_eval: list[dict[str, Any]], dpo_eval: list[dict[str, Any]]) -> dict[str, float]:
    """Paper-table metrics on held-out records:
    - action accuracy: same action type (click / fill / select)
    - selector match: same target label (and the same profile token for fills)
    - decoy rejection: on DPO pairs whose rejected action targets a decoy control (newsletter,
      share, login...), the model does NOT pick the decoy."""
    actions = selectors = 0
    for row in sft_eval:
        msgs = row["messages"]
        want = parse_action(msgs[-1]["content"]) or {}
        got = parse_action(generate(msgs[:-1])) or {}
        actions += got.get("action") == want.get("action")
        same_target = (got.get("label") or "").lower() == (want.get("label") or "").lower()
        same_token = got.get("value_token") == want.get("value_token")
        selectors += same_target and same_token
    decoys = rejected_decoys = 0
    for row in dpo_eval:
        bad = parse_action(row["rejected"]) or {}
        if not DECOY.search(str(bad.get("label") or "")):
            continue
        decoys += 1
        got = parse_action(generate([{"role": "user", "content": row["prompt"]}])) or {}
        rejected_decoys += (got.get("label") or "") != bad.get("label")
    n = max(1, len(sft_eval))
    return {
        "action_accuracy_pct": round(100 * actions / n, 1),
        "selector_match_pct": round(100 * selectors / n, 1),
        "decoy_rejection_pct": round(100 * rejected_decoys / decoys, 1) if decoys else float("nan"),
        "eval_sft": len(sft_eval),
        "eval_decoy_pairs": decoys,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--base-model", required=True, help="a 4-bit (bnb) instruct model of your choice")
    ap.add_argument("--sft", default="sft.jsonl")
    ap.add_argument("--dpo", default="dpo.jsonl")
    ap.add_argument("--out", default="ootg-agent-lora")
    ap.add_argument("--max-seq-len", type=int, default=2048)
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--dpo-epochs", type=float, default=1.0)
    ap.add_argument("--holdout", type=float, default=0.1)
    ap.add_argument("--push-to", default="", help="Hugging Face repo id for the adapter")
    args = ap.parse_args()

    from datasets import Dataset
    from trl import DPOConfig, DPOTrainer, SFTConfig, SFTTrainer
    from unsloth import FastLanguageModel

    sft_rows, dpo_rows = load_jsonl(args.sft), load_jsonl(args.dpo)
    if not sft_rows:
        raise SystemExit(f"no SFT records in {args.sft}: export them from /settings/training")
    assert_no_raw_pii(sft_rows + dpo_rows)
    sft_train, sft_eval = split(sft_rows, args.holdout)
    dpo_train, dpo_eval = split(dpo_rows, args.holdout)

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.base_model, max_seq_length=args.max_seq_len, load_in_4bit=True
    )
    model = FastLanguageModel.get_peft_model(model, **LORA, use_gradient_checkpointing="unsloth")

    def to_text(row: dict[str, Any]) -> dict[str, str]:
        return {"text": tokenizer.apply_chat_template(row["messages"], tokenize=False)}

    sft = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=Dataset.from_list([to_text(r) for r in sft_train]),
        args=SFTConfig(
            output_dir=f"{args.out}/sft",
            dataset_text_field="text",
            max_seq_length=args.max_seq_len,
            per_device_train_batch_size=2,
            gradient_accumulation_steps=4,
            num_train_epochs=args.epochs,
            learning_rate=2e-4,
            logging_steps=5,
            optim="adamw_8bit",
            report_to="none",
        ),
    )
    sft.train()

    if dpo_train:
        dpo = DPOTrainer(
            model=model,
            ref_model=None,  # LoRA: the frozen base acts as the reference
            tokenizer=tokenizer,
            train_dataset=Dataset.from_list(
                [{"prompt": r["prompt"], "chosen": r["chosen"], "rejected": r["rejected"]} for r in dpo_train]
            ),
            args=DPOConfig(
                output_dir=f"{args.out}/dpo",
                per_device_train_batch_size=1,
                gradient_accumulation_steps=4,
                num_train_epochs=args.dpo_epochs,
                learning_rate=5e-6,
                beta=0.1,
                logging_steps=5,
                report_to="none",
            ),
        )
        dpo.train()

    FastLanguageModel.for_inference(model)

    def generate(messages: list[dict[str, str]]) -> str:
        ids = tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
        out = model.generate(input_ids=ids.to(model.device), max_new_tokens=128, do_sample=False)
        return tokenizer.decode(out[0][ids.shape[1]:], skip_special_tokens=True)

    metrics = evaluate(generate, sft_eval, dpo_eval)
    print(json.dumps(metrics, indent=2))
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    model.save_pretrained(args.out)
    tokenizer.save_pretrained(args.out)
    if args.push_to:
        model.push_to_hub(args.push_to)
        tokenizer.push_to_hub(args.push_to)
        print(f"adapter pushed to https://huggingface.co/{args.push_to}")


if __name__ == "__main__":
    main()
