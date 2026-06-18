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

PW_EXE = shutil.which("pw.x") or "/usr/bin/pw.x"
PH_EXE = shutil.which("ph.x") or "/usr/bin/ph.x"
DOS_EXE = shutil.which("dos.x") or "/usr/bin/dos.x"
Q2R_EXE = shutil.which("q2r.x") or "/usr/bin/q2r.x"
MATDYN_EXE = shutil.which("matdyn.x") or "/usr/bin/matdyn.x"

# =============================================================================
# 2. HELFER & GIT
# =============================================================================
def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ HPC, {message}"}
        requests.post(url, data=payload, timeout=10)
    except: pass

def check_and_free_disk_space(min_free_gb=5.0):
    try:
        total, used, free = shutil.disk_usage("/")
        free_gb = free / (1024**3)
        if free_gb < min_free_gb:
            print(f"      🧹 Festplatte fast voll ({free_gb:.2f} GB frei). Starte Notfall-Cleanup...")
            for wfc_file in glob.glob(os.path.join(WORK_DIR, "RUN_*", "tmp", "*.wfc*")):
                try: os.remove(wfc_file)
                except: pass
            for ph_dir in glob.glob(os.path.join(WORK_DIR, "RUN_*", "tmp", "_ph0")):
                try: shutil.rmtree(ph_dir, ignore_errors=True)
                except: pass
            new_free = shutil.disk_usage("/").free / (1024**3)
            print(f"      ✅ Cleanup beendet. Jetzt {new_free:.2f} GB frei.")
    except Exception as e:
        print(f"      ⚠️ Warnung beim Disk-Check, {e}")

