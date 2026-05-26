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
# 0. LIVE-LOGGING & LOG-TRUNCATION
# =============================================================================
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

def truncate_log(log_path, max_size_mb=1.0):
    """Kürzt das Logfile auf max_size_mb, behält den neuesten Teil."""
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

def get_scf_cores(scf_out_path, default_cores=4):
    """Liest aus der scf.out aus, mit wie vielen Cores die Rechnung beendet wurde."""
    if not os.path.exists(scf_out_path):
        return int(default_cores)
    try:
        with open(scf_out_path, 'r', errors='ignore') as f:
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

# Angepasst an Standard D4s v5 — 4 produktive Kerne, 2 als sicherer Fallback
DEFAULT_CORES        = "4"
SAFE_CORES           = "2"
MEMORY_LIMIT_PERCENT = 92.0
MAX_BFGS_STEPS       = 100
MAX_RETRIES_LEVEL    = 3
ERROR_LOG_LINES      = 30

FORCE_RETRY_LIST = []  # Namen von Jobs die einen erzwungenen Neustart bekommen

WORK_DIR    = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR  = os.path.join(WORK_DIR, "Inputs")
PSEUDO_DIR  = os.path.join(WORK_DIR, "pseudo")
SIGNAL_FILE = os.path.join(WORK_DIR, "rechnung_fertig.txt")
CSV_FILE    = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")

TXT_LOG_FILE    = os.path.join(WORK_DIR, "pipeline_output.txt")
BACKUP_LOG_FILE = os.path.join(WORK_DIR, "pipeline_output_backup.txt")

PW_EXE     = shutil.which("pw.x")     or "/usr/bin/pw.x"
PH_EXE     = shutil.which("ph.x")     or "/usr/bin/ph.x"
DOS_EXE    = shutil.which("dos.x")    or "/usr/bin/dos.x"
Q2R_EXE    = shutil.which("q2r.x")    or "/usr/bin/q2r.x"
MATDYN_EXE = shutil.which("matdyn.x") or "/usr/bin/matdyn.x"

# =============================================================================
# 2. HELFER & GIT & BACKUP
# =============================================================================
def backup_log_file():
    truncate_log(TXT_LOG_FILE, max_size_mb=1.0)
    if os.path.exists(TXT_LOG_FILE):
        try: shutil.copy(TXT_LOG_FILE, BACKUP_LOG_FILE)
        except: pass

def send_notification(message):
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ HPC, {message}"}
        requests.post(url, data=payload, timeout=10)
    except: pass

def set_logic_app_state(state="Enabled"):
    if not shutil.which("az"): return
    try:
        subprocess.run(
            ["az", "logic", "workflow", "set-state",
             "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME,
             "--state", state],
            capture_output=True, timeout=30)
    except: pass

def initial_git_pull():
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        subprocess.run(
            ["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"],
            cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
    except Exception as e:
        print(f"⚠️ Initialer Git Pull Fehler: {e}")

def git_sync(message):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    lock_file = os.path.join(WORK_DIR, ".git", "index.lock")
    try:
        # Stale lock entfernen (sicher wenn kein Git-Prozess aktiv ist)
        if os.path.exists(lock_file):
            lock_age = time.time() - os.path.getmtime(lock_file)
            if lock_age > 60:
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
    """Aktualisiert oder erstellt einen Eintrag in der CSV-Datei.
       Neue Felder Lambda, Omega_log, Tc werden für die Tc-Berechnung benötigt."""
    fieldnames = ['Name', 'Status', 'Fermi Energie (eV)', 'DOS @ Fermi', 'Metall?',
                  'Min Freq (THz)', 'Stabilität', 'Lambda', 'Omega_log (K)', 'Tc (K)',
                  'Timestamp']
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
                        'Timestamp': datetime.now().strftime("%Y-%m-%d %H,%M")})
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
        rows.append({'Name': name, 'Status': status,
                     'Fermi Energie (eV)': str(e_fermi), 'DOS @ Fermi': str(dos_val),
                     'Metall?': str(is_metal), 'Min Freq (THz)': str(min_f),
                     'Stabilität': str(stab), 'Lambda': str(lam),
                     'Omega_log (K)': str(wlog), 'Tc (K)': str(tc),
                     'Timestamp': datetime.now().strftime("%Y-%m-%d %H,%M")})

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
        job_marker = f"💎 Job, {job_name}"
        for line in reversed(lines):
            if job_marker in line: count += 1
            elif "💎 Job," in line and job_name not in line: break
    except: return 1
    return max(1, count)

def berechne_tc(omega_log_K, lambda_ep, mu_star=0.13):
    """McMillan-Allen-Dynes Formel zur Tc-Abschätzung (in Kelvin)."""
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
    """Löscht große temporäre Dateien (tmp, dvscf) nach erfolgreicher Analyse."""
    if not force:
        row_data = get_csv_full_info(name)
        if not row_data:
            print(f"      ⚠️ Cleanup abgebrochen: {name} nicht in CSV.")
            return
        status = row_data.get('Status', '')
        tc_val = row_data.get('Tc (K)', '-')
        stab   = row_data.get('Stabilität', '-')
        is_finished = ("Isolator" in status) or (stab == "INSTABIL") or (tc_val not in ["-", ""])
        if not is_finished:
            print(f"      ⚠️ Cleanup blockiert: {name} noch nicht final gesichert.")
            return

    deleted_something = False
    for dvscf_file in glob.glob(os.path.join(work_dir, "*dvscf*")):
        try: os.remove(dvscf_file); deleted_something = True
        except: pass
    for path in [os.path.join(work_dir, p)
                 for p in ["tmp", "tmp_SAFE_CHECKPOINT", "tmp_SAFE_PHONON_CHECKPOINT"]]:
        if os.path.exists(path):
            try: shutil.rmtree(path, ignore_errors=True); deleted_something = True
            except Exception as e: print(f"      ⚠️ Konnte {path} nicht löschen: {e}")

    if deleted_something:
        print(f"      🧹 Heavy Files & dvscf für {name} bereinigt.")
        git_sync(f"Cleanup Heavy Files: {name}")

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

