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
from datetime import datetime

# =============================================================================
# 0. LIVE-LOGGING
# =============================================================================
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# =============================================================================
# 1. KONFIGURATION
# =============================================================================
TELEGRAM_TOKEN = "8202414068:AAHnnLMa7nfo0E3gCDLUVnUmIomoyveDPBA"
TELEGRAM_CHAT_ID = "711461437"

LOGIC_APP_NAME = "AutoRestart-Supraleiter"
RESOURCE_GROUP = "Supraleiter-HPC-Knoten_group"
DOS_THRESHOLD = 0.05

DEFAULT_CORES = "4"
SAFE_CORES = "2"
PHASE3_OOM_CORES = "1" 
MEMORY_LIMIT_PERCENT = 92.0
MAX_BFGS_STEPS = 100 
MAX_RETRIES_LEVEL = 3

FORCE_RETRY_LIST = []

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
PSEUDO_DIR = os.path.join(WORK_DIR, "pseudo")
SIGNAL_FILE = os.path.join(WORK_DIR, "rechnung_fertig.txt")
CSV_FILE = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")

TXT_LOG_FILE = os.path.join(WORK_DIR, "pipeline_output.txt")

# Korrekte feste Pfade nach dem VM-Update
PW_EXE = "/home/marco/qe-source/bin/pw.x"
PH_EXE = "/home/marco/qe-source/bin/ph.x"
DOS_EXE = "/home/marco/qe-source/bin/dos.x"
Q2R_EXE = "/home/marco/qe-source/bin/q2r.x"
MATDYN_EXE = "/home/marco/qe-source/bin/matdyn.x"

# =============================================================================
# 2. HELFER & GIT & CLEANUP
# =============================================================================
def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ HPC: {message}"}
        requests.post(url, data=payload, timeout=10)
    except Exception: pass

