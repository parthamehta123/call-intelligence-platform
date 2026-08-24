#!/usr/bin/env python3
"""Store a Databricks secret without it touching shell history or an editor.

`databricks secrets put-secret <scope> <key>` with no value opens $EDITOR
and stores nothing if you exit without saving. `read -rs` is shell
dependent -- zsh and bash differ, and a silent prompt that captures nothing
fails later as "Secret value must be specified", which does not point at
the cause.

This prompts with getpass (portable, no echo), reports the length so you
can confirm the paste registered before anything is sent, and passes the
value straight to the CLI. Nothing is written to disk and the value is
never printed.

    python3 scripts/put_secret.py cip anthropic_api_key
"""

from __future__ import annotations

import getpass
import subprocess
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    scope, key = argv[1], argv[2]

    value = getpass.getpass(f"value for {scope}/{key} (input hidden): ").strip()
    if not value:
        print("nothing captured -- the paste did not register; not sending.",
              file=sys.stderr)
        return 1
    print(f"captured {len(value)} characters "
          f"(starts {value[:7]}…, ends …{value[-4:]})")

    completed = subprocess.run(
        ["databricks", "secrets", "put-secret", scope, key,
         "--string-value", value],
        capture_output=True, text=True)
    if completed.returncode != 0:
        print(completed.stderr.strip()[-400:], file=sys.stderr)
        return completed.returncode

    listing = subprocess.run(["databricks", "secrets", "list-secrets", scope],
                             capture_output=True, text=True)
    stored = key in listing.stdout
    print(f"stored: {stored}")
    return 0 if stored else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
