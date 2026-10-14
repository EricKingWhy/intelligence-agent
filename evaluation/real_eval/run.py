"""`python -m evaluation.real_eval.run` 入口（issue #1005）。

逻辑在 `runner.py`；本模块只提供模块级 CLI，使验收命令
`python -m evaluation.real_eval.run --baseline A --out <jsonl>` 直接可用。
"""

from __future__ import annotations

from evaluation.real_eval.runner import main

if __name__ == "__main__":  # pragma: no cover - CLI 薄壳
    main()