def set_logic_app_state(state="Enabled"):
    az_cmd = shutil.which("az") or "/usr/bin/az"
    if not os.path.exists(az_cmd): return
    try:
        subprocess.run([az_cmd, "logic", "workflow", "set-state", "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME, "--state", state], capture_output=True, timeout=30)
    except Exception: pass

def kill_process_tree(pid):
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try: child.kill()
            except psutil.NoSuchProcess: pass
        parent.kill()
        print(f"      🧹 Stammbaum-Mörder hat Prozessbaum (PID {pid}) sauber beendet.")
    except psutil.NoSuchProcess: pass

def sync_checkpoint(src, dest):
    try:
        if not os.path.exists(dest): os.makedirs(dest)
        subprocess.run(["rsync", "-a", "--delete", f"{src}/", f"{dest}/"], check=True, capture_output=True)
        return True
    except Exception as e:
        print(f"      ⚠️ rsync Checkpoint-Fehler: {e}")
        return False

def initial_git_pull():
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        subprocess.run(["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"], cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
    except Exception as e:
        print(f"⚠️ Initialer Git Pull Fehler: {e}")

def git_sync(message):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        subprocess.run(["git", "config", "credential.helper", "store"], cwd=WORK_DIR, env=env, timeout=10)
        subprocess.run(["git", "add", "."], cwd=WORK_DIR, env=env, timeout=30)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR, capture_output=True, env=env, timeout=30)
        subprocess.run(["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"], cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=WORK_DIR, env=env, timeout=60)
    except Exception as e:
        print(f"⚠️ Git Fehler: {e}")

def berechne_tc(omega_log_K, lambda_ep, mu_star=0.13):
    try:
        lam = float(lambda_ep)
        wlog = float(omega_log_K)
        if lam <= 0: return 0.0
        vorfaktor = wlog / 1.20
        zaehler = -1.04 * (1.0 + lam)
        nenner = lam - mu_star * (1.0 + 0.62 * lam)
        if nenner <= 0: return 0.0
        return vorfaktor * math.exp(zaehler / nenner)
    except Exception: return "-"

def update_csv(name, status, e_fermi="-", dos_val="-", is_metal="-", min_f="-", stab="-", lam="-", wlog="-", tc="-"):
    fieldnames = ['Name', 'Status', 'Fermi Energie (eV)', 'DOS @ Fermi', 'Metall?', 'Min Freq (THz)', 'Stabilität', 'Lambda', 'Omega_log (K)', 'Tc (K)', 'Timestamp']
    rows = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'r') as f: rows = list(csv.DictReader(f))
    found = False
    for row in rows:
        if row['Name'] == name:
            row.update({'Status': status, 'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")})
            if e_fermi != "-": row['Fermi Energie (eV)'] = str(e_fermi)
            if dos_val != "-": row['DOS @ Fermi'] = str(dos_val)
            if is_metal != "-": row['Metall?'] = str(is_metal)
            if min_f != "-": row['Min Freq (THz)'] = str(min_f)
            if stab != "-": row['Stabilität'] = str(stab)
            if lam != "-": row['Lambda'] = str(lam)
            if wlog != "-": row['Omega_log (K)'] = str(wlog)
            if tc != "-": row['Tc (K)'] = str(tc)
            found = True
            break
    if not found:
        new_row = {'Name': name, 'Status': status, 'Fermi Energie (eV)': str(e_fermi), 'DOS @ Fermi': str(dos_val), 'Metall?': str(is_metal), 'Min Freq (THz)': str(min_f), 'Stabilität': str(stab), 'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")}
        if lam != "-": new_row['Lambda'] = str(lam)
        if wlog != "-": new_row['Omega_log (K)'] = str(wlog)
        if tc != "-": new_row['Tc (K)'] = str(tc)
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
        job_marker = f"💎 Job {job_name}"
        for line in reversed(lines):
            if job_marker in line:
                count += 1
            elif "💎 Job" in line and job_name not in line:
                break
    except Exception: return 1
    return max(1, count)

def cleanup_heavy_files(work_dir, name, force=False):
    """Löscht riesige tmp-Ordner von abgeschlossenen oder verworfenen Kandidaten."""
    if not force:
        row_data = get_csv_full_info(name)
        if not row_data: return
        status = row_data.get('Status', '')
        tc_val = str(row_data.get('Tc (K)', '-')).strip()
        stab = row_data.get('Stabilität', '-')
        is_finished = ("Isolator" in status) or (stab == "INSTABIL") or (tc_val != "-")
        if not is_finished: return

    deleted_something = False
    for dvscf_file in glob.glob(os.path.join(work_dir, "*dvscf*")):
        try: os.remove(dvscf_file); deleted_something = True
        except: pass

    for path in [os.path.join(work_dir, "tmp"), os.path.join(work_dir, "tmp_SAFE_CHECKPOINT"), os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
        if os.path.exists(path):
            try: shutil.rmtree(path, ignore_errors=True); deleted_something = True
            except: pass
            
    if deleted_something:
        print(f"      🧹 Heavy Files & dvscf für {name} sicher bereinigt (VM Speicher gespart).")

def cleanup_system_memory():
    print("      🧹 Bereinige Zombie-Prozesse und Shared Memory (/dev/shm)...")
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
                if os.path.isfile(item_path) or os.path.islink(item_path): os.remove(item_path)
                elif os.path.isdir(item_path): shutil.rmtree(item_path)
            except: pass
    print("      ✅ System-RAM und Prozesse sind sauber.")

# =============================================================================
# 3. SMART LOGIC & VALIDATION & CRASH ANALYSE
# =============================================================================

def analyze_crash_reason(output_file):
    if not os.path.exists(output_file): return "NONE"
    try:
        with open(output_file, 'rb') as f:
            try: f.seek(-20000, 2)
            except OSError: f.seek(0)
            lines = f.read().decode('utf-8', errors='ignore')
        
        if "JOB DONE" in lines: return "DONE"
        if "convergence NOT achieved" in lines: return "NON_CONVERGED"
        if "The maximum number of steps has been reached"  in lines: return "RESTART_NEEDED"
        
        lines_lower = lines.lower()
        if "fatal error reading xml" in lines_lower or "reading output_obj of xsd" in lines_lower or "wrong number of occurrences" in lines_lower:
            print("      🧨 XML-Struktur zerstört (Corruption).")
            return "XML_ERROR"
        if "not orthogonal" in lines_lower and "d_s" in lines_lower:
            print("      🧩 Symmetrie-Fehler erkannt (D_S not orthogonal).")
            return "SYMMETRY_ERROR"
        if "mx dimension too small" in lines_lower:
            print("      🧨 FATAL Pseudopotential übersteigt QE-Limit. Neues Pseudo (PAW) benötigt!")
            return "PSEUDO_ERROR"

        ram_match = re.search(r"estimated total dynamical ram\s*>\s*([0-9\.]+)\s*(mb|gb)", lines_lower)
        error_keywords = ["error", "mpi_abort", "segmentation fault", "stopping", "fatal", "diagonalization failed"]
        has_error_msg = any(key in lines_lower for key in error_keywords)

        if has_error_msg: return "HARD"
        if ram_match:
            if "self-consistent calculation" not in lines_lower and "iteration #" not in lines_lower:
                return "LIKELY_OOM"
        if "iteration #" in lines_lower or "diagonalization" in lines_lower:
            if not has_error_msg: return "LIKELY_OOM"
        
        return "SOFT"
    except Exception: return "HARD"
    
def is_xml_valid(xml_path):
    if not os.path.exists(xml_path): return False
    try:
        with open(xml_path, 'rb') as f:
            try: f.seek(-1000, 2) 
            except Exception: f.seek(0)
            tail = f.read().decode('utf-8', errors='ignore')
        if "</qes:espresso>" in tail or "</qes:data-file-schema>" in tail:
            return True
        return False
    except Exception: return False

def is_recoverable_fragmentation_error(ph_output_file):
    if not os.path.exists(ph_output_file): return False
    try:
        with open(ph_output_file, 'r', errors='ignore') as f:
            content = f.read()
        if "mismatch in number of G-vectors" in content or ("error reading file" in content and "xml" not in content):
            return True
        return False
    except Exception: return False

def run_cleanup_scf(scf_input_file, cwd, cores_to_use=2):
    print(f"      🚑 Starte RECOVERY-Modus (Collect Waves), Cores {cores_to_use}")
    with open(scf_input_file, 'r') as f: content = f.read()
    if "restart_mode" in content: content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", "restart_mode='restart'", content)
    else: content = content.replace("&CONTROL", "&CONTROL\n restart_mode='restart',")
    if "wf_collect" in content: content = re.sub(r"wf_collect\s*=\s*\.?[a-zA-Z]+\.?", "wf_collect=.true.", content)
    else: content = content.replace("&CONTROL", "&CONTROL\n wf_collect=.true.,")
    if "nstep" in content: content = re.sub(r"nstep\s*=\s*\d+", "nstep=0", content)
    else: content = content.replace("&CONTROL", "&CONTROL\n nstep=0,")

    recover_in = scf_input_file + ".recover"
    recover_out = os.path.join(cwd, "scf_recover.out")
    with open(recover_in, 'w') as f: f.write(content)
    
    with open(recover_in, 'r') as f_in, open(recover_out, 'w') as f_out:
        cmd = ["mpirun", "--oversubscribe", "-np", str(cores_to_use), PW_EXE]
        try:
            subprocess.run(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd, timeout=300)
            print("      ✅ Recovery-Lauf beendet. Daten sollten jetzt consolidated sein.")
            return True
        except Exception as e:
            print(f"      ❌ Recovery fehlgeschlagen {e}")
            return False

def detect_oom_level(input_file):
    if not os.path.exists(input_file): return 0
    with open(input_file, 'r', errors='ignore') as f: content = f.read()
    if "mixing_ndim = 2" in content or "mixing_ndim=2" in content: return 4
    if "mixing_ndim = 3" in content or "mixing_ndim=3" in content: return 3
    if "disk_io='low'" in content or 'disk_io="low"' in content: return 2
    if "diagonalization='cg'" in content or 'diagonalization="cg"' in content: return 1
    return 0

def apply_oom_settings(input_file, level):
    with open(input_file, 'r') as f: content = f.read()
    diag = 'cg'; mix = 6; disk = None 
    msg = "Standard (cg, mix=6, david=2)"

    if level >= 1: disk = 'low'; msg = "Stufe 1 (cg, mix=6, disk_io='low')"
    if level >= 2: mix = 3; msg = "Stufe 2 (cg, mix=3, disk_io='low')"
    if level >= 3: mix = 2; msg = "Stufe 3 (cg, mix=2, disk_io='low')"
    if level >= 4: mix = 2; msg = "Stufe 4 (cg, mix=2, disk_io='low', 1 Core)"

    print(f"      📉 Setze RAM-Strategie {msg}")

    if "diagonalization" in content: content = re.sub(r"diagonalization\s*=\s*['\"].*['\"]", f"diagonalization='{diag}'", content)
    else: content = content.replace("&ELECTRONS", f"&ELECTRONS\n diagonalization='{diag}',")

    if "mixing_ndim" in content: content = re.sub(r"mixing_ndim\s*=\s*\d+", f"mixing_ndim = {mix}", content)
    else: content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_ndim = {mix},")
        
    if "diago_david_ndim" in content: content = re.sub(r"diago_david_ndim\s*=\s*\d+", "diago_david_ndim = 2", content)
    else: content = content.replace("&ELECTRONS", "&ELECTRONS\n diago_david_ndim = 2,")

    if disk == 'low':
        if "disk_io" in content: content = re.sub(r"disk_io\s*=\s*['\"][a-zA-Z]+['\"]", "disk_io='low'", content)
        else: content = content.replace("&CONTROL", "&CONTROL\n disk_io='low',")
    else:
        if "disk_io='low'" in content or 'disk_io="low"' in content: content = re.sub(r"disk_io\s*=\s*['\"]low['\"],?", "", content)

    with open(input_file, 'w') as f: f.write(content)
    return True

def fix_input_file(input_file, iteration_count=0):
    with open(input_file, 'r') as f: content = f.read()
    corr_path = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" in content: content = re.sub(r"pseudo_dir\s*=\s*['\"].*['\"]", f"pseudo_dir='{corr_path}'", content)
    else: content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{corr_path}',")

    if "ecutwfc" in content: content = re.sub(r"ecutwfc\s*=\s*[0-9\.]+", "ecutwfc = 80.0", content)
    if "ecutrho" in content: content = re.sub(r"ecutrho\s*=\s*[0-9\.]+", "ecutrho = 800.0", content)

    target_beta = 0.7
    if iteration_count >= 30: target_beta = 0.4
    if iteration_count >= 60: target_beta = 0.25
    if iteration_count >= 90: target_beta = 0.15

    if "mixing_beta" in content: content = re.sub(r"mixing_beta\s*=\s*[0-9\.]+", f"mixing_beta = {target_beta}", content)
    else: content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_beta = {target_beta},")
    
    if "mixing_ndim" in content: content = re.sub(r"mixing_ndim\s*=\s*\d+", "mixing_ndim = 6", content)
    else: content = content.replace("&ELECTRONS", "&ELECTRONS\n mixing_ndim = 6,")
        
    if "diago_david_ndim" in content: content = re.sub(r"diago_david_ndim\s*=\s*\d+", "diago_david_ndim = 2", content)
    else: content = content.replace("&ELECTRONS", "&ELECTRONS\n diago_david_ndim = 2,")
    
    if "electron_maxstep" in content: content = re.sub(r"electron_maxstep\s*=\s*\d+", "electron_maxstep = 150", content)
    else: content = content.replace("&ELECTRONS", "&ELECTRONS\n electron_maxstep = 150,")

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
        scf_matches = re.findall(r"iteration #\s*(\d+)", chunk)
        val = 0
        if bfgs_matches: val = int(bfgs_matches[-1])
        elif scf_matches: val = int(scf_matches[-1])
        return val
    except Exception: return 0

# =============================================================================
# 6. SCF-BLOCK
# =============================================================================
def run_scf_block(name, work_dir, scf_in, scf_out):
    if not os.path.exists(scf_out): file_level = 0
    else: file_level = detect_oom_level(scf_in)
        
    start_crash_reason = analyze_crash_reason(scf_out)

    if start_crash_reason == "LIKELY_OOM":
        attempts = count_job_attempts(TXT_LOG_FILE, name)
        print(f"      🕵️ OOM-Signatur erkannt. Versuch Nr. {attempts} auf Level {file_level}.")

        if os.path.exists(scf_out):
            ts = datetime.now().strftime("%H%M%S")
            try:
                os.rename(scf_out, f"{scf_out}.crash_{ts}")
                print(f"      👻 Ghost-Protection: Alte scf.out zu scf.out.crash_{ts} verschoben.")
            except Exception: pass

        if attempts >= MAX_RETRIES_LEVEL:
            oom_level = file_level + 1
            print(f"      ❗ Threshold ({MAX_RETRIES_LEVEL}) erreicht! Eskaliere HÄRTER (Level {file_level} -> {oom_level}).")
            update_csv(name, f"Recovering (Escalating to Lvl {oom_level})")
        else:
            oom_level = file_level
            print(f"      🔄 Threshold noch nicht erreicht. Gebe Level {file_level} noch eine Chance.")
            update_csv(name, f"Retrying (Attempt {attempts}/{MAX_RETRIES_LEVEL})")
    else:
        oom_level = file_level

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
            except Exception: pass

        apply_oom_settings(scf_in, oom_level)
        print(f"   1️⃣  SCF ({current_cores} Cores, OOM-Lvl {oom_level})")
        
        # --- INTEGRIERTE PWSCF LOGIK ---
        fix_input_file(scf_in, 0)
        last_git_sync = time.time()
        last_checkpoint_time = 0 
        result = "CRASH"
        
        with open(scf_in, 'r') as f: content = f.read()
        tmp_dir = os.path.join(work_dir, "tmp") 
        checkpoint_dir = os.path.join(work_dir, "tmp_SAFE_CHECKPOINT") 
        if "wf_collect" in content: content = re.sub(r"wf_collect\s*=\s*\.?[a-zA-Z]+\.?", "wf_collect=.true.", content)
        else: content = content.replace("&CONTROL", "&CONTROL\n wf_collect=.true.,")

        prefix_match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", content)
        current_prefix = prefix_match.group(1) if prefix_match else "calc"
        xml_path = os.path.join(tmp_dir, f"{current_prefix}.save", "data-file-schema.xml")
        
        mode = 'from_scratch'
        if os.path.exists(scf_out) and is_xml_valid(xml_path):
            mode = 'restart'
            print("      ✅ Gültige XML im tmp-Ordner gefunden -> Normaler Restart.")
        elif os.path.exists(scf_out) and os.path.exists(checkpoint_dir):
            print("      🛡️ tmp-Ordner defekt/unvollständig! Hole Safe-Checkpoint...")
            if sync_checkpoint(checkpoint_dir, tmp_dir):
                if is_xml_valid(xml_path):
                    mode = 'restart'
                    print("      ✅ Checkpoint erfolgreich geladen!")
                else:
                    print("      ❌ Checkpoint war auch defekt. Starte von vorne.")
        else:
            print("      🆕 Kein gültiger Speicherstand gefunden -> Starte von vorne (From Scratch).")

        if mode == 'from_scratch':
            if os.path.exists(tmp_dir): shutil.rmtree(tmp_dir, ignore_errors=True)
            if os.path.exists(checkpoint_dir): shutil.rmtree(checkpoint_dir, ignore_errors=True)

        if "restart_mode" in content: content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{mode}'", content)
        else: content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{mode}',")
        
        run_input = scf_in + ".run"
        with open(run_input, 'w') as f: f.write(content)
        file_mode = 'a' if mode == 'restart' else 'w'
        
        with open(run_input, 'r') as f_in, open(scf_out, file_mode) as f_out:
            cmd = ["mpirun", "--oversubscribe", "-np", str(current_cores), PW_EXE]
            print(f"      ⚙️ Starte PWSCF ({mode}, {current_cores} Cores)...")
            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=work_dir)
            try:
                while process.poll() is None:
                    time.sleep(5)
                    if time.time() - last_checkpoint_time > 900: 
                        if is_xml_valid(xml_path):
                            print("      💾 XML valide -> Erstelle Checkpoint per rsync...")
                            if sync_checkpoint(tmp_dir, checkpoint_dir):
                                last_checkpoint_time = time.time()
                                print("      ✅ Checkpoint erstellt.")
                                git_sync("Checkpoint & Log Update")
                                last_git_sync = time.time() 

                    if time.time() - last_git_sync > 3600:
                        print("      ❤️ Git Heartbeat...")
                        git_sync("Log Update (Heartbeat)")
                        last_git_sync = time.time()

                    try:
                        mem_usage = psutil.virtual_memory().percent
                        if mem_usage > MEMORY_LIMIT_PERCENT:
                            print(f"      ⚠️ RAM NOT-AUS (Python Monitor)!")
                            kill_process_tree(process.pid)
                            result = "OOM"
                            break
                    except Exception: pass

                    cur_iter = get_last_iteration(scf_out)
                    if cur_iter >= MAX_BFGS_STEPS:
                        print(f"      🛑 Limit erreicht ({cur_iter}/{MAX_BFGS_STEPS} BFGS Schritte). Breche ab.")
                        kill_process_tree(process.pid)
                        result = "MAX_STEPS"
                        break
                    
                    if cur_iter > 30: fix_input_file(scf_in, cur_iter)

            except Exception: 
                kill_process_tree(process.pid)
                result = "CRASH"
            
            time.sleep(1.5)
            if process.poll() is not None:
                if process.returncode == -9:
                    print("      💀 Prozess wurde vom OS getötet (Exit -9 -> Wahrscheinlich OOM).")
                    result = "OOM"
                elif process.returncode != 0:
                    reason = analyze_crash_reason(scf_out)
                    if reason == "LIKELY_OOM":
                        print("      💀 Logfile endet abrupt (Silent Death) -> OOM.")
                        result = "OOM"
                    else: result = "CRASH"
                else:
                    final_reason = analyze_crash_reason(scf_out)
                    if final_reason == "DONE": result = "DONE"
                    elif final_reason == "LIKELY_OOM": result = "OOM"
                    else: result = "CRASH"

        # --- ENDE INTEGRIERTE PWSCF LOGIK ---

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
            
            tmp_p = os.path.join(work_dir, "tmp")
            chkpt_p = os.path.join(work_dir, "tmp_SAFE_CHECKPOINT")
            if os.path.exists(tmp_p): shutil.rmtree(tmp_p, ignore_errors=True)
            if os.path.exists(chkpt_p): shutil.rmtree(chkpt_p, ignore_errors=True)
            
            labels = {1: "Retrying (OOM Lvl 1, CG)", 2: "Retrying (OOM Lvl 2, DiskIO)", 3: "Retrying (OOM Lvl 3, Mix3)", 4: "Retrying (OOM Lvl 4, 1Core)"}
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
            if reason == "NON_CONVERGED":
                update_csv(name, "SKIPPED (Non-Conv)")
                return "NON_CONV"
            if reason == "AAINIT_ERROR":
                print("      ❌ aainit-Fehler. System zu komplex. Skippe.")
                update_csv(name, "SKIPPED (OOM Limit)")
                return "OOM_LIMIT"
            if reason == "PSEUDO_ERROR":
                update_csv(name, "SKIPPED (Pseudo Limit)")
                print(f"      ❌ Skippe Job wegen inkompatiblem Pseudopotential.")
                git_sync(f"Skipped {name}, Pseudo Error")
                return "PSEUDO_ERROR"

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

# --- ROBUSTE PHONON WRAPPER OHNE EXTERNE CHECKPOINTS ---
def run_monitored_ph(input_file, output_file, cwd, active_cores):
    last_git_sync = time.time()
    with open(input_file, 'r') as f: content = f.read()
    if os.path.exists(output_file):
        if "recover" not in content: content = content.replace("&INPUTPH", "&INPUTPH\n recover=.true.,")
    
    run_input = input_file + ".run"
    with open(run_input, 'w') as f: f.write(content)
    
    file_mode = 'a' if "recover=.true." in content else 'w'

    with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
        cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PH_EXE]
        print(f"      ⚙️ Starte PHONONEN (Cores: {active_cores})...")
        process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)
        
        try:
            while process.poll() is None:
                time.sleep(5)
                if time.time() - last_git_sync > 1800:
                    print("      ❤️ Git Heartbeat (Phonon)...")
                    git_sync("Log Update (Phonon Running)")
                    last_git_sync = time.time()
                try:
                    mem_usage = psutil.virtual_memory().percent
                    if mem_usage > MEMORY_LIMIT_PERCENT:
                        print(f"      ⚠️ RAM NOT-AUS (Python Monitor)!")
                        kill_process_tree(process.pid)
                        return "OOM"
                except Exception: pass
        except Exception: 
            kill_process_tree(process.pid)
            return "CRASH"
        
        time.sleep(1.5)
        if process.returncode == -9:
            print("      💀 Prozess wurde vom OS getötet (Exit -9 -> Wahrscheinlich OOM).")
            return "OOM"
        if process.returncode != 0: return "CRASH"

        try:
            with open(output_file, 'r', errors='ignore') as f:
                if "JOB DONE" in f.read(): return "DONE"
        except Exception: pass
        
        return "CRASH"
    
def deallocate_vm():
    az_cmd = shutil.which("az") or "/usr/bin/az"
    if not os.path.exists(az_cmd): 
        print("🛑 Azure CLI nicht gefunden. Verlasse mich auf lokalen Shutdown.")
        return
    try:
        result = subprocess.run([az_cmd, "vm", "deallocate", "--resource-group", RESOURCE_GROUP, "--name", "Supraleiter-HPC-Knoten"], capture_output=True, text=True, timeout=60)
        if result.returncode != 0: print(f"⚠️ Azure CLI Deallocate Fehler {result.stderr}")
        else: print("✅ VM erfolgreich deallokiert.")
    except Exception as e: 
        print(f"⚠️ Fehler beim Aufruf der Azure CLI {e}")

def is_ssh_session_active():
    try:
        output = subprocess.check_output(["who"]).decode("utf-8")
        return "pts/" in output or "tty" in output
    except Exception:
        return False

# =============================================================================
# 4. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        print("☁️ Führe initialen Git Pull aus...")
        initial_git_pull()
        
        set_logic_app_state("Enabled")
        print(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        
        cleanup_system_memory()

        if os.path.exists(SIGNAL_FILE): os.remove(SIGNAL_FILE)
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)
        
        input_files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.in")))
        send_notification(f"Start {len(input_files)} Jobs.")
        git_sync("🚀 Start")

        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            scf_out = os.path.join(work_dir, "scf.out")
            phase3_file = os.path.join(work_dir, "Phase 3.txt")
            
            if name in FORCE_RETRY_LIST:
                print(f"🔄 ERZWUNGENER NEUSTART für {name} (Lösche korrupten RUN-Ordner)...")
                if os.path.exists(work_dir): shutil.rmtree(work_dir, ignore_errors=True)
                update_csv(name, "NEW", "-", "-", "-", "-", "-")
            
            row_data = get_csv_full_info(name)
            last_status = row_data.get('Status', 'NEW')
            stability = row_data.get('Stabilität', '-')
            tc_status = str(row_data.get('Tc (K)', '-')).strip()
            lam_status = str(row_data.get('Lambda', '-')).strip()

            if not stability: stability = "-"
            if not tc_status: tc_status = "-"
            if not lam_status: lam_status = "-"

            if "SKIPPED" in last_status:
                print(f"⏩ Überspringe {name} (Status: {last_status})")
                continue
            
            if "Isolator" in last_status:
                cleanup_heavy_files(work_dir, name)
                print(f"⏩ Überspringe {name} (Ist ein Isolator)")
                continue

            if "Metall" in last_status:
                if stability == "INSTABIL":
                    cleanup_heavy_files(work_dir, name)
                    print(f"⏩ Überspringe {name} (Bereits vollständig analysiert, INSTABIL)")
                    continue
                elif stability == "STABIL" and tc_status != "-" and lam_status != "-":
                    cleanup_heavy_files(work_dir, name)
                    print(f"⏩ Überspringe {name} (Bereits vollständig analysiert, STABIL, Tc={tc_status}K)")
                    continue

            if "Metall" in last_status and (stability == "-" or stability == "Unbekannt"):
                print(f"🔄 Retry Phonon für {name} (Metall, aber Stabilität unbekannt)...")

            crash_type = analyze_crash_reason(scf_out)
            if crash_type == "NON_CONVERGED":
                update_csv(name, "SKIPPED (Non-Conv)")
                continue
            elif crash_type == "DONE":
                print(f"✅ {name} SCF ist fertig.")
            
            try:
                if not os.path.exists(work_dir): os.makedirs(work_dir)
                print(f"\n💎 Job: {name}")
                scf_in = os.path.join(work_dir, "scf.in")
                
                # DIE ENTSCHEIDENDE TRENNUNG
                dos_in = os.path.join(work_dir, "dos.in")
                dos_log = os.path.join(work_dir, "dos.out")
                dos_data = os.path.join(work_dir, f"{name}.dos")
                
                phase2_in = os.path.join(work_dir, "ph_phase2.in")
                phase2_out = os.path.join(work_dir, "ph_phase2.out")
                phase3_in = os.path.join(work_dir, "ph_phase3_elph.in")
                phase3_out = os.path.join(work_dir, "ph_phase3_elph.out")

                if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)

                if not (os.path.exists(scf_out) and "JOB DONE" in open(scf_out, errors='ignore').read()):
                    update_csv(name, "Rechnet SCF...")
                    scf_result = run_scf_block(name, work_dir, scf_in, scf_out)
                    if scf_result != "DONE":
                        git_sync(f"Failed SCF, {name} ({scf_result})")
                        continue

                if analyze_crash_reason(scf_out) != "DONE":
                    git_sync(f"Failed, {name}")
                    continue 

                with open(scf_in, 'r') as f: 
                    match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", f.read())
                    prefix = match.group(1) if match else "calc"
                
                # DIE ENTSCHEIDENDE FERMI-SUCHE MIT re.findall UND [-1]
                e_fermi = "-"
                if os.path.exists(scf_out):
                    with open(scf_out, 'r', errors='ignore') as f:
                        content = f.read()
                        matches = re.findall(r"the Fermi energy is\s+([0-9\.\-]+)\s+eV", content, re.IGNORECASE)
                        if matches:
                            e_fermi = float(matches[-1])
                        else:
                            matches_iso = re.findall(r"highest occupied.*level[s]?\s*\(ev\):\s+([0-9\.\-]+)", content, re.IGNORECASE)
                            if matches_iso:
                                e_fermi = float(matches_iso[-1])

                update_csv(name, "Rechnet DOS...", e_fermi=e_fermi)
                
                if not os.path.exists(dos_data):
                    with open(dos_in, "w") as f: 
                        f.write(f"&DOS\n prefix='{prefix}', outdir='./tmp', fildos='{name}.dos', Emin=-20.0, Emax=30.0, DeltaE=0.1 /\n")
                    
                    with open(dos_in, "r") as f_in, open(dos_log, "w") as f_out:
                        subprocess.run([DOS_EXE], stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=work_dir)

                dos_crash = analyze_crash_reason(dos_log)
                if dos_crash == "XML_ERROR" or dos_crash == "WF_COLLECT_ERROR":
                    print("      🧨 FATAL, DOS.x meldet korrupte XML/Wellenfunktionen. Lösche .save und erzwinge SCF-Neustart.")
                    tmp_save_path = os.path.join(work_dir, "tmp")
                    if os.path.exists(tmp_save_path): shutil.rmtree(tmp_save_path, ignore_errors=True)
                    if os.path.exists(scf_out): os.remove(scf_out)
                    if os.path.exists(dos_log): os.remove(dos_log)
                    update_csv(name, "SCF_RESET (XML Error in DOS)")
                    continue

                is_metal, dos_val = False, 0.0
                if os.path.exists(dos_data) and e_fermi != "-":
                    closest_diff = 99.9
                    with open(dos_data, 'r') as f:
                        for line in f:
                            if line.strip().startswith("#"): continue
                            p = line.split()
                            if len(p) >= 2:
                                try:
                                    e, d = float(p[0]), float(p[1])
                                    if abs(e - e_fermi) < closest_diff:
                                        closest_diff = abs(e - e_fermi)
                                        dos_val = d
                                except Exception: continue
                    is_metal = dos_val > DOS_THRESHOLD

                if not is_metal:
                    print(f"   🛑 Isolator (DOS={dos_val:.3f}).")
                    update_csv(name, "Fertig (Isolator)", e_fermi, round(dos_val, 4), "NEIN")
                    cleanup_heavy_files(work_dir, name)
                    git_sync(f"Fertig, {name} (Isolator)")
                    continue

                print(f"   ⚡ Metall (DOS={dos_val:.3f}). Berechne Phononen...")
                update_csv(name, "Rechnet Phononen...", e_fermi, round(dos_val, 4), "JA")
                
                # --- PHASE 2 REINE PHONONEN ---
                if not os.path.exists(phase2_out) or "JOB DONE" not in open(phase2_out, errors='ignore').read():
                    if not os.path.exists(phase2_in):
                        print("   🧹 Manueller Reset erkannt. Bereinige Phononen-Daten für Phase 2...")
                        shutil.rmtree(os.path.join(work_dir, "tmp", "_ph0"), ignore_errors=True)
                        for ext in ["*.dvscf*", "*.a2Fsave*", "*.dyn*", "*.fc", "*.freq", "*.phdos"]:
                            for f in glob.glob(os.path.join(work_dir, "tmp", ext)) + glob.glob(os.path.join(work_dir, ext)):
                                try: os.remove(f)
                                except Exception: pass
                        
                        with open(phase2_in, "w") as f: 
                            f.write(f"Phonons\n&INPUTPH\n tr2_ph=1.0d-14, prefix='{prefix}', outdir='./tmp', fildyn='{name}.dyn', ldisp=.true., fildvscf='dvscf', nq1=2, nq2=2, nq3=2 /\n")
                    
                    ph_cores = int(DEFAULT_CORES)
                    ph_res = run_monitored_ph(phase2_in, phase2_out, work_dir, ph_cores)
                    
                    if ph_res != "DONE":
                        print("      ⚠️ Crash/OOM in Phase 2 Phononen erkannt!")
                        crash_reason = analyze_crash_reason(phase2_out)
                        
                        if crash_reason == "XML_ERROR":
                            print("      🧨 FATAL XML korrupt. Lösche .save und erzwinge SCF-Neustart im nächsten Durchlauf.")
                            tmp_save_path = os.path.join(work_dir, "tmp")
                            if os.path.exists(tmp_save_path): shutil.rmtree(tmp_save_path, ignore_errors=True)
                            if os.path.exists(scf_out): os.remove(scf_out)
                            update_csv(name, "SCF_RESET (XML Error)")
                            continue

                        print("      ❌ Phononen fehlgeschlagen. Notfallmodus ist deaktiviert.")
                        update_csv(name, "SKIPPED (Phonon Crash)") 
                        git_sync(f"Phonon Crash {name}")
                        continue

                min_f, stab = "-", "Unbekannt"
                if os.path.exists(phase2_out):
                      with open(phase2_out, 'r') as f:
                          content = f.read()
                          if "JOB DONE" in content:
                              freqs = re.findall(r"freq\s+\(\s*\d+\)\s+=\s+([0-9\.\-]+)\s+\[THz\]", content)
                              if freqs:
                                  min_f = min([float(f) for f in freqs])
                                  stab = "STABIL" if min_f > -0.05 else "INSTABIL"

                if stab == "INSTABIL":
                    update_csv(name, "Fertig (Metall)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
                    cleanup_heavy_files(work_dir, name)
                    git_sync(f"Fertig {name} (INSTABIL)")
                    continue

                if stab == "STABIL":
                    # --- PHASE 3 ELECTRON-PHONON ---
                    if not os.path.exists(phase3_out) or "JOB DONE" not in open(phase3_out, errors='ignore').read():
                        print(f"   ⚛️ Bereite Phase 3 (El-Ph) für {name} vor...")
                        
                        print("      🧹 Lösche eventuelle a2Fsave-Leichen vor Phase 3...")
                        for f in glob.glob(os.path.join(work_dir, "tmp", "*.a2Fsave*")):
                            try: os.remove(f)
                            except Exception: pass
                            
                        c_nq1, c_nq2, c_nq3 = "2", "2", "2"
                        if os.path.exists(phase2_in):
                            with open(phase2_in, 'r') as f:
                                c = f.read()
                                m1 = re.search(r"nq1\s*=\s*(\d+)", c)
                                m2 = re.search(r"nq2\s*=\s*(\d+)", c)
                                m3 = re.search(r"nq3\s*=\s*(\d+)", c)
                                if m1: c_nq1 = m1.group(1)
                                if m2: c_nq2 = m2.group(1)
                                if m3: c_nq3 = m3.group(1)
                                
                        with open(phase3_in, "w") as f:
                            f.write(f"Phonons\n&INPUTPH\n tr2_ph=1.0d-14, prefix='{prefix}', outdir='./tmp', fildyn='{name}.dyn', ldisp=.true., fildvscf='dvscf', electron_phonon='interpolated', trans=.false., recover=.false., nq1={c_nq1}, nq2={c_nq2}, nq3={c_nq3} /\n")

                        update_csv(name, "Rechnet El-Ph (Phase 3)...", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
                        
                        ph_cores_p3 = int(DEFAULT_CORES)
                        if os.path.exists(phase3_file) or count_job_attempts(TXT_LOG_FILE, name) > 2:
                            print(f"   📄 Erhöhtes Risiko erkannt! Drossele Phase 3 hart auf {PHASE3_OOM_CORES} Core(s), um MPI-Speicher zu sparen.")
                            ph_cores_p3 = int(PHASE3_OOM_CORES)

                        print(f"   ⚛️ Starte Phase 3 (El-Ph) auf {ph_cores_p3} Kernen...")
                        ph_res_3 = run_monitored_ph(phase3_in, phase3_out, work_dir, ph_cores_p3)
                        
                        if ph_res_3 != "DONE":
                            print("   ❌ El-Ph Phononen fehlgeschlagen.")
                            update_csv(name, "ERROR (El-Ph Crash)")
                            git_sync(f"El-Ph Crash {name}")
                            continue

                    q2r_in = os.path.join(work_dir, "q2r.in")
                    q2r_out = os.path.join(work_dir, "q2r.out")
                    matdyn_in = os.path.join(work_dir, "matdyn.in")
                    matdyn_out = os.path.join(work_dir, "matdyn.out")

                    update_csv(name, "Rechnet El-Ph (Q2R)...", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)

                    if not (os.path.exists(q2r_out) and "JOB DONE" in open(q2r_out, errors='ignore').read()):
                        print("   4️⃣  Q2R...")
                        with open(q2r_in, "w") as f:
                            f.write(f"&input\n fildyn='{name}.dyn',\n zasr='simple',\n flfrc='{name}.fc',\n la2F=.true.\n/\n")
                        with open(q2r_in, "r") as fi, open(q2r_out, "w") as fo:
                            subprocess.run([Q2R_EXE], stdin=fi, stdout=fo, stderr=subprocess.STDOUT, cwd=work_dir)

                    if not (os.path.exists(q2r_out) and "JOB DONE" in open(q2r_out, errors='ignore').read()):
                        print(f"      ❌ Q2R fehlgeschlagen!")
                        update_csv(name, "ERROR (Q2R Crash)")
                        git_sync(f"Q2R Crash {name}")
                        continue

                    update_csv(name, "Rechnet El-Ph (Matdyn)...", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)

                    if not (os.path.exists(matdyn_out) and "JOB DONE" in open(matdyn_out, errors='ignore').read()):
                        print("   5️⃣  Matdyn...")
                        with open(matdyn_in, "w") as f:
                            f.write(f"&input\n asr='simple',\n flfrc='{name}.fc',\n flfrq='{name}.freq',\n fildyn='{name}.dyn',\n dos=.true.,\n elph=.true.,\n fildos='{name}.phdos',\n nk1=10, nk2=10, nk3=10\n/\n")
                        with open(matdyn_in, "r") as fi, open(matdyn_out, "w") as fo:
                            subprocess.run([MATDYN_EXE], stdin=fi, stdout=fo, stderr=subprocess.STDOUT, cwd=work_dir)

                    if not (os.path.exists(matdyn_out) and "JOB DONE" in open(matdyn_out, errors='ignore').read()):
                        print(f"      ❌ Matdyn fehlgeschlagen!")
                        update_csv(name, "ERROR (Matdyn Crash)")
                        git_sync(f"Matdyn Crash {name}")
                        continue

                    lam, wlog, tc = "-", "-", "-"
                    if os.path.exists(matdyn_out):
                        with open(matdyn_out, 'r', errors='ignore') as f:
                            mc = f.read()
                            if "JOB DONE" in mc:
                                ml = re.search(r"lambda\s*=\s*([0-9\.]+)", mc)
                                mw = re.search(r"omega_log\s*=\s*([0-9\.]+)", mc)
                                if ml and mw:
                                    lam = ml.group(1)
                                    wlog = mw.group(1)
                                    tc_v = berechne_tc(wlog, lam)
                                    if tc_v != "-":
                                        tc = round(tc_v, 3)

                    update_csv(name, "Fertig (Metall)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab, lam=lam, wlog=wlog, tc=tc)
                    cleanup_heavy_files(work_dir, name)
                    git_sync(f"Fertig {name} (Tc={tc}K)")

            except Exception as job_err:
                print(f"🚨 Fehler bei Job {name}: {job_err}")
                update_csv(name, f"ERROR (Python: {str(job_err)[:30]})")
                continue 

        send_notification("🎉 Alle Jobs erledigt.")
        
        with open(SIGNAL_FILE, "w") as f: f.write(f"Status: Fertig\nTimestamp: {time.ctime()}")
        git_sync("🏁 Finaler Sync vor Shutdown (Erfolgreich)")
        set_logic_app_state("Disabled")
        print("🛑 Deallokiere VM über Azure CLI...")
        deallocate_vm() 
        
        if os.name != 'nt': 
            if is_ssh_session_active():
                print("🛑 Shutdown blockiert (Aktive SSH-Sitzung erkannt!)")
                git_sync("🛑 Shutdown blockiert (SSH aktiv)")
            else:
                print("🛑 Fahre System herunter...")
                os.system("sudo shutdown -h now")

    except Exception as e:
        full_error = f"\n\n🚨 KRITISCHER ABSTURZ ({datetime.now()})\n{e}\n{traceback.format_exc()}\n"
        print(full_error)
        git_sync("🚨 Notfall Sync nach Skript-Absturz")
        send_notification(f"🚨 KRITISCHER FEHLER: {e} -> Shutdown.")
        
        set_logic_app_state("Disabled")
        print("🛑 Deallokiere VM über Azure CLI nach Crash...")
        deallocate_vm()
        if os.name != 'nt': os.system("sudo shutdown -h now")
        sys.exit()

if __name__ == "__main__":
    main()