def set_logic_app_state(state="Enabled"):
    if not shutil.which("az"): return
    try:
        subprocess.run(["az", "logic", "workflow", "set-state", "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME, "--state", state], capture_output=True, timeout=30)
    except: pass

def initial_git_pull():
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        subprocess.run(["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"], cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
    except Exception as e:
        print(f"⚠️ Initialer Git Pull Fehler, {e}")

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
        print(f"⚠️ Git Fehler, {e}")

def print_error_tail(log_file, lines=100):
    if not os.path.exists(log_file): return
    try:
        with open(log_file, 'r', errors='ignore') as f:
            tail = f.readlines()[-lines:]
            
        error_msg = f"\n      --- LETZTE {lines} ZEILEN VON {os.path.basename(log_file)} ---\n"
        for line in tail: 
            error_msg += "      " + line.rstrip() + "\n"
        error_msg += "      -----------------------------------------\n"
        
        print(error_msg)
        with open(TXT_LOG_FILE, 'a') as f_out:
            f_out.write(error_msg)
            
        work_dir = os.path.dirname(log_file)
        crash_file = os.path.join(work_dir, "CRASH")
        if os.path.exists(crash_file):
            with open(crash_file, 'r', errors='ignore') as fc:
                crash_content = fc.read()
            crash_msg = f"\n      🚨 QE CRASH FILE GEFUNDEN 🚨\n{crash_content}\n      -----------------------------------------\n"
            print(crash_msg)
            with open(TXT_LOG_FILE, 'a') as f_out:
                f_out.write(crash_msg)
                
    except Exception as e: 
        print(f"      ⚠️ Fehler beim Auslesen des Logs, {e}")

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
    except: return "-"

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
        job_marker = f"💎 Job, {job_name}"
        for line in reversed(lines):
            if job_marker in line:
                count += 1
            elif "💎 Job," in line and job_name not in line:
                break
    except: return 1
    return max(1, count)

def get_cores_from_log(log_file, default_cores=4):
    if not os.path.exists(log_file): return int(default_cores)
    try:
        with open(log_file, 'r', errors='ignore') as f:
            content = f.read()
        matches = re.findall(r"running on\s+(\d+)\s+processors", content, re.IGNORECASE)
        if matches: return int(matches[-1])
        return int(default_cores)
    except: return int(default_cores)

# =============================================================================
# 3. SMART LOGIC & VALIDATION & CRASH ANALYSE
# =============================================================================

def make_kpoints_dense(filepath):
    if not os.path.exists(filepath): return False
    with open(filepath, 'r') as f: content = f.read()
    if "! KPOINTS_DENSIFIED" in content: return False 
    
    lines = content.split('\n')
    out_lines = []
    in_kpoints = False
    for line in lines:
        if "K_POINTS" in line.upper() and "automatic" in line.lower():
            in_kpoints = True
            out_lines.append(line)
            continue
        if in_kpoints and line.strip() and not line.strip().startswith("!"):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    base_k1, base_k2, base_k3 = int(parts[0]), int(parts[1]), int(parts[2])
                    target_k1 = max(12, base_k1 * 3)
                    target_k2 = max(12, base_k2 * 3)
                    target_k3 = max(12, base_k3 * 3)
                    shift = " ".join(parts[3:]) if len(parts) > 3 else "0 0 0"
                    out_lines.append(f" {target_k1} {target_k2} {target_k3} {shift} ! KPOINTS_DENSIFIED")
                    in_kpoints = False
                    continue
                except: pass
        out_lines.append(line)
    with open(filepath, 'w') as f: f.write("\n".join(out_lines))
    return True

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
        
        if "wrong trans" in lines_lower or "wrong elph" in lines_lower:
            return "WRONG_TRANS_ERROR"
            
        if "fatal error reading xml" in lines_lower or "reading output_obj of xsd" in lines_lower or "wrong number of occurrences" in lines_lower or "tag root not found" in lines_lower or "partial_el_phon" in lines_lower or "xmltools.f90" in lines_lower:
            print("      🧨 XML-Struktur zerstört (Corruption).")
            return "XML_ERROR"

        if "not orthogonal" in lines_lower and "d_s" in lines_lower:
            print("      🧩 Symmetrie-Fehler erkannt (D_S not orthogonal).")
            return "SYMMETRY_ERROR"

        if "mx dimension too small" in lines_lower:
            print("      🧨 FATAL, Pseudopotential übersteigt QE-Limit. Neues Pseudo (PAW) benötigt!")
            return "PSEUDO_ERROR"
            
        if "i/o past end of record" in lines_lower or ("end of file" in lines_lower and ("elphon.f90" in lines_lower or "write_rec.f90" in lines_lower)):
            return "ELPH_CORRUPT"

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
    except: return "HARD"
    
def run_monitored_pw(input_file, output_file, cwd, active_cores):
    fix_input_file(input_file, 0)
    last_git_sync = time.time()
    last_checkpoint_time = 0 

    while True:
        check_and_free_disk_space()
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
            try:
                if os.path.exists(tmp_dir): shutil.rmtree(tmp_dir)
                shutil.copytree(checkpoint_dir, tmp_dir)
                if is_xml_valid(xml_path):
                    mode = 'restart'
                    print("      ✅ Checkpoint erfolgreich geladen!")
                else:
                    print("      ❌ Checkpoint war auch defekt. Starte von vorne.")
            except Exception as e: print(f"      ❌ Fehler beim Laden des Checkpoints, {e}")
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
                            print("      💾 XML valide -> Erstelle Checkpoint...")
                            try:
                                if os.path.exists(checkpoint_dir): shutil.rmtree(checkpoint_dir)
                                shutil.copytree(tmp_dir, checkpoint_dir)
                                last_checkpoint_time = time.time()
                                print("      ✅ Checkpoint erstellt.")
                                git_sync("Checkpoint & Log Update")
                                last_git_sync = time.time() 
                            except Exception as e: print(f"      ⚠️ Checkpoint fail, {e}")

                    if time.time() - last_git_sync > 3600:
                        print("      ❤️ Git Heartbeat...")
                        git_sync("Log Update (Heartbeat)")
                        last_git_sync = time.time()

                    try:
                        mem_usage = psutil.virtual_memory().percent
                        if mem_usage > MEMORY_LIMIT_PERCENT:
                            print(f"      ⚠️ RAM NOT-AUS (Python Monitor)!")
                            process.kill()
                            return "OOM" 
                    except: pass

                    cur_iter = get_last_iteration(output_file)
                    if cur_iter >= MAX_BFGS_STEPS:
                        print(f"      🛑 Limit erreicht ({cur_iter}/{MAX_BFGS_STEPS} BFGS Schritte). Breche ab.")
                        process.kill()
                        return "MAX_STEPS"
                    
                    if cur_iter > 30: fix_input_file(input_file, cur_iter)

            except: process.kill(); return "CRASH"
            
            time.sleep(1.5)
            
            if process.returncode == -9:
                print("      💀 Prozess wurde vom OS getötet (Exit -9 -> Wahrscheinlich OOM).")
                return "OOM"

            if process.returncode != 0:
                reason = analyze_crash_reason(output_file)
                if reason == "LIKELY_OOM":
                    print("      💀 Logfile endet abrupt (Silent Death) -> OOM.")
                    return "OOM"
                return "CRASH"

            final_reason = analyze_crash_reason(output_file)
            if final_reason == "DONE": return "DONE"
            elif final_reason == "LIKELY_OOM": return "OOM"
            
            return "CRASH"

def execute_scf_block(name, scf_in, scf_out, work_dir, input_file, phase_label):
    if os.path.exists(scf_out) and "JOB DONE" in open(scf_out, errors='ignore').read():
        return "DONE", get_cores_from_log(scf_out, DEFAULT_CORES)
    
    update_csv(name, f"Rechnet SCF ({phase_label})...")
    file_level = detect_oom_level(scf_in)
    start_crash_reason = analyze_crash_reason(scf_out)
    
    if start_crash_reason == "LIKELY_OOM":
        attempts = count_job_attempts(TXT_LOG_FILE, name)
        print(f"      🕵️ OOM-Signatur vom letzten Lauf erkannt. Versuch Nr. {attempts} auf diesem Level.")
        if os.path.exists(scf_out):
            timestamp = datetime.now().strftime("%H%M%S")
            try: os.rename(scf_out, f"{scf_out}.crash_{timestamp}")
            except: pass
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

    crash_counter = 0
    oom_tolerance_counter = 0

    while True:
        current_cores = int(SAFE_CORES) if oom_level >= 4 else int(DEFAULT_CORES)
        apply_oom_settings(scf_in, oom_level)
        
        print(f"   1️⃣  SCF {phase_label} ({current_cores} Cores, OOM-Lvl {oom_level})")
        result = run_monitored_pw(scf_in, scf_out, work_dir, current_cores)
        
        if result == "DONE":
            return "DONE", current_cores
        elif result == "MAX_STEPS":
            update_csv(name, "SKIPPED (Max BFGS Steps)")
            git_sync(f"Skipped {name}, >{MAX_BFGS_STEPS} BFGS Steps")
            return "SKIPPED", current_cores
        elif result == "OOM":
            oom_tolerance_counter += 1
            if oom_tolerance_counter >= 2:
                oom_level += 1
                oom_tolerance_counter = 0
                crash_counter = 0
                print(f"      ⚠️ Zweiter OOM Fehler in Folge. Eskaliere zu Level {oom_level}...")
                if oom_level == 1: update_csv(name, "Retrying (OOM Lvl 1, CG)")
                elif oom_level == 2: update_csv(name, "Retrying (OOM Lvl 2, DiskIO)")
                elif oom_level == 3: update_csv(name, "Retrying (OOM Lvl 3, Mix3)")
                elif oom_level == 4: update_csv(name, "Retrying (OOM Lvl 4, Cores reduziert)")
                else:
                    update_csv(name, "SKIPPED (OOM Limit)")
                    print("      ❌ System zu komplex für verfügbaren RAM. Skippe.")
                    return "SKIPPED", current_cores
            else:
                print(f"      🔄 OOM Fehler erkannt (1/2). Toleranz greift, probiere Level {oom_level} erneut...")
                update_csv(name, f"Retrying (OOM Tol {oom_tolerance_counter}/2)")
            continue
        elif result == "CRASH":
            reason = analyze_crash_reason(scf_out)
            if reason == "NON_CONVERGED":
                update_csv(name, "SKIPPED (Non-Conv)")
                return "SKIPPED", current_cores
            elif reason == "PSEUDO_ERROR":
                update_csv(name, "SKIPPED (Pseudo Limit)")
                print(f"      ❌ Skippe Job wegen inkompatiblem Pseudopotential.")
                git_sync(f"Skipped {name}, Pseudo Error")
                return "SKIPPED", current_cores
            else:
                crash_counter += 1
                if crash_counter >= 3:
                    print(f"      ❌ Zu viele unlösbare Abstürze ({crash_counter}). Skippe Job.")
                    update_csv(name, "SKIPPED (Permanent Crash)")
                    git_sync(f"Skipped {name}, Permanent Crash")
                    return "SKIPPED", current_cores
                update_csv(name, f"Retrying (Crash {crash_counter}/3)")
                time.sleep(2)
                continue
    return "CRASH", current_cores
        
def is_xml_valid(xml_path):
    if not os.path.exists(xml_path): return False
    try:
        with open(xml_path, 'rb') as f:
            try: f.seek(-1000, 2) 
            except: f.seek(0)
            tail = f.read().decode('utf-8', errors='ignore')
        if "</qes:espresso>" in tail or "</qes:data-file-schema>" in tail:
            return True
        return False
    except: return False

def is_recoverable_fragmentation_error(ph_output_file):
    if not os.path.exists(ph_output_file): return False
    try:
        with open(ph_output_file, 'r', errors='ignore') as f:
            content = f.read()
        if "mismatch in number of G-vectors" in content or ("error reading file" in content and "xml" not in content):
            return True
        return False
    except: return False

def run_cleanup_scf(scf_input_file, cwd, cores_to_use=2):
    print(f"      🚑 Starte RECOVERY-Modus (Collect Waves), Cores={cores_to_use}")
    
    with open(scf_input_file, 'r') as f: content = f.read()
    
    if "restart_mode" in content:
        content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", "restart_mode='restart'", content)
    else:
        content = content.replace("&CONTROL", "&CONTROL\n restart_mode='restart',")
        
    if "wf_collect" in content:
        content = re.sub(r"wf_collect\s*=\s*\.?[a-zA-Z]+\.?", "wf_collect=.true.", content)
    else:
        content = content.replace("&CONTROL", "&CONTROL\n wf_collect=.true.,")

    if "nstep" in content:
        content = re.sub(r"nstep\s*=\s*\d+", "nstep=0", content)
    else:
        content = content.replace("&CONTROL", "&CONTROL\n nstep=0,")

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
            print(f"      ❌ Recovery fehlgeschlagen, {e}")
            return False

def disable_symmetries_and_reduce_grid(input_file):
    if not os.path.exists(input_file): return
    with open(input_file, 'r') as f: content = f.read()
    
    content = content.replace("noinv=.true.,", "")
    content = content.replace("noinv=.true.", "")

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
    if "mixing_ndim = 2" in content or "mixing_ndim=2" in content: return 4
    if "mixing_ndim = 3" in content or "mixing_ndim=3" in content: return 3
    if "disk_io='low'" in content or 'disk_io="low"' in content: return 2
    if "diagonalization='cg'" in content or 'diagonalization="cg"' in content: return 1
    return 0

def apply_oom_settings(input_file, level):
    with open(input_file, 'r') as f: content = f.read()
    diag = 'cg'; mix = 4; disk = None 
    msg = "Standard (cg, mix=4)"

    if level >= 1: disk = 'low'; msg = "Stufe 1 (cg, mix=4, disk_io='low')"
    if level >= 2: mix = 3; msg = "Stufe 2 (cg, mix=3, disk_io='low')"
    if level >= 3: mix = 2; msg = "Stufe 3 (cg, mix=2, disk_io='low')"
    if level >= 4: mix = 2; msg = "Stufe 4 (cg, mix=2, disk_io='low', 1 Core)"

    print(f"      📉 Setze RAM-Strategie, {msg}")

    if "diagonalization" in content:
        content = re.sub(r"diagonalization\s*=\s*['\"].*['\"]", f"diagonalization='{diag}'", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n diagonalization='{diag}',")

    if "mixing_ndim" in content:
        content = re.sub(r"mixing_ndim\s*=\s*\d+", f"mixing_ndim = {mix}", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_ndim = {mix},")

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

    target_beta = 0.7
    if iteration_count >= 30: target_beta = 0.4
    if iteration_count >= 60: target_beta = 0.25
    if iteration_count >= 90: target_beta = 0.15

    if "mixing_beta" in content:
        content = re.sub(r"mixing_beta\s*=\s*[0-9\.]+", f"mixing_beta = {target_beta}", content)
    
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
    except: return 0


# --- ROBUSTE PHONON WRAPPER ---
def run_monitored_ph(input_file, output_file, cwd, active_cores):
    last_git_sync = time.time()
    check_and_free_disk_space()

    with open(input_file, 'r') as f: content = f.read()
    
    if os.path.exists(output_file) and "recover=.false." not in content.lower():
        if "recover" not in content:
            content = content.replace("&INPUTPH", "&INPUTPH\n recover=.true.,")
    
    run_input = input_file + ".run"
    with open(run_input, 'w') as f: f.write(content)
    
    file_mode = 'a' if ("recover=.true." in content.lower()) else 'w'

    with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
        cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PH_EXE]
        print(f"      ⚙️ Starte PHONONEN (Cores {active_cores})...")
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
                        process.kill()
                        return "OOM"
                except: pass

        except: 
            process.kill()
            return "CRASH"
        
        time.sleep(1.5)
        
        if process.returncode == -9:
            print("      💀 Prozess wurde vom OS getötet (Exit -9 -> Wahrscheinlich OOM).")
            return "OOM"

        if process.returncode != 0:
            reason = analyze_crash_reason(output_file)
            if reason == "WRONG_TRANS_ERROR":
                 print("      🧨 Falscher 'trans/elph' Parameter in ph.x erkannt.")
                 return "WRONG_TRANS_ERROR"
            if reason == "ELPH_CORRUPT" or reason == "XML_ERROR":
                return reason
            return "CRASH"

        try:
            with open(output_file, 'r', errors='ignore') as f:
                if "JOB DONE" in f.read(): return "DONE"
        except: pass
        
        return "CRASH"
    
def deallocate_vm():
    if not shutil.which("az"): 
        print("🛑 Azure CLI nicht gefunden. Verlasse mich auf lokalen Shutdown.")
        return
    try:
        subprocess.run(["az", "login", "--identity"], capture_output=True, timeout=30)
        result = subprocess.run(["az", "vm", "deallocate", "--resource-group", RESOURCE_GROUP, "--name", "Supraleiter-HPC-Knoten", "--no-wait"], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"⚠️ Azure CLI Deallocate Fehler, {result.stderr}")
        else:
            print("✅ VM erfolgreich deallokiert (Asynchron).")
    except Exception as e: 
        print(f"⚠️ Fehler beim Aufruf der Azure CLI, {e}")

# =============================================================================
# 4. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        print("☁️ Führe initialen Git Pull aus...")
        initial_git_pull()
        
        set_logic_app_state("Enabled")
        with open(TXT_LOG_FILE, "a") as f:
            f.write(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        print(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        
        if os.path.exists(SIGNAL_FILE): os.remove(SIGNAL_FILE)
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)
        
        input_files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.in")))
        send_notification(f"Start, {len(input_files)} Jobs.")
        git_sync("🚀 Start")

        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            
            try:
                if name in FORCE_RETRY_LIST:
                    print(f"🔄 ERZWUNGENER NEUSTART für {name} (Lösche korrupten RUN-Ordner)...")
                    if os.path.exists(work_dir):
                        shutil.rmtree(work_dir, ignore_errors=True)
                    update_csv(name, "NEW", "-", "-", "-", "-", "-")
                    FORCE_RETRY_LIST.remove(name)
                
                row_data = get_csv_full_info(name)
                last_status = row_data.get('Status', 'NEW')
                stability = row_data.get('Stabilität', '-').strip()
                lam = row_data.get('Lambda', '-').strip()

                if "SKIPPED" in last_status:
                    print(f"⏩ Überspringe {name} (Status, {last_status})")
                    continue
                
                if "Isolator" in last_status:
                    print(f"⏩ Überspringe {name} (Ist ein Isolator)")
                    continue

                if stability == "INSTABIL":
                    print(f"⏩ Überspringe {name} (Bereits vollständig analysiert, INSTABIL)")
                    continue

                if stability == "STABIL" and lam != "-" and lam != "":
                    print(f"⏩ Überspringe {name} (Bereits vollständig analysiert, STABIL)")
                    continue

                needs_dense_run = False
                if stability == "STABIL" and (lam == "-" or lam == ""):
                    needs_dense_run = True

                if not needs_dense_run:
                    
                    # --- PHASE 1 (Test-Modus) ---
                    if not os.path.exists(work_dir): os.makedirs(work_dir)
                    print(f"\n💎 Job, {name} (Phase 1, Test)")
                    
                    scf_in = os.path.join(work_dir, "scf.in")
                    scf_out = os.path.join(work_dir, "scf.out")
                    dos_in, dos_out = os.path.join(work_dir, "dos.in"), os.path.join(work_dir, f"{name}.dos")
                    ph_in, ph_out = os.path.join(work_dir, "ph.in"), os.path.join(work_dir, "ph.out")

                    if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)

                    scf_res, scf_cores = execute_scf_block(name, scf_in, scf_out, work_dir, input_file, "Test")
                    if scf_res != "DONE": continue 

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
                                    except: continue
                        is_metal = dos_val > DOS_THRESHOLD

                    if not is_metal:
                        print(f"   🛑 Isolator (DOS={dos_val:.3f}).")
                        update_csv(name, "Fertig (Isolator)", e_fermi, round(dos_val, 4), "NEIN")
                        git_sync(f"Fertig, {name} (Isolator)")
                        continue

                    print(f"   ⚡ Metall (DOS={dos_val:.3f}). Berechne Phononen-Test...")
                    update_csv(name, "Rechnet Phononen (Test)...", e_fermi, round(dos_val, 4), "JA")
                    
                    if not os.path.exists(ph_out) or "JOB DONE" not in open(ph_out, errors='ignore').read():
                        if not os.path.exists(ph_in):
                            with open(ph_in, "w") as f: 
                                f.write(f"Phonons\n&INPUTPH\n tr2_ph=1.0d-14, prefix='{prefix}', outdir='./tmp', fildyn='{name}.dyn', ldisp=.true., nq1=2, nq2=2, nq3=2, reduce_io=.true., recover=.true. /\n")

                        ph_attempts = 0
                        while ph_attempts < 3:
                            ph_attempts += 1
                            ph_res = run_monitored_ph(ph_in, ph_out, work_dir, scf_cores)
                            
                            if ph_res == "DONE":
                                break
                                
                            print("      ⚠️ Crash in Test-Phononen!")
                            crash_reason = analyze_crash_reason(ph_out)
                            print_error_tail(ph_out, 100)
                            
                            if crash_reason == "XML_ERROR":
                                print("      🧨 FATAL, XML korrupt. Lösche .save und erzwinge SCF-Neustart.")
                                tmp_save_path = os.path.join(work_dir, "tmp")
                                if os.path.exists(tmp_save_path): shutil.rmtree(tmp_save_path, ignore_errors=True)
                                if os.path.exists(scf_out): os.remove(scf_out)
                                update_csv(name, "SCF_RESET (XML Error)")
                                continue

                            if is_recoverable_fragmentation_error(ph_out):
                                print("      🤕 Diagnose, Fragmentierung erkannt. Starte 'Collect-Recovery'...")
                                if run_cleanup_scf(scf_in, work_dir, int(DEFAULT_CORES)):
                                    print("      👍 Recovery erfolgreich.")
                                else:
                                    print("      👎 Recovery fehlgeschlagen.")

                            print(f"      🛡️ Aktiviere NOTFALL-MODUS, Grid=1x1x1, Sym=OFF (Vererbe {scf_cores} Cores)...")
                            disable_symmetries_and_reduce_grid(ph_in)
                            
                            tmp_path = os.path.join(work_dir, "tmp")
                            ph0_path = os.path.join(tmp_path, "_ph0")
                            if os.path.exists(ph0_path):
                                try: shutil.rmtree(ph0_path, ignore_errors=True)
                                except: pass
                            if os.path.exists(ph_out):
                                try: os.remove(ph_out)
                                except: pass

                        if ph_res != "DONE":
                             print("      ❌ Test-Phononen endgültig fehlgeschlagen.")
                             update_csv(name, "SKIPPED (Phonon Crash)") 
                             git_sync(f"Phonon Crash, {name}")
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
                        git_sync(f"Fertig, {name} (INSTABIL)")
                        continue

                    if stab == "STABIL":
                        print(f"   ✅ Material ist STABIL (Min Freq {min_f} THz). Phase 1 abgeschlossen.")
                        update_csv(name, "Test bestanden (STABIL)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
                        needs_dense_run = True

                # --- PHASE 2 (Präzisions-Modus & El-Ph) ---
                if needs_dense_run:
                    if not os.path.exists(work_dir): os.makedirs(work_dir)
                    
                    scf_in = os.path.join(work_dir, "scf.in")
                    scf_out = os.path.join(work_dir, "scf.out")
                    ph_in = os.path.join(work_dir, "ph.in")
                    ph_out = os.path.join(work_dir, "ph.out")
                    
                    if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)
                    
                    was_densified = make_kpoints_dense(scf_in)
                    if was_densified:
                        print("   🧹 Test-Daten werden gelöscht, K-Punkte für Präzisions-Phase verdoppelt.")
                        for f in [scf_out, ph_out, ph_in]:
                            if os.path.exists(f): os.remove(f)
                        shutil.rmtree(os.path.join(work_dir, "tmp"), ignore_errors=True)
                        shutil.rmtree(os.path.join(work_dir, "tmp_SAFE_CHECKPOINT"), ignore_errors=True)
                    
                    print(f"\n💎 Job, {name} (Phase 2, Präzision)")
                    
                    scf_res, scf_cores = execute_scf_block(name, scf_in, scf_out, work_dir, input_file, "Präzision")
                    if scf_res != "DONE": continue
                    
                    e_fermi, dos_val = row_data.get('Fermi Energie (eV)', '-'), row_data.get('DOS @ Fermi', '-')
                    min_f, stab = row_data.get('Min Freq (THz)', '-'), row_data.get('Stabilität', 'STABIL')
                    
                    with open(scf_in, 'r') as f: 
                        match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", f.read())
                        prefix = match.group(1) if match else "calc"
                    
                    update_csv(name, "Rechnet El-Ph (Präzision)...", e_fermi, dos_val, "JA", min_f=min_f, stab=stab)
                    
                    if not os.path.exists(ph_out) or "JOB DONE" not in open(ph_out, errors='ignore').read():
                        if not os.path.exists(ph_in):
                            ph0_path = os.path.join(work_dir, "tmp", "_ph0")
                            if os.path.exists(ph0_path):
                                try: shutil.rmtree(ph0_path, ignore_errors=True)
                                except: pass
                            with open(ph_in, "w") as f: 
                                f.write(f"Phonons\n&INPUTPH\n tr2_ph=1.0d-14, prefix='{prefix}', outdir='./tmp', fildyn='{name}.dyn', ldisp=.true., fildvscf='dvscf', nq1=2, nq2=2, nq3=2, reduce_io=.true., recover=.true., electron_phonon='interpolated' /\n")

                        ph_attempts = 0
                        while ph_attempts < 3:
                            ph_attempts += 1
                            ph_res = run_monitored_ph(ph_in, ph_out, work_dir, scf_cores)
                            
                            if ph_res == "DONE": break
                                
                            print("      ⚠️ Crash in Präzisions-Phononen!")
                            crash_reason = analyze_crash_reason(ph_out)
                            print_error_tail(ph_out, 100)
                            
                            print("      🛡️ Bereinige Caches und erzwinge sauberen Neustart der Phononen...")
                            for p in [os.path.join(work_dir, "tmp", "_ph0"), os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                                if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                            for f in glob.glob(os.path.join(work_dir, "tmp", "*.a2Fsave*")) + glob.glob(os.path.join(work_dir, "tmp", "*.dvscf*")):
                                try: os.remove(f)
                                except: pass
                            
                            with open(ph_in, 'r') as f: ph_content = f.read()
                            ph_content = ph_content.replace("recover=.true.", "recover=.false.")
                            with open(ph_in, 'w') as f: f.write(ph_content)
                            if os.path.exists(ph_out): os.remove(ph_out)

                        if ph_res != "DONE":
                             print("      ❌ Präzisions-Phononen endgültig fehlgeschlagen.")
                             update_csv(name, "SKIPPED (El-Ph Crash)") 
                             git_sync(f"El-Ph Crash, {name}")
                             continue

                    print("   ✅ El-Ph (Präzision) fertig. Starte Q2R und Matdyn...")

                    q2r_in = os.path.join(work_dir, "q2r.in")
                    q2r_out = os.path.join(work_dir, "q2r.out")
                    matdyn_in = os.path.join(work_dir, "matdyn.in")
                    matdyn_out = os.path.join(work_dir, "matdyn.out")

                    update_csv(name, "Rechnet El-Ph (Q2R)...", e_fermi, dos_val, "JA", min_f=min_f, stab=stab)

                    if not (os.path.exists(q2r_out) and "JOB DONE" in open(q2r_out, errors='ignore').read()):
                        print("   4️⃣  Q2R...")
                        with open(q2r_in, "w") as f:
                            f.write(f"&input\n fildyn='{name}.dyn',\n zasr='simple',\n flfrc='{name}.fc',\n la2F=.true.\n/\n")
                        with open(q2r_in, "r") as fi, open(q2r_out, "w") as fo:
                            subprocess.run([Q2R_EXE], stdin=fi, stdout=fo, stderr=subprocess.STDOUT, cwd=work_dir)

                    if not (os.path.exists(q2r_out) and "JOB DONE" in open(q2r_out, errors='ignore').read()):
                        print(f"      ❌ Q2R fehlgeschlagen!")
                        print_error_tail(q2r_out, 100)
                        update_csv(name, "ERROR (Q2R Crash)")
                        git_sync(f"Q2R Crash, {name}")
                        continue

                    update_csv(name, "Rechnet El-Ph (Matdyn)...", e_fermi, dos_val, "JA", min_f=min_f, stab=stab)

                    if not (os.path.exists(matdyn_out) and "JOB DONE" in open(matdyn_out, errors='ignore').read()):
                        print("   5️⃣  Matdyn...")
                        with open(matdyn_in, "w") as f:
                            f.write(f"&input\n asr='simple',\n flfrc='{name}.fc',\n flfrq='{name}.freq',\n fildyn='{name}.dyn',\n dos=.true.,\n elph=.true.,\n fildos='{name}.phdos',\n nk1=10, nk2=10, nk3=10\n/\n")
                        with open(matdyn_in, "r") as fi, open(matdyn_out, "w") as fo:
                            subprocess.run([MATDYN_EXE], stdin=fi, stdout=fo, stderr=subprocess.STDOUT, cwd=work_dir)

                    if not (os.path.exists(matdyn_out) and "JOB DONE" in open(matdyn_out, errors='ignore').read()):
                        print(f"      ❌ Matdyn fehlgeschlagen!")
                        print_error_tail(matdyn_out, 100)
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
                                    lam = ml.group(1)
                                    wlog = mw.group(1)
                                    tc_v = berechne_tc(wlog, lam)
                                    if tc_v != "-":
                                        tc = round(tc_v, 3)

                    update_csv(name, "Fertig (Metall)", e_fermi, dos_val, "JA", min_f=min_f, stab=stab, lam=lam, wlog=wlog, tc=tc)
                    git_sync(f"Fertig, {name} (Tc={tc}K)")

            except Exception as job_err:
                print(f"🚨 Fehler bei Job {name}, {job_err}")
                update_csv(name, f"ERROR (Python, {str(job_err)[:30]})")
                continue 

        send_notification("🎉 Alle Jobs erledigt.")
        
        with open(SIGNAL_FILE, "w") as f: f.write(f"Status, Fertig\nTimestamp, {time.ctime()}")
        git_sync("🏁 Finaler Sync vor Shutdown (Erfolgreich)")
        set_logic_app_state("Disabled")
        print("🛑 Deallokiere VM über Azure CLI...")
        deallocate_vm() 
        
        if os.name != 'nt': 
            print("🛑 Fahre System herunter...")
            os.system("sudo shutdown -h now")

    except Exception as e:
        full_error = f"\n\n🚨 KRITISCHER ABSTURZ ({datetime.now()})\n{e}\n{traceback.format_exc()}\n"
        with open(TXT_LOG_FILE, "a") as f: f.write(full_error)
        git_sync("🚨 Notfall Sync nach Skript-Absturz")
        send_notification(f"🚨 KRITISCHER FEHLER, {e} -> Shutdown.")
        
        set_logic_app_state("Disabled")
        print("🛑 Deallokiere VM über Azure CLI nach Crash...")
        deallocate_vm()
        if os.name != 'nt': os.system("sudo shutdown -h now")
        sys.exit()

if __name__ == "__main__":
    main()