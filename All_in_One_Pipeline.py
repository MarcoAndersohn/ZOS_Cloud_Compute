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
TELEGRAM_TOKEN = open("/home/marco/.telegram_token").read().strip()
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

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
PSEUDO_DIR = os.path.join(WORK_DIR, "pseudo")
SIGNAL_FILE = os.path.join(WORK_DIR, "rechnung_fertig.txt")
CSV_FILE = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")

TXT_LOG_FILE = os.path.join(WORK_DIR, "pipeline_output.txt")

# Korrekte feste Pfade zu QE 7.4
PW_EXE = "/home/marco/qe-7.4/bin/pw.x"
PH_EXE = "/home/marco/qe-7.4/bin/ph.x"
DOS_EXE = "/home/marco/qe-7.4/bin/dos.x"
Q2R_EXE = "/home/marco/qe-7.4/bin/q2r.x"
MATDYN_EXE = "/home/marco/qe-7.4/bin/matdyn.x"

# =============================================================================
# 2. HELFER & GIT
# =============================================================================
def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ HPC {message}"}
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
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
        print(f"      🧹 Stammbaum-Mörder hat Prozessbaum (PID {pid}) sauber beendet.")
    except psutil.NoSuchProcess:
        pass

def sync_checkpoint(src, dest):
    try:
        if not os.path.exists(dest):
            os.makedirs(dest)
        subprocess.run(["rsync", "-a", "--delete", f"{src}/", f"{dest}/"], check=True, capture_output=True)
        return True
    except Exception as e:
        print(f"      ⚠️ rsync Checkpoint-Fehler {e}")
        return False

