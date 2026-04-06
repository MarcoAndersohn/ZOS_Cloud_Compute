import os
import shutil
import subprocess
import time
import glob
import re
import sys
import traceback
import requests
import csv
import psutil
import math
import signal
from datetime import datetime

# =============================================================================
# 0. LIVE-LOGGING & TRUNCATE
# =============================================================================
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

def truncate_log(log_path, max_size_mb=1.0):
    if not os.path.exists(log_path): return
    max_bytes = int(max_size_mb * 1024 * 1024)
    if os.path.getsize(log_path) > max_bytes:
        try:
            with open(log_path, 'rb') as f:
                f.seek(-max_bytes, 2)
                content = f.read()
            with open(log_path, 'wb') as f:
                f.write(content)
            print(f"✂️ Logfile {os.path.basename(log_path)} auf {max_size_mb} MB gekürzt.")
        except Exception as e:
            print(f"⚠️ Konnte Logfile nicht kürzen: {e}")

def get_scf_cores(scf_out_path, default_cores=2):
    """Liest aus der scf.out aus, mit wie vielen Cores die Rechnung beendet wurde."""
    if not os.path.exists(scf_out_path): 
        return int(default_cores)
    try:
        with open(scf_out_path, 'r', errors='ignore') as f:
            # Wir suchen alle Einträge und nehmen den allerletzten (relevant bei Restarts)
            matches = re.findall(r"running on\s+(\d+)\s+processors", f.read())
            if matches:
                return int(matches[-1])
    except: pass
    return int(default_cores)

# =============================================================================
# 1. KONFIGURATION
# =============================================================================
TELEGRAM_TOKEN   = "8202414068:AAHnnLMa7nfo0E3gCDLUVnUmIomoyveDPBA"
TELEGRAM_CHAT_ID = "711461437"

LOGIC_APP_NAME  = "AutoRestart-Supraleiter"
RESOURCE_GROUP  = "Supraleiter-HPC-Knoten_group"
DOS_THRESHOLD   = 0.05

DEFAULT_CORES        = "2"
SAFE_CORES           = "1"
MEMORY_LIMIT_PERCENT = 88.0
MAX_BFGS_STEPS       = 200
MAX_RETRIES_LEVEL    = 3
ERROR_LOG_LINES      = 30

WORK_DIR    = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR  = os.path.join(WORK_DIR, "Inputs")
PSEUDO_DIR  = os.path.join(WORK_DIR, "pseudo")
SIGNAL_FILE = os.path.join(WORK_DIR, "rechnung_fertig.txt")
CSV_FILE    = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")

TXT_LOG_FILE    = os.path.join(WORK_DIR, "pipeline_output.txt")
BACKUP_LOG_FILE = os.path.join(WORK_DIR, "pipeline_output_backup.txt")

# PW_EXE     = shutil.which("pw.x")     or "/usr/bin/pw.x"
# PH_EXE     = shutil.which("ph.x")     or "/usr/bin/ph.x"
PW_EXE = "/home/marco/qe-source/bin/pw.x"
PH_EXE = "/home/marco/qe-source/bin/ph.x"
DOS_EXE    = shutil.which("dos.x")    or "/usr/bin/dos.x"
Q2R_EXE    = shutil.which("q2r.x")    or "/usr/bin/q2r.x"
MATDYN_EXE = shutil.which("matdyn.x") or "/usr/bin/matdyn.x"

# =============================================================================
# 2. HELFER & GIT & BACKUP & CLEANUP
# =============================================================================
def backup_log_file():
    truncate_log(TXT_LOG_FILE, max_size_mb=1.0)
    if os.path.exists(TXT_LOG_FILE):
        try: shutil.copy(TXT_LOG_FILE, BACKUP_LOG_FILE)
        except: pass

def send_notification(message):
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ HPC: {message}"}
        requests.post(url, data=payload, timeout=10)
    except: pass

def set_logic_app_state(state="Enabled"):
    if not shutil.which("az"): return
    try:
        subprocess.run(
            ["az", "logic", "workflow", "set-state",
             "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME, "--state", state],
            capture_output=True, timeout=30)
    except: pass

def git_sync(message):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    lock_file = os.path.join(WORK_DIR, ".git", "index.lock")
    try:
        # Stale lock entfernen (sicher wenn kein Git-Prozess aktiv)
        if os.path.exists(lock_file):
            lock_age = time.time() - os.path.getmtime(lock_file)
            if lock_age > 60:   # älter als 60s → garantiert stale
                os.remove(lock_file)
                print("⚠️ Stale index.lock entfernt.")
        subprocess.run(["git", "add", "."], cwd=WORK_DIR, env=env, timeout=30)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR,
                       capture_output=True, env=env, timeout=30)
        subprocess.run(["git", "pull", "origin", "main",
                        "--strategy-option=ours", "--no-rebase"],
                       cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"],
                       cwd=WORK_DIR, env=env, timeout=60)
    except Exception as e:
        print(f"⚠️ Git Fehler: {e}")