def cleanup_system_memory():
    """Tötet hängengebliebene QE-Prozesse und leert das Shared Memory (/dev/shm)."""
    print("      🧹 Bereinige Zombie-Prozesse und /dev/shm...")
    target_procs = ['pw.x', 'ph.x', 'dos.x', 'q2r.x', 'matdyn.x']
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'] in target_procs: proc.kill()
        except: pass
    shm_dir = "/dev/shm"
    if os.path.exists(shm_dir):
        for item in os.listdir(shm_dir):
            item_path = os.path.join(shm_dir, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except: pass
    print("      ✅ System-RAM und Prozesse sauber.")

def is_ssh_session_active():
    """Prüft ob eine aktive SSH-Sitzung läuft, um versehentlichen Shutdown zu verhindern."""
    try:
        output = subprocess.check_output(["who"]).decode("utf-8")
        return "pts/" in output or "tty" in output
    except: return False

def apply_aainit_workaround(input_file):
    """Reduziert ecutwfc/ecutrho um den aainit-Fehler zu umgehen."""
    with open(input_file, 'r') as f: content = f.read()
    content = re.sub(r"ecutwfc\s*=\s*[0-9\.]+", "ecutwfc = 40.0", content)
    content = re.sub(r"ecutrho\s*=\s*[0-9\.]+", "ecutrho = 320.0", content)
    with open(input_file, 'w') as f: f.write(content)
    print("      🔧 aainit-Workaround: ecutwfc=40, ecutrho=320.")

def deallocate_vm():
    if not shutil.which("az"):
        print("🛑 Azure CLI nicht gefunden. Verlasse mich auf lokalen Shutdown.")
        return
    try:
        result = subprocess.run(
            ["az", "vm", "deallocate",
             "--resource-group", RESOURCE_GROUP,
             "--name", "Supraleiter-HPC-Knoten"],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"⚠️ Azure CLI Deallocate Fehler: {result.stderr}")
        else:
            print("✅ VM erfolgreich deallokiert.")
    except Exception as e:
        print(f"⚠️ Fehler beim Aufruf der Azure CLI: {e}")

# =============================================================================
# 3. SMART LOGIC & VALIDATION & CRASH-ANALYSE
# =============================================================================
def analyze_crash_reason(output_file):
    if not os.path.exists(output_file): return "NONE"
    try:
        with open(output_file, 'rb') as f:
            try: f.seek(-20000, 2)
            except OSError: f.seek(0)
            lines = f.read().decode('utf-8', errors='ignore')

        if "JOB DONE"                                     in lines: return "DONE"
        if "convergence NOT achieved"                     in lines: return "NON_CONVERGED"
        if "The maximum number of steps has been reached" in lines: return "RESTART_NEEDED"

        lines_lower = lines.lower()

        if ("fatal error reading xml"         in lines_lower or
                "reading output_obj of xsd"   in lines_lower or
                "wrong number of occurrences" in lines_lower):
            print("      🧨 XML-Struktur zerstört (Corruption).")
            return "XML_ERROR"

        if "Wavefunctions in collected format not available" in lines:
            print("      ⚠️ Wellenfunktionen fehlen (wf_collect Error).")
            return "WF_COLLECT_ERROR"

        if ("I/O past end of record" in lines or
                ("End of file" in lines and
                 ("elphon.f90" in lines or "write_rec.f90" in lines))):
            print("      ⚠️ Korrupte Lese-/Schreibdatei (I/O Error).")
            return "CORRUPT_FILE_ERROR"

        if "not orthogonal" in lines and "D_S" in lines:
            print("      🧩 Symmetrie-Fehler (D_S not orthogonal).")
            return "SYMMETRY_ERROR"

        if "FFT grid incompatible with symmetry" in lines:
            print("      🧩 FFT-Gitter Inkompatibilität (Symmetrie-Konflikt).")
            return "FFT_SYMMETRY_ERROR"

        if "error reading file" in lines_lower and "xml" not in lines_lower:
            print("      🤕 Fragmentierungsfehler (davcio).")
            return "DAVCIO_ERROR"

        if "mx dimension too small" in lines_lower:
            print("      🧨 FATAL: Pseudopotential übersteigt QE-Limit. PAW-Pseudo benötigt!")
            return "PSEUDO_ERROR"

        if "aainit" in lines_lower and "mx dimension too small" in lines_lower:
            print("      🔩 aainit-Fehler erkannt (MPI-Bug oder RAM).")
            return "AAINIT_ERROR"

        error_keywords = ["error", "mpi_abort", "segmentation fault",
                          "stopping", "fatal", "diagonalization failed"]
        has_error_msg = any(key in lines_lower for key in error_keywords)

        if has_error_msg: return "HARD"

        ram_match = re.search(
            r"estimated total dynamical ram\s*>\s*([0-9\.]+)\s*(mb|gb)", lines_lower)
        if ram_match:
            if ("self-consistent calculation" not in lines_lower and
                    "iteration #" not in lines_lower):
                return "LIKELY_OOM"

        if "iteration #" in lines_lower or "diagonalization" in lines_lower:
            if not has_error_msg:
                return "LIKELY_OOM"

        return "SOFT"
    except: return "HARD"

def run_monitored_pw(input_file, output_file, cwd, active_cores):
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
        if os.path.exists(output_file) and is_xml_valid(xml_path):
            mode = 'restart'
            print("      ✅ Gültige XML im tmp-Ordner gefunden -> Normaler Restart.")
        elif os.path.exists(output_file) and os.path.exists(checkpoint_dir):
            print("      🛡️ tmp-Ordner defekt! Hole Safe-Checkpoint...")
            try:
                if os.path.exists(tmp_dir): shutil.rmtree(tmp_dir)
                shutil.copytree(checkpoint_dir, tmp_dir)
                if is_xml_valid(xml_path):
                    mode = 'restart'
                    print("      ✅ Checkpoint erfolgreich geladen!")
                else:
                    print("      ❌ Checkpoint war auch defekt. Starte von vorne.")
            except Exception as e:
                print(f"      ❌ Fehler beim Laden des Checkpoints: {e}")
        else:
            print("      🆕 Kein gültiger Speicherstand gefunden -> Starte von vorne (From Scratch).")

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
        cleanup_system_memory()

        with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
            # '-ndiag 1' verhindert BLACS/ScaLAPACK-Probleme bei kleinen Systemen
            cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores),
                   PW_EXE, "-ndiag", "1"]
            print(f"      ⚙️ Starte PWSCF ({mode}, {active_cores} Cores, -ndiag 1)...")
            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out,
                                       stderr=subprocess.STDOUT, cwd=cwd,
                                       start_new_session=True)
            try:
                while process.poll() is None:
                    time.sleep(5)

                    if time.time() - last_checkpoint_time > 900:
                        if is_xml_valid(xml_path):
                            print("      💾 XML valide -> Erstelle Checkpoint...")
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
                                print(f"      ⚠️ Checkpoint fail: {e}")

                    if time.time() - last_git_sync > 3600:
                        print("      ❤️ Git Heartbeat...")
                        git_sync("Log Update (Heartbeat)")
                        backup_log_file()
                        last_git_sync = time.time()

                    try:
                        mem_usage = psutil.virtual_memory().percent
                        if mem_usage > MEMORY_LIMIT_PERCENT:
                            print(f"      ⚠️ RAM NOT-AUS (Python Monitor)!")
                            try: os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                            except: process.kill()
                            return "OOM"
                    except: pass

                    cur_iter = get_last_iteration(output_file)
                    if cur_iter >= MAX_BFGS_STEPS:
                        print(f"      🛑 Limit erreicht ({cur_iter}/{MAX_BFGS_STEPS} BFGS Schritte). Breche ab.")
                        try: os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        except: process.kill()
                        return "MAX_STEPS"

                    if cur_iter > 30: fix_input_file(input_file, cur_iter)

            except:
                try: os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except: process.kill()
                return "CRASH"

        time.sleep(1.5)

        if process.returncode == -9:
            print("      💀 Prozess vom OS getötet (Exit -9 -> Wahrscheinlich OOM).")
            return "OOM"

        if process.returncode != 0:
            reason = analyze_crash_reason(output_file)
            if reason == "DONE":
                print("      ⚠️ MPI-Fehlalarm (JOB DONE gefunden, ignoriere Exit-Code).")
                return "DONE"
            if reason == "RESTART_NEEDED": return "RESTART_NEEDED"
            if reason == "LIKELY_OOM":
                print("      💀 Logfile endet abrupt (Silent Death) -> OOM.")
                return "OOM"
            return "CRASH"

        final_reason = analyze_crash_reason(output_file)
        if final_reason == "DONE":           return "DONE"
        if final_reason == "RESTART_NEEDED": return "RESTART_NEEDED"
        if final_reason == "LIKELY_OOM":     return "OOM"
        return "CRASH"

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
        with open(ph_output_file, 'r', errors='ignore') as f: content = f.read()
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
            print("      ✅ Recovery-Lauf beendet. Daten sollten jetzt consolidated sein.")
            return True
        except Exception as e:
            print(f"      ❌ Recovery fehlgeschlagen: {e}")
            return False