def initial_git_pull():
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        subprocess.run(["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"], cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
    except Exception as e:
        print(f"⚠️ Initialer Git Pull Fehler {e}")

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
        print(f"⚠️ Git Fehler {e}")

def print_error_tail(output_file, lines=50):
    cwd = os.path.dirname(output_file)
    crash_file = os.path.join(cwd, "CRASH")
    
    if os.path.exists(crash_file):
        try:
            with open(crash_file, 'r', errors='ignore') as f:
                crash_content = f.read()
                msg = f"\n--- Inhalt der CRASH-Datei ---\n{crash_content}\n-----------------------------------------\n"
                print(msg)
        except Exception: pass

    if not os.path.exists(output_file): return
    try:
        with open(output_file, 'r', errors='ignore') as f:
            content = f.readlines()
            tail = "".join(content[-lines:])
            msg = f"\n--- Letzte {lines} Zeilen von {os.path.basename(output_file)} ---\n{tail}\n-----------------------------------------\n"
            print(msg)
    except Exception: pass

def berechne_tc(omega_log_K, lambda_ep, mu_star=0.13):
    try:
        lam = float(lambda_ep)
        wlog = float(omega_log_K)
        if lam <= 0:
            return 0.0
        vorfaktor = wlog / 1.20
        zaehler = -1.04 * (1.0 + lam)
        nenner = lam - mu_star * (1.0 + 0.62 * lam)
        if nenner <= 0:
            return 0.0
        return vorfaktor * math.exp(zaehler / nenner)
    except Exception:
        return "-"

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

        if has_error_msg: 
            return "HARD"

        if ram_match:
            if "self-consistent calculation" not in lines_lower and "iteration #" not in lines_lower:
                return "LIKELY_OOM"

        if "iteration #" in lines_lower or "diagonalization" in lines_lower:
            if not has_error_msg:
                return "LIKELY_OOM"
        
        return "SOFT"
    except Exception: return "HARD"
    
def run_monitored_pw(input_file, output_file, cwd, active_cores):
    fix_input_file(input_file, 0)
    last_git_sync = time.time()
    last_checkpoint_time = 0 

    while True:
        with open(input_file, 'r') as f: content = f.read()
        tmp_dir = os.path.join(cwd, "tmp") 
        checkpoint_dir = os.path.join(cwd, "tmp_SAFE_CHECKPOINT") 

        if "wf_collect" in content:
            content = re.sub(r"wf_collect\s*=\s*\.?[a-zA-Z]+\.?", "wf_collect=.true.", content)
        else:
            content = content.replace("&CONTROL", "&CONTROL\n wf_collect=.true.,")

        prefix_match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", content)
        current_prefix = prefix_match.group(1) if prefix_match else "calc"
        xml_path = os.path.join(tmp_dir, f"{current_prefix}.save", "data-file-schema.xml")
        
        mode = 'from_scratch'
        if os.path.exists(output_file) and is_xml_valid(xml_path):
            mode = 'restart'
            print("      ✅ Gültige XML im tmp-Ordner gefunden -> Normaler Restart.")
        elif os.path.exists(output_file) and os.path.exists(checkpoint_dir):
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

        if "restart_mode" in content:
            content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{mode}'", content)
        else:
            content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{mode}',")
        
        run_input = input_file + ".run"
        with open(run_input, 'w') as f: f.write(content)

        file_mode = 'a' if mode == 'restart' else 'w'
        
        with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
            cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PW_EXE, "-ndiag", "1"]
            print(f"      ⚙️ Starte PWSCF ({mode}, {active_cores} Cores, -ndiag 1)...")
            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)
            
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
                            print_error_tail(output_file)
                            return "OOM" 
                    except Exception: pass

                    cur_iter = get_last_iteration(output_file)
                    if cur_iter >= MAX_BFGS_STEPS:
                        print(f"      🛑 Limit erreicht ({cur_iter}/{MAX_BFGS_STEPS} BFGS Schritte). Breche ab.")
                        kill_process_tree(process.pid)
                        return "MAX_STEPS"
                    
                    if cur_iter > 30: fix_input_file(input_file, cur_iter)

            except Exception: 
                kill_process_tree(process.pid)
                print_error_tail(output_file)
                return "CRASH"
            
            time.sleep(1.5)
            
            if process.returncode == -9:
                print("      💀 Prozess wurde vom OS getötet (Exit -9 -> Wahrscheinlich OOM).")
                print_error_tail(output_file)
                return "OOM"

            if process.returncode != 0:
                print_error_tail(output_file)
                reason = analyze_crash_reason(output_file)
                if reason == "LIKELY_OOM":
                    print("      💀 Logfile endet abrupt (Silent Death) -> OOM.")
                    return "OOM"
                return "CRASH"

            final_reason = analyze_crash_reason(output_file)
            if final_reason == "DONE": return "DONE"
            elif final_reason == "LIKELY_OOM":
                print_error_tail(output_file)
                return "OOM"
            
            print_error_tail(output_file)
            return "CRASH"
        
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
    except Exception:
        return False

