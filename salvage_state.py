"""Salvage de state JSONs de runs interrompidas — chamado pelo local_refresh.bat (step 1).

Se uma run anterior foi interrompida (PC desligou) antes do commit final, os
state JSONs ficam modificados no working tree. O comportamento antigo era
DESCARTAR tudo (git checkout --) pra destravar o pull — jogando fora trabalho
bom, incluindo resumos Gemini que já gastaram cota e seriam refeitos.

Este script preserva o que presta e descarta só o que não presta:
  - JSON modificado e VÁLIDO  → git add (vira "salvage commit")
  - JSON modificado e CORROMPIDO (escrita interrompida no meio) → git checkout
    (restaura a versão sã do último commit; o scraper regenera no run atual)

Sem isso, um JSON corrompido commitado quebraria os scrapers (json.loads sem
try) até intervenção manual.
"""
import json
import subprocess
import sys

STATE_FILES = [
    "news_history.json", "news_diagnostic.json",
    "aneel_aux_history.json", "aneel_aux_diagnostic.json",
    "dou_history.json",
    "sei_processes.json",
    "mme_legislacao_history.json", "mme_legislacao_diagnostic.json",
    "cvm_recent.json", "alerts_state.json",
    "cvm_insider_history.json", "cvm_insider_diagnostic.json",
]


def _json_valido(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            json.load(f)
        return True
    except Exception:
        return False


def main():
    out = subprocess.run(
        ["git", "status", "--porcelain", "--"] + STATE_FILES,
        capture_output=True, text=True,
    ).stdout
    dirty = [line[3:].strip().strip('"') for line in out.splitlines() if line.strip()]
    if not dirty:
        print("[salvage] tree limpo — nada a preservar", file=sys.stderr)
        return

    for f in dirty:
        if _json_valido(f):
            subprocess.run(["git", "add", f])
            print(f"[salvage] preservado: {f}", file=sys.stderr)
        else:
            subprocess.run(["git", "checkout", "--", f])
            print(f"[salvage] DESCARTADO (JSON corrompido): {f}", file=sys.stderr)

    # Commit só se algo foi staged
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        subprocess.run(["git", "commit", "-m", "Salvage state de run interrompida [skip ci]"])
        print("[salvage] salvage commit criado", file=sys.stderr)


if __name__ == "__main__":
    main()