def disable_symmetries_and_reduce_grid(input_file):
    if not os.path.exists(input_file): return
    with open(input_file, 'r') as f: content = f.read()

    if "&INPUTPH" in content:
        if "search_sym" not in content:
            content = content.replace("&INPUTPH", "&INPUTPH\n search_sym=.false.,")

    content = re.sub(r"nq1\s*=\s*\d+", "nq1=1", content)
    content = re.sub(r"nq2\s*=\s*\d+", "nq2=1", content)
    content = re.sub(r"nq3\s*=\s*\d+", "nq3=1", content)

    with open(input_file, 'w') as f: f.write(content)
    print(f"      🛡️ Symmetrien deaktiviert & Grid auf 1x1x1 reduziert.")

def detect_oom_level(input_file):
    if not os.path.exists(input_file): return 0
    with open(input_file, 'r', errors='ignore') as f: content = f.read()
    # Zuerst das eingebettete Level-Tag prüfen (zuverlässiger)
    match = re.search(r"!\s*SMART_OOM_LEVEL\s*=\s*(\d+)", content)
    if match: return int(match.group(1))
    # Fallback auf Keyword-Erkennung
    if "mixing_ndim = 2" in content or "mixing_ndim=2" in content: return 4
    if "mixing_ndim = 3" in content or "mixing_ndim=3" in content: return 3
    if "disk_io='low'" in content or 'disk_io="low"' in content: return 2
    if "diagonalization='cg'" in content or 'diagonalization="cg"' in content: return 1
    return 0

def apply_oom_settings(input_file, level):
    with open(input_file, 'r') as f: content = f.read()
    diag = 'cg'; mix = 4; disk = None
    msg  = "Standard (cg, mix=4)"

    if level >= 1: disk = 'low'; msg = "Stufe 1 (cg, mix=4, disk_io='low')"
    if level >= 2: mix  = 3;     msg = "Stufe 2 (cg, mix=3, disk_io='low')"
    if level >= 3: mix  = 2;     msg = "Stufe 3 (cg, mix=2, disk_io='low')"
    if level >= 4: mix  = 2;     msg = "Stufe 4 (cg, mix=2, disk_io='low', SafeCores)"

    print(f"      📉 Setze RAM-Strategie: {msg}")

    if "diagonalization" in content:
        content = re.sub(r"diagonalization\s*=\s*['\"].*['\"]",
                         f"diagonalization='{diag}'", content)
    else:
        content = content.replace("&ELECTRONS",
                                  f"&ELECTRONS\n diagonalization='{diag}',")

    if "mixing_ndim" in content:
        content = re.sub(r"mixing_ndim\s*=\s*\d+", f"mixing_ndim = {mix}", content)
    else:
        content = content.replace("&ELECTRONS",
                                  f"&ELECTRONS\n mixing_ndim = {mix},")

    # diago_david_ndim explizit setzen — neuere QE-Versionen benötigen das
    ndim = 4 if level == 0 else 2
    if "diago_david_ndim" in content:
        content = re.sub(r"diago_david_ndim\s*=\s*\d+",
                         f"diago_david_ndim = {ndim}", content)
    else:
        content = content.replace("&ELECTRONS",
                                  f"&ELECTRONS\n diago_david_ndim = {ndim},")

    if disk == 'low':
        if "disk_io" in content:
            content = re.sub(r"disk_io\s*=\s*['\"][a-zA-Z]+['\"]", "disk_io='low'", content)
        else:
            content = content.replace("&CONTROL", "&CONTROL\n disk_io='low',")
    else:
        if "disk_io='low'" in content or 'disk_io="low"' in content:
            content = re.sub(r"disk_io\s*=\s*['\"]low['\"],?", "", content)

    # OOM-Level als Kommentar in der Datei speichern für den nächsten Neustart
    if "! SMART_OOM_LEVEL" in content:
        content = re.sub(r"!\s*SMART_OOM_LEVEL\s*=\s*\d+",
                         f"! SMART_OOM_LEVEL={level}", content)
    else:
        content += f"\n! SMART_OOM_LEVEL={level}\n"

    with open(input_file, 'w') as f: f.write(content)
    return True