def is_recoverable_fragmentation_error(ph_output_file):
    if not os.path.exists(ph_output_file): return False
    try:
        with open(ph_output_file, 'r', errors='ignore') as f:
            content = f.read()
        if "mismatch in number of G-vectors" in content or ("error reading file" in content and "xml" not in content):
            return True
        return False
    except Exception: return False

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

    if "diagonalization" in content:
        content = re.sub(r"diagonalization\s*=\s*['\"].*['\"]", f"diagonalization='{diag}'", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n diagonalization='{diag}',")

    if "mixing_ndim" in content:
        content = re.sub(r"mixing_ndim\s*=\s*\d+", f"mixing_ndim = {mix}", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_ndim = {mix},")
        
    if "diago_david_ndim" in content:
        content = re.sub(r"diago_david_ndim\s*=\s*\d+", "diago_david_ndim = 2", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n diago_david_ndim = 2,")

    if disk == 'low':
        if "disk_io" in content:
            content = re.sub(r"disk_io\s*=\s*['\"][a-zA-Z]+['\"]", "disk_io='low'", content)
        else:
            content = content.replace("&CONTROL", "&CONTROL\n disk_io='low',")
    else:
        if "disk_io='low'" in content or 'disk_io="low"' in content:
             content = re.sub(r"disk_io\s*=\s*['\"]low['\"],?", "", content)

    with open(input_file, 'w') as f: f.write(content)
    return True

def fix_input_file(input_file, iteration_count=0):
    with open(input_file, 'r') as f: content = f.read()
    corr_path = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" in content:
        content = re.sub(r"pseudo_dir\s*=\s*['\"].*['\"]", f"pseudo_dir='{corr_path}'", content)
    else:
        content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{corr_path}',")

    # Handbuch Option 1 Anforderung: la2f=.true. im SCF Lauf muss in &CONTROL
    if "la2f" not in content.lower():
        content = re.sub(r"&(CONTROL|control)", r"&\1\n la2f=.true.,", content, count=1)

    if "ecutwfc" in content:
        content = re.sub(r"ecutwfc\s*=\s*[0-9\.]+", "ecutwfc = 80.0", content)
    if "ecutrho" in content:
        content = re.sub(r"ecutrho\s*=\s*[0-9\.]+", "ecutrho = 800.0", content)

    target_beta = 0.7
    if iteration_count >= 30: target_beta = 0.4
    if iteration_count >= 60: target_beta = 0.25
    if iteration_count >= 90: target_beta = 0.15

    if "mixing_beta" in content:
        content = re.sub(r"mixing_beta\s*=\s*[0-9\.]+", f"mixing_beta = {target_beta}", content)
        
    if "mixing_ndim" in content:
        content = re.sub(r"mixing_ndim\s*=\s*\d+", "mixing_ndim = 6", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n mixing_ndim = 6,")
        
    if "diago_david_ndim" in content:
        content = re.sub(r"diago_david_ndim\s*=\s*\d+", "diago_david_ndim = 2", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n diago_david_ndim = 2,")
    
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
        bfgs_matches = re.findall(r"number of bfgs steps\s*=\s*(\d+)", chunk)
        scf_matches = re.findall(r"iteration #\s*(\d+)", chunk)
        val = 0
        if bfgs_matches: val = int(bfgs_matches[-1])
        elif scf_matches: val = int(scf_matches[-1])
        return val
    except Exception: return 0


# --- ROBUSTE PHONON WRAPPER OHNE EXTERNE CHECKPOINTS ---
def run_monitored_ph(input_file, output_file, cwd, active_cores):
    last_git_sync = time.time()

    with open(input_file, 'r') as f: content = f.read()
    if os.path.exists(output_file):
        if "recover" not in content:
            content = content.replace("&INPUTPH", "&INPUTPH\n recover=.true.,")
    
    run_input = input_file + ".run"
    with open(run_input, 'w') as f: f.write(content)
    
    file_mode = 'a' if "recover=.true." in content else 'w'

    with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
        cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PH_EXE]
        print(f"      ⚙️ Starte PHONONEN & El-Ph (Cores {active_cores})...")
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
                        print_error_tail(output_file)
                        return "OOM"
                except Exception: pass

        except Exception: 
            kill_process_tree(process.pid)
            print_error_tail(output_file)
            return "CRASH"
        
        time.sleep(1.5)
        
        if process.returncode == -9:
            print("      💀 Prozess wurde vom OS getötet (Exit -9 -> Wahrscheinlich OOM).")
            print_error_tail(output_file)
            return "OOM"

        if process.returncode != 0:
            print_error_tail(output_file)
            return "CRASH"

        try:
            with open(output_file, 'r', errors='ignore') as f:
                if "JOB DONE" in f.read(): return "DONE"
        except Exception: pass
        
        print_error_tail(output_file)
        return "CRASH"
    
def deallocate_vm():
    az_cmd = shutil.which("az") or "/usr/bin/az"
    if not os.path.exists(az_cmd): 
        print("🛑 Azure CLI nicht gefunden. Verlasse mich auf lokalen Shutdown.")
        return
    try:
        result = subprocess.run([az_cmd, "vm", "deallocate", "--resource-group", RESOURCE_GROUP, "--name", "Supraleiter-HPC-Knoten"], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"⚠️ Azure CLI Deallocate Fehler {result.stderr}")
        else:
            print("✅ VM erfolgreich deallokiert.")
    except Exception as e: 
        print(f"⚠️ Fehler beim Aufruf der Azure CLI {e}")

# =============================================================================
# 4. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        print("☁️ Führe initialen Git Pull aus...")
        initial_git_pull()
        
        set_logic_app_state("Enabled")
        print(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        
        if os.path.exists(SIGNAL_FILE): os.remove(SIGNAL_FILE)
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)
        
        input_files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.in")))
        send_notification(f"Start {len(input_files)} Jobs.")
        git_sync("🚀 Start")

        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            scf_out = os.path.join(work_dir, "scf.out")
            
            row_data = get_csv_full_info(name)
            last_status = row_data.get('Status', 'NEW')
            stability = row_data.get('Stabilität', '-')

            if "SKIPPED" in last_status:
                print(f"⏩ Überspringe {name} (Status {last_status})")
                continue
            
            if "Isolator" in last_status:
                print(f"⏩ Überspringe {name} (Ist ein Isolator)")
                continue

            if "Metall" in last_status and stability in ["STABIL", "INSTABIL"]:
                print(f"⏩ Überspringe {name} (Bereits vollständig analysiert, {stability})")
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
                print(f"\n💎 Job {name}")
                scf_in = os.path.join(work_dir, "scf.in")
                dos_in, dos_out = os.path.join(work_dir, "dos.in"), os.path.join(work_dir, f"{name}.dos")
                
                ph_in = os.path.join(work_dir, "ph.in")
                ph_out = os.path.join(work_dir, "ph.out")

                if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)

                result = "DONE"
                crash_counter = 0

                if not (os.path.exists(scf_out) and "JOB DONE" in open(scf_out, errors='ignore').read()):
                    update_csv(name, "Rechnet SCF...")
                    
                    file_level = detect_oom_level(scf_in)
                    start_crash_reason = analyze_crash_reason(scf_out)
                    
                    if start_crash_reason == "LIKELY_OOM":
                        attempts = count_job_attempts(TXT_LOG_FILE, name)
                        print(f"      🕵️ OOM-Signatur vom letzten Lauf erkannt. Versuch Nr. {attempts} auf diesem Level.")
                        
                        if os.path.exists(scf_out):
                            timestamp = datetime.now().strftime("%H%M%S")
                            new_name = f"{scf_out}.crash_{timestamp}"
                            try:
                                os.rename(scf_out, new_name)
                                print(f"      👻 Ghost-Protection, Alte scf.out zu {os.path.basename(new_name)} verschoben.")
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
                    
                    while True:
                        apply_oom_settings(scf_in, oom_level)
                        
                        print(f"   1️⃣  SCF ({current_cores} Cores, OOM-Lvl {oom_level})")
                        result = run_monitored_pw(scf_in, scf_out, work_dir, current_cores)
                        
                        if result == "DONE": break 
                        
                        elif result == "MAX_STEPS":
                            update_csv(name, "SKIPPED (Max BFGS Steps)")
                            git_sync(f"Skipped {name}, >{MAX_BFGS_STEPS} BFGS Steps")
                            break

                        elif result == "OOM":
                            oom_level += 1
                            crash_counter = 0 
                            print(f"      ⚠️ OOM Fehler erkannt. Eskaliere zu Level {oom_level}...")
                            
                            if oom_level == 1: update_csv(name, "Retrying (OOM Lvl 1, CG)")
                            elif oom_level == 2: update_csv(name, "Retrying (OOM Lvl 2, DiskIO)")
                            elif oom_level == 3: update_csv(name, "Retrying (OOM Lvl 3, Mix3)")
                            elif oom_level == 4:
                                update_csv(name, "Retrying (OOM Lvl 4, 1Core)")
                                current_cores = int(SAFE_CORES)
                            else:
                                update_csv(name, "SKIPPED (OOM Limit)")
                                print("      ❌ System zu komplex für verfügbaren RAM. Skippe.")
                                break
                            continue

                        elif result == "CRASH":
                            reason = analyze_crash_reason(scf_out)
                            if reason == "NON_CONVERGED":
                                update_csv(name, "SKIPPED (Non-Conv)")
                                break
                            elif reason == "PSEUDO_ERROR":
                                update_csv(name, "SKIPPED (Pseudo Limit)")
                                print(f"      ❌ Skippe Job wegen inkompatiblem Pseudopotential.")
                                git_sync(f"Skipped {name}, Pseudo Error")
                                break
                            else:
                                crash_counter += 1
                                if crash_counter >= 3:
                                    print(f"      ❌ Zu viele unlösbare Abstürze ({crash_counter}). Skippe Job.")
                                    update_csv(name, "SKIPPED (Permanent Crash)")
                                    git_sync(f"Skipped {name}, Permanent Crash")
                                    break
                                
                                update_csv(name, f"Retrying (Crash {crash_counter}/3)")
                                time.sleep(2)
                                continue 

                if result == "MAX_STEPS" or result == "OOM" or crash_counter >= 3 or analyze_crash_reason(scf_out) == "PSEUDO_ERROR": continue 
                if analyze_crash_reason(scf_out) != "DONE":
                    git_sync(f"Failed {name}")
                    continue 

                with open(scf_in, 'r') as f: 
                    match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", f.read())
                    prefix = match.group(1) if match else "calc"
                
                e_fermi = "-"
                if os.path.exists(scf_out):
                    with open(scf_out, 'r', errors='ignore') as f:
                        match = re.search(r"the Fermi energy is\s+([0-9\.\-]+)\s+ev", f.read())
                        if match: e_fermi = float(match.group(1))

                update_csv(name, "Rechnet DOS...", e_fermi=e_fermi)
                if not os.path.exists(dos_out):
                    with open(dos_in, "w") as f: 
                        f.write(f"&DOS\n prefix='{prefix}', outdir='./tmp', fildos='{name}.dos', Emin=-20.0, Emax=30.0, DeltaE=0.1 /\n")
                    with open(dos_in, "r") as f_in, open(dos_out, "w") as f_out:
                        subprocess.run([DOS_EXE], stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=work_dir)

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
                                except Exception: continue
                    is_metal = dos_val > DOS_THRESHOLD

                if not is_metal:
                    print(f"   🛑 Isolator (DOS={dos_val:.3f}).")
                    update_csv(name, "Fertig (Isolator)", e_fermi, round(dos_val, 4), "NEIN")
                    git_sync(f"Fertig {name} (Isolator)")
                    continue

                print(f"   ⚡ Metall (DOS={dos_val:.3f}). Berechne Phononen & El-Ph (Option C)...")
                update_csv(name, "Rechnet Phononen (Option C)...", e_fermi, round(dos_val, 4), "JA")
                
                # --- OPTION C (Ansatz 1) ---
                if not os.path.exists(ph_out) or "JOB DONE" not in open(ph_out, errors='ignore').read():
                    if not os.path.exists(ph_in):
                        print("   🧹 Erstelle neues Setup für Phononen & El-Ph (Option C)...")
                        shutil.rmtree(os.path.join(work_dir, "tmp", "_ph0"), ignore_errors=True)
                        for ext in ["*.dvscf*", "*.a2Fsave*", "*.dyn*", "*.fc", "*.freq", "*.phdos"]:
                            for f in glob.glob(os.path.join(work_dir, "tmp", ext)) + glob.glob(os.path.join(work_dir, ext)):
                                try: os.remove(f)
                                except Exception: pass
                        
                        with open(ph_in, "w") as f: 
                            f.write(f"Phonons\n&INPUTPH\n tr2_ph=1.0d-14, prefix='{prefix}', outdir='./tmp', fildyn='{name}.dyn', ldisp=.true., fildvscf='dvscf', electron_phonon='interpolated', nq1=2, nq2=2, nq3=2 /\n")
                    
                    ph_cores = int(DEFAULT_CORES)
                    if count_job_attempts(TXT_LOG_FILE, name) > 1: ph_cores = int(SAFE_CORES)

                    ph_res = run_monitored_ph(ph_in, ph_out, work_dir, ph_cores)
                    
                    if ph_res != "DONE":
                        print("      ⚠️ Crash in Phononen/El-Ph (Option C) erkannt!")
                        print_error_tail(ph_out)
                        crash_reason = analyze_crash_reason(ph_out)
                        
                        if crash_reason == "XML_ERROR":
                            print("      🧨 FATAL XML korrupt. Lösche .save und erzwinge SCF-Neustart im nächsten Durchlauf.")
                            tmp_save_path = os.path.join(work_dir, "tmp")
                            if os.path.exists(tmp_save_path): shutil.rmtree(tmp_save_path, ignore_errors=True)
                            if os.path.exists(scf_out): os.remove(scf_out)
                            update_csv(name, "SCF_RESET (XML Error)")
                            continue

                        update_csv(name, "SKIPPED (Phonon/El-Ph Crash)") 
                        git_sync(f"Phonon/El-Ph Crash {name}")
                        continue

                min_f, stab = "-", "Unbekannt"
                if os.path.exists(ph_out):
                      with open(ph_out, 'r') as f:
                          content = f.read()
                          if "JOB DONE" in content:
                              freqs = re.findall(r"freq\s+\(\s*\d+\)\s+=\s+([0-9\.\-]+)\s+\[THz\]", content)
                              if freqs:
                                  min_f = min([float(f) for f in freqs])
                                  stab = "STABIL" if min_f > -0.05 else "INSTABIL"

                if stab == "INSTABIL":
                    update_csv(name, "Fertig (Metall)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
                    git_sync(f"Fertig {name} (INSTABIL)")
                    continue

                if stab == "STABIL":
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
                        print_error_tail(q2r_out)
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
                        print_error_tail(matdyn_out)
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
                    git_sync(f"Fertig {name} (Tc={tc}K)")

            except Exception as job_err:
                print(f"🚨 Fehler bei Job {name}: {job_err}")
                update_csv(name, f"ERROR (Python {str(job_err)[:30]})")
                continue 

        send_notification("🎉 Alle Jobs erledigt.")
        
        with open(SIGNAL_FILE, "w") as f: f.write(f"Status: Fertig\nTimestamp: {time.ctime()}")
        git_sync("🏁 Finaler Sync vor Shutdown (Erfolgreich)")
        set_logic_app_state("Disabled")
        print("🛑 Deallokiere VM über Azure CLI...")
        deallocate_vm() 
        
        if os.name != 'nt': 
            print("🛑 Fahre System herunter...")
            os.system("sudo shutdown -h now")

    except Exception as e:
        full_error = f"\n\n🚨 KRITISCHER ABSTURZ\n{e}\n{traceback.format_exc()}\n"
        with open(TXT_LOG_FILE, "a") as f: f.write(full_error)
        git_sync("🚨 Notfall Sync nach Skript-Absturz")
        send_notification(f"🚨 KRITISCHER FEHLER {e} -> Shutdown.")
        
        set_logic_app_state("Disabled")
        print("🛑 Deallokiere VM über Azure CLI nach Crash...")
        deallocate_vm()
        if os.name != 'nt': os.system("sudo shutdown -h now")
        sys.exit()

if __name__ == "__main__":
    main()