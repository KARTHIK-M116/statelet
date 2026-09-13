# Setup

Requires Python 3.10 or newer (3.12 recommended). Check with
`python3 --version`.

---

## VS Code

**1. Unzip and open**

Unzip `statelet.zip`. You should have a folder named `statelet`
containing `statelet/`, `tests/`, `specs/`, `docs/`, `README.md`.

Open VS Code → File → Open Folder → select the outer `statelet` folder.

> Open the folder that *contains* `README.md`, not the inner
> `statelet/` package folder. If imports fail later, this is almost
> always why.

**2. Install the Python extension**

Extensions panel (`Ctrl+Shift+X` / `Cmd+Shift+X`) → search "Python" →
install the one from Microsoft.

**3. Create a virtual environment**

Open the terminal (`Ctrl+` ` backtick).

macOS / Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows (PowerShell):
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` and retry.

**4. Select the interpreter**

`Ctrl+Shift+P` / `Cmd+Shift+P` → "Python: Select Interpreter" → pick the
one showing `.venv`. Without this, VS Code will underline your imports
in red even though the code runs.

**5. Install dependencies**

```bash
pip install -r requirements.txt
```

**6. Verify**

```bash
python -m pytest tests/ -q
```

Expect `20 passed`.

**7. Enable the test panel (optional)**

`Ctrl+Shift+P` → "Python: Configure Tests" → pytest → `tests`. A flask
icon appears in the sidebar; you can run individual tests from there,
which is handy when a judge asks "which test proves that?"

---

## PyCharm

**1. Unzip and open**

Unzip, then PyCharm → Open → select the outer `statelet` folder (the one
with `README.md`).

**2. Create the interpreter**

Settings → Project: statelet → Python Interpreter → gear icon → Add.

Choose **Virtualenv Environment → New**, base interpreter Python 3.10+,
location `<project>/.venv`. OK.

**3. Install dependencies**

PyCharm usually shows a banner offering to install from
`requirements.txt` — accept it. Otherwise, in the terminal tab:

```bash
pip install -r requirements.txt
```

**4. Mark the project root as sources root**

Right-click the outer `statelet` folder in the Project panel → Mark
Directory as → **Sources Root**. This is what makes
`from statelet.core import ...` resolve.

**5. Verify**

Terminal tab:
```bash
python -m pytest tests/ -q
```

**6. Add run configurations (optional but useful for the demo)**

Run → Edit Configurations → `+` → Python.

- Name: `chaos`
- Choose **module name** (not script path): `statelet.cli`
- Parameters: `chaos --trials 25`
- Working directory: the project root

Duplicate it for the others:

| Name | Parameters |
|---|---|
| apply | `apply --spec specs/new-hire.yaml` |
| drift | `drift --spec specs/new-hire.yaml --break slack:backend --empty notion:onboarding-checklist` |
| offboard | `offboard --spec specs/new-hire.yaml` |
| doctor | `doctor` |

Having these as one-click buttons during a recorded demo is worth the
five minutes.

---

## Check it works

All of these run with no credentials at all:

```bash
python -m pytest tests/ -q                       # 20 passed

python -m statelet.cli apply --spec specs/new-hire.yaml
python -m statelet.cli apply --spec specs/new-hire.yaml   # in sync, 0 writes

python -m statelet.cli drift --spec specs/new-hire.yaml \
    --break slack:backend --empty notion:onboarding-checklist

python -m statelet.cli chaos --trials 25
```

---

## Optional: real app credentials

Only needed for `--live`. See `docs/AUTH.md` for how to get each token,
then:

```bash
python -m statelet.cli doctor
```

This preflights all four apps and tells you exactly which ones are not
ready. Do this before any live run.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'statelet'`**
You opened the inner package folder instead of the project root, or the
terminal is not in the project root. `cd` to the folder containing
`README.md` and retry.

**`ModuleNotFoundError: No module named 'yaml'`**
The venv is not active, or dependencies were installed into a different
interpreter. Re-activate and re-run `pip install -r requirements.txt`.

**Imports underlined red in VS Code but tests pass**
Interpreter not selected. Step 4 above.

**Tests pass but take ~17 seconds**
Expected. The seed sweep in `test_chaos_suite_no_unexplained_orphans`
runs 200 reconciliations with retry backoff.

**`statelet: command not found`**
There is no installed entry point by design. Always invoke it as
`python -m statelet.cli ...`.
