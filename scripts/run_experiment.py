"""Run the registered investigator comparison on a frozen capture. Manually invoked.

    .venv/Scripts/python.exe scripts/run_experiment.py --capture captures/payment-latency-1 \
        --out captures/results/run-1 [--llm-repeats 5] \
        [--price-input-per-mtok <usd> --price-output-per-mtok <usd>]

Makes up to (scenarios x llm_repeats) paid requests to the registered Anthropic model; the
deterministic baseline makes none. It refuses unless the capture is frozen, verified, not smoke
data and made from the code checked out now (see `shared.evaluation.runner.check_registration`).
The model and endpoint come from the registration; `ANTHROPIC_MODEL` or `ANTHROPIC_BASE_URL` set
to something else is an error, not a substitution. Only `ANTHROPIC_API_KEY` is read from the
environment and it is never written anywhere.

Output (a fresh folder): `runs.jsonl` gets one line per run as it completes, so a stopped run keeps
what it recorded; `results.json` is written only when the whole run finished (its absence means the
run did not complete). Prices are optional operator-supplied numbers; without them cost is null.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from shared.evaluation import runner
from shared.evaluation import scenarios as sc


def main(
    argv: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
    client: httpx.Client | None = None,
    git_state=None,
    code_unchanged_since=None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--capture", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--llm-repeats", type=int, default=runner.LLM_REPEATS)
    parser.add_argument("--price-input-per-mtok", type=float)
    parser.add_argument("--price-output-per-mtok", type=float)
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env

    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into the non-empty {out}; use a new --out folder")
        return 2
    if (args.price_input_per_mtok is None) != (args.price_output_per_mtok is None):
        print("give both --price-input-per-mtok and --price-output-per-mtok, or neither")
        return 2
    pricing = (
        {
            "input_per_mtok_usd": args.price_input_per_mtok,
            "output_per_mtok_usd": args.price_output_per_mtok,
        }
        if args.price_input_per_mtok is not None
        else None
    )
    api_key = env.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set")
        return 2

    kwargs = {}
    if git_state is not None:
        kwargs["git_state"] = git_state
    if code_unchanged_since is not None:
        kwargs["code_unchanged_since"] = code_unchanged_since
    try:
        registration = runner.check_registration(Path(args.capture), env=env, **kwargs)
    except runner.RunnerRefused as exc:
        print("REFUSED, nothing was run:")
        for problem in exc.problems:
            print(" -", problem)
        return 2

    probe = runner.LLMProbe()
    investigator = runner.build_llm_investigator(
        registration, api_key=api_key, probe=probe, client=client
    )
    out.mkdir(parents=True, exist_ok=True)
    with (out / "runs.jsonl").open("x", encoding="utf-8", newline="\n") as lines:

        def sink(record: dict) -> None:
            lines.write(json.dumps(record, sort_keys=True) + "\n")
            lines.flush()

        runs = runner.run_experiment(
            registration,
            llm_investigator=investigator,
            probe=probe,
            llm_repeats=args.llm_repeats,
            pricing=pricing,
            sink=sink,
        )
    results = runner.build_results(
        registration, runs, llm_repeats=args.llm_repeats, pricing=pricing
    )
    with (out / "results.json").open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(sc.canonical_json(results))
    for name, block in results["summary"]["per_investigator"].items():
        print(name, "runs", block["runs"], "scored", block["scored_runs"], block["outcomes"])
    print(f"wrote {out}/results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