def update_csv(name, status, e_fermi="-", dos_val="-", is_metal="-",
               min_f="-", stab="-", lam="-", wlog="-", tc="-"):
    fieldnames = ['Name','Status','Fermi Energie (eV)','DOS @ Fermi','Metall?',
                  'Min Freq (THz)','Stabilität','Lambda','Omega_log (K)','Tc (K)','Timestamp']
    rows = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'r') as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                for ef in reader.fieldnames:
                    if ef not in fieldnames: fieldnames.append(ef)
            rows = list(reader)

    found = False
    for row in rows:
        if row['Name'] == name:
            row.update({'Status': status,
                        'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")})
            if e_fermi  != "-": row['Fermi Energie (eV)'] = str(e_fermi)
            if dos_val  != "-": row['DOS @ Fermi']        = str(dos_val)
            if is_metal != "-": row['Metall?']             = str(is_metal)
            if min_f    != "-": row['Min Freq (THz)']      = str(min_f)
            if stab     != "-": row['Stabilität']          = str(stab)
            if lam      != "-": row['Lambda']              = str(lam)
            if wlog     != "-": row['Omega_log (K)']       = str(wlog)
            if tc       != "-": row['Tc (K)']              = str(tc)
            found = True
            break

    if not found:
        new_row = {
            'Name': name, 'Status': status,
            'Fermi Energie (eV)': str(e_fermi), 'DOS @ Fermi': str(dos_val),
            'Metall?': str(is_metal), 'Min Freq (THz)': str(min_f),
            'Stabilität': str(stab),
            'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")}
        if lam  != "-": new_row['Lambda']        = str(lam)
        if wlog != "-": new_row['Omega_log (K)'] = str(wlog)
        if tc   != "-": new_row['Tc (K)']        = str(tc)
        rows.append(new_row)

    with open(CSV_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

def get_csv_full_info(name):
    if not os.path.exists(CSV_FILE): return {}
    with open(CSV_FILE, 'r') as f:
        for row in csv.DictReader(f):
            if row['Name'] == name: return row
    return {}

def count_job_attempts(log_file, job_name):
    if not os.path.exists(log_file): return 1
    count = 0
    try:
        with open(log_file, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 50000), 0)
            lines = f.read().decode('utf-8', errors='ignore').splitlines()
        job_marker = f"💎 Job: {job_name}"
        for line in reversed(lines):
            if job_marker in line: count += 1
            elif "💎 Job:" in line and job_name not in line: break
    except: return 1
    return max(1, count)

def berechne_tc(omega_log_K, lambda_ep, mu_star=0.13):
    try:
        lam  = float(lambda_ep)
        wlog = float(omega_log_K)
        if lam <= 0: return 0.0
        vorfaktor = wlog / 1.20
        zaehler   = -1.04 * (1.0 + lam)
        nenner    = lam - mu_star * (1.0 + 0.62 * lam)
        if nenner <= 0: return 0.0
        return vorfaktor * math.exp(zaehler / nenner)
    except: return "-"

def cleanup_heavy_files(work_dir, name, force=False):
    if not force:
        row_data = get_csv_full_info(name)
        if not row_data:
            print(f"      ⚠️ Cleanup abgebrochen, {name} nicht in CSV gefunden.")
            return
        
        status = row_data.get('Status', '')
        tc_val = row_data.get('Tc (K)', '-')
        stab = row_data.get('Stabilität', '-')
        
        is_finished = ("Isolator" in status) or (stab == "INSTABIL") or (tc_val != "-")
        
        if not is_finished:
            print(f"      ⚠️ Cleanup blockiert, {name} ist noch nicht final in der CSV gesichert.")
            return

    deleted_something = False
    
    for dvscf_file in glob.glob(os.path.join(work_dir, "*dvscf*")):
        try:
            os.remove(dvscf_file)
            deleted_something = True
        except: pass

    for path in [os.path.join(work_dir, p) for p in ["tmp", "tmp_SAFE_CHECKPOINT", "tmp_SAFE_PHONON_CHECKPOINT"]]:
        if os.path.exists(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
                deleted_something = True
            except Exception as e:
                print(f"      ⚠️ Konnte {path} nicht löschen, {e}")
                
    if deleted_something:
        print(f"      🧹 Heavy Files & dvscf für {name} sicher bereinigt.")
        git_sync(f"Cleanup Heavy Files, {name}")
        
def print_error_log(output_file, label="QE ERROR LOG"):
    """Gibt die letzten ERROR_LOG_LINES Zeilen einer Output-Datei aus."""
    if not os.path.exists(output_file): return
    try:
        err_lines = open(output_file, errors='ignore').read().strip().split('\n')
        snippet   = err_lines[-ERROR_LOG_LINES:]
        print(f"      --- {label} ---")
        print("      " + "\n      ".join(snippet))
        print("      " + "-" * 20)
    except: pass

# =============================================================================
# 3. CRASH-ANALYSE & VALIDATION
# =============================================================================

def analyze_crash_reason(output_file):
    if not os.path.exists(output_file): return "NONE"
    try:
        with open(output_file, 'rb') as f:
            try: f.seek(-20000, 2)
            except OSError: f.seek(0)
            lines = f.read().decode('utf-8', errors='ignore')

        if "JOB DONE"                                     in lines: return "DONE"
        if "convergence NOT achieved"                      in lines: return "NON_CONVERGED"
        if "The maximum number of steps has been reached"  in lines: return "RESTART_NEEDED"

        if ("fatal error reading xml" in lines or
                "reading output_obj of xsd" in lines or
                "wrong number of occurrences" in lines):
            print("      🧨 XML-Struktur zerstört (Corruption).")
            return "XML_ERROR"

        # NEU: Wellenfunktionen fehlen (nach SCF_RESET oder OOM-Kill während Schreiben)
        if "Wavefunctions in collected format not available" in lines:
            print("      ⚠️ Wellenfunktionen fehlen (wf_collect Error).")
            return "WF_COLLECT_ERROR"

        # NEU: Korrupte Phonon-Dateien (I/O past end, End of file in elphon/write_rec)
        if "I/O past end of record" in lines or (
                "End of file" in lines and ("elphon.f90" in lines or "write_rec.f90" in lines)):
            print("      ⚠️ Korrupte Lese-/Schreibdatei (I/O Error).")
            return "CORRUPT_FILE_ERROR"

        if "not orthogonal" in lines and "D_S" in lines:
            print("      🧩 Symmetrie-Fehler (D_S not orthogonal).")
            return "SYMMETRY_ERROR"

        if "FFT grid incompatible with symmetry" in lines:
            print("      🧩 FFT-Gitter Inkompatibilität (Symmetrie-Konflikt).")
            return "FFT_SYMMETRY_ERROR"

        if "error reading file" in lines and "xml" not in lines:
            print("      🤕 Fragmentierungsfehler (davcio).")
            return "DAVCIO_ERROR"

        if "aainit" in lines and "mx dimension too small" in lines:
            print("      🔩 aainit-Fehler erkannt (MPI-Bug oder RAM).")
            return "AAINIT_ERROR"

        error_keywords = ["Error", "error", "Mpi_Abort", "MPI_ABORT",
                          "segmentation fault", "stopping", "fatal",
                          "diagonalization failed"]
        if any(key in lines for key in error_keywords): return "HARD"

        ram_match = re.search(
            r"Estimated total dynamical RAM\s*>\s*([0-9\.]+)\s*GB", lines)
        if ram_match:
            if ("Self-consistent Calculation" not in lines and
                    "iteration #" not in lines):
                return "LIKELY_OOM"

        if "iteration #" in lines or "diagonalization" in lines:
            return "LIKELY_OOM"

        return "SOFT"
    except: return "HARD"
    
def is_xml_valid(xml_path):
    if not os.path.exists(xml_path): return False
    try:
        with open(xml_path, 'rb') as f:
            try: f.seek(-1000, 2)
            except: f.seek(0)
            tail = f.read().decode('utf-8', errors='ignore')
        return "</qes:espresso>" in tail or "</qes:data-file-schema>" in tail
    except: return False

def is_recoverable_fragmentation_error(ph_output_file):
    if not os.path.exists(ph_output_file): return False
    try:
        with open(ph_output_file, 'r', errors='ignore') as f:
            content = f.read()
        return ("mismatch in number of G-vectors" in content or
                ("error reading file" in content and "xml" not in content))
    except: return False

def run_cleanup_scf(scf_input_file, cwd, cores_to_use=2):
    print(f"      🚑 Starte RECOVERY-Modus (Collect Waves), Cores={cores_to_use}")
    with open(scf_input_file, 'r') as f: content = f.read()

    if "restart_mode" in content:
        content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]",
                         "restart_mode='restart'", content)
    else:
        content = content.replace("&CONTROL", "&CONTROL\n restart_mode='restart',")

    if "wf_collect" in content:
        content = re.sub(r"wf_collect\s*=\s*\.?[a-zA-Z]+\.?",
                         "wf_collect=.true.", content)
    else:
        content = content.replace("&CONTROL", "&CONTROL\n wf_collect=.true.,")

    if "nstep" in content:
        content = re.sub(r"nstep\s*=\s*\d+", "nstep=0", content)
    else:
        content = content.replace("&CONTROL", "&CONTROL\n nstep=0,")

    recover_in  = scf_input_file + ".recover"
    recover_out = os.path.join(cwd, "scf_recover.out")
    with open(recover_in, 'w') as f: f.write(content)

    with open(recover_in, 'r') as f_in, open(recover_out, 'w') as f_out:
        cmd = ["mpirun", "--oversubscribe", "-np", str(cores_to_use), PW_EXE]
        try:
            subprocess.run(cmd, stdin=f_in, stdout=f_out,
                           stderr=subprocess.STDOUT, cwd=cwd, timeout=300)
            print("      ✅ Recovery-Lauf beendet.")
            return True
        except Exception as e:
            print(f"      ❌ Recovery fehlgeschlagen: {e}")
            return False

def disable_symmetries_and_reduce_grid(input_file):
    if not os.path.exists(input_file): return
    with open(input_file, 'r') as f: content = f.read()

    prefix_match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", content)
    prefix       = prefix_match.group(1) if prefix_match else "calc"
    fildyn_match = re.search(r"fildyn\s*=\s*['\"]([^'\"]+)['\"]", content)
    fildyn       = fildyn_match.group(1) if fildyn_match else f"{prefix}.dyn"

    new_content = (
        f"Phonons\n&INPUTPH\n"
        f" tr2_ph=1.0d-10,\n"
        f" prefix='{prefix}',\n"
        f" outdir='./tmp',\n"
        f" fildyn='{fildyn}',\n"
        f" fildvscf='dvscf',\n"
        f" ldisp=.true.,\n"
        f" electron_phonon='interpolated',\n"
        f" search_sym=.false.,\n"
        f" nq1=1, nq2=1, nq3=1\n"
        f"/\n"
    )
    with open(input_file, 'w') as f: f.write(new_content)
    print("      🛡️ ph.in neu generiert: Symmetrien deaktiviert & Grid auf 1x1x1.")

def detect_oom_level(input_file):
    if not os.path.exists(input_file): return 0
    with open(input_file, 'r', errors='ignore') as f: content = f.read()
    match = re.search(r"!\s*SMART_OOM_LEVEL\s*=\s*(\d+)", content)
    if match: return int(match.group(1))
    if "disk_io='low'" in content or 'disk_io="low"' in content: return 2
    if "diagonalization='cg'" in content or 'diagonalization="cg"' in content: return 1
    return 0

def apply_oom_settings(input_file, level, force_cg=False):
    with open(input_file, 'r') as f: content = f.read()
    # FIX: Level 0 startet jetzt mit mix=6 (nicht 8) — spart RAM von Anfang an
    diag = 'david'; mix = 6; disk = None
    msg = "Standard (david, mix=6)"

    if level >= 1 or force_cg: diag = 'cg'; mix = 4; msg = "Stufe 1 (cg, mix=4)"
    if level >= 2: disk = 'low'; msg = "Stufe 2 (cg, mix=4, disk_io='low')"
    if level >= 3: mix = 3; msg = "Stufe 3 (cg, mix=3, disk_io='low')"
    if level >= 4: mix = 2; msg = "Stufe 4 (cg, mix=2, disk_io='low', 1 Core)"

    print(f"      📉 RAM-Strategie, {msg}")

    if "diagonalization" in content:
        content = re.sub(r"diagonalization\s*=\s*['\"].*['\"]", f"diagonalization='{diag}'", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n diagonalization='{diag}',")

    if "mixing_ndim" in content:
        content = re.sub(r"mixing_ndim\s*=\s*\d+", f"mixing_ndim = {mix}", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_ndim = {mix},")

    # FIX: diago_david_ndim explizit setzen — neues QE braucht das
    ndim = 4 if level == 0 else 2
    if "diago_david_ndim" in content:
        content = re.sub(r"diago_david_ndim\s*=\s*\d+", f"diago_david_ndim = {ndim}", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n diago_david_ndim = {ndim},")

    if disk == 'low':
        if "disk_io" in content:
            content = re.sub(r"disk_io\s*=\s*['\"][a-zA-Z]+['\"]", "disk_io='low'", content)
        else:
            content = content.replace("&CONTROL", "&CONTROL\n disk_io='low',")
    else:
        if "disk_io='low'" in content or 'disk_io="low"' in content:
             content = re.sub(r"disk_io\s*=\s*['\"]low['\"],?", "", content)

    if "! SMART_OOM_LEVEL" in content:
        content = re.sub(r"!\s*SMART_OOM_LEVEL\s*=\s*\d+", f"! SMART_OOM_LEVEL={level}", content)
    else:
        content += f"\n! SMART_OOM_LEVEL={level}\n"

    with open(input_file, 'w') as f: f.write(content)
    
def fix_input_file(input_file, iteration_count=0):
    with open(input_file, 'r') as f: content = f.read()
    corr_path = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" in content:
        content = re.sub(r"pseudo_dir\s*=\s*['\"].*['\"]", f"pseudo_dir='{corr_path}'", content)
    else:
        content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{corr_path}',")

    # FIX: local-TF spart bei Metallen signifikant RAM
    if "mixing_mode" in content:
        content = re.sub(r"mixing_mode\s*=\s*['\"][a-zA-Z\-]+['\"]", "mixing_mode='local-TF'", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n mixing_mode='local-TF',")

    target_beta = 0.4
    if iteration_count >= 30: target_beta = 0.25
    if iteration_count >= 60: target_beta = 0.15
    if iteration_count >= 90: target_beta = 0.10

    if "mixing_beta" in content:
        content = re.sub(r"mixing_beta\s*=\s*[0-9\.]+", f"mixing_beta = {target_beta}", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_beta = {target_beta},")

    if "electron_maxstep" in content:
        content = re.sub(r"electron_maxstep\s*=\s*\d+", "electron_maxstep = 150", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n electron_maxstep = 150,")

    with open(input_file, 'w') as f: f.write(content)
    return True

def get_last_iteration(output_file):
    if not os.path.exists(output_file): return 0
    try:
        file_size = os.path.getsize(output_file)
        with open(output_file, 'rb') as f:
            f.seek(max(0, file_size - 10000), 0)
            chunk = f.read().decode('utf-8', errors='ignore')
        bfgs = re.findall(r"number of bfgs steps\s*=\s*(\d+)", chunk)
        scf  = re.findall(r"iteration #\s*(\d+)", chunk)
        if bfgs: return int(bfgs[-1])
        if scf:  return int(scf[-1])
    except: pass
    return 0

# =============================================================================
# 4. PWSCF WRAPPER
# =============================================================================
def run_monitored_pw(input_file, output_file, cwd, active_cores, force_cg=False):
    fix_input_file(input_file, 0)
    last_git_sync        = time.time()
    last_checkpoint_time = 0

    while True:
        with open(input_file, 'r') as f: content = f.read()
        tmp_dir        = os.path.join(cwd, "tmp")
        checkpoint_dir = os.path.join(cwd, "tmp_SAFE_CHECKPOINT")

        if "wf_collect" in content:
            content = re.sub(r"wf_collect\s*=\s*\.?[a-zA-Z]+\.?",
                             "wf_collect=.true.", content)
        else:
            content = content.replace("&CONTROL", "&CONTROL\n wf_collect=.true.,")

        prefix_match   = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", content)
        current_prefix = prefix_match.group(1) if prefix_match else "calc"
        xml_path       = os.path.join(tmp_dir, f"{current_prefix}.save",
                                      "data-file-schema.xml")

        mode = 'from_scratch'
        if is_xml_valid(xml_path):
            mode = 'restart'
            print("      ✅ Gültige XML gefunden -> Restart.")
        elif os.path.exists(checkpoint_dir):
            print("      🛡️ tmp defekt! Lade Safe-Checkpoint...")
            try:
                if os.path.exists(tmp_dir): shutil.rmtree(tmp_dir)
                shutil.copytree(checkpoint_dir, tmp_dir)
                if is_xml_valid(xml_path):
                    mode = 'restart'
                    print("      ✅ Checkpoint geladen!")
                else:
                    print("      ❌ Checkpoint defekt. Starte von vorne.")
            except Exception as e:
                print(f"      ❌ Checkpoint-Fehler, {e}")
        else:
            print("      🆕 Kein Speicherstand -> From Scratch.")

        if mode == 'from_scratch':
            if os.path.exists(tmp_dir):        shutil.rmtree(tmp_dir, ignore_errors=True)
            if os.path.exists(checkpoint_dir): shutil.rmtree(checkpoint_dir, ignore_errors=True)

        if "restart_mode" in content:
            content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]",
                             f"restart_mode='{mode}'", content)
        else:
            content = content.replace("&CONTROL",
                                      f"&CONTROL\n restart_mode='{mode}',")

        run_input = input_file + ".run"
        with open(run_input, 'w') as f: f.write(content)

        file_mode = 'a' if mode == 'restart' else 'w'
        backup_log_file()

        # Grundreinigung vor Ausführung
        cleanup_system_memory()

        with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
            cmd     = ["mpirun", "--oversubscribe", "-np", str(active_cores), PW_EXE]
            print(f"      ⚙️ PWSCF ({mode}, {active_cores} Cores)...")

            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out,
                                       stderr=subprocess.STDOUT, cwd=cwd,
                                       start_new_session=True)
            try:
                while process.poll() is None:
                    time.sleep(5)

                    if time.time() - last_checkpoint_time > 900:
                        if is_xml_valid(xml_path):
                            print("      💾 Erstelle Checkpoint...")
                            try:
                                if os.path.exists(checkpoint_dir):
                                    shutil.rmtree(checkpoint_dir)
                                shutil.copytree(tmp_dir, checkpoint_dir)
                                last_checkpoint_time = time.time()
                                print("      ✅ Checkpoint erstellt.")
                                git_sync("Checkpoint & Log Update")
                                backup_log_file()
                                last_git_sync = time.time()
                            except Exception as e:
                                print(f"      ⚠️ Checkpoint fail, {e}")

                    if time.time() - last_git_sync > 3600:
                        print("      ❤️ Git Heartbeat...")
                        git_sync("Log Update (Heartbeat)")
                        backup_log_file()
                        last_git_sync = time.time()

                    try:
                        if psutil.virtual_memory().percent > MEMORY_LIMIT_PERCENT:
                            print("      ⚠️ RAM NOT-AUS!")
                            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                            return "OOM"
                    except: pass

                    cur_iter = get_last_iteration(output_file)
                    if cur_iter >= MAX_BFGS_STEPS:
                        print(f"      🛑 BFGS-Limit ({cur_iter}/{MAX_BFGS_STEPS}).")
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        return "MAX_STEPS"

                    if cur_iter > 30: fix_input_file(input_file, cur_iter)

            except:
                try: 
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except: 
                    pass
                return "CRASH"

        if process.returncode == -9:
            print("      💀 OS killed (-9) -> OOM.")
            return "OOM"

        reason = analyze_crash_reason(output_file)
        if reason == "DONE":
            if process.returncode != 0:
                print("      ⚠️ MPI-Fehlalarm (JOB DONE gefunden).")
            return "DONE"
        if reason == "RESTART_NEEDED":
            print("      🔄 nstep-Limit -> Neustart für weitere Optimierung.")
            return "RESTART_NEEDED"
        if reason == "LIKELY_OOM":
            print("      💀 Abruptes Ende (Silent OOM).")
            return "OOM"
        if reason == "AAINIT_ERROR":
            return "CRASH"
        return "CRASH"
    
# =============================================================================
# 5. PHONON WRAPPER
# =============================================================================
def run_monitored_ph(input_file, output_file, cwd, active_cores):
    last_git_sync        = time.time()
    last_checkpoint_time = time.time()
    tmp_dir        = os.path.join(cwd, "tmp")
    ph0_dir        = os.path.join(tmp_dir, "_ph0")
    checkpoint_dir = os.path.join(cwd, "tmp_SAFE_PHONON_CHECKPOINT")

    with open(input_file, 'r') as f: content = f.read()

    if not os.path.exists(ph0_dir) and os.path.exists(checkpoint_dir):
        print("      🛡️ _ph0 fehlt! Lade Phonon-Checkpoint...")
        try:
            shutil.copytree(checkpoint_dir, ph0_dir)
            print("      ✅ Phonon-Checkpoint geladen!")
        except Exception as e:
            print(f"      ❌ Phonon-Checkpoint-Fehler, {e}")

    if os.path.exists(ph0_dir):
        if "recover" not in content:
            content = content.replace("&INPUTPH", "&INPUTPH\n recover=.true.,")
    else:
        if os.path.exists(checkpoint_dir):
            shutil.rmtree(checkpoint_dir, ignore_errors=True)

    run_input = input_file + ".run"
    with open(run_input, 'w') as f: f.write(content)

    file_mode = 'a' if "recover=.true." in content else 'w'
    backup_log_file()

    # Grundreinigung vor Ausführung
    cleanup_system_memory()

    with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
        cmd     = ["mpirun", "--oversubscribe", "-np", str(active_cores), PH_EXE]
        print(f"      ⚙️ PHONONEN (Cores, {active_cores})...")

        process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out,
                                   stderr=subprocess.STDOUT, cwd=cwd,
                                   start_new_session=True)
        try:
            while process.poll() is None:
                time.sleep(5)
                current_time = time.time()

                if current_time - last_git_sync > 1800:
                    print("      ❤️ Git Heartbeat (Phonon)...")
                    git_sync("Log Update (Phonon Running)")
                    backup_log_file()
                    last_git_sync = current_time

                if current_time - last_checkpoint_time > 1200:
                    if os.path.exists(ph0_dir):
                        print("      💾 Phonon-Checkpoint...")
                        try:
                            if os.path.exists(checkpoint_dir):
                                shutil.rmtree(checkpoint_dir)
                            shutil.copytree(ph0_dir, checkpoint_dir)
                            last_checkpoint_time = current_time
                            backup_log_file()
                            print("      ✅ Phonon-Checkpoint gesichert.")
                        except Exception as e:
                            print(f"      ⚠️ Phonon-Checkpoint fail, {e}")

                try:
                    if psutil.virtual_memory().percent > MEMORY_LIMIT_PERCENT:
                        print("      ⚠️ RAM NOT-AUS!")
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        return "OOM"
                except: pass

        except:
            try: 
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except: 
                pass
            return "CRASH"

    if process.returncode == -9:
        print("      💀 OS killed (-9) -> OOM.")
        return "OOM"

    try:
        with open(output_file, 'r', errors='ignore') as f:
            if "JOB DONE" in f.read():
                if process.returncode != 0:
                    print("      ⚠️ MPI-Fehlalarm (JOB DONE gefunden).")
                return "DONE"
    except: pass

    return "CRASH"

# =============================================================================
# 6. SCF-BLOCK
# =============================================================================
def run_scf_block(name, work_dir, scf_in, scf_out):
    aainit_ecut_reduced = False
    
    # Auto-Heal: Frischer Start = Level 0
    if not os.path.exists(scf_out): file_level = 0
    else: file_level = detect_oom_level(scf_in)
        
    start_crash_reason = analyze_crash_reason(scf_out)

    if start_crash_reason == "LIKELY_OOM":
        attempts = count_job_attempts(TXT_LOG_FILE, name)
        if os.path.exists(BACKUP_LOG_FILE):
            attempts = max(attempts, count_job_attempts(BACKUP_LOG_FILE, name))
        print(f"      🕵️ OOM-Signatur erkannt. Versuch Nr. {attempts} auf Level {file_level}.")

        if os.path.exists(scf_out):
            ts = datetime.now().strftime("%H%M%S")
            try:
                os.rename(scf_out, f"{scf_out}.crash_{ts}")
                print(f"      👻 Ghost-Protection, scf.out.crash_{ts}")
            except: pass

        if attempts >= MAX_RETRIES_LEVEL:
            oom_level = file_level + 1
            print(f"      ❗ Eskaliere, Level {file_level} -> {oom_level}.")
            update_csv(name, f"Recovering (Escalating to Lvl {oom_level})")
        else:
            oom_level = file_level
            print(f"      🔄 Level {file_level} nochmal.")
            update_csv(name, f"Retrying (Attempt {attempts}/{MAX_RETRIES_LEVEL})")
    else:
        oom_level = file_level

    # RETTUNG: Bei Stufe 4 wird auf 1 Core zurückgeschaltet!
    current_cores = int(DEFAULT_CORES)
    if oom_level >= 4: current_cores = int(SAFE_CORES)

    crash_counter = 0
    oom_counter   = 0

    while True:
        force_cg = False
        if os.path.exists(scf_out):
            try:
                with open(scf_out, 'r', errors='ignore') as f:
                    if "eigenvalues not converged" in f.read():
                        force_cg = True
                        print("      ⚠️ Konvergenz-Probleme -> erzwinge CG.")
            except: pass

        apply_oom_settings(scf_in, oom_level, force_cg)
        print(f"   1️⃣  SCF ({current_cores} Cores, OOM-Lvl {oom_level})")
        result = run_monitored_pw(scf_in, scf_out, work_dir, current_cores)

        if result == "DONE": return "DONE"
        if result == "MAX_STEPS":
            update_csv(name, "SKIPPED (Max BFGS Steps)")
            git_sync(f"Skipped {name}, >{MAX_BFGS_STEPS} BFGS Steps")
            return "MAX_STEPS"

        if result == "RESTART_NEEDED":
            update_csv(name, "Rechnet SCF (Fortsetzung)...")
            print("      🔄 nstep-Limit -> Geometrie-Optimierung fortsetzen...")
            continue

        if result == "OOM":
            oom_counter += 1
            if oom_counter < 3:
                print(f"      ⚠️ OOM Verdacht. Versuch {oom_counter}/3 auf Lvl {oom_level}...")
                update_csv(name, f"Retrying (OOM Wait {oom_counter}/3)")
                time.sleep(2)
                continue

            oom_level += 1
            oom_counter = 0
            crash_counter = 0
            print(f"      ⚠️ OOM-Limit. Eskaliere zu Level {oom_level}...")
            
            labels = {1: "Retrying (OOM Lvl 1, CG)",
                      2: "Retrying (OOM Lvl 2, DiskIO)",
                      3: "Retrying (OOM Lvl 3, Mix3)",
                      4: "Retrying (OOM Lvl 4, 1Core)"}
            if oom_level in labels:
                update_csv(name, labels[oom_level])
                if oom_level == 4: current_cores = int(SAFE_CORES)
            else:
                update_csv(name, "SKIPPED (OOM Limit)")
                print("      ❌ Hardware-Limit erreicht. Skippe.")
                return "OOM_LIMIT"
            continue

        if result == "CRASH":
            reason = analyze_crash_reason(scf_out)
            print_error_log(scf_out)

            if reason == "NON_CONVERGED":
                update_csv(name, "SKIPPED (Non-Conv)")
                return "NON_CONV"

            # RETTUNG: aainit schaltet ebenfalls wieder zurück auf 1 Core
            if reason == "AAINIT_ERROR":
                if current_cores > 1:
                    print("      🔩 aainit-Fehler -> LÖSCHE tmp und wechsle auf 1 Core.")
                    current_cores = 1
                    crash_counter = 0
                    tmp_path = os.path.join(work_dir, "tmp")
                    if os.path.exists(tmp_path): shutil.rmtree(tmp_path, ignore_errors=True)
                    update_csv(name, "Retrying (aainit -> 1 Core)")
                    continue
                else:
                    if not aainit_ecut_reduced:
                        aainit_ecut_reduced = True
                        apply_aainit_workaround(scf_in)
                        tmp_p = os.path.join(work_dir, "tmp")
                        if os.path.exists(tmp_p): shutil.rmtree(tmp_p, ignore_errors=True)
                        print("      🔧 aainit auf 1 Core -> reduziere ecutwfc und lösche altes tmp.")
                        update_csv(name, "Retrying (aainit -> ecutwfc=40)")
                        continue
                    print("      ❌ aainit-Fehler unlösbar. System zu komplex. Skippe.")
                    update_csv(name, "SKIPPED (OOM Limit)")
                    return "OOM_LIMIT"

            crash_counter += 1
            print(f"      ⚠️ Crash ({reason}). Versuch {crash_counter}/3...")
            if crash_counter >= 3:
                print(f"      ❌ Zu viele Abstürze ({crash_counter}). Skippe.")
                update_csv(name, "SKIPPED (Permanent Crash)")
                git_sync(f"Skipped {name}, Permanent Crash")
                return "PERM_CRASH"

            update_csv(name, f"Retrying (Crash {crash_counter}/3)")
            time.sleep(2)
            continue
            
# =============================================================================
# 7. PHONON-BLOCK (STRATEGIE A: 2-PHASEN-LOGIK)
# =============================================================================
def run_phonon_block(name, work_dir, scf_in, scf_out, ph_in, ph_out, e_fermi, dos_val):
    with open(scf_in, 'r') as f:
        match  = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", f.read())
        prefix = match.group(1) if match else "calc"

    def write_ph_input(fname, tr2="1.0d-14", nq="2,2,2", search_sym=True, elph=False):
        nq1, nq2, nq3 = nq.split(",")
        sym_line = " search_sym=.false.,\n" if not search_sym else ""
        elph_lines = " fildvscf='dvscf',\n electron_phonon='interpolated',\n" if elph else ""
        with open(fname, "w") as f:
            f.write(
                f"Phonons\n&INPUTPH\n"
                f" tr2_ph={tr2},\n"
                f" prefix='{prefix}',\n"
                f" outdir='./tmp',\n"
                f" fildyn='{name}.dyn',\n"
                f" ldisp=.true.,\n"
                f"{elph_lines}"
                f"{sym_line}"
                f" nq1={nq1}, nq2={nq2}, nq3={nq3}\n"
                f"/\n"
            )

    def execute_ph_phase(is_elph_phase=False):
        ph_cores = get_scf_cores(scf_out, DEFAULT_CORES)
        print(f"      🧠 Erbe Kernanzahl von SCF: Starte mit {ph_cores} Core(s).")
        phonon_attempts = 0
        aainit_1core_done = False

        while phonon_attempts < 3:
            phonon_attempts += 1
            ph_res = run_monitored_ph(ph_in, ph_out, work_dir, ph_cores)
            if ph_res == "DONE": return "DONE"

            phase_name = "El-Ph" if is_elph_phase else "Stabilität"
            print(f"      ⚠️ Phonon Crash/OOM! (Phase: {phase_name})")
            crash_reason = analyze_crash_reason(ph_out)
            print_error_log(ph_out, "PHONON ERROR LOG")

            if crash_reason == "AAINIT_ERROR":
                if ph_cores > 1 and not aainit_1core_done:
                    print("      🔩 aainit-Fehler auf 2 Cores -> LÖSCHE _ph0 und wechsle auf 1 Core.")
                    ph_cores = 1
                    aainit_1core_done = True
                    for p in [os.path.join(work_dir, "tmp", "_ph0"),
                               os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                        if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                    if os.path.exists(ph_out): os.remove(ph_out)
                    phonon_attempts -= 1
                    continue
                else:
                    print("      🔩 aainit-Fehler unlösbar -> Skippe.")
                    update_csv(name, f"SKIPPED (Phonon OOM, Phase {phase_name})")
                    git_sync(f"Phonon OOM, {name}")
                    return "CRASH"

            # NEU: Korrupte Phonon-Datei (_ph0/recover/a2Fsave) -> nur _ph0 löschen, nicht SCF
            if crash_reason == "CORRUPT_FILE_ERROR":
                print("      🧨 Defekte Phonon-Datei -> Lösche _ph0 und starte Phonon-Phase neu...")
                for p in [os.path.join(work_dir, "tmp", "_ph0"),
                           os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                    if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                if os.path.exists(ph_out): os.remove(ph_out)
                phonon_attempts -= 1  # nicht zählen
                continue

            # NEU: Wellenfunktionen fehlen -> erst collect-SCF, dann ggf. Reset
            if crash_reason == "WF_COLLECT_ERROR":
                print("      🌊 Wellenfunktionen fehlen -> starte Collect-SCF (nstep=0)...")
                if run_cleanup_scf(scf_in, work_dir, ph_cores):
                    print("      ✅ Collect-SCF OK -> Phononen neu starten.")
                    if os.path.exists(ph_out): os.remove(ph_out)
                    phonon_attempts -= 1
                    continue
                else:
                    print("      ❌ Collect-SCF fehlgeschlagen -> vollständiger SCF-Reset.")
                    tmp_save = os.path.join(work_dir, "tmp")
                    if os.path.exists(tmp_save): shutil.rmtree(tmp_save, ignore_errors=True)
                    if os.path.exists(scf_out): os.remove(scf_out)
                    update_csv(name, "SCF_RESET (WF_Collect)")
                    return "SCF_RESET"

            if crash_reason in ["XML_ERROR"]:
                print("      🧨 XML korrupt -> SCF-Reset.")
                tmp_save = os.path.join(work_dir, "tmp")
                if os.path.exists(tmp_save): shutil.rmtree(tmp_save, ignore_errors=True)
                if os.path.exists(scf_out):  os.remove(scf_out)
                update_csv(name, "SCF_RESET (XML Error)")
                return "SCF_RESET"

            if crash_reason in ["SYMMETRY_ERROR", "FFT_SYMMETRY_ERROR"]:
                print("      🧩 Symmetrie-Problem -> nosym injizieren + SCF-Reset.")
                source_in = os.path.join(INPUTS_DIR, f"{name}.in")
                if os.path.exists(source_in):
                    with open(source_in, 'r') as f: c = f.read()
                    if "nosym" not in c:
                        c = c.replace("&SYSTEM", "&SYSTEM\n nosym=.true.,")
                        with open(source_in, 'w') as f: f.write(c)
                if os.path.exists(work_dir): shutil.rmtree(work_dir, ignore_errors=True)
                update_csv(name, "SCF_RESET (Sym Error)")
                return "SCF_RESET"

            if crash_reason == "DAVCIO_ERROR" or is_recoverable_fragmentation_error(ph_out):
                print("      🤕 Fragmentierung -> 'Collect-Recovery'...")
                if run_cleanup_scf(scf_in, work_dir, int(DEFAULT_CORES)):
                    print("      👍 Recovery OK -> Phononen neu starten.")
                    if os.path.exists(ph_out): os.remove(ph_out)
                    continue
                print("      👎 Recovery fehlgeschlagen.")

            if crash_reason == "HARD":
                if os.path.exists(ph_out):
                    try:
                        ph_content_check = open(ph_out, errors='ignore').read()
                        if "bad line in namelist" in ph_content_check:
                            print("      📝 Namelist-Fehler -> schreibe ph.in komplett neu.")
                            nq_match = re.search(r"nq1\s*=\s*(\d+).*?nq2\s*=\s*(\d+).*?nq3\s*=\s*(\d+)",
                                                 ph_content_check, re.DOTALL)
                            nq = "2,2,2"
                            if nq_match: nq = f"{nq_match.group(1)},{nq_match.group(2)},{nq_match.group(3)}"
                            write_ph_input(ph_in, tr2="1.0d-14", nq=nq, elph=is_elph_phase)
                            if os.path.exists(ph_out): os.remove(ph_out)
                            phonon_attempts -= 1
                            continue
                    except: pass

            print(f"      🛡️ Phonon-Recovery, Versuch {phonon_attempts}/3")

            if phonon_attempts == 1:
                print("      📉 tr2_ph=1.0d-12")
                with open(ph_in, 'r') as f: c = f.read()
                c = re.sub(r"tr2_ph\s*=\s*[0-9\.dD\-]+", "tr2_ph=1.0d-12", c)
                with open(ph_in, 'w') as f: f.write(c)
                if os.path.exists(ph_out): os.remove(ph_out)
                continue

            elif phonon_attempts == 2:
                print("      🚨 NOTFALL-MODUS, Grid=1x1x1, Sym=OFF, tr2_ph=1.0d-10")
                write_ph_input(ph_in, tr2="1.0d-10", nq="1,1,1", search_sym=False, elph=is_elph_phase)
                for p in [os.path.join(work_dir, "tmp", "_ph0"),
                           os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                    if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                if os.path.exists(ph_out): os.remove(ph_out)
                continue

        print("      ❌ Phononen endgültig fehlgeschlagen.")
        update_csv(name, "SKIPPED (Phonon Crash)")
        git_sync(f"Phonon Crash, {name}")
        return "CRASH"

    # --- INIT ---
    current_nq = "2,2,2"
    if os.path.exists(ph_in):
        with open(ph_in, 'r') as f:
            nq_match = re.search(r"nq1\s*=\s*(\d+).*?nq2\s*=\s*(\d+).*?nq3\s*=\s*(\d+)", f.read(), re.DOTALL)
            if nq_match: current_nq = f"{nq_match.group(1)},{nq_match.group(2)},{nq_match.group(3)}"

    row_data = get_csv_full_info(name)
    already_stable = (row_data.get('Stabilität', '') == 'STABIL')

    if not already_stable:
        print(f"   🔍 PHASE 1: Stabilitätsanalyse für {name}...")
        write_ph_input(ph_in, nq=current_nq, elph=False)
        if os.path.exists(ph_out): os.remove(ph_out)
        phase1_res = execute_ph_phase(is_elph_phase=False)
        if phase1_res != "DONE": return phase1_res

        min_f, stab = "-", "Unbekannt"
        with open(ph_out, 'r') as f:
            freqs = re.findall(r"freq\s+\(\s*\d+\)\s+=\s+([0-9\.\-]+)\s+\[THz\]", f.read())
            if freqs:
                min_f = min(float(x) for x in freqs)
                stab  = "STABIL" if min_f > -0.05 else "INSTABIL"

        if stab == "INSTABIL":
            print(f"   🛑 Material ist INSTABIL (Min Freq, {min_f} THz). Überspringe El-Ph.")
            return "DONE"
        print(f"   ✅ Material ist STABIL (Min Freq, {min_f} THz). Gehe zu Phase 2...")
        update_csv(name, "Fertig (Metall)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)

    # --- PHASE 2 VORBEREITUNG ---
    print(f"   ⚛️ PHASE 2 Vorbereitung für {name}: Lösche alle alten Dateien...")
    tmp_path = os.path.join(work_dir, "tmp")
    for p in [os.path.join(tmp_path, "_ph0"),
               os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
        if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
    for f in glob.glob(os.path.join(tmp_path, "*.a2Fsave*")): 
        try: os.remove(f)
        except: pass
    for f in glob.glob(os.path.join(tmp_path, "*.dvscf*")):
        try: os.remove(f)
        except: pass
    if os.path.exists(ph_out): os.remove(ph_out)

    write_ph_input(ph_in, nq=current_nq, elph=True)
    print("   ⚛️ PHASE 2: Berechne Elektron-Phonon-Kopplung...")
    return execute_ph_phase(is_elph_phase=True)

    def execute_ph_phase(is_elph_phase=False):
        ph_cores = get_scf_cores(scf_out, DEFAULT_CORES)
        print(f"      🧠 Erbe Kernanzahl von SCF: Starte mit {ph_cores} Core(s).")
        
        phonon_attempts = 0
        aainit_1core_done = False
    
        while phonon_attempts < 3:
            phonon_attempts += 1
            ph_res = run_monitored_ph(ph_in, ph_out, work_dir, ph_cores)
            
            if ph_res == "DONE": return "DONE"
    
            phase_name = "El-Ph" if is_elph_phase else "Stabilität"
            print(f"      ⚠️ Phonon Crash/OOM! (Phase: {phase_name})")
            crash_reason = analyze_crash_reason(ph_out)
            print_error_log(ph_out, "PHONON ERROR LOG")
    
            # NEU: Wellenfunktionen fehlen — erst collect-SCF versuchen
            if crash_reason == "WFC_MISSING":
                print("      🌊 Wellenfunktionen fehlen -> starte Collect-SCF (nstep=0)...")
                if run_cleanup_scf(scf_in, work_dir, ph_cores):
                    print("      ✅ Collect-SCF OK -> Phononen neu starten.")
                    if os.path.exists(ph_out): os.remove(ph_out)
                    phonon_attempts -= 1  # nicht zählen
                    continue
                else:
                    print("      ❌ Collect-SCF fehlgeschlagen -> vollständiger SCF-Reset.")
                    tmp_save = os.path.join(work_dir, "tmp")
                    if os.path.exists(tmp_save): shutil.rmtree(tmp_save, ignore_errors=True)
                    if os.path.exists(scf_out): os.remove(scf_out)
                    update_csv(name, "SCF_RESET (WFC Missing)")
                    return "SCF_RESET"
    
            if crash_reason == "AAINIT_ERROR":
                if ph_cores > 1 and not aainit_1core_done:
                    print("      🔩 aainit-Fehler auf 2 Cores -> LÖSCHE _ph0 und wechsle auf 1 Core.")
                    ph_cores = 1
                    aainit_1core_done = True
                    ph0_path = os.path.join(work_dir, "tmp", "_ph0")
                    if os.path.exists(ph0_path): shutil.rmtree(ph0_path, ignore_errors=True)
                    chkpt_path = os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")
                    if os.path.exists(chkpt_path): shutil.rmtree(chkpt_path, ignore_errors=True)
                    if os.path.exists(ph_out): os.remove(ph_out)
                    phonon_attempts -= 1
                    continue
                else:
                    print("      🔩 aainit-Fehler unlösbar -> System-Komplexität zu hoch. Skippe.")
                    update_csv(name, f"SKIPPED (Phonon OOM, Phase {phase_name})")
                    git_sync(f"Phonon OOM, {name}")
                    return "CRASH"
    
            if crash_reason == "HARD":
                if os.path.exists(ph_out):
                    try:
                        ph_content_check = open(ph_out, errors='ignore').read()
                        if "bad line in namelist" in ph_content_check:
                            print("      📝 Namelist-Fehler -> schreibe ph.in komplett neu.")
                            nq_match = re.search(r"nq1\s*=\s*(\d+).*?nq2\s*=\s*(\d+).*?nq3\s*=\s*(\d+)", ph_content_check, re.DOTALL)
                            nq = "2,2,2"
                            if nq_match: nq = f"{nq_match.group(1)},{nq_match.group(2)},{nq_match.group(3)}"
                            write_ph_input(ph_in, tr2="1.0d-14", nq=nq, elph=is_elph_phase)
                            if os.path.exists(ph_out): os.remove(ph_out)
                            phonon_attempts -= 1
                            continue
                    except: pass
    
            if crash_reason == "XML_ERROR":
                print("      🧨 XML korrupt -> SCF-Reset.")
                tmp_save = os.path.join(work_dir, "tmp")
                if os.path.exists(tmp_save): shutil.rmtree(tmp_save, ignore_errors=True)
                if os.path.exists(scf_out):  os.remove(scf_out)
                update_csv(name, "SCF_RESET (XML Error)")
                return "SCF_RESET"
    
            if crash_reason in ["SYMMETRY_ERROR", "FFT_SYMMETRY_ERROR"]:
                print("      🧩 Symmetrie-Problem -> nosym injizieren + SCF-Reset.")
                source_in = os.path.join(INPUTS_DIR, f"{name}.in")
                if os.path.exists(source_in):
                    with open(source_in, 'r') as f: c = f.read()
                    if "nosym" not in c:
                        c = c.replace("&SYSTEM", "&SYSTEM\n nosym=.true.,")
                        with open(source_in, 'w') as f: f.write(c)
                if os.path.exists(work_dir): shutil.rmtree(work_dir, ignore_errors=True)
                update_csv(name, "SCF_RESET (Sym Error)")
                return "SCF_RESET"
    
            if crash_reason == "DAVCIO_ERROR" or is_recoverable_fragmentation_error(ph_out):
                print("      🤕 Fragmentierung -> 'Collect-Recovery'...")
                if run_cleanup_scf(scf_in, work_dir, int(DEFAULT_CORES)):
                    print("      👍 Recovery OK -> Phononen neu starten.")
                    if os.path.exists(ph_out): os.remove(ph_out)
                    continue
                print("      👎 Recovery fehlgeschlagen.")
    
            print(f"      🛡️ Phonon-Recovery, Versuch {phonon_attempts}/3")
    
            if phonon_attempts == 1:
                print("      📉 tr2_ph=1.0d-12")
                with open(ph_in, 'r') as f: c = f.read()
                c = re.sub(r"tr2_ph\s*=\s*[0-9\.dD\-]+", "tr2_ph=1.0d-12", c)
                with open(ph_in, 'w') as f: f.write(c)
                if os.path.exists(ph_out): os.remove(ph_out)
                continue
    
            elif phonon_attempts == 2:
                print("      🚨 NOTFALL-MODUS, Grid=1x1x1, Sym=OFF, tr2_ph=1.0d-10")
                write_ph_input(ph_in, tr2="1.0d-10", nq="1,1,1", search_sym=False, elph=is_elph_phase)
                for cleanup_path in [os.path.join(work_dir, "tmp", "_ph0"),
                                      os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                    if os.path.exists(cleanup_path): shutil.rmtree(cleanup_path, ignore_errors=True)
                if os.path.exists(ph_out): os.remove(ph_out)
                continue
    
        print("      ❌ Phononen endgültig fehlgeschlagen.")
        update_csv(name, "SKIPPED (Phonon Crash)")
        git_sync(f"Phonon Crash, {name}")
        return "CRASH"
    
        # -------------------------------------------------------------
        # INIT: Lese aktuelles Grid aus bestehender ph.in (falls existent)
        # -------------------------------------------------------------
        current_nq = "2,2,2"
        is_phase2 = False
        
        if os.path.exists(ph_in):
            with open(ph_in, 'r') as f:
                content = f.read()
                if "electron_phonon" in content: is_phase2 = True
                nq_match = re.search(r"nq1\s*=\s*(\d+).*?nq2\s*=\s*(\d+).*?nq3\s*=\s*(\d+)", content, re.DOTALL)
                if nq_match: current_nq = f"{nq_match.group(1)},{nq_match.group(2)},{nq_match.group(3)}"
    
        row_data = get_csv_full_info(name)
        already_stable = (row_data.get('Stabilität', '') == 'STABIL')
    
        # -------------------------------------------------------------
        # PHASE 1: Reine Stabilitätsanalyse (Kein dvscf, kein el-ph)
        # -------------------------------------------------------------
        if not already_stable:
            print(f"   🔍 PHASE 1: Stabilitätsanalyse für {name}...")
            
            write_ph_input(ph_in, nq=current_nq, elph=False)
            if os.path.exists(ph_out): os.remove(ph_out)
            
            phase1_res = execute_ph_phase(is_elph_phase=False)
            if phase1_res != "DONE": return phase1_res
    
            min_f, stab = "-", "Unbekannt"
            with open(ph_out, 'r') as f:
                content = f.read()
                freqs = re.findall(r"freq\s+\(\s*\d+\)\s+=\s+([0-9\.\-]+)\s+\[THz\]", content)
                if freqs:
                    min_f = min(float(x) for x in freqs)
                    stab  = "STABIL" if min_f > -0.05 else "INSTABIL"
    
            if stab == "INSTABIL":
                print(f"   🛑 Material ist INSTABIL (Min Freq, {min_f} THz). Überspringe El-Ph.")
                return "DONE"
                
            print(f"   ✅ Material ist STABIL (Min Freq, {min_f} THz). Gehe zu Phase 2...")
            update_csv(name, "Fertig (Metall)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
    
        # -------------------------------------------------------------
        # PHASE 2: Elektron-Phonon-Kopplung (Nur für stabile Materialien)
        # -------------------------------------------------------------
        print(f"   ⚛️ PHASE 2 Vorbereitung für {name}: Lösche alle alten Dateien...")
        tmp_path = os.path.join(work_dir, "tmp")
        
        ph0_path = os.path.join(tmp_path, "_ph0")
        if os.path.exists(ph0_path): shutil.rmtree(ph0_path, ignore_errors=True)
        
        chkpt_path = os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")
        if os.path.exists(chkpt_path): shutil.rmtree(chkpt_path, ignore_errors=True)
        
        for f in glob.glob(os.path.join(tmp_path, "*.a2Fsave*")): os.remove(f)
        for f in glob.glob(os.path.join(tmp_path, "*.dvscf*")): os.remove(f)
    
        if os.path.exists(ph_out): os.remove(ph_out)
        
        write_ph_input(ph_in, nq=current_nq, elph=True)
        print("   ⚛️ PHASE 2: Berechne Elektron-Phonon-Kopplung...")
        return execute_ph_phase(is_elph_phase=True)

def cleanup_system_memory():
    """Tötet hängengebliebene QE-Prozesse und leert das Shared Memory."""
    print("      🧹 Bereinige Zombie-Prozesse und Shared Memory (/dev/shm)...")
    
    target_procs = ['pw.x', 'ph.x', 'dos.x', 'q2r.x', 'matdyn.x']
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'] in target_procs:
                proc.kill()
        except:
            pass

    shm_dir = "/dev/shm"
    if os.path.exists(shm_dir):
        for item in os.listdir(shm_dir):
            item_path = os.path.join(shm_dir, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except:
                pass
                
    print("      ✅ System-RAM und Prozesse sind sauber.")
    
def apply_aainit_workaround(input_file):
    """Reduziert ecutwfc/ecutrho um aainit zu umgehen."""
    with open(input_file, 'r') as f: content = f.read()
    # ecutwfc von 50 auf 40 Ry reduzieren
    content = re.sub(r"ecutwfc\s*=\s*[0-9\.]+", "ecutwfc = 40.0", content)
    content = re.sub(r"ecutrho\s*=\s*[0-9\.]+", "ecutrho = 320.0", content)
    with open(input_file, 'w') as f: f.write(content)
    print("      🔧 aainit-Workaround: ecutwfc=40, ecutrho=320.")
    
# =============================================================================
# 8. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        set_logic_app_state("Enabled")
        
        if not os.path.exists(TXT_LOG_FILE):
            with open(TXT_LOG_FILE, 'w', encoding='utf-8') as f:
                f.write(f"--- Init {datetime.now().strftime('%Y-%m-%d %H:%M')} ---\n")
            git_sync("📄 pipeline_output.txt initialisiert")

        truncate_log(TXT_LOG_FILE, max_size_mb=1.0)
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        with open(TXT_LOG_FILE, "a") as f:
            f.write(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {ts}\n{'='*40}\n")
        print(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {ts}\n{'='*40}\n")

        # Initiale Grundreinigung beim Hochfahren
        cleanup_system_memory()

        if os.path.exists(SIGNAL_FILE):
            os.remove(SIGNAL_FILE)
            git_sync("🧹 rechnung_fertig.txt gelöscht (Neuer Start)")

        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)

        input_files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.in")))
        send_notification(f"Start, {len(input_files)} Jobs.")
        git_sync("🚀 Start")

        for input_file in input_files:
            name     = os.path.basename(input_file).replace(".in", "")
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            scf_out  = os.path.join(work_dir, "scf.out")

            row_data    = get_csv_full_info(name)
            last_status = row_data.get('Status', 'NEW')        if row_data else 'NEW'
            stability   = str(row_data.get('Stabilität','-')).strip() if row_data else '-'
            tc_status   = str(row_data.get('Tc (K)','-')).strip()      if row_data else '-'
            lam_status  = str(row_data.get('Lambda','-')).strip()      if row_data else '-'

            if not stability: stability = "-"
            if not tc_status: tc_status = "-"
            if not lam_status: lam_status = "-"

            if "Isolator" in last_status:
                cleanup_heavy_files(work_dir, name)
                update_csv(name, last_status)
                print(f"⏩ Skip {name} (Isolator)")
                continue

            if stability == "INSTABIL":
                cleanup_heavy_files(work_dir, name)
                update_csv(name, last_status)
                print(f"⏩ Skip {name} (INSTABIL)")
                continue

            if stability == "STABIL" and tc_status != "-" and lam_status != "-":
                update_csv(name, last_status)
                print(f"⏩ Skip {name} (vollständig, Tc={tc_status}K)")
                continue

            if "SKIPPED" in last_status or "ERROR" in last_status or "SCF_RESET" in last_status:

                if "Max BFGS" in last_status:
                    cur_iter = get_last_iteration(scf_out)
                    if cur_iter < MAX_BFGS_STEPS:
                        print(f"🔄 Reaktiviere {name} ({cur_iter} Steps < Limit {MAX_BFGS_STEPS})")
                    else:
                        update_csv(name, last_status)
                        print(f"⏩ Skip {name} (BFGS-Limit {cur_iter} erreicht)")
                        continue

                elif "Permanent Crash" in last_status:
                    print(f"🔄 Reaktiviere {name} nach Permanent Crash (Reset)...")
                    if os.path.exists(work_dir):
                        shutil.rmtree(work_dir, ignore_errors=True)
                        git_sync(f"Cleaned corrupted RUN dir, {name}")
                        
                elif "OOM Limit" in last_status or "Phonon OOM" in last_status:
                    if "OOM Limit" in last_status:
                        print(f"🔄 Reaktiviere {name} nach OOM Limit (Kompletter VM-Reset)...")
                        if os.path.exists(work_dir):
                            shutil.rmtree(work_dir, ignore_errors=True)
                    else:
                        print(f"🔄 Reaktiviere {name} nach Phonon OOM...")

                elif "Phonon Crash" in last_status:
                    print(f"🔄 Reaktiviere {name} nach Phonon Crash...")

                elif "Non-Conv" in last_status:
                    update_csv(name, last_status)
                    print(f"⏩ Skip {name} (Non-Convergence, dauerhaft)")
                    continue

                elif "SCF_RESET" in last_status:
                    print(f"🔄 Reaktiviere {name} nach SCF_RESET...")
                    if os.path.exists(work_dir):
                        tmp_p = os.path.join(work_dir, "tmp")
                        if os.path.exists(tmp_p):
                            shutil.rmtree(tmp_p, ignore_errors=True)

                elif "ERROR" in last_status:
                    print(f"🔄 Reaktiviere {name} nach ERROR (bereinige El-Ph)...")
                    if os.path.exists(work_dir):
                        for ext in ["q2r.out", "matdyn.out", "*.fc", "*.freq", "*.phdos"]:
                            for fd in glob.glob(os.path.join(work_dir, ext)):
                                try: os.remove(fd)
                                except: pass

                else:
                    update_csv(name, last_status)
                    print(f"⏩ Skip {name} (Unbekannter Status, {last_status})")
                    continue

            if stability == "STABIL":
                print(f"🔄 Fortsetzen {name} (STABIL, El-Ph fehlt)...")
            if "Metall" in last_status and stability == "-":
                print(f"🔄 Retry Phonon {name} (Metall, Stabilität unbekannt)...")

            try:
                if not os.path.exists(work_dir): os.makedirs(work_dir)
                print(f"\n💎 Job, {name}")

                scf_in  = os.path.join(work_dir, "scf.in")
                dos_in  = os.path.join(work_dir, "dos.in")
                dos_out = os.path.join(work_dir, f"{name}.dos")
                ph_in   = os.path.join(work_dir, "ph.in")
                ph_out  = os.path.join(work_dir, "ph.out")

                if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)

                scf_already_done = (os.path.exists(scf_out) and
                    "JOB DONE" in open(scf_out, errors='ignore').read())

                if not scf_already_done and "Phonon Crash" in last_status:
                    if os.path.exists(scf_out):
                        if analyze_crash_reason(scf_out) == "DONE":
                            print("   ✅ SCF bereits fertig (skip SCF-Phase).")
                            scf_already_done = True

                if not scf_already_done:
                    update_csv(name, "Rechnet SCF...")
                    scf_result = run_scf_block(name, work_dir, scf_in, scf_out)
                    if scf_result != "DONE":
                        git_sync(f"Failed SCF, {name} ({scf_result})")
                        continue

                if analyze_crash_reason(scf_out) != "DONE":
                    git_sync(f"Failed, {name}")
                    continue

                print(f"   ✅ SCF fertig, {name}")

                with open(scf_in, 'r') as f:
                    m      = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", f.read())
                    prefix = m.group(1) if m else "calc"

                e_fermi = "-"
                if os.path.exists(scf_out):
                    with open(scf_out, 'r', errors='ignore') as f:
                        m = re.search(r"the Fermi energy is\s+([0-9\.\-]+)\s+ev",
                                      f.read())
                        if m: e_fermi = float(m.group(1))

                update_csv(name, "Rechnet DOS...", e_fermi=e_fermi)
                if not os.path.exists(dos_out):
                    with open(dos_in, "w") as f:
                        f.write(f"&DOS\n prefix='{prefix}', outdir='./tmp',"
                                f" fildos='{name}.dos',"
                                f" Emin=-20.0, Emax=30.0, DeltaE=0.1 /\n")
                    with open(dos_in, "r") as fi, open(dos_out, "w") as fo:
                        subprocess.run([DOS_EXE], stdin=fi, stdout=fo,
                                       stderr=subprocess.STDOUT, cwd=work_dir)

                is_metal, dos_val = False, 0.0
                if os.path.exists(dos_out) and e_fermi != "-":
                    closest_diff = 99.9
                    with open(dos_out, 'r') as f:
                        for line in f:
                            if line.strip().startswith("#"): continue
                            p = line.split()
                            if len(p) >= 2:
                                try:
                                    e, d = float(p[0]), float(p[1])
                                    if abs(e - e_fermi) < closest_diff:
                                        closest_diff = abs(e - e_fermi)
                                        dos_val = d
                                except: continue
                    is_metal = dos_val > DOS_THRESHOLD

                if not is_metal:
                    print(f"   🛑 Isolator (DOS={dos_val:.3f}).")
                    update_csv(name, "Fertig (Isolator)", e_fermi,
                               round(dos_val, 4), "NEIN")
                    cleanup_heavy_files(work_dir, name)
                    git_sync(f"Fertig, {name} (Isolator)")
                    continue

                print(f"   ⚡ Metall (DOS={dos_val:.3f}). Berechne Phononen...")
                update_csv(name, "Rechnet Phononen...", e_fermi,
                           round(dos_val, 4), "JA")

                elph_files = (glob.glob(os.path.join(work_dir, "elph_dir", "a2Fq2r.*")) +
                              glob.glob(os.path.join(work_dir, "a2Fq2r.*")))

                phonon_already_done = (os.path.exists(ph_out) and
                    "JOB DONE" in open(ph_out, errors='ignore').read())

                if phonon_already_done and not elph_files:
                    print("      ⚠️ JOB DONE aber a2Fq2r fehlt -> Neustart für El-Ph.")
                    try: os.remove(ph_out)
                    except: pass
                    phonon_already_done = False

                if not phonon_already_done:
                    ph_result = run_phonon_block(
                        name, work_dir, scf_in, scf_out,
                        ph_in, ph_out, e_fermi, dos_val)
                    if ph_result != "DONE": continue

                min_f, stab = "-", "Unbekannt"
                if os.path.exists(ph_out):
                    with open(ph_out, 'r') as f:
                        content = f.read()
                        if "JOB DONE" in content:
                            freqs = re.findall(
                                r"freq\s+\(\s*\d+\)\s+=\s+([0-9\.\-]+)\s+\[THz\]",
                                content)
                            if freqs:
                                min_f = min(float(x) for x in freqs)
                                stab  = "STABIL" if min_f > -0.05 else "INSTABIL"

                if stab == "INSTABIL":
                    update_csv(name, "Fertig (Metall)", e_fermi,
                               round(dos_val, 4), "JA", min_f=min_f, stab=stab)
                    cleanup_heavy_files(work_dir, name)
                    git_sync(f"Fertig, {name} (INSTABIL)")
                    continue

                if stab == "STABIL":
                    q2r_in     = os.path.join(work_dir, "q2r.in")
                    q2r_out    = os.path.join(work_dir, "q2r.out")
                    matdyn_in  = os.path.join(work_dir, "matdyn.in")
                    matdyn_out = os.path.join(work_dir, "matdyn.out")

                    if lam_status == "-" or tc_status == "-":
                        if os.path.exists(q2r_out):    os.remove(q2r_out)
                        if os.path.exists(matdyn_out): os.remove(matdyn_out)

                    update_csv(name, "Rechnet El-Ph (Q2R)...", e_fermi,
                               round(dos_val, 4), "JA", min_f=min_f, stab=stab)

                    if not (os.path.exists(q2r_out) and
                            "JOB DONE" in open(q2r_out, errors='ignore').read()):
                        print("   4️⃣  Q2R...")
                        with open(q2r_in, "w") as f:
                            f.write(f"&input\n fildyn='{name}.dyn',\n"
                                    f" zasr='simple',\n flfrc='{name}.fc',\n"
                                    f" la2F=.true.\n/\n")
                        with open(q2r_in, "r") as fi, open(q2r_out, "w") as fo:
                            subprocess.run([Q2R_EXE], stdin=fi, stdout=fo,
                                           stderr=subprocess.STDOUT, cwd=work_dir)

                    if not (os.path.exists(q2r_out) and
                            "JOB DONE" in open(q2r_out, errors='ignore').read()):
                        print("      ❌ Q2R fehlgeschlagen!")
                        print_error_log(q2r_out, "Q2R ERROR LOG")
                        update_csv(name, "ERROR (Q2R Crash)")
                        git_sync(f"Q2R Crash, {name}")
                        continue

                    update_csv(name, "Rechnet El-Ph (Matdyn)...", e_fermi,
                               round(dos_val, 4), "JA", min_f=min_f, stab=stab)

                    if not (os.path.exists(matdyn_out) and
                            "JOB DONE" in open(matdyn_out, errors='ignore').read()):
                        print("   5️⃣  Matdyn...")
                        with open(matdyn_in, "w") as f:
                            f.write(f"&input\n asr='simple',\n flfrc='{name}.fc',\n"
                                    f" flfrq='{name}.freq',\n fildyn='{name}.dyn',\n"
                                    f" dos=.true.,\n elph=.true.,\n"
                                    f" fildos='{name}.phdos',\n"
                                    f" nk1=10, nk2=10, nk3=10\n/\n")
                        with open(matdyn_in, "r") as fi, open(matdyn_out, "w") as fo:
                            subprocess.run([MATDYN_EXE], stdin=fi, stdout=fo,
                                           stderr=subprocess.STDOUT, cwd=work_dir)

                    if not (os.path.exists(matdyn_out) and
                            "JOB DONE" in open(matdyn_out, errors='ignore').read()):
                        print("      ❌ Matdyn fehlgeschlagen!")
                        print_error_log(matdyn_out, "MATDYN ERROR LOG")
                        update_csv(name, "ERROR (Matdyn Crash)")
                        git_sync(f"Matdyn Crash, {name}")
                        continue

                    lam, wlog, tc = "-", "-", "-"
                    if os.path.exists(matdyn_out):
                        with open(matdyn_out, 'r', errors='ignore') as f:
                            mc = f.read()
                            if "JOB DONE" in mc:
                                ml = re.search(r"lambda\s*=\s*([0-9\.]+)", mc)
                                mw = re.search(r"omega_log\s*=\s*([0-9\.]+)", mc)
                                if ml and mw:
                                    lam  = ml.group(1)
                                    wlog = mw.group(1)
                                    tc_v = berechne_tc(wlog, lam)
                                    if tc_v != "-": tc = round(tc_v, 3)

                    update_csv(name, "Fertig (Metall)", e_fermi,
                               round(dos_val, 4), "JA",
                               min_f=min_f, stab=stab, lam=lam, wlog=wlog, tc=tc)
                    git_sync(f"Fertig, {name} (Tc={tc}K)")
                    
                    cleanup_heavy_files(work_dir, name)

            except Exception as job_err:
                print(f"🚨 Fehler bei {name}, {job_err}")
                traceback.print_exc()
                update_csv(name, f"ERROR (Python, {str(job_err)[:30]})")
                continue

        send_notification("🎉 Warteschlange komplett abgearbeitet.")
        set_logic_app_state("Disabled")
        with open(SIGNAL_FILE, "w") as f:
            f.write(f"Status, Fertig\nTimestamp, {time.ctime()}")
        git_sync("🏁 Pipeline vollständig beendet")
        if os.name != 'nt':
            if is_ssh_session_active():
                print("🛑 Shutdown blockiert (Aktive SSH-Sitzung erkannt!)")
                git_sync("🛑 Shutdown blockiert (SSH aktiv)")
            else:
                os.system("sudo shutdown -h now")

    except Exception as e:
        full_error = (f"\n\n🚨 KRITISCHER ABSTURZ ({datetime.now()})\n"
                      f"{e}\n{traceback.format_exc()}\n")
        with open(TXT_LOG_FILE, "a") as f: f.write(full_error)
        send_notification(f"🚨 KRITISCHER FEHLER, {e} -> Shutdown.")
        set_logic_app_state("Disabled")
        if os.name != 'nt':
            if is_ssh_session_active():
                print("🛑 Shutdown blockiert (Aktive SSH-Sitzung erkannt!)")
                git_sync("🛑 Shutdown blockiert (SSH aktiv)")
            else:
                os.system("sudo shutdown -h now")
        sys.exit()
        
def is_ssh_session_active():
    try:
        output = subprocess.check_output(["who"]).decode("utf-8")
        return "pts/" in output or "tty" in output
    except Exception:
        return False

if __name__ == "__main__":
    main()