def fix_input_file(input_file, iteration_count=0):
    with open(input_file, 'r') as f: content = f.read()
    corr_path = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" in content:
        content = re.sub(r"pseudo_dir\s*=\s*['\"].*['\"]",
                         f"pseudo_dir='{corr_path}'", content)
    else:
        content = content.replace("&CONTROL",
                                  f"&CONTROL\n pseudo_dir='{corr_path}',")

    # SSSP-Standard: PAW/NC brauchen hohe Cutoffs (80/800 Ry)
    if "ecutwfc" in content:
        content = re.sub(r"ecutwfc\s*=\s*[0-9\.]+", "ecutwfc = 80.0", content)
    if "ecutrho" in content:
        content = re.sub(r"ecutrho\s*=\s*[0-9\.]+", "ecutrho = 800.0", content)

    # local-TF spart bei Metallen signifikant RAM im Vergleich zu plain mixing
    if "mixing_mode" in content:
        content = re.sub(r"mixing_mode\s*=\s*['\"][a-zA-Z\-]+['\"]",
                         "mixing_mode='local-TF'", content)
    else:
        content = content.replace("&ELECTRONS",
                                  "&ELECTRONS\n mixing_mode='local-TF',")

    # Adaptive mixing_beta: aggressiv am Anfang, vorsichtig bei vielen Iterationen
    target_beta = 0.7
    if iteration_count >= 30: target_beta = 0.4
    if iteration_count >= 60: target_beta = 0.25
    if iteration_count >= 90: target_beta = 0.15

    if "mixing_beta" in content:
        content = re.sub(r"mixing_beta\s*=\s*[0-9\.]+",
                         f"mixing_beta = {target_beta}", content)

    if "electron_maxstep" in content:
        content = re.sub(r"electron_maxstep\s*=\s*\d+", "electron_maxstep = 150", content)
    else:
        content = content.replace("&ELECTRONS",
                                  "&ELECTRONS\n electron_maxstep = 150,")

    with open(input_file, 'w') as f: f.write(content)
    return True

def get_last_iteration(output_file):
    if not os.path.exists(output_file): return 0
    try:
        file_size = os.path.getsize(output_file)
        with open(output_file, 'rb') as f:
            f.seek(max(0, file_size - 10000), 0)
            chunk = f.read().decode('utf-8', errors='ignore')
        bfgs_matches = re.findall(r"number of bfgs steps\s*=\s*(\d+)", chunk)
        scf_matches  = re.findall(r"iteration #\s*(\d+)", chunk)
        if bfgs_matches: return int(bfgs_matches[-1])
        if scf_matches:  return int(scf_matches[-1])
    except: pass
    return 0

# =============================================================================
# 4. PHONON WRAPPER
# =============================================================================
def run_monitored_ph(input_file, output_file, cwd, active_cores):
    """Phonon-Wrapper — bewährte Skript-1-Logik:
       recover=.true. NUR wenn output_file bereits existiert.
       _ph0-Checkpoint wird passiv gesichert, aber NIE automatisch
       geladen (das war der Bug: falscher el-ph-Checkpoint → wrong elph)."""
    last_git_sync        = time.time()
    last_checkpoint_time = time.time()
    ph0_dir        = os.path.join(cwd, "tmp", "_ph0")
    checkpoint_dir = os.path.join(cwd, "tmp_SAFE_PHONON_CHECKPOINT")

    with open(input_file, 'r') as f: content = f.read()

    # *** KERNPRINZIP (wie Skript 1): recover nur wenn output bereits existiert ***
    # NICHT basierend auf _ph0-Existenz — _ph0 könnte von anderem Lauf-Typ stammen!
    if os.path.exists(output_file):
        if "recover" not in content:
            content = content.replace("&INPUTPH", "&INPUTPH\n recover=.true.,")

    run_input = input_file + ".run"
    with open(run_input, 'w') as f: f.write(content)

    file_mode = 'a' if "recover=.true." in content else 'w'
    backup_log_file()
    cleanup_system_memory()

    with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
        cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PH_EXE]
        print(f"      ⚙️ Starte PHONONEN (Cores: {active_cores})...")
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

                # Passiv: _ph0 sichern — nur schreiben, nie lesen/laden
                if current_time - last_checkpoint_time > 1200:
                    if os.path.exists(ph0_dir):
                        try:
                            if os.path.exists(checkpoint_dir):
                                shutil.rmtree(checkpoint_dir)
                            shutil.copytree(ph0_dir, checkpoint_dir)
                            last_checkpoint_time = current_time
                            backup_log_file()
                        except: pass

                try:
                    if psutil.virtual_memory().percent > MEMORY_LIMIT_PERCENT:
                        print("      ⚠️ RAM NOT-AUS!")
                        try: os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        except: process.kill()
                        return "OOM"
                except: pass

        except:
            try: os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except: process.kill()
            return "CRASH"

    time.sleep(1.5)

    if process.returncode == -9:
        print("      💀 OS killed (-9) -> OOM.")
        return "OOM"

    if process.returncode != 0:
        try:
            with open(output_file, 'r', errors='ignore') as f:
                if "JOB DONE" in f.read():
                    print("      ⚠️ MPI-Fehlalarm (JOB DONE gefunden).")
                    return "DONE"
        except: pass
        return "CRASH"

    try:
        with open(output_file, 'r', errors='ignore') as f:
            if "JOB DONE" in f.read(): return "DONE"
    except: pass

    return "CRASH"

# =============================================================================
# 5. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        print("☁️ Führe initialen Git Pull aus...")
        initial_git_pull()

        set_logic_app_state("Enabled")
        truncate_log(TXT_LOG_FILE, max_size_mb=1.0)
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        with open(TXT_LOG_FILE, "a") as f:
            f.write(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE: {ts}\n{'='*40}\n")
        print(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE: {ts}\n{'='*40}\n")

        # Grundreinigung beim Hochfahren
        cleanup_system_memory()

        if os.path.exists(SIGNAL_FILE): os.remove(SIGNAL_FILE)
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)

        input_files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.in")))
        send_notification(f"Start: {len(input_files)} Jobs.")
        git_sync("🚀 Start")

        for input_file in input_files:
            name     = os.path.basename(input_file).replace(".in", "")
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            scf_out  = os.path.join(work_dir, "scf.out")

            # Erzwungener Neustart für bestimmte Jobs (Admin-Tool)
            if name in FORCE_RETRY_LIST:
                print(f"🔄 ERZWUNGENER NEUSTART für {name} (Lösche RUN-Ordner)...")
                if os.path.exists(work_dir):
                    shutil.rmtree(work_dir, ignore_errors=True)
                update_csv(name, "NEW")

            row_data    = get_csv_full_info(name)
            last_status = row_data.get('Status', 'NEW')
            stability   = row_data.get('Stabilität', '-')
            tc_status   = row_data.get('Tc (K)', '-')
            lam_status  = row_data.get('Lambda', '-')

            if "SKIPPED" in last_status:
                print(f"⏩ Überspringe {name} (Status: {last_status})")
                continue

            if "Isolator" in last_status:
                cleanup_heavy_files(work_dir, name)
                print(f"⏩ Überspringe {name} (Ist ein Isolator)")
                continue

            if stability == "INSTABIL":
                cleanup_heavy_files(work_dir, name)
                print(f"⏩ Überspringe {name} (INSTABIL)")
                continue

            if stability == "STABIL" and tc_status not in ["-", ""] and lam_status not in ["-", ""]:
                print(f"⏩ Überspringe {name} (Vollständig: Tc={tc_status}K)")
                continue

            if "Metall" in last_status and stability in ["STABIL", "INSTABIL"] and tc_status in ["-", ""]:
                print(f"🔄 Retry Post-Processing für {name} (Metall, Tc fehlt)...")

            crash_type = analyze_crash_reason(scf_out)
            if crash_type == "NON_CONVERGED":
                update_csv(name, "SKIPPED (Non-Conv)")
                continue
            elif crash_type == "DONE":
                print(f"✅ {name} SCF ist fertig.")

            try:
                if not os.path.exists(work_dir): os.makedirs(work_dir)
                print(f"\n💎 Job: {name}")
                scf_in  = os.path.join(work_dir, "scf.in")
                dos_in  = os.path.join(work_dir, "dos.in")
                dos_out = os.path.join(work_dir, f"{name}.dos")
                ph_in   = os.path.join(work_dir, "ph.in")
                ph_out  = os.path.join(work_dir, "ph.out")

                if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)

                # ================================================================
                # SCF BLOCK
                # ================================================================
                if not (os.path.exists(scf_out) and
                        "JOB DONE" in open(scf_out, errors='ignore').read()):
                    update_csv(name, "Rechnet SCF...")

                    file_level         = detect_oom_level(scf_in)
                    start_crash_reason = analyze_crash_reason(scf_out)

                    if start_crash_reason == "LIKELY_OOM":
                        attempts = count_job_attempts(TXT_LOG_FILE, name)
                        print(f"      🕵️ OOM-Signatur erkannt. Versuch Nr. {attempts} auf Level {file_level}.")

                        if os.path.exists(scf_out):
                            ts_stamp = datetime.now().strftime("%H%M%S")
                            try:
                                os.rename(scf_out, f"{scf_out}.crash_{ts_stamp}")
                                print(f"      👻 Ghost-Protection: scf.out.crash_{ts_stamp}")
                            except: pass

                        if attempts >= MAX_RETRIES_LEVEL:
                            oom_level = file_level + 1
                            print(f"      ❗ Threshold erreicht! Eskaliere Level {file_level} -> {oom_level}.")
                            update_csv(name, f"Recovering (Escalating to Lvl {oom_level})")
                        else:
                            oom_level = file_level
                            print(f"      🔄 Level {file_level} nochmal ({attempts}/{MAX_RETRIES_LEVEL}).")
                            update_csv(name, f"Retrying (Attempt {attempts}/{MAX_RETRIES_LEVEL})")
                    else:
                        oom_level = file_level

                    current_cores       = int(DEFAULT_CORES)
                    if oom_level >= 4: current_cores = int(SAFE_CORES)

                    crash_counter       = 0
                    oom_counter         = 0
                    aainit_ecut_reduced = False

                    while True:
                        apply_oom_settings(scf_in, oom_level)
                        print(f"   1️⃣  SCF ({current_cores} Cores, OOM-Lvl {oom_level})")
                        result = run_monitored_pw(scf_in, scf_out, work_dir, current_cores)

                        if result == "DONE": break

                        elif result == "MAX_STEPS":
                            update_csv(name, "SKIPPED (Max BFGS Steps)")
                            git_sync(f"Skipped {name}, >{MAX_BFGS_STEPS} BFGS Steps")
                            break

                        elif result == "RESTART_NEEDED":
                            update_csv(name, "Rechnet SCF (Fortsetzung)...")
                            print("      🔄 nstep-Limit -> Geometrie-Optimierung fortsetzen...")
                            continue

                        elif result == "OOM":
                            # Erst 3x auf gleichem Level versuchen, dann eskalieren
                            oom_counter += 1
                            if oom_counter < 3:
                                print(f"      ⚠️ OOM Verdacht. Versuch {oom_counter}/3 auf Lvl {oom_level}...")
                                update_csv(name, f"Retrying (OOM Wait {oom_counter}/3)")
                                time.sleep(2)
                                continue
                            oom_level    += 1
                            oom_counter   = 0
                            crash_counter = 0
                            print(f"      ⚠️ OOM-Limit. Eskaliere zu Level {oom_level}...")
                            # Alte Checkpoints löschen — mixing_ndim hat sich geändert
                            for p in [os.path.join(work_dir, "tmp"),
                                       os.path.join(work_dir, "tmp_SAFE_CHECKPOINT")]:
                                if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                            if oom_level == 1: update_csv(name, "Retrying (OOM Lvl 1, CG)")
                            elif oom_level == 2: update_csv(name, "Retrying (OOM Lvl 2, DiskIO)")
                            elif oom_level == 3: update_csv(name, "Retrying (OOM Lvl 3, Mix3)")
                            elif oom_level == 4:
                                update_csv(name, "Retrying (OOM Lvl 4, SafeCores)")
                                current_cores = int(SAFE_CORES)
                            else:
                                update_csv(name, "SKIPPED (OOM Limit)")
                                print("      ❌ System zu komplex für verfügbaren RAM. Skippe.")
                                break
                            continue

                        elif result == "CRASH":
                            reason = analyze_crash_reason(scf_out)
                            print_error_log(scf_out)

                            if reason == "NON_CONVERGED":
                                update_csv(name, "SKIPPED (Non-Conv)")
                                break

                            elif reason == "PSEUDO_ERROR":
                                update_csv(name, "SKIPPED (Pseudo Limit)")
                                print(f"      ❌ Skippe Job wegen inkompatiblem Pseudopotential.")
                                git_sync(f"Skipped {name}, Pseudo Error")
                                break

                            elif reason == "AAINIT_ERROR":
                                if current_cores > int(SAFE_CORES):
                                    print("      🔩 aainit-Fehler -> wechsle auf SafeCores, lösche tmp.")
                                    current_cores = int(SAFE_CORES)
                                    crash_counter = 0
                                    for p in [os.path.join(work_dir, "tmp"),
                                               os.path.join(work_dir, "tmp_SAFE_CHECKPOINT")]:
                                        if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                                    update_csv(name, "Retrying (aainit -> SafeCores)")
                                    continue
                                elif not aainit_ecut_reduced:
                                    aainit_ecut_reduced = True
                                    apply_aainit_workaround(scf_in)
                                    for p in [os.path.join(work_dir, "tmp"),
                                               os.path.join(work_dir, "tmp_SAFE_CHECKPOINT")]:
                                        if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                                    print("      🔧 aainit -> ecutwfc=40, lösche alte Speicherstände.")
                                    update_csv(name, "Retrying (aainit -> ecutwfc=40)")
                                    continue
                                else:
                                    print("      ❌ aainit-Fehler unlösbar. System zu komplex. Skippe.")
                                    update_csv(name, "SKIPPED (OOM Limit)")
                                    break

                            else:
                                crash_counter += 1
                                if crash_counter >= 3:
                                    print(f"      ❌ Zu viele unlösbare Abstürze ({crash_counter}). Skippe.")
                                    update_csv(name, "SKIPPED (Permanent Crash)")
                                    git_sync(f"Skipped {name}, Permanent Crash")
                                    break
                                update_csv(name, f"Retrying (Crash {crash_counter}/3)")
                                time.sleep(2)
                                continue

                    if analyze_crash_reason(scf_out) != "DONE":
                        git_sync(f"Failed: {name}")
                        continue

                # ================================================================
                # DOS BLOCK
                # ================================================================
                with open(scf_in, 'r') as f:
                    match  = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", f.read())
                    prefix = match.group(1) if match else "calc"

                e_fermi = "-"
                if os.path.exists(scf_out):
                    with open(scf_out, 'r', errors='ignore') as f:
                        match = re.search(r"the Fermi energy is\s+([0-9\.\-]+)\s+ev",
                                          f.read())
                        if match: e_fermi = float(match.group(1))

                update_csv(name, "Rechnet DOS...", e_fermi=e_fermi)
                if not os.path.exists(dos_out):
                    with open(dos_in, "w") as f:
                        f.write(f"&DOS\n prefix='{prefix}', outdir='./tmp',"
                                f" fildos='{name}.dos', Emin=-20.0, Emax=30.0, DeltaE=0.1 /\n")
                    with open(dos_in, "r") as f_in, open(dos_out, "w") as f_out:
                        subprocess.run([DOS_EXE], stdin=f_in, stdout=f_out,
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
                    git_sync(f"Fertig: {name} (Isolator)")
                    continue

                print(f"   ⚡ Metall (DOS={dos_val:.3f}). Berechne Phononen...")
                update_csv(name, "Rechnet Phononen...", e_fermi,
                           round(dos_val, 4), "JA")

                # ================================================================
                # PHONON BLOCK — PHASE 1: Stabilitätsanalyse
                # Exakt wie Skript 1: simple ph.in, KEIN electron_phonon hier.
                # electron_phonon kommt erst in Phase 2 (nur für stabile Metalle).
                # ================================================================
                if not (os.path.exists(ph_out) and
                        "JOB DONE" in open(ph_out, errors='ignore').read()):
                    # Einfache ph.in ohne electron_phonon (bewährte Skript-1-Logik)
                    if not os.path.exists(ph_in):
                        with open(ph_in, "w") as f:
                            f.write(f"Phonons\n&INPUTPH\n"
                                    f" tr2_ph=1.0d-14, prefix='{prefix}',"
                                    f" outdir='./tmp', fildyn='{name}.dyn',"
                                    f" ldisp=.true., nq1=2, nq2=2, nq3=2 /\n")

                    # PFLICHT: Alten Phonon-State löschen wenn kein ph_out existiert.
                    # Verhindert "wrong elph" wenn _ph0/Checkpoint von früherem
                    # el-ph-Lauf stammt und run_monitored_ph recover setzen würde.
                    if not os.path.exists(ph_out):
                        for p in [os.path.join(work_dir, "tmp", "_ph0"),
                                   os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                            if os.path.exists(p):
                                shutil.rmtree(p, ignore_errors=True)
                                print(f"      🧹 Alter Phonon-State gelöscht: {os.path.basename(p)}")

                    # Kernanzahl aus SCF erben (konsistenter Speicherbedarf)
                    ph_cores = get_scf_cores(scf_out, DEFAULT_CORES)
                    print(f"      🧠 Erbe Kernanzahl von SCF: Starte mit {ph_cores} Core(s).")

                    ph_res = run_monitored_ph(ph_in, ph_out, work_dir, ph_cores)

                    if ph_res != "DONE":
                        print("      ⚠️ Phonon Crash/OOM!")
                        crash_reason = analyze_crash_reason(ph_out)
                        print_error_log(ph_out, "PHONON ERROR LOG")

                        if crash_reason == "XML_ERROR":
                            print("      🧨 FATAL: XML korrupt -> Lösche .save und erzwinge SCF-Neustart.")
                            tmp_save = os.path.join(work_dir, "tmp")
                            if os.path.exists(tmp_save): shutil.rmtree(tmp_save, ignore_errors=True)
                            if os.path.exists(scf_out):  os.remove(scf_out)
                            update_csv(name, "SCF_RESET (XML Error)")
                            continue

                        if crash_reason == "WF_COLLECT_ERROR":
                            print("      🌊 Wellenfunktionen fehlen -> starte Collect-SCF (nstep=0)...")
                            if run_cleanup_scf(scf_in, work_dir, ph_cores):
                                print("      ✅ Collect-SCF OK -> Phononen neu starten.")
                                try: os.remove(ph_out)
                                except: pass
                                ph_res = run_monitored_ph(ph_in, ph_out, work_dir, ph_cores)
                            else:
                                print("      ❌ Collect-SCF fehlgeschlagen -> vollständiger SCF-Reset.")
                                tmp_save = os.path.join(work_dir, "tmp")
                                if os.path.exists(tmp_save): shutil.rmtree(tmp_save, ignore_errors=True)
                                if os.path.exists(scf_out): os.remove(scf_out)
                                update_csv(name, "SCF_RESET (WF_Collect)")
                                continue

                        if crash_reason == "CORRUPT_FILE_ERROR":
                            print("      🧨 Defekte Phonon-Datei -> lösche _ph0 und starte neu...")
                            for p in [os.path.join(work_dir, "tmp", "_ph0"),
                                       os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                                if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                            try: os.remove(ph_out)
                            except: pass
                            ph_res = run_monitored_ph(ph_in, ph_out, work_dir, ph_cores)

                        if crash_reason in ["SYMMETRY_ERROR", "FFT_SYMMETRY_ERROR"]:
                            print("      🧩 Symmetrie-Problem erkannt!")

                        if crash_reason == "DAVCIO_ERROR" or is_recoverable_fragmentation_error(ph_out):
                            print("      🤕 Diagnose: Fragmentierung erkannt. Starte 'Collect-Recovery'...")
                            if run_cleanup_scf(scf_in, work_dir, int(DEFAULT_CORES)):
                                print("      👍 Recovery erfolgreich.")
                            else:
                                print("      👎 Recovery fehlgeschlagen.")

                        if ph_res != "DONE":
                            print("      🛡️ Aktiviere NOTFALL-MODUS: Grid=1x1x1, Sym=OFF, SafeCores...")
                            disable_symmetries_and_reduce_grid(ph_in)
                            # _ph0 UND Checkpoint löschen — frischer Lauf, kein recover!
                            for p in [os.path.join(work_dir, "tmp", "_ph0"),
                                       os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                                if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                            if os.path.exists(ph_out):
                                try: os.remove(ph_out)
                                except: pass
                            ph_res = run_monitored_ph(ph_in, ph_out, work_dir, int(SAFE_CORES))

                    if ph_res != "DONE":
                        print("      ❌ Phononen endgültig fehlgeschlagen.")
                        update_csv(name, "SKIPPED (Phonon Crash)")
                        git_sync(f"Phonon Crash: {name}")
                        continue

                # ================================================================
                # AUSWERTUNG PHONONEN — Stabilität bestimmen
                # ================================================================
                min_f, stab = "-", "Unbekannt"
                if os.path.exists(ph_out):
                    with open(ph_out, 'r') as f:
                        content_ph = f.read()
                        if "JOB DONE" in content_ph:
                            freqs = re.findall(
                                r"freq\s+\(\s*\d+\)\s+=\s+([0-9\.\-]+)\s+\[THz\]",
                                content_ph)
                            if freqs:
                                min_f = min(float(x) for x in freqs)
                                stab  = "STABIL" if min_f > -0.05 else "INSTABIL"

                if stab == "INSTABIL":
                    print(f"   🛑 Material ist INSTABIL (Min Freq: {min_f} THz).")
                    update_csv(name, "Fertig (Metall)", e_fermi,
                               round(dos_val, 4), "JA", min_f=min_f, stab=stab)
                    cleanup_heavy_files(work_dir, name)
                    git_sync(f"Fertig: {name} (INSTABIL)")
                    continue

                if stab == "Unbekannt":
                    # Keine Frequenzen im Output — trotzdem als Metall speichern
                    update_csv(name, "Fertig (Metall)", e_fermi,
                               round(dos_val, 4), "JA", min_f=min_f, stab=stab)
                    git_sync(f"Fertig: {name} (Metall, Stabilität unbekannt)")
                    continue

                # ================================================================
                # PHONON BLOCK — PHASE 2: Elektron-Phonon-Kopplung
                # Nur für stabile Metalle. Neuer Phonon-Lauf mit electron_phonon.
                # Phase-1-Cache (_ph0) wird vorher geleert — andere Berechnung!
                # ================================================================
                print(f"   ✅ Material ist STABIL (Min Freq: {min_f} THz). Starte El-Ph...")
                update_csv(name, "Rechnet El-Ph (Phonon)...", e_fermi,
                           round(dos_val, 4), "JA", min_f=min_f, stab=stab)

                ph_elph_out = os.path.join(work_dir, "ph_elph.out")

                if not (os.path.exists(ph_elph_out) and
                        "JOB DONE" in open(ph_elph_out, errors='ignore').read()):

                    # Phase-1-_ph0 löschen: el-ph ist eine andere Rechnung,
                    # recover auf falschen Daten würde den Lauf korrumpieren.
                    for p in [os.path.join(work_dir, "tmp", "_ph0"),
                               os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                        if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)

                    # Neue ph.in mit electron_phonon (fildyn-Name muss gleich bleiben
                    # damit Q2R die .dyn-Dateien findet)
                    with open(ph_in, "w") as f:
                        f.write(f"Phonons El-Ph\n&INPUTPH\n"
                                f" tr2_ph=1.0d-14, prefix='{prefix}',"
                                f" outdir='./tmp', fildyn='{name}.dyn',"
                                f" fildvscf='dvscf', ldisp=.true.,"
                                f" electron_phonon='interpolated',"
                                f" nq1=2, nq2=2, nq3=2 /\n")

                    ph_cores = get_scf_cores(scf_out, DEFAULT_CORES)
                    print(f"      ⚛️ El-Ph Phonon-Lauf ({ph_cores} Cores)...")
                    ph_elph_res = run_monitored_ph(ph_in, ph_elph_out, work_dir, ph_cores)

                    if ph_elph_res != "DONE":
                        print("      ⚠️ El-Ph Phonon-Crash! Versuche mit SafeCores...")
                        print_error_log(ph_elph_out, "EL-PH PHONON ERROR LOG")
                        for p in [os.path.join(work_dir, "tmp", "_ph0"),
                                   os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                            if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                        try: os.remove(ph_elph_out)
                        except: pass
                        ph_elph_res = run_monitored_ph(ph_in, ph_elph_out, work_dir, int(SAFE_CORES))

                    if ph_elph_res != "DONE":
                        print("      ❌ El-Ph Phonon endgültig fehlgeschlagen. Speichere Stabilität ohne Tc.")
                        update_csv(name, "Fertig (Metall)", e_fermi,
                                   round(dos_val, 4), "JA", min_f=min_f, stab=stab)
                        git_sync(f"Fertig: {name} (STABIL, El-Ph fehlgeschlagen)")
                        continue

                # ================================================================
                # Q2R + MATDYN + Tc BLOCK
                # ================================================================
                q2r_in     = os.path.join(work_dir, "q2r.in")
                q2r_out    = os.path.join(work_dir, "q2r.out")
                matdyn_in  = os.path.join(work_dir, "matdyn.in")
                matdyn_out = os.path.join(work_dir, "matdyn.out")

                update_csv(name, "Rechnet El-Ph (Q2R)...", e_fermi,
                           round(dos_val, 4), "JA", min_f=min_f, stab=stab)

                if not (os.path.exists(q2r_out) and
                        "JOB DONE" in open(q2r_out, errors='ignore').read()):
                    print("   4️⃣  Q2R (Kraft-Konstanten)...")
                    with open(q2r_in, "w") as f:
                        f.write(f"&input\n fildyn='{name}.dyn',\n"
                                f" zasr='simple',\n flfrc='{name}.fc',\n"
                                f" la2F=.true.\n/\n")
                    with open(q2r_in, "r") as f_in, open(q2r_out, "w") as f_out:
                        subprocess.run([Q2R_EXE], stdin=f_in, stdout=f_out,
                                       stderr=subprocess.STDOUT, cwd=work_dir)

                if not (os.path.exists(q2r_out) and
                        "JOB DONE" in open(q2r_out, errors='ignore').read()):
                    print("      ❌ Q2R fehlgeschlagen!")
                    print_error_log(q2r_out, "Q2R ERROR LOG")
                    update_csv(name, "ERROR (Q2R Crash)")
                    git_sync(f"Q2R Crash: {name}")
                    continue

                update_csv(name, "Rechnet El-Ph (Matdyn)...", e_fermi,
                           round(dos_val, 4), "JA", min_f=min_f, stab=stab)

                if not (os.path.exists(matdyn_out) and
                        "JOB DONE" in open(matdyn_out, errors='ignore').read()):
                    print("   5️⃣  Matdyn (Phonon-DOS + El-Ph)...")
                    with open(matdyn_in, "w") as f:
                        f.write(f"&input\n asr='simple',\n flfrc='{name}.fc',\n"
                                f" flfrq='{name}.freq',\n fildyn='{name}.dyn',\n"
                                f" dos=.true.,\n elph=.true.,\n"
                                f" fildos='{name}.phdos',\n"
                                f" nk1=10, nk2=10, nk3=10\n/\n")
                    with open(matdyn_in, "r") as f_in, open(matdyn_out, "w") as f_out:
                        subprocess.run([MATDYN_EXE], stdin=f_in, stdout=f_out,
                                       stderr=subprocess.STDOUT, cwd=work_dir)

                if not (os.path.exists(matdyn_out) and
                        "JOB DONE" in open(matdyn_out, errors='ignore').read()):
                    print("      ❌ Matdyn fehlgeschlagen!")
                    print_error_log(matdyn_out, "MATDYN ERROR LOG")
                    update_csv(name, "ERROR (Matdyn Crash)")
                    git_sync(f"Matdyn Crash: {name}")
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
                cleanup_heavy_files(work_dir, name)
                git_sync(f"Fertig: {name} (Tc={tc}K)")

            except Exception as job_err:
                print(f"🚨 Fehler bei Job {name}: {job_err}")
                traceback.print_exc()
                update_csv(name, f"ERROR (Python: {str(job_err)[:30]})")
                continue

        send_notification("🎉 Alle Jobs erledigt.")

        # 1. Signal-Datei erstellen
        with open(SIGNAL_FILE, "w") as f:
            f.write(f"Status: Fertig\nTimestamp: {time.ctime()}")

        # 2. Letzter sauberer Git Sync
        git_sync("🏁 Finaler Sync vor Shutdown (Erfolgreich)")

        # 3. Azure Logic App deaktivieren (verhindert sofortigen Neustart)
        set_logic_app_state("Disabled")

        # 4. VM über Azure CLI hart deallokieren
        print("🛑 Deallokiere VM über Azure CLI...")
        deallocate_vm()

        # 5. Lokales OS herunterfahren — aber nur ohne aktive SSH-Sitzung
        if os.name != 'nt':
            if is_ssh_session_active():
                print("🛑 Shutdown blockiert (Aktive SSH-Sitzung erkannt!)")
                git_sync("🛑 Shutdown blockiert (SSH aktiv)")
            else:
                print("🛑 Fahre System herunter...")
                os.system("sudo shutdown -h now")

    except Exception as e:
        full_error = (f"\n\n🚨 KRITISCHER ABSTURZ ({datetime.now()})\n"
                      f"{e}\n{traceback.format_exc()}\n")
        with open(TXT_LOG_FILE, "a") as f: f.write(full_error)
        git_sync("🚨 Notfall Sync nach Skript-Absturz")
        send_notification(f"🚨 KRITISCHER FEHLER: {e} -> Shutdown.")

        set_logic_app_state("Disabled")
        print("🛑 Deallokiere VM nach Crash...")
        deallocate_vm()
        if os.name != 'nt':
            if is_ssh_session_active():
                print("🛑 Shutdown blockiert (Aktive SSH-Sitzung erkannt!)")
                git_sync("🛑 Shutdown blockiert (SSH aktiv)")
            else:
                os.system("sudo shutdown -h now")
        sys.exit()

if __name__ == "__main__":
    main()